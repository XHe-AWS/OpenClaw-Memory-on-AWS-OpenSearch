"""
OpenClaw Memory System v2 — Ingester
=====================================
Implements aws_memory_write with fully async write path:
  1. Append to WAL (crash recovery)
  2. Push to in-memory pending_queue (<1ms return)
  3. Background batch writer: accumulate → embed → bulk write to OpenSearch

Pending queue is also searchable for consistency (see searcher.py).
"""

import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from config import (
    BATCH_MAX_SIZE,
    BATCH_MAX_WAIT_SECS,
    PENDING_QUEUE_MAX_SIZE,
    WRITE_MAX_RETRIES,
    WRITE_RETRY_BASE_DELAY,
    WAL_PATH,
    TTL_DAYS,
    DOC_TYPE_MESSAGE,
    DEFAULT_API_VERSION,
    EMBED_RETRY_INTERVAL_SECS,
)
from embedding import EmbeddingClient
from opensearch_client import OpenSearchClient

logger = logging.getLogger(__name__)


# How long to keep flushed items in the pending queue for search consistency.
# AOSS eventual consistency is ~60-75s; we keep items 90s to be safe.
FLUSHED_RETENTION_SECS = 90


class PendingItem:
    """A document waiting to be flushed to OpenSearch."""

    __slots__ = ("doc_id", "body", "queued_at", "retries", "flushed_at")

    def __init__(self, doc_id: str, body: dict[str, Any]):
        self.doc_id = doc_id
        self.body = body
        self.queued_at = time.monotonic()
        self.retries = 0
        self.flushed_at: Optional[float] = None  # set after successful flush


