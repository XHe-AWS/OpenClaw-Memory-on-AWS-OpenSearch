"""
OpenClaw Memory System v2 — File Indexer
=========================================
Watches workspace memory files (MEMORY.md, memory/*.md) and
indexes their chunks into OpenSearch.

Two modes:
  - Polling: check file hashes every 30 seconds
  - Event: use watchdog for filesystem events (lower latency)

Only files in the MEMORY_PATHS whitelist are indexed.
"""

import fnmatch
import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from config import (
    MEMORY_PATHS,
    EXTRA_MEMORY_PATHS,
    INDEX_STATE_PATH,
    INDEX_POLL_INTERVAL_SECS,
    INDEX_DEBOUNCE_SECS,
    DOC_TYPE_FILE_CHUNK,
)
from chunker import chunk_markdown, Chunk
from embedding import EmbeddingClient
from opensearch_client import OpenSearchClient

logger = logging.getLogger(__name__)


class Indexer:
    """
    File watcher + chunker + indexer for workspace memory files.
    """

    def __init__(
        self,
        os_client: OpenSearchClient,
        embed_client: EmbeddingClient,
        workspace_root: str,
    ):
        self.os_client = os_client
        self.embed_client = embed_client
        self.workspace_root = Path(workspace_root)
        self._state_path = Path(INDEX_STATE_PATH)
        self._state: dict[str, dict[str, Any]] = {}
        self._shutdown = threading.Event()
        self._watcher_thread: Optional[threading.Thread] = None

        # Build combined whitelist
        self._patterns = list(MEMORY_PATHS) + list(EXTRA_MEMORY_PATHS)

        # Load existing state
        self._load_state()

    # ─── Whitelist Check ──────────────────────────────

    def _is_whitelisted(self, rel_path: str) -> bool:
        """Check if a relative path matches any whitelist pattern."""
        for pattern in self._patterns:
            if fnmatch.fnmatch(rel_path, pattern):
                return True
        return False

    def _find_memory_files(self) -> list[Path]:
        """Find all whitelisted memory files in the workspace."""
        files = []
        for pattern in self._patterns:
            matched = list(self.workspace_root.glob(pattern))
            files.extend(matched)
        # Deduplicate and filter existing
        seen = set()
        result = []
        for f in files:
            if f.is_file() and f not in seen:
                seen.add(f)
                result.append(f)
        return sorted(result)

    # ─── State Management ─────────────────────────────

    def _load_state(self) -> None:
        """Load index state from disk."""
        try:
            if self._state_path.exists():
                self._state = json.loads(
                    self._state_path.read_text(encoding="utf-8")
                )
                logger.info("Loaded index state: %d files", len(self._state))
        except Exception as e:
            logger.warning("Failed to load index state: %s", e)
            self._state = {}

    def _save_state(self) -> None:
        """Persist index state to disk."""
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps(self._state, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error("Failed to save index state: %s", e)

    @staticmethod
    def _file_hash(filepath: Path) -> str:
        """Compute SHA-256 hash of a file's content."""
        return hashlib.sha256(
            filepath.read_bytes()
        ).hexdigest()

    # ─── Core Indexing Logic ──────────────────────────

    def run_once(self, force: bool = False) -> dict[str, Any]:
        """
        Scan all whitelisted files, detect changes, and reindex.
        
        Args:
            force: If True, reindex all files regardless of hash.
        
        Returns:
            {
                "files_scanned": int,
                "files_changed": int,
                "chunks_indexed": int,
            }
        """
        files = self._find_memory_files()
        stats = {
            "files_scanned": len(files),
            "files_changed": 0,
            "chunks_indexed": 0,
        }

        current_files = set()

        for filepath in files:
            rel_path = str(filepath.relative_to(self.workspace_root))
            current_files.add(rel_path)

            file_hash = self._file_hash(filepath)
            prev = self._state.get(rel_path, {})

            if not force and prev.get("hash") == file_hash:
                continue  # Unchanged

            logger.info("Indexing changed file: %s", rel_path)
            stats["files_changed"] += 1

            # Delete old chunks for this file
            self._delete_file_chunks(rel_path)

            # Read and chunk
            content = filepath.read_text(encoding="utf-8")
            chunks = chunk_markdown(content, source_file=rel_path)

            if not chunks:
                logger.debug("No chunks generated for %s", rel_path)
                self._state[rel_path] = {
                    "hash": file_hash,
                    "chunks": 0,
                    "last_indexed": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                continue

            # Generate embeddings
            texts = [c.text for c in chunks]
            embeddings = self.embed_client.embed_batch(texts)

            # Build documents
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            documents = []

            for chunk, embedding in zip(chunks, embeddings):
                doc_id = f"chunk:{rel_path}:{chunk.index}"
                body: dict[str, Any] = {
                    "doc_id": doc_id,
                    "doc_type": DOC_TYPE_FILE_CHUNK,
                    "content": chunk.text,
                    "source_file": rel_path,
                    "source_lines": chunk.source_lines,
                    "chunk_index": chunk.index,
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                    "importance": 0.0,
                    "recall_count": 0,
                    "recall_queries": [],
                    "promoted": False,
                    "needs_embed": embedding is None,
                }
                if embedding:
                    body["embedding"] = embedding

                documents.append((doc_id, body))

            # Bulk index
            if documents:
                try:
                    self.os_client.bulk_index(documents)
                    stats["chunks_indexed"] += len(documents)
                    logger.info(
                        "Indexed %d chunks from %s",
                        len(documents), rel_path,
                    )
                except Exception as e:
                    logger.error("Failed to index chunks from %s: %s", rel_path, e)

            # Update state
            self._state[rel_path] = {
                "hash": file_hash,
                "chunks": len(chunks),
                "last_indexed": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }

        # Check for deleted files
        deleted_files = set(self._state.keys()) - current_files
        for rel_path in deleted_files:
            logger.info("File deleted, removing chunks: %s", rel_path)
            self._delete_file_chunks(rel_path)
            del self._state[rel_path]

        self._save_state()
        return stats

    def _delete_file_chunks(self, rel_path: str) -> None:
        """Delete all chunks for a given source file."""
        try:
            self.os_client.delete_by_query({
                "bool": {
                    "must": [
                        {"term": {"source_file": rel_path}},
                        {"term": {"doc_type": DOC_TYPE_FILE_CHUNK}},
                    ]
                }
            })
        except Exception as e:
            logger.warning("Failed to delete chunks for %s: %s", rel_path, e)

    # ─── Background Watcher ───────────────────────────

    def start_polling(self) -> None:
        """Start background polling thread."""
        if self._watcher_thread and self._watcher_thread.is_alive():
            return

        self._shutdown.clear()
        self._watcher_thread = threading.Thread(
            target=self._polling_loop,
            name="memory-indexer",
            daemon=True,
        )
        self._watcher_thread.start()
        logger.info("Indexer polling started (interval=%ds)", INDEX_POLL_INTERVAL_SECS)

    def stop(self) -> None:
        """Stop the background watcher."""
        self._shutdown.set()
        if self._watcher_thread:
            self._watcher_thread.join(timeout=5)

    def _polling_loop(self) -> None:
        """Main polling loop."""
        # Initial full scan
        self.run_once()

        while not self._shutdown.is_set():
            self._shutdown.wait(timeout=INDEX_POLL_INTERVAL_SECS)
            if not self._shutdown.is_set():
                try:
                    self.run_once()
                except Exception as e:
                    logger.error("Indexer poll error: %s", e)
