"""
OpenClaw Memory System v2 — Configuration
==========================================
All configuration constants in one place.
Environment variables override defaults.
"""

import os
from pathlib import Path

# ──────────────────────────────────────────
# OpenSearch Serverless
# ──────────────────────────────────────────
OPENSEARCH_ENDPOINT = os.environ.get(
    "OPENCLAW_MEMORY_OPENSEARCH_ENDPOINT",
    ""  # Set by deploy.sh or env
)
OPENSEARCH_REGION = os.environ.get(
    "OPENCLAW_MEMORY_OPENSEARCH_REGION",
    "us-west-2"
)
COLLECTION_NAME = os.environ.get(
    "OPENCLAW_MEMORY_COLLECTION_NAME",
    "openclaw-memory"
)
INDEX_NAME = os.environ.get(
    "OPENCLAW_MEMORY_INDEX_NAME",
    "openclaw-memory"
)
SEARCH_PIPELINE_NAME = "memory-search-pipeline"

# ──────────────────────────────────────────
# Bedrock Models
# ──────────────────────────────────────────
EMBED_MODEL_ID = os.environ.get(
    "OPENCLAW_MEMORY_EMBED_MODEL",
    "amazon.titan-embed-text-v2:0"
)
EMBED_DIMENSIONS = 1024

# LLM for Dreaming phases (extraction, summarization, scoring)
EXTRACT_MODEL_ID = os.environ.get(
    "OPENCLAW_MEMORY_EXTRACT_MODEL",
    "us.anthropic.claude-sonnet-4-6"
)
BEDROCK_REGION = os.environ.get(
    "OPENCLAW_MEMORY_BEDROCK_REGION",
    "us-west-2"
)

# ──────────────────────────────────────────
# Ingester — Async Write Queue
# ──────────────────────────────────────────
# Batch triggers: whichever comes first
BATCH_MAX_SIZE = 10          # Max messages per batch
BATCH_MAX_WAIT_SECS = 2.0   # Max wait time before flushing

# Pending queue limits
PENDING_QUEUE_MAX_SIZE = 200  # Block writes beyond this to prevent OOM

# Retry on write failures
WRITE_MAX_RETRIES = 3
WRITE_RETRY_BASE_DELAY = 1.0  # Exponential backoff base (seconds)

# Embed re-try scan interval (for needs_embed=true docs)
EMBED_RETRY_INTERVAL_SECS = 300  # 5 minutes

# ──────────────────────────────────────────
# WAL (Write-Ahead Log) — crash recovery
# ──────────────────────────────────────────
WAL_PATH = os.environ.get(
    "OPENCLAW_MEMORY_WAL_PATH",
    os.path.expanduser("~/.openclaw/memory/wal.jsonl")
)

# ──────────────────────────────────────────
# Searcher
# ──────────────────────────────────────────
# Hybrid search weights (BM25 vs kNN)
BM25_WEIGHT = 0.3
KNN_WEIGHT = 0.7

# Temporal decay: half-life 90 days
TEMPORAL_DECAY_HALF_LIFE_DAYS = 90

# MMR (Maximal Marginal Relevance)
MMR_LAMBDA = 0.7  # 0=max diversity, 1=max relevance

# Default search parameters
DEFAULT_TOP_K = 5
SEARCH_OVERSAMPLE_FACTOR = 4  # Fetch top_k * factor from OpenSearch, then rerank

# ──────────────────────────────────────────
# Alert thresholds
# ──────────────────────────────────────────
ALERT_QUEUE_INFO = 10
ALERT_QUEUE_WARNING = 50
ALERT_QUEUE_CRITICAL = 200
ALERT_OPENSEARCH_ERROR_WINDOW_SECS = 300  # 5 minutes

# ──────────────────────────────────────────
# Indexer — File Watcher
# ──────────────────────────────────────────
# Only index these paths (relative to workspace root)
MEMORY_PATHS = [
    "MEMORY.md",
    "memory/*.md",
]
EXTRA_MEMORY_PATHS_ENV = os.environ.get("OPENCLAW_MEMORY_EXTRA_PATHS", "")
EXTRA_MEMORY_PATHS = [
    p.strip() for p in EXTRA_MEMORY_PATHS_ENV.split(",") if p.strip()
]

# Index state file
INDEX_STATE_PATH = os.environ.get(
    "OPENCLAW_MEMORY_INDEX_STATE_PATH",
    os.path.expanduser("~/.openclaw/memory/index-state.json")
)

# Polling interval
INDEX_POLL_INTERVAL_SECS = 30  # Polling interval for file changes

# Debounce for event-based watcher
INDEX_DEBOUNCE_SECS = 2.0

# ──────────────────────────────────────────
# Chunker
# ──────────────────────────────────────────
CHUNK_TARGET_TOKENS = 400
CHUNK_OVERLAP_TOKENS = 80

# ──────────────────────────────────────────
# Dreaming
# ──────────────────────────────────────────
# Deep phase scoring weights (7 dimensions)
DEEP_WEIGHTS = {
    "frequency":           0.20,
    "relevance":           0.20,
    "query_diversity":     0.15,
    "recency":             0.15,
    "consolidation":       0.10,
    "conceptual_richness": 0.05,
    "content_quality":     0.15,
}

# Promotion thresholds
DEEP_MIN_SCORE = 0.6
DEEP_MIN_RECALL_COUNT = 1
DEEP_MIN_UNIQUE_QUERIES = 1
DEEP_MAX_AGE_DAYS = 30

# Dedup thresholds
DEDUP_HIGH_SIMILARITY = 0.92  # Skip (duplicate)
DEDUP_MERGE_SIMILARITY = 0.85  # Mark as mergeable

# Diversity cap for query_diversity signal
QUERY_DIVERSITY_CAP = 20

# ──────────────────────────────────────────
# Cross-Agent Query
# ──────────────────────────────────────────
# Agents in this list can search ALL agents' memories (no filter)
EXCEPTION_AGENT_LIST = os.environ.get(
    "OPENCLAW_MEMORY_EXCEPTION_AGENTS",
    "xiaoxiami"
).split(",")

# ──────────────────────────────────────────
# Doc types & TTLs
# ──────────────────────────────────────────
DOC_TYPE_MESSAGE = "message"
DOC_TYPE_FILE_CHUNK = "file_chunk"
DOC_TYPE_EXTRACTED = "extracted"
DOC_TYPE_SESSION_SUMMARY = "session_summary"
DOC_TYPE_DAILY_NOTE = "daily_note"

TTL_DAYS = {
    DOC_TYPE_MESSAGE: 7,
    DOC_TYPE_SESSION_SUMMARY: 30,
    DOC_TYPE_DAILY_NOTE: 14,
    DOC_TYPE_FILE_CHUNK: None,     # Follows file lifecycle
    DOC_TYPE_EXTRACTED: None,      # Long-term
}

# ──────────────────────────────────────────
# API versioning
# ──────────────────────────────────────────
DEFAULT_API_VERSION = "v1"

# ──────────────────────────────────────────
# Workspace root (auto-detected or manual)
# ──────────────────────────────────────────
WORKSPACE_ROOT = os.environ.get(
    "OPENCLAW_WORKSPACE_ROOT",
    ""  # Must be set by MCP server on startup
)

# ──────────────────────────────────────────
# Logging
# ──────────────────────────────────────────
LOG_LEVEL = os.environ.get("OPENCLAW_MEMORY_LOG_LEVEL", "INFO")
LOG_FILE = os.environ.get(
    "OPENCLAW_MEMORY_LOG_FILE",
    os.path.expanduser("~/.openclaw/memory/memory-v2.log")
)
