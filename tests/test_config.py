"""
Tests for config.py
"""
import os
import importlib


def test_defaults_loaded():
    """Verify all key config constants are defined with sane defaults."""
    import config
    importlib.reload(config)

    assert config.OPENSEARCH_REGION == "us-west-2"
    assert config.INDEX_NAME == "openclaw-memory"
    assert config.EMBED_DIMENSIONS == 1024
    assert config.BATCH_MAX_SIZE == 10
    assert config.BATCH_MAX_WAIT_SECS == 2.0
    assert config.PENDING_QUEUE_MAX_SIZE == 200
    assert config.TEMPORAL_DECAY_HALF_LIFE_DAYS == 90
    assert config.MMR_LAMBDA == 0.7
    assert config.DEFAULT_TOP_K == 5
    assert config.BM25_WEIGHT == 0.3
    assert config.KNN_WEIGHT == 0.7
    assert config.DEFAULT_API_VERSION == "v1"
    assert config.DEEP_MIN_SCORE == 0.6
    assert config.DEEP_MIN_RECALL_COUNT == 1
    assert config.DEEP_MIN_UNIQUE_QUERIES == 1
    assert "xiaoxiami" in config.EXCEPTION_AGENT_LIST
    assert "MEMORY.md" in config.MEMORY_PATHS


def test_env_override():
    """Verify environment variables override defaults."""
    os.environ["OPENCLAW_MEMORY_OPENSEARCH_ENDPOINT"] = "https://test.example.com"
    os.environ["OPENCLAW_MEMORY_OPENSEARCH_REGION"] = "us-east-1"
    os.environ["OPENCLAW_MEMORY_INDEX_NAME"] = "test-index"

    import config
    importlib.reload(config)

    assert config.OPENSEARCH_ENDPOINT == "https://test.example.com"
    assert config.OPENSEARCH_REGION == "us-east-1"
    assert config.INDEX_NAME == "test-index"

    # Cleanup
    del os.environ["OPENCLAW_MEMORY_OPENSEARCH_ENDPOINT"]
    del os.environ["OPENCLAW_MEMORY_OPENSEARCH_REGION"]
    del os.environ["OPENCLAW_MEMORY_INDEX_NAME"]


def test_exception_agent_list_env():
    """EXCEPTION_AGENT_LIST can be overridden via env."""
    os.environ["OPENCLAW_MEMORY_EXCEPTION_AGENTS"] = "agentA,agentB"
    import config
    importlib.reload(config)

    assert "agentA" in config.EXCEPTION_AGENT_LIST
    assert "agentB" in config.EXCEPTION_AGENT_LIST

    del os.environ["OPENCLAW_MEMORY_EXCEPTION_AGENTS"]


def test_deep_weights_sum_to_one():
    """Dreaming deep weights should sum to 1.0."""
    import config
    importlib.reload(config)

    total = sum(config.DEEP_WEIGHTS.values())
    assert abs(total - 1.0) < 0.001, f"Weights sum to {total}, expected 1.0"


def test_ttl_days():
    """TTL days should be defined for all doc types."""
    import config
    importlib.reload(config)

    assert config.TTL_DAYS[config.DOC_TYPE_MESSAGE] == 7
    assert config.TTL_DAYS[config.DOC_TYPE_SESSION_SUMMARY] == 30
    assert config.TTL_DAYS[config.DOC_TYPE_FILE_CHUNK] is None
    assert config.TTL_DAYS[config.DOC_TYPE_EXTRACTED] is None
