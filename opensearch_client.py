"""
OpenClaw Memory System v2 — OpenSearch Serverless Client
=========================================================
Wraps boto3 + opensearch-py with AWS SigV4 auth for
OpenSearch Serverless (AOSS).
"""

import json
import logging
import time
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError
from opensearchpy import OpenSearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth

import time

from config import (
    OPENSEARCH_ENDPOINT,
    OPENSEARCH_REGION,
    INDEX_NAME,
    SEARCH_PIPELINE_NAME,
)

logger = logging.getLogger(__name__)


class OpenSearchClient:
    """
    Thin wrapper around opensearch-py for OpenSearch Serverless.
    Handles SigV4 auth, index/pipeline creation, CRUD, bulk, and search.
    """

    def __init__(
        self,
        endpoint: str = OPENSEARCH_ENDPOINT,
        region: str = OPENSEARCH_REGION,
        index_name: str = INDEX_NAME,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.region = region
        self.index_name = index_name
        self._client: Optional[OpenSearch] = None
        self._client_created_at: float = 0

    @property
    def client(self) -> OpenSearch:
        """Lazy-init OpenSearch client with SigV4 auth. Refreshes every 50min."""
        if self._client is None or (time.time() - self._client_created_at) > 3000:
            self._client = self._create_client()
            self._client_created_at = time.time()
        return self._client

    def _create_client(self) -> OpenSearch:
        """Create an OpenSearch client authenticated via IAM (SigV4)."""
        credentials = boto3.Session().get_credentials()
        auth = AWS4Auth(
            credentials.access_key,
            credentials.secret_key,
            self.region,
            "aoss",  # Service name for OpenSearch Serverless
            session_token=credentials.token,
        )

        # Extract host from endpoint URL
        host = self.endpoint.replace("https://", "").replace("http://", "")

        client = OpenSearch(
            hosts=[{"host": host, "port": 443}],
            http_auth=auth,
            use_ssl=True,
            verify_certs=True,
            connection_class=RequestsHttpConnection,
            pool_maxsize=20,
            timeout=30,
        )
        logger.info("OpenSearch client created for %s", self.endpoint)
        return client

    # ─── Index Management ──────────────────────────────

    def create_index(self, mapping: dict[str, Any]) -> dict:
        """Create the memory index with the given mapping."""
        if self.index_exists():
            logger.info("Index '%s' already exists, skipping creation", self.index_name)
            return {"acknowledged": True, "already_exists": True}

        body = {
            "settings": {
                "index": {
                    "knn": True,
                    "knn.algo_param.ef_search": 100,
                }
            },
            "mappings": mapping,
        }
        resp = self.client.indices.create(index=self.index_name, body=body)
        logger.info("Created index '%s': %s", self.index_name, resp)
        return resp

    def index_exists(self) -> bool:
        """Check if the memory index exists."""
        try:
            return self.client.indices.exists(index=self.index_name)
        except Exception as e:
            logger.warning("Failed to check index existence: %s", e)
            return False

    def delete_index(self) -> dict:
        """Delete the memory index (use with caution)."""
        resp = self.client.indices.delete(index=self.index_name)
        logger.warning("Deleted index '%s': %s", self.index_name, resp)
        return resp

    # ─── Search Pipeline ──────────────────────────────

    def create_search_pipeline(self, pipeline_name: str = SEARCH_PIPELINE_NAME) -> dict:
        """Create the hybrid search pipeline with normalization + weighted combination."""
        pipeline_body = {
            "description": "Memory hybrid search pipeline",
            "phase_results_processors": [
                {
                    "normalization-processor": {
                        "normalization": {"technique": "min_max"},
                        "combination": {
                            "technique": "arithmetic_mean",
                            "parameters": {"weights": [0.3, 0.7]},
                        },
                    }
                }
            ],
        }
        # OpenSearch Serverless uses the _search_pipeline API
        resp = self.client.transport.perform_request(
            "PUT",
            f"/_search/pipeline/{pipeline_name}",
            body=pipeline_body,
        )
        logger.info("Created search pipeline '%s': %s", pipeline_name, resp)
        return resp

    # ─── Document CRUD ────────────────────────────────

    def index_document(self, doc_id: str, body: dict[str, Any]) -> dict:
        """Index a single document. AOSS auto-generates _id; doc_id is kept in body."""
        if "doc_id" not in body:
            body["doc_id"] = doc_id
        resp = self.client.index(
            index=self.index_name,
            body=body,
        )
        return resp

    def _find_internal_id(self, doc_id: str) -> Optional[str]:
        """Find the AOSS auto-generated _id for a given business doc_id."""
        resp = self.client.search(
            index=self.index_name,
            body={
                "size": 1,
                "query": {"term": {"doc_id": doc_id}},
                "_source": False,
            },
        )
        hits = resp.get("hits", {}).get("hits", [])
        return hits[0]["_id"] if hits else None

    def get_document(self, doc_id: str) -> Optional[dict[str, Any]]:
        """Get a single document by business doc_id. Returns None if not found."""
        resp = self.client.search(
            index=self.index_name,
            body={
                "size": 1,
                "query": {"term": {"doc_id": doc_id}},
            },
        )
        hits = resp.get("hits", {}).get("hits", [])
        if not hits:
            return None
        return hits[0]["_source"]

    def update_document(self, doc_id: str, partial_doc: dict[str, Any]) -> dict:
        """Update a document by business doc_id.
        AOSS doesn't support partial update or reliable delete,
        so we fetch the full doc, merge, and re-index.
        The old copy will have the same doc_id but older updated_at."""
        # Find the existing doc with retry (AOSS eventual consistency)
        hits = []
        for attempt in range(3):
            resp = self.client.search(
                index=self.index_name,
                body={"size": 1, "query": {"term": {"doc_id": doc_id}}},
            )
            hits = resp.get("hits", {}).get("hits", [])
            if hits:
                break
            logger.info("update_document: doc_id=%s not found yet, retry %d/3", doc_id, attempt + 1)
            time.sleep(2)

        if not hits:
            raise Exception(f"Document not found: {doc_id}")

        existing = hits[0]["_source"]

        # Merge partial_doc into existing
        existing.update(partial_doc)

        # Re-index merged doc (creates a new copy; old copy stays but is superseded)
        new_resp = self.client.index(index=self.index_name, body=existing)
        logger.info("Updated doc %s (reindex)", doc_id)
        return new_resp

    def delete_document(self, doc_id: str) -> dict:
        """Delete a single document by business doc_id.
        On AOSS, individual doc delete is unreliable due to ID encoding issues.
        Instead, we re-index with importance=-1 so the searcher filters it out."""
        resp = self.client.search(
            index=self.index_name,
            body={"size": 1, "query": {"term": {"doc_id": doc_id}}},
        )
        hits = resp.get("hits", {}).get("hits", [])
        if not hits:
            logger.warning("delete_document: doc_id=%s not found", doc_id)
            return {"deleted": 0}

        existing = hits[0]["_source"]
        existing["importance"] = -1.0
        self.client.index(index=self.index_name, body=existing)
        logger.info("Soft-deleted doc %s (set importance=-1)", doc_id)
        return {"deleted": 1}

    # ─── Bulk Operations ──────────────────────────────

    def bulk_index(self, documents: list[tuple[str, dict[str, Any]]]) -> dict:
        """
        Bulk index documents. AOSS auto-generates _id; doc_id is kept in body.
        
        Args:
            documents: List of (doc_id, body) tuples.
        
        Returns:
            OpenSearch bulk response.
        """
        if not documents:
            return {"items": [], "errors": False}

        bulk_body = []
        for doc_id, body in documents:
            if "doc_id" not in body:
                body["doc_id"] = doc_id
            bulk_body.append({"index": {"_index": self.index_name}})
            bulk_body.append(body)

        resp = self.client.bulk(body=bulk_body)
        if resp.get("errors"):
            failed = [
                item for item in resp["items"]
                if item.get("index", {}).get("error")
            ]
            logger.error("Bulk index had %d failures: %s", len(failed), failed[:3])
        else:
            logger.debug("Bulk indexed %d documents", len(documents))
        return resp

    def bulk_delete(self, doc_ids: list[str]) -> dict:
        """Bulk delete documents by business doc_ids."""
        if not doc_ids:
            return {"items": [], "errors": False}

        resp = self.delete_by_query(
            {"terms": {"doc_id": doc_ids}}
        )
        logger.debug("Bulk deleted %d documents", len(doc_ids))
        return resp

    # ─── Search ───────────────────────────────────────

    def hybrid_search(
        self,
        query_text: str,
        query_vector: list[float],
        k: int = 20,
        filters: Optional[dict] = None,
        pipeline_name: str = SEARCH_PIPELINE_NAME,
    ) -> list[dict[str, Any]]:
        """
        Execute a hybrid search (BM25 + kNN) using the search pipeline.
        
        Args:
            query_text: Text for BM25 matching.
            query_vector: Embedding vector for kNN matching.
            k: Number of results to return.
            filters: Optional OpenSearch filter clause.
            pipeline_name: Search pipeline to use.
        
        Returns:
            List of hit dicts with _score and _source (doc_id is in _source).
        """
        # Build the hybrid query
        bm25_query: dict[str, Any] = {
            "match": {
                "content": {
                    "query": query_text,
                }
            }
        }

        knn_query: dict[str, Any] = {
            "knn": {
                "embedding": {
                    "vector": query_vector,
                    "k": k,
                }
            }
        }

        # Add filters if provided
        if filters:
            bm25_query = {
                "bool": {
                    "must": [bm25_query],
                    "filter": filters,
                }
            }
            knn_query["knn"]["embedding"]["filter"] = {"bool": {"filter": filters}}

        search_body = {
            "size": k,
            "query": {
                "hybrid": {
                    "queries": [bm25_query, knn_query]
                }
            },
        }

        resp = self.client.search(
            index=self.index_name,
            body=search_body,
            params={"search_pipeline": pipeline_name},
        )

        hits = resp.get("hits", {}).get("hits", [])
        return hits

    def keyword_search(
        self,
        query_text: str,
        k: int = 20,
        filters: Optional[dict] = None,
    ) -> list[dict[str, Any]]:
        """
        Fallback: BM25-only search (when embedding is unavailable).
        """
        must_clause: dict[str, Any] = {
            "match": {
                "content": {
                    "query": query_text,
                }
            }
        }

        body: dict[str, Any] = {"size": k, "query": must_clause}
        if filters:
            body["query"] = {
                "bool": {
                    "must": [must_clause],
                    "filter": filters,
                }
            }

        resp = self.client.search(index=self.index_name, body=body)
        return resp.get("hits", {}).get("hits", [])

    def delete_by_query(self, query: dict[str, Any]) -> dict:
        """Soft-delete documents matching a query by setting importance=-1.
        AOSS doesn't support _delete_by_query or individual DELETE reliably."""
        deleted = 0
        resp = self.client.search(
            index=self.index_name,
            body={"size": 100, "query": query},
        )
        hits = resp.get("hits", {}).get("hits", [])
        for hit in hits:
            try:
                existing = hit["_source"]
                existing["importance"] = -1.0
                self.client.index(index=self.index_name, body=existing)
                deleted += 1
            except Exception as e:
                logger.warning("Soft-delete failed for %s: %s", hit.get("_id", "?"), e)
        logger.info("Soft-deleted by query: %d docs", deleted)
        return {"deleted": deleted}

    def count(self, query: Optional[dict] = None) -> int:
        """Count documents, optionally filtered."""
        body = {"query": query} if query else {"query": {"match_all": {}}}
        resp = self.client.count(index=self.index_name, body=body)
        return resp.get("count", 0)

    # ─── Health Check ─────────────────────────────────

    def ping(self) -> bool:
        """Check if OpenSearch is reachable.
        AOSS doesn't support HEAD /, so we try cat.indices instead."""
        try:
            self.client.cat.indices(format="json")
            return True
        except Exception:
            return False
