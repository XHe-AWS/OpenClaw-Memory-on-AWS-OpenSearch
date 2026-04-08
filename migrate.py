"""
OpenClaw Memory System v2 — Data Migration
============================================
Migrates existing data from:
  1. DynamoDB (OpenClaw-memory-short-term) → OpenSearch messages
  2. S3 Vectors (openclaw-memory-long-term) → OpenSearch extracted memories

Usage:
    python migrate.py [--dry-run]
"""

import argparse
import json
import logging
import math
import re
import sys
import time
from datetime import datetime, timezone

import boto3

from config import OPENSEARCH_ENDPOINT, DOC_TYPE_MESSAGE, DOC_TYPE_EXTRACTED
from embedding import EmbeddingClient
from opensearch_client import OpenSearchClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── Old Infrastructure Config ────────────────────
DYNAMO_TABLE = "OpenClaw-memory-short-term"
S3V_BUCKET = "openclaw-memory-long-term"
S3V_MEMORY_INDEX = "memory-index"
AWS_REGION = "us-west-2"


def epoch_to_iso(epoch_val) -> str:
    """Convert epoch seconds/milliseconds to ISO format."""
    try:
        ts = float(epoch_val)
        # If it looks like milliseconds (> year 2100 in seconds), convert
        if ts > 4000000000:
            ts = ts / 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return datetime.now(timezone.utc).isoformat()


