"""
Tests for searcher.py — uses mocked OpenSearch and Bedrock clients.
"""
import math
import time
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

from searcher import Searcher, _DECAY_LAMBDA


class MockEmbedClient:
    def embed_text(self, text, max_retries=3):
        return [0.1] * 1024

    def cosine_similarity(self, a, b):
        if a == b:
            return 1.0
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x**2 for x in a) ** 0.5
        nb = sum(x**2 for x in b) ** 0.5
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)


class MockOSClient:
    def __init__(self, hits=None):
        self.hits = hits or []
        self.updates = []

    def hybrid_search(self, **kwargs):
        return self.hits

    def keyword_search(self, **kwargs):
        return self.hits

    def update_document(self, doc_id, partial):
        self.updates.append((doc_id, partial))


class MockIngester:
    def __init__(self, items=None):
        self._items = items or []

    def get_pending_items(self):
        return self._items

    def _check_alerts(self):
        return []


class TestSearcherBasic:
    def test_empty_search(self):
        """Search with no results returns empty list."""
        searcher = Searcher(
            MockOSClient([]),
            MockEmbedClient(),
            MockIngester(),
        )
        result = searcher.search("hello")
        assert result["total"] == 0
        assert result["results"] == []
        assert "alerts" in result

    def test_search_returns_formatted_results(self):
        """Search results are properly formatted."""
        now = datetime.now(timezone.utc)
        hits = [
            {
                "_id": "doc-1",
                "_score": 0.9,
                "_source": {
                    "content": "test memory content",
                    "doc_type": "message",
                    "agent_id": "a1",
                    "session_id": "s1",
                    "role": "user",
                    "category": "",
                    "created_at": now.isoformat(),
                    "source_file": "",
                    "recall_count": 0,
                    "recall_queries": [],
                },
            }
        ]
        searcher = Searcher(
            MockOSClient(hits),
            MockEmbedClient(),
            MockIngester(),
        )
        result = searcher.search("test", agent_id="a1")
        assert result["total"] == 1
        assert result["results"][0]["text"] == "test memory content"
        assert result["results"][0]["doc_id"] == "doc-1"


class TestTemporalDecay:
    def test_recent_doc_minimal_decay(self):
        """Document created just now should have minimal decay."""
        now = time.time()
        hit = {
            "_score": 1.0,
            "_source": {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "doc_type": "message",
            },
        }
        searcher = Searcher(MockOSClient(), MockEmbedClient())
        decayed = searcher._apply_temporal_decay(hit, now)
        assert decayed > 0.99  # Almost no decay

    def test_90_day_old_doc_half_decay(self):
        """Document 90 days old should have ~50% score."""
        now = time.time()
        created = datetime.now(timezone.utc) - timedelta(days=90)
        hit = {
            "_score": 1.0,
            "_source": {
                "created_at": created.isoformat(),
                "doc_type": "message",
            },
        }
        searcher = Searcher(MockOSClient(), MockEmbedClient())
        decayed = searcher._apply_temporal_decay(hit, now)
        assert 0.45 < decayed < 0.55  # ~0.5 with some tolerance

    def test_memory_md_evergreen(self):
        """MEMORY.md file_chunks don't decay."""
        now = time.time()
        created = datetime.now(timezone.utc) - timedelta(days=365)
        hit = {
            "_score": 1.0,
            "_source": {
                "created_at": created.isoformat(),
                "doc_type": "file_chunk",
                "source_file": "MEMORY.md",
            },
        }
        searcher = Searcher(MockOSClient(), MockEmbedClient())
        decayed = searcher._apply_temporal_decay(hit, now)
        assert decayed == 1.0  # No decay


class TestPendingQueueMerge:
    def test_pending_items_searchable(self):
        """Items in pending queue are included in search results."""
        pending = [
            {
                "doc_id": "pending-1",
                "content": "I love sushi",
                "agent_id": "a1",
                "doc_type": "message",
            }
        ]
        searcher = Searcher(
            MockOSClient([]),
            MockEmbedClient(),
            MockIngester(pending),
        )
        result = searcher.search("sushi", agent_id="a1")
        assert result["total"] == 1
        assert "sushi" in result["results"][0]["text"]

    def test_dedup_pending_vs_os(self):
        """Pending item with same doc_id as OS hit is deduplicated."""
        now = datetime.now(timezone.utc).isoformat()
        os_hits = [
            {
                "_id": "same-id",
                "_score": 0.8,
                "_source": {
                    "content": "from opensearch",
                    "created_at": now,
                    "doc_type": "message",
                    "recall_count": 0,
                    "recall_queries": [],
                },
            }
        ]
        pending = [
            {
                "doc_id": "same-id",
                "content": "from pending",
                "agent_id": "a1",
                "doc_type": "message",
            }
        ]
        searcher = Searcher(
            MockOSClient(os_hits),
            MockEmbedClient(),
            MockIngester(pending),
        )
        result = searcher.search("test", agent_id="a1")
        # Should only have one result (OS wins)
        assert result["total"] == 1


class TestCrossAgentSearch:
    def test_exception_agent_no_filter(self):
        """Agents in EXCEPTION_AGENT_LIST search all agents."""
        searcher = Searcher(MockOSClient([]), MockEmbedClient(), MockIngester())
        filters = searcher._build_filters("xiaoxiami", None, None, None)
        # Should NOT have agent_id filter
        if filters:
            for f in (filters if isinstance(filters, list) else [filters]):
                assert "agent_id" not in str(f).lower() or "term" not in f

    def test_regular_agent_has_filter(self):
        """Regular agents get agent_id filter."""
        searcher = Searcher(MockOSClient([]), MockEmbedClient(), MockIngester())
        filters = searcher._build_filters("other-agent", None, None, None)
        assert filters is not None
        filter_str = str(filters)
        assert "other-agent" in filter_str

    def test_empty_agent_no_filter(self):
        """Empty agent_id = search all."""
        searcher = Searcher(MockOSClient([]), MockEmbedClient(), MockIngester())
        filters = searcher._build_filters(None, None, None, None)
        # Should only have importance filter, no agent_id
        if filters:
            filter_str = str(filters)
            assert "agent_id" not in filter_str or "term" not in filter_str


class TestRecallSignals:
    def test_recall_count_updated(self):
        """Search updates recall_count for returned results."""
        now = datetime.now(timezone.utc).isoformat()
        os_client = MockOSClient([
            {
                "_id": "doc-recall",
                "_score": 0.9,
                "_source": {
                    "content": "recall test",
                    "created_at": now,
                    "doc_type": "message",
                    "recall_count": 5,
                    "recall_queries": ["old query"],
                    "source_file": "",
                },
            }
        ])
        searcher = Searcher(os_client, MockEmbedClient(), MockIngester())
        searcher.search("recall test")

        assert len(os_client.updates) == 1
        doc_id, partial = os_client.updates[0]
        assert doc_id == "doc-recall"
        assert partial["recall_count"] == 6
        assert "recall test" in partial["recall_queries"]
