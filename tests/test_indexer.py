"""
Tests for indexer.py — file watching and chunk indexing.
"""
import json
import os
import tempfile
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from indexer import Indexer


class MockOSClient:
    def __init__(self):
        self.indexed = []
        self.deleted_queries = []

    def bulk_index(self, documents):
        self.indexed.extend(documents)
        return {"errors": False, "items": []}

    def delete_by_query(self, query):
        self.deleted_queries.append(query)
        return {"deleted": 0}


class MockEmbedClient:
    def embed_batch(self, texts, max_retries=3):
        return [[0.1] * 1024 for _ in texts]


class TestIndexer:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.workspace = Path(self.tmpdir) / "workspace"
        self.workspace.mkdir()
        self.state_dir = Path(self.tmpdir) / "state"
        self.state_dir.mkdir()

        self.os_client = MockOSClient()
        self.embed_client = MockEmbedClient()

        # Patch config for testing
        import config
        self._orig_state = config.INDEX_STATE_PATH
        config.INDEX_STATE_PATH = str(self.state_dir / "index-state.json")

        self.indexer = Indexer(
            self.os_client,
            self.embed_client,
            str(self.workspace),
        )
        self.indexer._state_path = Path(config.INDEX_STATE_PATH)

    def teardown_method(self):
        import shutil
        import config
        config.INDEX_STATE_PATH = self._orig_state
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_empty_workspace(self):
        """No files = no chunks indexed."""
        result = self.indexer.run_once()
        assert result["files_scanned"] == 0
        assert result["files_changed"] == 0
        assert result["chunks_indexed"] == 0

    def test_index_memory_md(self):
        """MEMORY.md is whitelisted and indexed."""
        (self.workspace / "MEMORY.md").write_text(
            "## Important\n\nThis is a memory.", encoding="utf-8"
        )
        result = self.indexer.run_once()
        assert result["files_scanned"] == 1
        assert result["files_changed"] == 1
        assert result["chunks_indexed"] >= 1
        assert len(self.os_client.indexed) >= 1

    def test_index_daily_note(self):
        """memory/*.md files are whitelisted and indexed."""
        (self.workspace / "memory").mkdir()
        (self.workspace / "memory" / "2026-04-07.md").write_text(
            "## Daily\n\nToday's notes.", encoding="utf-8"
        )
        result = self.indexer.run_once()
        assert result["files_scanned"] == 1
        assert result["chunks_indexed"] >= 1

    def test_non_whitelisted_ignored(self):
        """SOUL.md and other non-whitelisted files are NOT indexed."""
        (self.workspace / "SOUL.md").write_text("Soul content", encoding="utf-8")
        (self.workspace / "AGENTS.md").write_text("Agent content", encoding="utf-8")
        (self.workspace / "TOOLS.md").write_text("Tools content", encoding="utf-8")
        result = self.indexer.run_once()
        assert result["files_scanned"] == 0
        assert result["chunks_indexed"] == 0

    def test_unchanged_file_skipped(self):
        """Unchanged files are not re-indexed."""
        (self.workspace / "MEMORY.md").write_text(
            "## Test\n\nContent.", encoding="utf-8"
        )
        result1 = self.indexer.run_once()
        assert result1["files_changed"] == 1

        # Run again without changes
        result2 = self.indexer.run_once()
        assert result2["files_changed"] == 0

    def test_changed_file_reindexed(self):
        """Changed files trigger reindex."""
        (self.workspace / "MEMORY.md").write_text("## V1\n\nOriginal.", encoding="utf-8")
        self.indexer.run_once()

        # Modify the file
        (self.workspace / "MEMORY.md").write_text("## V2\n\nUpdated content.", encoding="utf-8")
        result = self.indexer.run_once()
        assert result["files_changed"] == 1
        assert result["chunks_indexed"] >= 1

    def test_deleted_file_cleaned_up(self):
        """Deleted files have their chunks removed."""
        f = self.workspace / "MEMORY.md"
        f.write_text("## Test\n\nContent.", encoding="utf-8")
        self.indexer.run_once()

        # Delete the file
        f.unlink()
        self.indexer.run_once()

        # Should have issued a delete_by_query
        assert len(self.os_client.deleted_queries) >= 1

    def test_force_reindex(self):
        """force=True reindexes even unchanged files."""
        (self.workspace / "MEMORY.md").write_text("## Test\n\nContent.", encoding="utf-8")
        self.indexer.run_once()

        result = self.indexer.run_once(force=True)
        assert result["files_changed"] == 1

    def test_state_persisted(self):
        """Index state is saved to disk."""
        (self.workspace / "MEMORY.md").write_text("## Test\n\nContent.", encoding="utf-8")
        self.indexer.run_once()

        assert self.indexer._state_path.exists()
        state = json.loads(self.indexer._state_path.read_text())
        assert "MEMORY.md" in state
        assert "hash" in state["MEMORY.md"]
        assert "chunks" in state["MEMORY.md"]

    def test_whitelist_check(self):
        """Whitelist patterns are correctly matched."""
        assert self.indexer._is_whitelisted("MEMORY.md")
        assert self.indexer._is_whitelisted("memory/2026-04-07.md")
        assert not self.indexer._is_whitelisted("SOUL.md")
        assert not self.indexer._is_whitelisted("AGENTS.md")
        assert not self.indexer._is_whitelisted("random.md")
