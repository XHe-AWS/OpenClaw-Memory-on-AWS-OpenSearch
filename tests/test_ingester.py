"""
Tests for ingester.py — uses mocked OpenSearch and Bedrock clients.
"""
import json
import os
import tempfile
import time
import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path

from ingester import Ingester, PendingItem


class MockEmbedClient:
    """Mock embedding client that returns deterministic vectors."""

    def embed_text(self, text, max_retries=3):
        return [0.1] * 1024

    def embed_batch(self, texts, max_retries=3):
        return [[0.1] * 1024 for _ in texts]


class MockOSClient:
    """Mock OpenSearch client that records calls."""

    def __init__(self):
        self.indexed = []
        self.bulk_calls = []

    def bulk_index(self, documents):
        self.bulk_calls.append(documents)
        self.indexed.extend(documents)
        return {"errors": False, "items": []}

    def ping(self):
        return True


class TestIngesterWrite:
    """Test the synchronous write path (queue + WAL)."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.wal_path = os.path.join(self.tmpdir, "wal.jsonl")

        with patch("ingester.WAL_PATH", self.wal_path):
            # Need to re-set WAL_PATH since it's read at import
            self.os_client = MockOSClient()
            self.embed_client = MockEmbedClient()
            self.ingester = Ingester(self.os_client, self.embed_client)
            self.ingester._wal_path = Path(self.wal_path)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_write_returns_immediately(self):
        """write() should return < ~10ms with status 'queued'."""
        start = time.time()
        result = self.ingester.write(
            session_id="sess-1",
            agent_id="test-agent",
            role="user",
            content="hello world",
        )
        elapsed = time.time() - start

        assert result["status"] == "queued"
        assert "doc_id" in result
        assert elapsed < 0.1  # Should be <1ms, give 100ms buffer for CI

    def test_write_adds_to_queue(self):
        """write() adds item to pending queue."""
        self.ingester.write("s1", "a1", "user", "msg1")
        self.ingester.write("s1", "a1", "assistant", "msg2")
        assert self.ingester.queue_size == 2

    def test_write_appends_wal(self):
        """write() appends to WAL file."""
        self.ingester.write("s1", "a1", "user", "wal test")
        assert os.path.exists(self.wal_path)
        with open(self.wal_path) as f:
            lines = f.readlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert "wal test" in entry["body"]["content"]

    def test_idempotency(self):
        """Duplicate idempotency_key returns 'duplicate'."""
        r1 = self.ingester.write("s1", "a1", "user", "msg", idempotency_key="key-1")
        assert r1["status"] == "queued"

        r2 = self.ingester.write("s1", "a1", "user", "msg", idempotency_key="key-1")
        assert r2["status"] == "duplicate"
        assert r2["doc_id"] == r1["doc_id"]

    def test_get_pending_items(self):
        """get_pending_items returns all queued items."""
        self.ingester.write("s1", "a1", "user", "msg1")
        self.ingester.write("s1", "a1", "user", "msg2")
        items = self.ingester.get_pending_items()
        assert len(items) == 2
        assert items[0]["content"] == "msg1"


class TestIngesterBatchFlush:
    """Test the background batch writer."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.wal_path = os.path.join(self.tmpdir, "wal.jsonl")
        self.os_client = MockOSClient()
        self.embed_client = MockEmbedClient()
        self.ingester = Ingester(self.os_client, self.embed_client)
        self.ingester._wal_path = Path(self.wal_path)

    def teardown_method(self):
        self.ingester._shutdown.set()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_flush_batch_embeds_and_writes(self):
        """_flush_batch generates embeddings and bulk writes."""
        self.ingester.write("s1", "a1", "user", "batch test 1")
        self.ingester.write("s1", "a1", "user", "batch test 2")

        self.ingester._flush_batch(force=True)

        assert len(self.os_client.bulk_calls) == 1
        batch = self.os_client.bulk_calls[0]
        assert len(batch) == 2
        # Check embedding was attached
        assert batch[0][1].get("embedding") is not None
        assert batch[0][1]["needs_embed"] is False

    def test_flush_batch_embed_failure_degrades(self):
        """If embedding fails, store without embedding (needs_embed=True)."""
        self.embed_client.embed_batch = lambda texts, **kw: [None] * len(texts)

        self.ingester.write("s1", "a1", "user", "degraded test")
        self.ingester._flush_batch(force=True)

        batch = self.os_client.bulk_calls[0]
        assert batch[0][1]["needs_embed"] is True
        assert "embedding" not in batch[0][1]


class TestIngesterWAL:
    """Test WAL replay and cleanup."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.wal_path = os.path.join(self.tmpdir, "wal.jsonl")
        self.os_client = MockOSClient()
        self.embed_client = MockEmbedClient()

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_wal_replay(self):
        """WAL entries are replayed into queue on startup."""
        # Write some WAL entries manually
        Path(self.wal_path).parent.mkdir(parents=True, exist_ok=True)
        entries = [
            {"doc_id": "a1:s1:100", "body": {"content": "replay1", "doc_type": "message"}},
            {"doc_id": "a1:s1:200", "body": {"content": "replay2", "doc_type": "message"}},
        ]
        with open(self.wal_path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        ingester = Ingester(self.os_client, self.embed_client)
        ingester._wal_path = Path(self.wal_path)
        ingester._wal_replay()

        assert ingester.queue_size == 2

    def test_wal_remove(self):
        """Successfully written doc is removed from WAL."""
        ingester = Ingester(self.os_client, self.embed_client)
        ingester._wal_path = Path(self.wal_path)

        # Write two entries
        ingester.write("s1", "a1", "user", "keep")
        ingester.write("s1", "a1", "user", "remove")

        # Get doc_ids
        items = ingester.get_pending_items()
        keep_id = items[0]["doc_id"]
        remove_id = items[1]["doc_id"]

        # Remove one
        ingester._wal_remove(remove_id)

        with open(self.wal_path) as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
        assert len(lines) == 1
        assert keep_id in lines[0]


class TestIngesterAlerts:
    """Test alert generation."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.os_client = MockOSClient()
        self.embed_client = MockEmbedClient()
        self.ingester = Ingester(self.os_client, self.embed_client)
        self.ingester._wal_path = Path(os.path.join(self.tmpdir, "wal.jsonl"))

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_alerts_empty_queue(self):
        """No alerts when queue is empty and healthy."""
        alerts = self.ingester._check_alerts()
        assert len(alerts) == 0

    def test_warning_alert_on_queue_buildup(self):
        """Warning when queue > 50."""
        for i in range(55):
            item = PendingItem(f"doc-{i}", {"content": f"msg-{i}"})
            self.ingester._queue.append(item)

        alerts = self.ingester._check_alerts()
        assert any("WARNING" in a or "⚠️" in a for a in alerts)

    def test_critical_alert_on_large_queue(self):
        """Critical when queue > 200."""
        for i in range(210):
            item = PendingItem(f"doc-{i}", {"content": f"msg-{i}"})
            self.ingester._queue.append(item)

        alerts = self.ingester._check_alerts()
        assert any("CRITICAL" in a or "🔴" in a for a in alerts)

    def test_opensearch_error_alert(self):
        """Alert when recent OpenSearch error."""
        self.ingester.last_opensearch_error = "Connection refused"
        self.ingester.last_opensearch_error_time = time.time()

        alerts = self.ingester._check_alerts()
        assert any("OpenSearch" in a for a in alerts)
