"""
OpenClaw Memory System v2 — Searcher
=====================================
Implements aws_memory_search with:
  - Hybrid query (BM25 + kNN via OpenSearch search pipeline)
  - Temporal decay (half-life 90 days, MEMORY.md evergreen)
  - MMR (Maximal Marginal Relevance) de-duplication
  - Pending queue merge (search consistency)
  - Recall signal recording
  - Cross-agent query support
  - Three-tier alerting
"""

import logging
import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

from config import (
    BM25_WEIGHT,
    KNN_WEIGHT,
    TEMPORAL_DECAY_HALF_LIFE_DAYS,
    MMR_LAMBDA,
    DEFAULT_TOP_K,
    SEARCH_OVERSAMPLE_FACTOR,
    EXCEPTION_AGENT_LIST,
    DEFAULT_API_VERSION,
    DOC_TYPE_FILE_CHUNK,
)
from embedding import EmbeddingClient
from opensearch_client import OpenSearchClient

logger = logging.getLogger(__name__)

# Temporal decay constant: λ = ln(2) / half_life_seconds
_HALF_LIFE_SECS = TEMPORAL_DECAY_HALF_LIFE_DAYS * 86400
_DECAY_LAMBDA = math.log(2) / _HALF_LIFE_SECS


class Searcher:
    """
    Hybrid memory search with temporal decay, MMR, and pending queue merge.
    """

    def __init__(
        self,
        os_client: OpenSearchClient,
        embed_client: EmbeddingClient,
        ingester=None,  # Optional reference to Ingester for pending queue
    ):
        self.os_client = os_client
        self.embed_client = embed_client
        self.ingester = ingester

    def search(
        self,
        query: str,
        agent_id: Optional[str] = None,
        session_id: Optional[str] = None,
        top_k: int = DEFAULT_TOP_K,
        doc_types: Optional[list[str]] = None,
        days_back: Optional[int] = None,
        min_score: float = 0.0,
        api_version: str = DEFAULT_API_VERSION,
    ) -> dict[str, Any]:
        """
        Execute hybrid search over all memory types.

        Args:
            query: Natural language search query.
            agent_id: Filter by agent. Empty/None = all agents.
                      Agents in EXCEPTION_AGENT_LIST also search all.
            session_id: Limit to specific session.
            top_k: Number of results to return.
            doc_types: Filter by doc_type list.
            days_back: Only search within last N days.
            min_score: Minimum relevance score threshold (0-1).
            api_version: API version string.

        Returns:
            {
                "results": [...],
                "query": "...",
                "total": N,
                "alerts": [...]
            }
        """
        now = time.time()

        # 1. Generate query embedding
        query_vector = self.embed_client.embed_text(query)
        use_hybrid = query_vector is not None

        # 2. Build filters
        filters = self._build_filters(agent_id, session_id, doc_types, days_back)

        # 3. Query OpenSearch
        oversample_k = top_k * SEARCH_OVERSAMPLE_FACTOR
        try:
            if use_hybrid:
                os_hits = self.os_client.hybrid_search(
                    query_text=query,
                    query_vector=query_vector,
                    k=oversample_k,
                    filters=filters,
                )
            else:
                # Degraded: BM25 only
                os_hits = self.os_client.keyword_search(
                    query_text=query,
                    k=oversample_k,
                    filters=filters,
                )
        except Exception as e:
            logger.error("OpenSearch search failed: %s", e)
            os_hits = []

        # 3.5 Deduplicate OS hits by doc_id — keep the LATEST copy (by updated_at)
        # AOSS creates new copies on re-index; old copies may have stale importance
        doc_id_map = {}
        for hit in os_hits:
            did = hit.get("_source", {}).get("doc_id", "")
            if not did:
                continue
            existing = doc_id_map.get(did)
            if existing is None:
                doc_id_map[did] = hit
            else:
                # Keep the one with the later updated_at
                new_ts = hit.get("_source", {}).get("updated_at", "")
                old_ts = existing.get("_source", {}).get("updated_at", "")
                if new_ts > old_ts:
                    doc_id_map[did] = hit
        os_hits = list(doc_id_map.values())

        # 3.6 Filter out soft-deleted docs (importance < 0) AFTER dedup
        os_hits = [
            h for h in os_hits
            if (h.get("_source", {}).get("importance") or 0) >= 0
        ]

        # 3.7 Filter out in-memory forgotten doc_ids (bridges AOSS eventual consistency)
        if self.ingester:
            forgotten_ids = self.ingester.get_forgotten_ids()
            if forgotten_ids:
                os_hits = [
                    h for h in os_hits
                    if h.get("_source", {}).get("doc_id", "") not in forgotten_ids
                ]

        # 4. Merge with pending queue
        pending_hits = self._search_pending_queue(query, agent_id, session_id, doc_types)

        # 4.5 Filter forgotten IDs from pending hits too
        if self.ingester:
            forgotten_ids = self.ingester.get_forgotten_ids()
            if forgotten_ids:
                pending_hits = [
                    h for h in pending_hits
                    if h.get("_source", {}).get("doc_id", "") not in forgotten_ids
                ]

        # 5. Combine and deduplicate
        all_hits = self._merge_hits(os_hits, pending_hits)

        # 6. Apply temporal decay
        for hit in all_hits:
            hit["decayed_score"] = self._apply_temporal_decay(hit, now)

        # 7. Sort by decayed score
        all_hits.sort(key=lambda h: h["decayed_score"], reverse=True)

        # 8. Apply MMR if we have vectors
        if use_hybrid and query_vector:
            selected = self._apply_mmr(all_hits, query_vector, top_k)
        else:
            selected = all_hits[:top_k]

        # 9. Filter by min_score
        if min_score > 0:
            selected = [h for h in selected if h["decayed_score"] >= min_score]

        # 10. Record recall signals — DISABLED on AOSS due to re-indexing duplication
        # self._record_recall_signals(selected, query)

        # 11. Format results
        results = self._format_results(selected)

        # 12. Collect alerts
        alerts = []
        if self.ingester:
            alerts = self.ingester._check_alerts()
        if not use_hybrid:
            alerts.append(
                "⚠️ 向量搜索不可用，已降级为关键字搜索。结果质量可能下降。"
            )

        return {
            "results": results,
            "query": query,
            "total": len(results),
            "alerts": alerts,
        }

    # ─── Filter Building ─────────────────────────────

    def _build_filters(
        self,
        agent_id: Optional[str],
        session_id: Optional[str],
        doc_types: Optional[list[str]],
        days_back: Optional[int],
    ) -> Optional[dict]:
        """Build OpenSearch filter clauses."""
        filter_clauses = []

        # Agent filter (skip for exception agents or empty agent_id)
        if agent_id and agent_id not in EXCEPTION_AGENT_LIST:
            filter_clauses.append({"term": {"agent_id": agent_id}})

        # Session filter
        if session_id:
            filter_clauses.append({"term": {"session_id": session_id}})

        # Doc type filter
        if doc_types:
            filter_clauses.append({"terms": {"doc_type": doc_types}})

        # Time filter
        if days_back:
            filter_clauses.append({
                "range": {
                    "created_at": {
                        "gte": f"now-{days_back}d",
                    }
                }
            })

        # Note: importance filter is applied in Python AFTER doc_id dedup,
        # because AOSS creates duplicate copies on re-index (can't delete old docs).
        # The old copy may have importance=1.0 while the new one has importance=-1.0.
        # If we filter at OS level, we'd see the old (stale) positive copy.

        if not filter_clauses:
            return None

        return filter_clauses if len(filter_clauses) > 1 else filter_clauses

    # ─── Pending Queue Search ─────────────────────────

    def _search_pending_queue(
        self,
        query: str,
        agent_id: Optional[str],
        session_id: Optional[str],
        doc_types: Optional[list[str]],
    ) -> list[dict[str, Any]]:
        """
        Simple text matching against pending (not yet flushed) items.
        Ensures recently written messages are searchable.
        """
        if not self.ingester:
            return []

        pending_items = self.ingester.get_pending_items()
        if not pending_items:
            return []

        query_lower = query.lower()
        # For CJK text: split by whitespace AND by individual characters
        # This ensures Chinese queries like "火锅" match content "我喜欢吃火锅"
        query_terms = set(query_lower.split())
        # Add individual CJK characters/substrings for character-level matching
        # For short queries (< 10 chars with no spaces), also try the whole query as substring
        has_cjk = any('\u4e00' <= c <= '\u9fff' for c in query_lower)
        if has_cjk:
            # Split CJK text into bigrams for better matching
            cjk_chars = [c for c in query_lower if '\u4e00' <= c <= '\u9fff']
            for i in range(len(cjk_chars)):
                # Single chars
                query_terms.add(cjk_chars[i])
                # Bigrams
                if i + 1 < len(cjk_chars):
                    query_terms.add(cjk_chars[i] + cjk_chars[i + 1])
        results = []

        for body in pending_items:
            # Apply filters
            if agent_id and agent_id not in EXCEPTION_AGENT_LIST:
                if body.get("agent_id") != agent_id:
                    continue
            if session_id and body.get("session_id") != session_id:
                continue
            if doc_types and body.get("doc_type") not in doc_types:
                continue

            # Simple text matching: any query term appears in content
            content_lower = body.get("content", "").lower()
            match_count = sum(1 for term in query_terms if term in content_lower)
            if match_count == 0:
                continue

            # Approximate score based on term overlap
            score = match_count / max(len(query_terms), 1) * 0.5  # Cap at 0.5

            results.append({
                "_id": body.get("doc_id", ""),
                "_score": score,
                "_source": body,
                "_from_pending": True,
            })

        return results

    # ─── Merge & Deduplicate ──────────────────────────

    def _merge_hits(
        self,
        os_hits: list[dict],
        pending_hits: list[dict],
    ) -> list[dict]:
        """Merge OpenSearch hits with pending queue hits, deduplicating by doc_id."""
        seen_ids = set()
        merged = []

        # OpenSearch hits take priority (they have real scores)
        for hit in os_hits:
            doc_id = hit.get("_source", {}).get("doc_id", "")
            if doc_id not in seen_ids:
                seen_ids.add(doc_id)
                merged.append(hit)

        # Add pending hits that aren't already in OS results
        for hit in pending_hits:
            doc_id = hit.get("_source", {}).get("doc_id", "")
            if doc_id not in seen_ids:
                seen_ids.add(doc_id)
                merged.append(hit)

        return merged

    # ─── Temporal Decay ───────────────────────────────

    def _apply_temporal_decay(self, hit: dict, now: float) -> float:
        """
        Apply exponential temporal decay to a hit's score.
        MEMORY.md file_chunks are evergreen (no decay).
        
        score *= exp(-λ * age_seconds)
        where λ = ln(2) / (90 days in seconds)
        """
        source = hit.get("_source", {})
        base_score = hit.get("_score", 0.0) or 0.0

        # Evergreen: MEMORY.md chunks don't decay
        source_file = source.get("source_file", "")
        if source_file == "MEMORY.md" and source.get("doc_type") == DOC_TYPE_FILE_CHUNK:
            return base_score

        # Parse created_at
        created_at_str = source.get("created_at", "")
        if not created_at_str:
            return base_score

        try:
            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            age_secs = now - created_at.timestamp()
            if age_secs < 0:
                age_secs = 0
            decay = math.exp(-_DECAY_LAMBDA * age_secs)
            return base_score * decay
        except (ValueError, TypeError):
            return base_score

    # ─── MMR (Maximal Marginal Relevance) ─────────────

    def _apply_mmr(
        self,
        hits: list[dict],
        query_vector: list[float],
        top_k: int,
    ) -> list[dict]:
        """
        Select top_k results using MMR to balance relevance and diversity.
        
        mmr_score = λ * relevance - (1-λ) * max_similarity_to_selected
        λ = 0.7 (biased toward relevance)
        """
        if len(hits) <= top_k:
            return hits

        # Pre-compute: get embeddings from hits
        hit_vectors = []
        for hit in hits:
            emb = hit.get("_source", {}).get("embedding")
            hit_vectors.append(emb)

        selected = []
        remaining = list(range(len(hits)))

        for _ in range(top_k):
            best_idx = -1
            best_mmr = -float("inf")

            for i in remaining:
                relevance = hits[i].get("decayed_score", 0.0)

                # Max similarity to already selected
                max_sim = 0.0
                if selected and hit_vectors[i] is not None:
                    for j in selected:
                        if hit_vectors[j] is not None:
                            sim = self.embed_client.cosine_similarity(
                                hit_vectors[i], hit_vectors[j]
                            )
                            max_sim = max(max_sim, sim)

                mmr = MMR_LAMBDA * relevance - (1 - MMR_LAMBDA) * max_sim
                if mmr > best_mmr:
                    best_mmr = mmr
                    best_idx = i

            if best_idx >= 0:
                selected.append(best_idx)
                remaining.remove(best_idx)

        return [hits[i] for i in selected]

    # ─── Recall Signal Recording ──────────────────────

    def _record_recall_signals(
        self,
        hits: list[dict],
        query: str,
    ) -> None:
        """
        Update recall_count and recall_queries for returned results.
        This data feeds into Dreaming Deep scoring.
        Non-blocking: errors are logged but don't affect search results.
        """
        for hit in hits:
            doc_id = hit.get("_source", {}).get("doc_id", "")
            if not doc_id or hit.get("_from_pending"):
                continue  # Skip pending items (not in OS yet)

            try:
                source = hit.get("_source", {})
                recall_count = (source.get("recall_count") or 0) + 1
                recall_queries = list(set(
                    (source.get("recall_queries") or []) + [query]
                ))
                # Keep recall_queries bounded
                if len(recall_queries) > 50:
                    recall_queries = recall_queries[-50:]

                self.os_client.update_document(doc_id, {
                    "recall_count": recall_count,
                    "recall_queries": recall_queries,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as e:
                logger.debug("Failed to update recall signal for %s: %s", doc_id, e)

    # ─── Result Formatting ────────────────────────────

    def _format_results(self, hits: list[dict]) -> list[dict[str, Any]]:
        """Format search hits into clean result dicts."""
        results = []
        for hit in hits:
            source = hit.get("_source", {})
            
            # Build source reference
            source_ref = ""
            if source.get("source_file"):
                source_ref = source["source_file"]
                if source.get("source_lines"):
                    source_ref += f"#{source['source_lines']}"
            elif source.get("session_id"):
                source_ref = f"session:{source['session_id']}"

            results.append({
                "text": source.get("content", ""),
                "source": source_ref,
                "score": round(hit.get("decayed_score", hit.get("_score", 0.0)), 4),
                "category": source.get("category", ""),
                "doc_type": source.get("doc_type", ""),
                "doc_id": source.get("doc_id", ""),
                "agent_id": source.get("agent_id", ""),
                "created_at": source.get("created_at", ""),
                "role": source.get("role", ""),
            })

        return results