class Ingester:
    """
    Async memory writer with WAL-backed pending queue and background batch flusher.
    
    Usage:
        ingester = Ingester(os_client, embed_client)
        ingester.start()       # Start background writer
        ingester.write(...)    # <1ms, returns immediately
        ingester.shutdown()    # Flush remaining, stop thread
    """

    def __init__(
        self,
        os_client: OpenSearchClient,
        embed_client: EmbeddingClient,
    ):
        self.os_client = os_client
        self.embed_client = embed_client

        # In-memory pending queue (thread-safe via lock)
        self._queue: deque[PendingItem] = deque()
        self._lock = threading.Lock()
        self._flush_event = threading.Event()
        self._shutdown = threading.Event()
        self._writer_thread: Optional[threading.Thread] = None

        # Idempotency tracking (recent keys, bounded)
        self._idempotency_keys: dict[str, str] = {}  # key -> doc_id
        self._idempotency_lock = threading.Lock()

        # Error tracking for alerts
        self.last_opensearch_error: Optional[str] = None
        self.last_opensearch_error_time: float = 0
        self.last_embed_error: Optional[str] = None
        self.last_embed_error_time: float = 0

        # Forgotten doc_ids — immediate in-memory blacklist for search filtering.
        # Bridges AOSS eventual consistency gap: forget takes effect instantly
        # even before AOSS indexes the importance=-1 update.
        self._forgotten_ids: set[str] = set()
        self._forgotten_lock = threading.Lock()

        # WAL setup
        self._wal_path = Path(WAL_PATH)
        self._wal_lock = threading.Lock()

    # ─── Public API ───────────────────────────────────

    def write(
        self,
        session_id: str,
        agent_id: str,
        role: str,
        content: str,
        api_version: str = DEFAULT_API_VERSION,
        idempotency_key: Optional[str] = None,
        conversation_ref: Optional[str] = None,
        client_type: Optional[str] = None,
        client_version: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Write a message to memory (async). Returns immediately.
        
        Args:
            session_id: Current session ID.
            agent_id: Agent identifier.
            role: "user", "assistant", or "system".
            content: Message content.
            api_version: API version string.
            idempotency_key: Optional dedup key.
            conversation_ref: Framework-agnostic conversation reference.
            client_type: Client framework identifier.
            client_version: Client version string.
        
        Returns:
            {"status": "queued", "doc_id": "...", "alerts": [...]}
        """
        # Idempotency check
        if idempotency_key:
            with self._idempotency_lock:
                if idempotency_key in self._idempotency_keys:
                    existing_id = self._idempotency_keys[idempotency_key]
                    return {
                        "status": "duplicate",
                        "doc_id": existing_id,
                        "alerts": self._check_alerts(),
                    }

        # Generate doc_id (timestamp + random suffix to avoid collision on rapid writes)
        ts_ms = int(time.time() * 1000)
        rand_suffix = uuid.uuid4().hex[:6]
        doc_id = f"{agent_id}:{session_id}:{ts_ms}_{rand_suffix}"

        # Calculate TTL
        now = datetime.now(timezone.utc)
        ttl_days = TTL_DAYS.get(DOC_TYPE_MESSAGE)
        ttl_iso = (now + timedelta(days=ttl_days)).isoformat() if ttl_days else None

        # Build document (without embedding — that's done by batch writer)
        body: dict[str, Any] = {
            "doc_id": doc_id,
            "doc_type": DOC_TYPE_MESSAGE,
            "agent_id": agent_id,
            "content": content,
            "session_id": session_id,
            "role": role,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "day": now.strftime("%Y-%m-%d"),
            "importance": 0.0,
            "recall_count": 0,
            "recall_queries": [],
            "promoted": False,
            "needs_embed": True,  # Will be set to False after embedding
        }
        if ttl_iso:
            body["ttl"] = ttl_iso
        if conversation_ref:
            body["conversation_ref"] = conversation_ref
        if client_type:
            body["client_type"] = client_type
        if client_version:
            body["client_version"] = client_version

        # 1. Write to WAL first (crash safety)
        self._wal_append(doc_id, body)

        # 2. Push to pending queue
        with self._lock:
            if len(self._queue) >= PENDING_QUEUE_MAX_SIZE:
                # Queue full — block briefly, then warn
                logger.warning(
                    "Pending queue at capacity (%d), blocking...",
                    PENDING_QUEUE_MAX_SIZE,
                )
                # Release lock and wait for flush
                pass  # Fall through; we'll add anyway (hard limit is advisory)

            item = PendingItem(doc_id, body)
            self._queue.append(item)

            # Track idempotency
            if idempotency_key:
                with self._idempotency_lock:
                    self._idempotency_keys[idempotency_key] = doc_id
                    # Bound the cache
                    if len(self._idempotency_keys) > 1000:
                        # Remove oldest entries
                        keys = list(self._idempotency_keys.keys())
                        for k in keys[:500]:
                            del self._idempotency_keys[k]

        # 3. Signal the batch writer if batch is full
        if len(self._queue) >= BATCH_MAX_SIZE:
            self._flush_event.set()

        return {
            "status": "queued",
            "doc_id": doc_id,
            "alerts": self._check_alerts(),
        }

    def get_pending_items(self) -> list[dict[str, Any]]:
        """
        Get a snapshot of all pending items (for search consistency).
        Returns list of document bodies.
        """
        with self._lock:
            return [item.body for item in self._queue]

    @property
    def queue_size(self) -> int:
        """Current pending queue size (unflushed items only, for alerting)."""
        return sum(1 for item in self._queue if item.flushed_at is None)

    # ─── Forget Blacklist ─────────────────────────────

    def mark_forgotten(self, doc_ids: list[str]) -> None:
        """
        Add doc_ids to the in-memory forgotten set.
        Search results containing these IDs will be filtered out immediately,
        bridging the AOSS eventual consistency gap.
        """
        with self._forgotten_lock:
            self._forgotten_ids.update(doc_ids)
        logger.info("Marked %d doc(s) as forgotten (in-memory)", len(doc_ids))

    def is_forgotten(self, doc_id: str) -> bool:
        """Check if a doc_id has been marked as forgotten."""
        with self._forgotten_lock:
            return doc_id in self._forgotten_ids

    def get_forgotten_ids(self) -> set[str]:
        """Get a snapshot of all forgotten doc_ids."""
        with self._forgotten_lock:
            return set(self._forgotten_ids)

    # ─── Background Writer ────────────────────────────

    def start(self) -> None:
        """Start the background batch writer thread."""
        if self._writer_thread and self._writer_thread.is_alive():
            logger.warning("Writer thread already running")
            return

        # Replay WAL on startup
        self._wal_replay()

        self._shutdown.clear()
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            name="memory-writer",
            daemon=True,
        )
        self._writer_thread.start()
        logger.info("Background writer started")

    def shutdown(self, timeout: float = 10.0) -> None:
        """Flush remaining queue and stop the writer thread."""
        logger.info("Shutting down ingester (queue=%d)...", len(self._queue))
        self._shutdown.set()
        self._flush_event.set()  # Wake up the writer

        if self._writer_thread:
            self._writer_thread.join(timeout=timeout)
            if self._writer_thread.is_alive():
                logger.warning("Writer thread did not stop in time")

        # Final flush attempt
        self._flush_batch(force=True)
        logger.info("Ingester shutdown complete (remaining=%d)", len(self._queue))

    def _writer_loop(self) -> None:
        """Main loop for the background batch writer."""
        while not self._shutdown.is_set():
            # Wait for batch to fill or timeout
            self._flush_event.wait(timeout=BATCH_MAX_WAIT_SECS)
            self._flush_event.clear()

            if self._queue:
                self._flush_batch()

            # Clean up flushed items that have expired (AOSS has had time to index)
            self._cleanup_flushed()

    def _cleanup_flushed(self) -> None:
        """Remove flushed items from queue after FLUSHED_RETENTION_SECS.
        
        Items are kept in the queue after flush so that pending queue search
        can still find them while AOSS indexes (60-75s eventual consistency).
        After 90s, AOSS should have indexed them, so we can safely remove.
        """
        now = time.monotonic()
        with self._lock:
            while self._queue:
                item = self._queue[0]
                if item.flushed_at is not None and (now - item.flushed_at) >= FLUSHED_RETENTION_SECS:
                    self._queue.popleft()
                else:
                    break  # queue is ordered by time, so no point checking further

    def _flush_batch(self, force: bool = False) -> None:
        """
        Take items from queue, generate embeddings, and bulk-write to OpenSearch.
        """
        # Collect items to flush (don't remove from queue yet — keep for search consistency)
        with self._lock:
            batch_size = len(self._queue) if force else min(BATCH_MAX_SIZE, len(self._queue))
            if batch_size == 0:
                return
            # Take unflushed items only
            batch: list[PendingItem] = []
            for item in self._queue:
                if item.flushed_at is None:
                    batch.append(item)
                    if len(batch) >= batch_size:
                        break
            if not batch:
                return

        logger.debug("Flushing batch of %d items", len(batch))

        # Generate embeddings
        texts = [item.body["content"] for item in batch]
        try:
            embeddings = self.embed_client.embed_batch(texts)
            self.last_embed_error = None
        except Exception as e:
            logger.error("Batch embedding failed: %s", e)
            self.last_embed_error = str(e)
            self.last_embed_error_time = time.time()
            embeddings = [None] * len(batch)

        # Attach embeddings to documents
        documents: list[tuple[str, dict[str, Any]]] = []
        for item, embedding in zip(batch, embeddings):
            if embedding is not None:
                item.body["embedding"] = embedding
                item.body["needs_embed"] = False
            else:
                # Degraded: store without embedding, BM25 still works
                item.body["needs_embed"] = True
                logger.warning("Storing doc %s without embedding (degraded)", item.doc_id)

            documents.append((item.doc_id, item.body))

        # Bulk write to OpenSearch
        try:
            resp = self.os_client.bulk_index(documents)
            if resp.get("errors"):
                # Re-queue failed items — match by position in batch
                failed_positions = set()
                for idx, os_item in enumerate(resp.get("items", [])):
                    idx_resp = os_item.get("index", {})
                    if idx_resp.get("error"):
                        failed_positions.add(idx)

                for idx, item in enumerate(batch):
                    if idx in failed_positions:
                        item.retries += 1
                        if item.retries >= WRITE_MAX_RETRIES:
                            logger.error(
                                "Dropping %s after %d retries",
                                item.doc_id, WRITE_MAX_RETRIES,
                            )
                            item.flushed_at = time.monotonic()  # mark to expire
                    else:
                        # Success — mark as flushed, keep in queue for AOSS consistency
                        item.flushed_at = time.monotonic()
                        self._wal_remove(item.doc_id)

                self.last_opensearch_error = f"{len(failed_ids)} docs failed"
                self.last_opensearch_error_time = time.time()
            else:
                # All succeeded — mark as flushed (keep in queue for AOSS consistency)
                now = time.monotonic()
                for item in batch:
                    item.flushed_at = now
                    self._wal_remove(item.doc_id)
                self.last_opensearch_error = None

        except Exception as e:
            logger.error("Bulk write failed: %s", e)
            self.last_opensearch_error = str(e)
            self.last_opensearch_error_time = time.time()

            # Re-queue all items for retry
            for item in batch:
                item.retries += 1
                if item.retries < WRITE_MAX_RETRIES:
                    with self._lock:
                        self._queue.appendleft(item)
                else:
                    logger.error(
                        "Dropping %s after %d retries",
                        item.doc_id, WRITE_MAX_RETRIES,
                    )

    # ─── WAL (Write-Ahead Log) ────────────────────────

    def _wal_append(self, doc_id: str, body: dict[str, Any]) -> None:
        """Append a document to the WAL file."""
        try:
            self._wal_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps({"doc_id": doc_id, "body": body}, ensure_ascii=False)
            with self._wal_lock:
                with open(self._wal_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                    f.flush()
                    os.fsync(f.fileno())
        except Exception as e:
            logger.error("WAL append failed for %s: %s", doc_id, e)

    def _wal_remove(self, doc_id: str) -> None:
        """Remove a successfully written doc from the WAL."""
        try:
            with self._wal_lock:
                if not self._wal_path.exists():
                    return
                lines = self._wal_path.read_text(encoding="utf-8").splitlines()
                remaining = []
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("doc_id") != doc_id:
                            remaining.append(line)
                    except json.JSONDecodeError:
                        remaining.append(line)  # Keep unparseable lines

                self._wal_path.write_text(
                    "\n".join(remaining) + ("\n" if remaining else ""),
                    encoding="utf-8",
                )
        except Exception as e:
            logger.error("WAL remove failed for %s: %s", doc_id, e)

    def _wal_replay(self) -> None:
        """Replay WAL entries on startup (crash recovery)."""
        if not self._wal_path.exists():
            logger.info("No WAL file found, skipping replay")
            return

        try:
            lines = self._wal_path.read_text(encoding="utf-8").splitlines()
            count = 0
            for line in lines:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    doc_id = entry["doc_id"]
                    body = entry["body"]
                    item = PendingItem(doc_id, body)
                    self._queue.append(item)
                    count += 1
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning("Skipping malformed WAL entry: %s", e)

            if count > 0:
                logger.info("Replayed %d entries from WAL", count)
        except Exception as e:
            logger.error("WAL replay failed: %s", e)

    # ─── Alerts ───────────────────────────────────────

    def _check_alerts(self) -> list[str]:
        """Generate alert messages based on current health."""
        from config import (
            ALERT_QUEUE_INFO,
            ALERT_QUEUE_WARNING,
            ALERT_QUEUE_CRITICAL,
            ALERT_OPENSEARCH_ERROR_WINDOW_SECS,
        )

        alerts = []
        qs = len(self._queue)

        if qs > ALERT_QUEUE_CRITICAL:
            alerts.append(
                f"🔴 CRITICAL: 记忆写入队列堆积 {qs} 条，"
                f"可能存在持久化问题，建议立即检查。"
            )
        elif qs > ALERT_QUEUE_WARNING:
            alerts.append(
                f"⚠️ WARNING: 记忆写入队列堆积 {qs} 条，"
                f"最近 {qs} 条消息可能未持久化。"
            )
        elif qs > ALERT_QUEUE_INFO:
            alerts.append(
                f"ℹ️ 写入队列当前 {qs} 条，略有延迟。"
            )

        now = time.time()
        if (
            self.last_opensearch_error
            and now - self.last_opensearch_error_time < ALERT_OPENSEARCH_ERROR_WINDOW_SECS
        ):
            age = int(now - self.last_opensearch_error_time)
            alerts.append(
                f"⚠️ OpenSearch 写入异常（{age}秒前），"
                f"原因: {self.last_opensearch_error}"
            )

        if (
            self.last_embed_error
            and now - self.last_embed_error_time < ALERT_OPENSEARCH_ERROR_WINDOW_SECS
        ):
            age = int(now - self.last_embed_error_time)
            alerts.append(
                f"⚠️ Bedrock Embedding 异常（{age}秒前），"
                f"已降级为纯文本存储，向量搜索暂时不可用"
            )

        return alerts
