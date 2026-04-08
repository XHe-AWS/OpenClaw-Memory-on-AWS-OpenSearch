"""
OpenClaw Memory System v2 — Dreaming Deep Phase
=================================================
Scores candidate memories using 7 dimensions and promotes
those that pass the threshold to long-term memory (MEMORY.md).

7 Dimensions:
  1. frequency (0.20)     — how often recalled
  2. relevance (0.20)     — average similarity score when recalled
  3. query_diversity (0.15) — variety of queries that recalled it
  4. recency (0.15)       — how recent
  5. consolidation (0.10) — cross-day appearance
  6. conceptual_richness (0.05) — entity/concept density
  7. content_quality (0.15) — LLM assessment of intrinsic value

The content_quality dimension solves the cold-start problem:
new memories with low recall but high intrinsic value can still be promoted.
"""

import json
import logging
import math
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from config import (
    EXTRACT_MODEL_ID,
    BEDROCK_REGION,
    DOC_TYPE_EXTRACTED,
    DEEP_WEIGHTS,
    DEEP_MIN_SCORE,
    DEEP_MIN_RECALL_COUNT,
    DEEP_MIN_UNIQUE_QUERIES,
    DEEP_MAX_AGE_DAYS,
    QUERY_DIVERSITY_CAP,
    WORKSPACE_ROOT,
)
from embedding import EmbeddingClient
from opensearch_client import OpenSearchClient

logger = logging.getLogger(__name__)

# Recency decay: λ = ln(2) / 14 days (for scoring only, not search decay)
_RECENCY_LAMBDA = math.log(2) / 14

# Prompt for content quality scoring
QUALITY_PROMPT = """Rate this memory's long-term value on a scale of 0.0 to 1.0:

"{content}"

Consider:
- Is it a specific preference, decision, or fact? (higher)
- Does it have long-term reference value? (higher)
- Is it concrete and actionable? (higher)
- Is it vague or generic? (lower)
- Is it transient/temporary information? (lower)

Respond with ONLY a number between 0.0 and 1.0, nothing else."""


