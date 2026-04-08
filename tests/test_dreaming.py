"""
Tests for dreaming phases — Light, REM, Deep.
Uses mocked Bedrock, OpenSearch, and embedding clients.
"""
import json
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
from io import BytesIO

from dreaming.light import LightDreaming
from dreaming.rem import REMDreaming
from dreaming.deep import DeepDreaming


class MockOSClient:
    def __init__(self, hits=None):
        self.hits = hits or []
        self.indexed = {}
        self.updated = {}

    def hybrid_search(self, **kwargs):
        return self.hits

    def keyword_search(self, **kwargs):
        return self.hits

    def index_document(self, doc_id, body):
        self.indexed[doc_id] = body
        return {"result": "created"}

    def update_document(self, doc_id, partial):
        self.updated[doc_id] = partial
        return {"result": "updated"}


class MockEmbedClient:
    def embed_text(self, text, max_retries=3):
        return [0.1] * 1024

    def embed_batch(self, texts, max_retries=3):
        return [[0.1] * 1024 for _ in texts]

    def cosine_similarity(self, a, b):
        return 0.5  # Default: not a duplicate


def _make_bedrock_response(text):
    body = json.dumps({
        "content": [{"type": "text", "text": text}]
    }).encode()
    return {"body": BytesIO(body)}


# ─── Light Phase Tests ────────────────────────────

class TestLightDreaming:
    def test_no_messages(self):
        """Empty messages = empty report."""
        dreaming = LightDreaming(MockOSClient([]), MockEmbedClient())
        report = dreaming.run()
        assert report["phase"] == "light"
        assert report["messages_processed"] == 0

    def test_parse_candidates(self):
        """Candidate parsing extracts [Category] content."""
        dreaming = LightDreaming(MockOSClient(), MockEmbedClient())
        text = """[Preference] User prefers Python over Java
[Fact] User works at AWS
[Decision] Chose OpenSearch over Elasticsearch"""
        candidates = dreaming._parse_candidates(text)
        assert len(candidates) == 3
        assert candidates[0] == ("Preference", "User prefers Python over Java")
        assert candidates[1] == ("Fact", "User works at AWS")

    def test_parse_candidates_with_dash(self):
        """Supports '- [Category] content' format."""
        dreaming = LightDreaming(MockOSClient(), MockEmbedClient())
        text = "- [Fact] Some fact\n- [Goal] Some goal"
        candidates = dreaming._parse_candidates(text)
        assert len(candidates) == 2

    def test_parse_candidates_none(self):
        """NONE output returns empty list."""
        dreaming = LightDreaming(MockOSClient(), MockEmbedClient())
        assert dreaming._parse_candidates("NONE") == []

    def test_dedup_stores_new(self):
        """New content (no duplicates) is stored."""
        os_client = MockOSClient([])  # No existing memories
        dreaming = LightDreaming(os_client, MockEmbedClient())
        result = dreaming._dedup_and_store("new fact", "Fact", "a1", "s1")
        assert result == "stored"
        assert len(os_client.indexed) == 1

    def test_dedup_detects_duplicate(self):
        """High similarity (>0.92) is detected as duplicate."""
        embed = MockEmbedClient()
        embed.cosine_similarity = lambda a, b: 0.95

        os_client = MockOSClient([
            {
                "_id": "existing",
                "_score": 0.9,
                "_source": {
                    "embedding": [0.1] * 1024,
                    "doc_type": "extracted",
                },
            }
        ])
        dreaming = LightDreaming(os_client, embed)
        result = dreaming._dedup_and_store("duplicate", "Fact", "a1", "s1")
        assert result == "duplicate"

    def test_format_conversation(self):
        """Messages are formatted as [role]: content."""
        dreaming = LightDreaming(MockOSClient(), MockEmbedClient())
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        result = dreaming._format_conversation(messages)
        assert "[user]: Hello" in result
        assert "[assistant]: Hi there" in result


# ─── REM Phase Tests ──────────────────────────────

