"""
OpenClaw Memory System v2 — Dreaming REM Phase
================================================
Clusters recent extracted memories, generates thematic summaries,
and identifies reinforcement patterns.

Input: extracted docs from the past 7 days (phase="light")
Output: REM signals (consolidation scores, theme notes)
"""

import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from config import (
    EXTRACT_MODEL_ID,
    BEDROCK_REGION,
    DOC_TYPE_EXTRACTED,
)
from embedding import EmbeddingClient
from opensearch_client import OpenSearchClient

logger = logging.getLogger(__name__)

# Prompt for thematic summary
THEME_PROMPT = """The following are memory fragments extracted over the past week, all related to a common theme.

{memories}

Please:
1. Summarize the core theme in 1-2 sentences.
2. Identify any recurring patterns or trends.
3. Note any contradictions or evolving views.
4. Rate the theme's importance for long-term memory (0-1).

Output as JSON:
{{
  "theme_summary": "...",
  "patterns": ["..."],
  "contradictions": ["..."],
  "importance": 0.0-1.0
}}"""


class REMDreaming:
    """
    REM Phase — cluster memories and identify themes.
    """

    def __init__(
        self,
        os_client: OpenSearchClient,
        embed_client: EmbeddingClient,
    ):
        self.os_client = os_client
        self.embed_client = embed_client
        self._bedrock = None

    @property
    def bedrock(self):
        if self._bedrock is None:
            import boto3
            self._bedrock = boto3.client(
                "bedrock-runtime",
                region_name=BEDROCK_REGION,
            )
        return self._bedrock

    def run(
        self,
        agent_id: str = "",
        lookback_days: int = 7,
        num_clusters: int = 5,
    ) -> dict[str, Any]:
        """
        Execute REM Phase dreaming.

        Args:
            agent_id: Filter by agent (empty = all).
            lookback_days: How many days of extracted memories to process.
            num_clusters: Target number of topic clusters.

        Returns:
            Report dict with cluster info, themes, and consolidation signals.
        """
        logger.info("REM Dreaming starting (agent=%s, lookback=%dd)", agent_id, lookback_days)

        report = {
            "phase": "rem",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "memories_processed": 0,
            "clusters_found": 0,
            "themes": [],
            "consolidation_updates": 0,
        }

        # 1. Fetch recent extracted memories
        memories = self._fetch_extracted_memories(agent_id, lookback_days)
        if not memories:
            logger.info("No extracted memories found for REM phase")
            report["completed_at"] = datetime.now(timezone.utc).isoformat()
            return report

        report["memories_processed"] = len(memories)

        # 2. Cluster by embedding similarity
        clusters = self._cluster_memories(memories, num_clusters)
        report["clusters_found"] = len(clusters)

        # 3. Process each cluster
        for i, cluster_ids in enumerate(clusters):
            cluster_memories = [m for m in memories if m.get("_id") in cluster_ids]
            if len(cluster_memories) < 2:
                continue

            logger.info("Processing cluster %d (%d memories)", i, len(cluster_memories))

            # Generate theme summary
            theme = self._generate_theme_summary(cluster_memories)
            if theme:
                report["themes"].append(theme)

                # Update consolidation scores
                for mem in cluster_memories:
                    self._update_consolidation(mem, len(cluster_ids), theme)
                    report["consolidation_updates"] += 1

        report["completed_at"] = datetime.now(timezone.utc).isoformat()
        logger.info(
            "REM Dreaming complete: %d clusters, %d themes, %d consolidation updates",
            report["clusters_found"],
            len(report["themes"]),
            report["consolidation_updates"],
        )
        return report

    # ─── Internal Methods ─────────────────────────────

    def _fetch_extracted_memories(
        self, agent_id: str, lookback_days: int
    ) -> list[dict]:
        """Fetch extracted memories from OpenSearch."""
        filters = [
            {"term": {"doc_type": DOC_TYPE_EXTRACTED}},
            {"term": {"phase": "light"}},
            {"range": {"created_at": {"gte": f"now-{lookback_days}d"}}},
        ]
        if agent_id:
            filters.append({"term": {"agent_id": agent_id}})

        try:
            # Use match_all with filters (keyword_search with "*" doesn't work on AOSS)
            body = {
                "size": 200,
                "query": {
                    "bool": {
                        "filter": filters
                    }
                },
                "sort": [{"created_at": {"order": "desc"}}],
            }
            resp = self.os_client.client.search(
                index=self.os_client.index_name,
                body=body,
            )
            hits = resp.get("hits", {}).get("hits", [])
            results = []
            for hit in hits:
                source = hit.get("_source", {})
                source["_id"] = source.get("doc_id", hit.get("_id", ""))
                results.append(source)
            return results
        except Exception as e:
            logger.error("Failed to fetch extracted memories: %s", e)
            return []

    def _cluster_memories(
        self,
        memories: list[dict],
        num_clusters: int,
    ) -> list[list[str]]:
        """
        Cluster memories by embedding similarity using simple K-means.
        Falls back to category-based grouping if embeddings are missing.
        """
        # Collect embeddings
        valid_memories = []
        vectors = []
        for mem in memories:
            emb = mem.get("embedding")
            if emb and len(emb) > 0:
                valid_memories.append(mem)
                vectors.append(emb)

        if len(valid_memories) < 3:
            # Not enough for clustering, group by category instead
            return self._group_by_category(memories)

        try:
            import numpy as np
            from sklearn.cluster import KMeans

            X = np.array(vectors)
            n_clusters = min(num_clusters, len(valid_memories))
            kmeans = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
            labels = kmeans.fit_predict(X)

            clusters: dict[int, list[str]] = defaultdict(list)
            for mem, label in zip(valid_memories, labels):
                clusters[label].append(mem.get("_id", ""))

            return list(clusters.values())
        except ImportError:
            logger.warning("scikit-learn not available, falling back to category grouping")
            return self._group_by_category(memories)
        except Exception as e:
            logger.error("Clustering failed: %s", e)
            return self._group_by_category(memories)

    def _group_by_category(self, memories: list[dict]) -> list[list[str]]:
        """Fallback: group memories by category."""
        groups: dict[str, list[str]] = defaultdict(list)
        for mem in memories:
            cat = mem.get("category", "Unknown")
            groups[cat].append(mem.get("_id", ""))
        return list(groups.values())

    def _generate_theme_summary(self, cluster_memories: list[dict]) -> Optional[dict]:
        """Use LLM to generate a thematic summary for a cluster."""
        # Format memories for the prompt
        memory_texts = []
        for mem in cluster_memories[:20]:  # Limit to 20 per cluster
            cat = mem.get("category", "")
            content = mem.get("content", "")
            memory_texts.append(f"[{cat}] {content}")

        prompt = THEME_PROMPT.format(memories="\n".join(memory_texts))

        try:
            body = json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1000,
                "messages": [
                    {"role": "user", "content": prompt}
                ],
            })

            resp = self.bedrock.invoke_model(
                modelId=EXTRACT_MODEL_ID,
                body=body,
                contentType="application/json",
                accept="application/json",
            )
            result = json.loads(resp["body"].read())
            text = result.get("content", [{}])[0].get("text", "")

            # Parse JSON from response
            # Try to extract JSON from the response text
            json_start = text.find("{")
            json_end = text.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                theme = json.loads(text[json_start:json_end])
                theme["memory_count"] = len(cluster_memories)
                theme["memory_ids"] = [m.get("_id", "") for m in cluster_memories]
                return theme

            logger.warning("Could not parse theme JSON from LLM response")
            return None

        except Exception as e:
            logger.error("Theme generation failed: %s", e)
            return None

    def _update_consolidation(
        self,
        memory: dict,
        cluster_size: int,
        theme: dict,
    ) -> None:
        """Update a memory's consolidation score based on REM findings."""
        doc_id = memory.get("_id", "")
        if not doc_id:
            return

        try:
            # Memories in larger clusters get higher consolidation scores
            consolidation_boost = min(cluster_size / 10.0, 1.0)
            theme_importance = theme.get("importance", 0.5)

            self.os_client.update_document(doc_id, {
                "rem_consolidation": consolidation_boost,
                "rem_theme_importance": theme_importance,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as e:
            logger.debug("Failed to update consolidation for %s: %s", doc_id, e)