class Migrator:
    """Migrates data from DynamoDB + S3 Vectors to OpenSearch."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.session = boto3.Session(region_name=AWS_REGION)
        self.os_client = OpenSearchClient()
        self.embed_client = EmbeddingClient()
        self.stats = {
            "dynamo_scanned": 0,
            "dynamo_migrated": 0,
            "s3v_scanned": 0,
            "s3v_migrated": 0,
            "errors": 0,
        }

    def migrate_all(self) -> dict:
        """Run full migration."""
        logger.info("Starting migration (dry_run=%s)...", self.dry_run)

        self.migrate_dynamodb()
        self.migrate_s3_vectors()

        logger.info("Migration complete: %s", self.stats)
        return self.stats

    # ─── DynamoDB Migration ───────────────────────────

    def migrate_dynamodb(self) -> None:
        """Migrate all items from DynamoDB short-term table."""
        logger.info("Migrating DynamoDB table: %s", DYNAMO_TABLE)

        dynamodb = self.session.resource("dynamodb")
        table = dynamodb.Table(DYNAMO_TABLE)

        # Scan all items
        items = []
        scan_kwargs: dict = {}
        while True:
            resp = table.scan(**scan_kwargs)
            items.extend(resp.get("Items", []))
            self.stats["dynamo_scanned"] += len(resp.get("Items", []))

            if "LastEvaluatedKey" not in resp:
                break
            scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

        logger.info("Found %d DynamoDB items", len(items))

        # Convert and bulk index
        documents = []
        for item in items:
            try:
                doc = self._convert_dynamo_item(item)
                if doc:
                    documents.append(doc)
            except Exception as e:
                logger.warning("Failed to convert DynamoDB item: %s", e)
                self.stats["errors"] += 1

        if documents and not self.dry_run:
            # Generate embeddings in batches
            batch_size = 10
            for i in range(0, len(documents), batch_size):
                batch = documents[i:i + batch_size]
                texts = [doc["body"]["content"] for doc in batch]
                embeddings = self.embed_client.embed_batch(texts)

                bulk_docs = []
                for doc, embedding in zip(batch, embeddings):
                    if embedding:
                        doc["body"]["embedding"] = embedding
                        doc["body"]["needs_embed"] = False
                    else:
                        doc["body"]["needs_embed"] = True
                    bulk_docs.append((doc["doc_id"], doc["body"]))

                try:
                    resp = self.os_client.bulk_index(bulk_docs)
                    # Count partial failures from bulk response
                    if resp.get("errors"):
                        failed = [
                            item for item in resp.get("items", [])
                            if item.get("index", {}).get("error")
                        ]
                        self.stats["errors"] += len(failed)
                        self.stats["dynamo_migrated"] += len(bulk_docs) - len(failed)
                    else:
                        self.stats["dynamo_migrated"] += len(bulk_docs)
                    logger.info(
                        "Migrated DynamoDB batch %d-%d (%d docs)",
                        i, i + len(batch), len(bulk_docs),
                    )
                except Exception as e:
                    logger.error("Bulk index failed: %s", e)
                    self.stats["errors"] += len(batch)

                # Rate limit
                time.sleep(0.5)
        elif self.dry_run:
            self.stats["dynamo_migrated"] = len(documents)
            logger.info("[DRY RUN] Would migrate %d DynamoDB items", len(documents))

    def _convert_dynamo_item(self, item: dict):
        """Convert a DynamoDB item to OpenSearch document format."""
        content = item.get("content", "")
        if not content:
            return None

        session_id = str(item.get("session_id", ""))
        agent_id = str(item.get("agent_id", "xiaoxiami"))
        timestamp = item.get("timestamp", 0)
        role = str(item.get("role", "user"))
        day = str(item.get("day", ""))
        ttl = item.get("ttl")

        doc_id = f"{agent_id}:{session_id}:{timestamp}"
        created_at = epoch_to_iso(timestamp)
        ttl_iso = epoch_to_iso(ttl) if ttl else None

        # Validate day field: must be yyyy-MM-dd format, otherwise derive from created_at
        if not day or not re.match(r'^\d{4}-\d{2}-\d{2}$', day):
            # Derive from created_at or leave empty
            day = created_at[:10] if created_at else ""

        body = {
            "doc_id": doc_id,
            "doc_type": DOC_TYPE_MESSAGE,
            "agent_id": agent_id,
            "content": content,
            "session_id": session_id,
            "role": role,
            "day": day,
            "created_at": created_at,
            "updated_at": created_at,
            "importance": 0.0,
            "recall_count": 0,
            "recall_queries": [],
            "promoted": False,
        }
        if ttl_iso:
            body["ttl"] = ttl_iso

        return {"doc_id": doc_id, "body": body}

    # ─── S3 Vectors Migration ─────────────────────────

    def migrate_s3_vectors(self) -> None:
        """Migrate vectors from S3 Vectors memory-index."""
        logger.info("Migrating S3 Vectors: %s/%s", S3V_BUCKET, S3V_MEMORY_INDEX)

        try:
            s3v = self.session.client("s3vectors")
        except Exception as e:
            logger.warning("S3 Vectors client not available: %s", e)
            return

        # List all vectors
        vectors = []
        try:
            pagination_token = None
            while True:
                kwargs = {
                    "vectorBucketName": S3V_BUCKET,
                    "indexName": S3V_MEMORY_INDEX,
                    "returnMetadata": True,
                    "returnData": True,
                }
                if pagination_token:
                    kwargs["paginationToken"] = pagination_token

                resp = s3v.list_vectors(**kwargs)
                vectors.extend(resp.get("vectors", []))
                self.stats["s3v_scanned"] += len(resp.get("vectors", []))

                pagination_token = resp.get("paginationToken")
                if not pagination_token:
                    break
        except Exception as e:
            logger.warning("Failed to list S3 Vectors: %s", e)
            return

        logger.info("Found %d S3 Vectors", len(vectors))

        # Convert and bulk index
        documents = []
        for vector in vectors:
            try:
                doc = self._convert_s3_vector(vector)
                if doc:
                    documents.append(doc)
            except Exception as e:
                logger.warning("Failed to convert S3 Vector: %s", e)
                self.stats["errors"] += 1

        if documents and not self.dry_run:
            batch_size = 10
            for i in range(0, len(documents), batch_size):
                batch = documents[i:i + batch_size]
                bulk_docs = [(doc["doc_id"], doc["body"]) for doc in batch]

                try:
                    resp = self.os_client.bulk_index(bulk_docs)
                    if resp.get("errors"):
                        failed = [
                            item for item in resp.get("items", [])
                            if item.get("index", {}).get("error")
                        ]
                        self.stats["errors"] += len(failed)
                        self.stats["s3v_migrated"] += len(bulk_docs) - len(failed)
                    else:
                        self.stats["s3v_migrated"] += len(bulk_docs)
                    logger.info(
                        "Migrated S3V batch %d-%d (%d docs)",
                        i, i + len(batch), len(bulk_docs),
                    )
                except Exception as e:
                    logger.error("Bulk index failed: %s", e)
                    self.stats["errors"] += len(batch)

                time.sleep(0.5)
        elif self.dry_run:
            self.stats["s3v_migrated"] = len(documents)
            logger.info("[DRY RUN] Would migrate %d S3 Vectors", len(documents))

    def _convert_s3_vector(self, vector: dict):
        """Convert an S3 Vector to OpenSearch document format."""
        key = vector.get("key", "")
        data = vector.get("data", {})
        metadata = vector.get("metadata", {})

        embedding = data.get("float32", [])
        source_text = metadata.get("source_text", "")

        if not source_text:
            return None

        agent_id = metadata.get("agent_id", "xiaoxiami")
        category = metadata.get("category", "")
        created_at = metadata.get("created_at", "")

        if created_at:
            created_at = epoch_to_iso(created_at)
        else:
            created_at = datetime.now(timezone.utc).isoformat()

        doc_id = f"migrated:{key}" if key else f"migrated:{hash(source_text)}"

        body = {
            "doc_id": doc_id,
            "doc_type": DOC_TYPE_EXTRACTED,
            "agent_id": agent_id,
            "content": source_text,
            "category": category,
            "created_at": created_at,
            "updated_at": created_at,
            "importance": 0.5,  # Existing extracted memories get base score
            "recall_count": 0,
            "recall_queries": [],
            "promoted": True,  # Already in long-term
            "promoted_at": created_at,
            "needs_embed": False,
        }

        if embedding and len(embedding) == 1024:
            body["embedding"] = embedding
        else:
            body["needs_embed"] = True

        return {"doc_id": doc_id, "body": body}


def main():
    parser = argparse.ArgumentParser(description="Migrate data to OpenSearch")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    if not OPENSEARCH_ENDPOINT and not args.dry_run:
        print("ERROR: OPENSEARCH_ENDPOINT not set. Use --dry-run or set the env var.")
        sys.exit(1)

    migrator = Migrator(dry_run=args.dry_run)
    stats = migrator.migrate_all()

    print("\n📊 Migration Summary:")
    print(f"  DynamoDB: {stats['dynamo_scanned']} scanned → {stats['dynamo_migrated']} migrated")
    print(f"  S3 Vectors: {stats['s3v_scanned']} scanned → {stats['s3v_migrated']} migrated")
    print(f"  Errors: {stats['errors']}")


if __name__ == "__main__":
    main()