class DeepDreaming:
    """
    Deep Phase — score and promote candidate memories.
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

    def run(self, agent_id: str = "") -> dict[str, Any]:
        """
        Execute Deep Phase dreaming.

        Returns:
            Report with scoring distribution and promotion results.
        """
        logger.info("Deep Dreaming starting (agent=%s)", agent_id)

        report = {
            "phase": "deep",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "candidates_scored": 0,
            "promoted": 0,
            "skipped_threshold": 0,
            "skipped_age": 0,
            "score_distribution": {},
            "promoted_memories": [],
        }

        # 1. Fetch unpromoted extracted memories
        candidates = self._fetch_candidates(agent_id)
        if not candidates:
            logger.info("No candidates for Deep phase")
            report["completed_at"] = datetime.now(timezone.utc).isoformat()
            return report

        # 2. Compute global stats for normalization
        max_recall = max(
            (c.get("recall_count", 0) for c in candidates), default=1
        ) or 1

        # 3. Score each candidate
        scores = []
        for candidate in candidates:
            report["candidates_scored"] += 1
            age_days = self._age_days(candidate)

            # Skip if too old
            if age_days > DEEP_MAX_AGE_DAYS:
                report["skipped_age"] += 1
                continue

            score_breakdown = self._compute_score(candidate, max_recall)
            total_score = sum(
                DEEP_WEIGHTS[dim] * score_breakdown[dim]
                for dim in DEEP_WEIGHTS
            )

            scores.append({
                "doc_id": candidate.get("_id", ""),
                "content": candidate.get("content", ""),
                "category": candidate.get("category", ""),
                "total_score": total_score,
                "breakdown": score_breakdown,
                "recall_count": candidate.get("recall_count", 0),
                "unique_queries": len(candidate.get("recall_queries", [])),
            })

        # 4. Score distribution
        bins = {"0.0-0.2": 0, "0.2-0.4": 0, "0.4-0.6": 0, "0.6-0.8": 0, "0.8-1.0": 0}
        for s in scores:
            ts = s["total_score"]
            if ts < 0.2:
                bins["0.0-0.2"] += 1
            elif ts < 0.4:
                bins["0.2-0.4"] += 1
            elif ts < 0.6:
                bins["0.4-0.6"] += 1
            elif ts < 0.8:
                bins["0.6-0.8"] += 1
            else:
                bins["0.8-1.0"] += 1
        report["score_distribution"] = bins

        # 5. Promote candidates that pass thresholds
        for s in scores:
            if (
                s["total_score"] >= DEEP_MIN_SCORE
                and s["recall_count"] >= DEEP_MIN_RECALL_COUNT
                and s["unique_queries"] >= DEEP_MIN_UNIQUE_QUERIES
            ):
                # Verify content is still relevant
                if self._verify_content(s["content"]):
                    self._promote(s)
                    report["promoted"] += 1
                    report["promoted_memories"].append({
                        "content": s["content"],
                        "category": s["category"],
                        "score": round(s["total_score"], 3),
                    })
            else:
                report["skipped_threshold"] += 1

        report["completed_at"] = datetime.now(timezone.utc).isoformat()
        logger.info(
            "Deep Dreaming complete: %d scored, %d promoted, %d below threshold",
            report["candidates_scored"],
            report["promoted"],
            report["skipped_threshold"],
        )
        return report

    # ─── Scoring ──────────────────────────────────────

    def _compute_score(self, candidate: dict, max_recall: int) -> dict[str, float]:
        """Compute all 7 dimension scores for a candidate."""
        recall_count = candidate.get("recall_count", 0)
        recall_queries = candidate.get("recall_queries", [])
        content = candidate.get("content", "")

        # 1. Frequency: normalized recall count
        frequency = min(recall_count / max(max_recall, 1), 1.0)

        # 2. Relevance: use REM theme importance as proxy, or 0.5 default
        relevance = candidate.get("rem_theme_importance", 0.5)

        # 3. Query diversity: number of unique queries that recalled this
        query_diversity = min(
            len(set(recall_queries)) / QUERY_DIVERSITY_CAP, 1.0
        )

        # 4. Recency: exponential decay, recent = higher
        age_days = self._age_days(candidate)
        recency = math.exp(-_RECENCY_LAMBDA * age_days)

        # 5. Consolidation: cross-day appearance
        consolidation = candidate.get("rem_consolidation", 0.0)

        # 6. Conceptual richness: entity/concept density
        conceptual_richness = self._compute_conceptual_richness(content)

        # 7. Content quality: LLM assessment (the cold-start solver)
        content_quality = self._compute_content_quality(content)

        return {
            "frequency": frequency,
            "relevance": relevance,
            "query_diversity": query_diversity,
            "recency": recency,
            "consolidation": consolidation,
            "conceptual_richness": conceptual_richness,
            "content_quality": content_quality,
        }

    def _compute_conceptual_richness(self, content: str) -> float:
        """
        Estimate entity/concept density.
        Heuristic: count capitalized words, technical terms, numbers.
        """
        words = content.split()
        if not words:
            return 0.0

        concept_indicators = 0
        for word in words:
            # Capitalized word (not sentence start)
            if word[0].isupper() and len(word) > 1:
                concept_indicators += 1
            # Numbers
            elif any(c.isdigit() for c in word):
                concept_indicators += 0.5
            # CJK characters (inherently concept-dense)
            elif any("\u4e00" <= c <= "\u9fff" for c in word):
                concept_indicators += 0.3
            # Technical terms (camelCase, snake_case, contains -)
            elif "_" in word or "-" in word or (
                any(c.isupper() for c in word[1:])
            ):
                concept_indicators += 0.5

        density = concept_indicators / len(words)
        return min(density, 1.0)

    def _compute_content_quality(self, content: str) -> float:
        """Use LLM to assess intrinsic value of the content."""
        try:
            prompt = QUALITY_PROMPT.format(content=content[:2000])
            body = json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 10,
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
            text = result.get("content", [{}])[0].get("text", "0.5").strip()

            # Parse the float
            score = float(text)
            return max(0.0, min(1.0, score))
        except (ValueError, Exception) as e:
            logger.debug("Content quality scoring failed: %s", e)
            return 0.5  # Default to neutral

    # ─── Promotion ────────────────────────────────────

    def _verify_content(self, content: str) -> bool:
        """
        Verify that the content is still relevant.
        Check that it hasn't been explicitly deleted or contradicted.
        """
        # For now, just check it's not empty
        return bool(content and content.strip())

    def _promote(self, scored: dict) -> None:
        """
        Promote a memory to long-term:
        1. Update OpenSearch: promoted=true, importance=score
        2. Append to MEMORY.md
        """
        doc_id = scored["doc_id"]
        now = datetime.now(timezone.utc)

        # Update OpenSearch
        try:
            self.os_client.update_document(doc_id, {
                "promoted": True,
                "promoted_at": now.isoformat(),
                "importance": scored["total_score"],
                "phase": "deep",
                "updated_at": now.isoformat(),
            })
        except Exception as e:
            logger.error("Failed to update promoted status for %s: %s", doc_id, e)
            return

        # Append to MEMORY.md
        self._append_to_memory_md(scored, now)

    def _append_to_memory_md(self, scored: dict, now: datetime) -> None:
        """Append a promoted memory to MEMORY.md."""
        if not WORKSPACE_ROOT:
            logger.warning("WORKSPACE_ROOT not set, skipping MEMORY.md append")
            return

        memory_path = Path(WORKSPACE_ROOT) / "MEMORY.md"
        date_str = now.strftime("%Y-%m-%d")
        category = scored.get("category", "Fact")
        content = scored.get("content", "")

        entry = f"\n## Auto-promoted [{date_str}]\n\n- [{category}] {content}\n"

        try:
            if memory_path.exists():
                existing = memory_path.read_text(encoding="utf-8")
                # Check if today's auto-promoted section already exists
                header = f"## Auto-promoted [{date_str}]"
                if header in existing:
                    # Append to existing section
                    entry = f"- [{category}] {content}\n"
                    # Find the section and append
                    idx = existing.index(header)
                    # Find the next section or end of file
                    next_section = existing.find("\n## ", idx + len(header))
                    if next_section > 0:
                        new_content = (
                            existing[:next_section]
                            + entry
                            + existing[next_section:]
                        )
                    else:
                        new_content = existing + entry
                    memory_path.write_text(new_content, encoding="utf-8")
                else:
                    # Append new section
                    memory_path.write_text(
                        existing + entry, encoding="utf-8"
                    )
            else:
                memory_path.write_text(
                    "# Long-Term Memory\n" + entry, encoding="utf-8"
                )

            logger.info("Appended to MEMORY.md: [%s] %s", category, content[:50])
        except Exception as e:
            logger.error("Failed to append to MEMORY.md: %s", e)

    # ─── Helpers ──────────────────────────────────────

    def _fetch_candidates(self, agent_id: str) -> list[dict]:
        """Fetch unpromoted extracted memories."""
        filters = [
            {"term": {"doc_type": DOC_TYPE_EXTRACTED}},
            {"term": {"promoted": False}},
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
            logger.error("Failed to fetch candidates: %s", e)
            return []

    @staticmethod
    def _age_days(candidate: dict) -> float:
        """Calculate age in days."""
        created_at = candidate.get("created_at", "")
        if not created_at:
            return 30  # Default to 30 days if unknown
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            age = datetime.now(timezone.utc) - created
            return max(age.total_seconds() / 86400, 0)
        except (ValueError, TypeError):
            return 30