class TestREMDreaming:
    def test_no_memories(self):
        """Empty memories = empty report."""
        dreaming = REMDreaming(MockOSClient([]), MockEmbedClient())
        report = dreaming.run()
        assert report["phase"] == "rem"
        assert report["memories_processed"] == 0

    def test_group_by_category_fallback(self):
        """Fallback grouping when not enough embeddings."""
        dreaming = REMDreaming(MockOSClient(), MockEmbedClient())
        memories = [
            {"_id": "1", "category": "Fact", "content": "a"},
            {"_id": "2", "category": "Fact", "content": "b"},
            {"_id": "3", "category": "Preference", "content": "c"},
        ]
        clusters = dreaming._group_by_category(memories)
        assert len(clusters) == 2


# ─── Deep Phase Tests ─────────────────────────────

class TestDeepDreaming:
    def test_no_candidates(self):
        """Empty candidates = empty report."""
        dreaming = DeepDreaming(MockOSClient([]), MockEmbedClient())
        report = dreaming.run()
        assert report["phase"] == "deep"
        assert report["candidates_scored"] == 0

    def test_conceptual_richness(self):
        """Conceptual richness scores correctly."""
        dreaming = DeepDreaming(MockOSClient(), MockEmbedClient())

        # Technical content should score higher
        high = dreaming._compute_conceptual_richness(
            "AWS OpenSearch Serverless uses HNSW algorithm for kNN"
        )
        low = dreaming._compute_conceptual_richness(
            "this is a simple test with nothing special"
        )
        assert high > low

    def test_age_calculation(self):
        """Age is computed correctly from created_at."""
        now = datetime.now(timezone.utc)
        three_days_ago = (now - timedelta(days=3)).isoformat()

        age = DeepDreaming._age_days({"created_at": three_days_ago})
        assert 2.9 < age < 3.1

    def test_age_default_on_missing(self):
        """Missing created_at defaults to 30 days."""
        age = DeepDreaming._age_days({})
        assert age == 30

    def test_score_weights_sum_to_one(self):
        """Verify DEEP_WEIGHTS sum to 1.0 (tested in config too)."""
        from config import DEEP_WEIGHTS
        total = sum(DEEP_WEIGHTS.values())
        assert abs(total - 1.0) < 0.001

    def test_promote_writes_to_opensearch(self):
        """Promotion updates the OpenSearch document."""
        os_client = MockOSClient()
        dreaming = DeepDreaming(os_client, MockEmbedClient())

        with patch.object(dreaming, '_append_to_memory_md'):
            dreaming._promote({
                "doc_id": "test-promote",
                "content": "important fact",
                "category": "Fact",
                "total_score": 0.85,
            })

        assert "test-promote" in os_client.updated
        assert os_client.updated["test-promote"]["promoted"] is True
        assert os_client.updated["test-promote"]["importance"] == 0.85

    def test_verify_content(self):
        """Content verification checks for non-empty."""
        dreaming = DeepDreaming(MockOSClient(), MockEmbedClient())
        assert dreaming._verify_content("valid content") is True
        assert dreaming._verify_content("") is False
        assert dreaming._verify_content("   ") is False

    def test_scoring_dimensions(self):
        """All 7 dimensions are computed."""
        dreaming = DeepDreaming(MockOSClient(), MockEmbedClient())
        # Mock LLM for content_quality
        dreaming._bedrock = MagicMock()
        dreaming._bedrock.invoke_model.return_value = _make_bedrock_response("0.7")

        candidate = {
            "content": "User prefers Python over Java",
            "recall_count": 3,
            "recall_queries": ["python", "java", "language"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "rem_theme_importance": 0.8,
            "rem_consolidation": 0.5,
        }

        scores = dreaming._compute_score(candidate, max_recall=5)

        assert "frequency" in scores
        assert "relevance" in scores
        assert "query_diversity" in scores
        assert "recency" in scores
        assert "consolidation" in scores
        assert "conceptual_richness" in scores
        assert "content_quality" in scores

        # All scores should be 0-1
        for dim, val in scores.items():
            assert 0.0 <= val <= 1.0, f"{dim} = {val} out of range"
