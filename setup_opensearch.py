"""
OpenClaw Memory System v2 — OpenSearch Setup
==============================================
Creates the index mapping and search pipeline.
Run once after the OpenSearch Serverless collection is created.

Usage:
    python setup_opensearch.py
"""

import logging
import sys

from opensearch_client import OpenSearchClient
from config import INDEX_NAME, SEARCH_PIPELINE_NAME, EMBED_DIMENSIONS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ─── Index Mapping ─────────────────────────────────
INDEX_MAPPING = {
    "properties": {
        # Identity
        "doc_id": {"type": "keyword"},
        "doc_type": {"type": "keyword"},
        "agent_id": {"type": "keyword"},

        # Content
        "content": {
            "type": "text",
            "analyzer": "icu_analyzer",
            "fields": {
                "keyword": {"type": "keyword", "ignore_above": 512}
            },
        },

        # Vector embedding (Titan Embed V2: 1024 dims)
        "embedding": {
            "type": "knn_vector",
            "dimension": EMBED_DIMENSIONS,
            "method": {
                "name": "hnsw",
                "space_type": "l2",
                "engine": "faiss",
            },
        },

        # Source tracking
        "source_file": {"type": "keyword"},
        "source_lines": {"type": "keyword"},

        # Session context
        "session_id": {"type": "keyword"},
        "role": {"type": "keyword"},
        "category": {"type": "keyword"},
        "chunk_index": {"type": "integer"},

        # Dreaming signals
        "importance": {"type": "float"},
        "recall_count": {"type": "integer"},
        "recall_queries": {"type": "keyword"},
        "promoted": {"type": "boolean"},
        "promoted_at": {"type": "date"},
        "phase": {"type": "keyword"},
        "source_sessions": {"type": "keyword"},
        "source_day": {"type": "keyword"},

        # Timestamps
        "created_at": {"type": "date"},
        "updated_at": {"type": "date"},
        "day": {"type": "date", "format": "yyyy-MM-dd"},
        "ttl": {"type": "date"},

        # Degradation flag
        "needs_embed": {"type": "boolean"},

        # Framework-agnostic identifiers
        "conversation_ref": {"type": "keyword"},
        "client_type": {"type": "keyword"},
        "client_version": {"type": "keyword"},

        # Idempotency
        "idempotency_key": {"type": "keyword"},
    }
}


def setup(client: OpenSearchClient) -> None:
    """Create index and search pipeline."""

    # 1. Create index
    logger.info("Creating index '%s'...", client.index_name)
    result = client.create_index(INDEX_MAPPING)
    logger.info("Index creation result: %s", result)

    # 2. Create search pipeline
    logger.info("Creating search pipeline '%s'...", SEARCH_PIPELINE_NAME)
    try:
        result = client.create_search_pipeline(SEARCH_PIPELINE_NAME)
        logger.info("Search pipeline creation result: %s", result)
    except Exception as e:
        logger.error("Failed to create search pipeline: %s", e)
        logger.info("You may need to create it manually if the collection doesn't support pipelines yet.")


def main():
    """Entry point."""
    from config import OPENSEARCH_ENDPOINT
    if not OPENSEARCH_ENDPOINT:
        print("ERROR: OPENSEARCH_ENDPOINT not set. Set OPENCLAW_MEMORY_OPENSEARCH_ENDPOINT env var.")
        sys.exit(1)

    client = OpenSearchClient()
    if not client.ping():
        print("ERROR: Cannot connect to OpenSearch. Check endpoint and credentials.")
        sys.exit(1)

    setup(client)
    print("\n✅ OpenSearch setup complete!")
    print(f"   Index: {INDEX_NAME}")
    print(f"   Pipeline: {SEARCH_PIPELINE_NAME}")


if __name__ == "__main__":
    main()
