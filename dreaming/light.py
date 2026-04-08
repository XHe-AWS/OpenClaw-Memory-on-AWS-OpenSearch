"""
OpenClaw Memory System v2 — Dreaming Light Phase
==================================================
Scans past 24h messages, uses LLM to extract candidate long-term memories.
Deduplicates against existing memories using embedding similarity.

Output: extracted documents in OpenSearch (phase="light", promoted=false).
"""

import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from config import (
    EXTRACT_MODEL_ID,
    BEDROCK_REGION,
    DOC_TYPE_MESSAGE,
    DOC_TYPE_EXTRACTED,
    DEDUP_HIGH_SIMILARITY,
    DEDUP_MERGE_SIMILARITY,
)
from embedding import EmbeddingClient
from opensearch_client import OpenSearchClient

logger = logging.getLogger(__name__)

# Prompt for extracting candidate memories
EXTRACT_PROMPT = """From the following conversation, extract information worth remembering long-term.
Each memory should be one line in the format: [Category] content
Categories: Preference, Fact, Decision, Skill, Goal, Lesson
Only output results. If nothing is worth remembering, output NONE.

Rules:
- Extract specific, actionable information (not vague statements)
- Preferences: things the user likes/dislikes
- Facts: personal info, project details, technical facts
- Decisions: choices made, why
- Skills: things the user knows/is learning
- Goals: what they want to achieve
- Lessons: mistakes, insights, learnings

Conversation:
{conversation}"""


class LightDreaming:
    """
    Light Phase — extract candidate memories from recent conversations.
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
        lookback_hours: int = 24,
    ) -> dict[str, Any]:
        """
        Execute Light Phase dreaming.

        Args:
            agent_id: Filter messages by agent. Empty = all agents.
            lookback_hours: How far back to look for messages.

        Returns:
            Report dict with extracted candidates, duplicates skipped, etc.
        """
        logger.info("Light Dreaming starting (agent=%s, lookback=%dh)", agent_id, lookback_hours)

        report = {
            "phase": "light",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "messages_processed": 0,
            "candidates_extracted": 0,
            "duplicates_skipped": 0,
            "mergeable_found": 0,
            "sessions": [],
        }

        # 1. Fetch recent messages
        messages = self._fetch_recent_messages(agent_id, lookback_hours)
        if not messages:
            logger.info("No recent messages found")
            report["completed_at"] = datetime.now(timezone.utc).isoformat()
            return report

        report["messages_processed"] = len(messages)

        # 2. Group by session
        sessions: dict[str, list[dict]] = {}
        for msg in messages:
            sid = msg.get("session_id", "unknown")
            sessions.setdefault(sid, []).append(msg)

        # 3. Process each session
        for session_id, session_msgs in sessions.items():
            logger.info("Processing session %s (%d messages)", session_id, len(session_msgs))

            # Build conversation text
            conversation = self._format_conversation(session_msgs)
            if not conversation.strip():
                continue

            # Extract candidates via LLM
            candidates = self._extract_candidates(conversation)
            if not candidates:
                continue

            session_report = {
                "session_id": session_id,
                "messages": len(session_msgs),
                "candidates": len(candidates),
                "stored": 0,
                "skipped_duplicate": 0,
                "marked_mergeable": 0,
            }

            # 4. Dedup and store each candidate
            for category, content in candidates:
                result = self._dedup_and_store(
                    content=content,
                    category=category,
                    agent_id=agent_id or session_msgs[0].get("agent_id", ""),
                    session_id=session_id,
                )
                if result == "stored":
                    session_report["stored"] += 1
                    report["candidates_extracted"] += 1
                elif result == "duplicate":
                    session_report["skipped_duplicate"] += 1
                    report["duplicates_skipped"] += 1
                elif result == "mergeable":
                    session_report["marked_mergeable"] += 1
                    report["mergeable_found"] += 1

            report["sessions"].append(session_report)

        report["completed_at"] = datetime.now(timezone.utc).isoformat()
        logger.info(
            "Light Dreaming complete: %d extracted, %d duplicates, %d mergeable",
            report["candidates_extracted"],
            report["duplicates_skipped"],
            report["mergeable_found"],
        )
        return report

    # ─── Internal Methods ─────────────────────────────

    def _fetch_recent_messages(
        self, agent_id: str, lookback_hours: int
    ) -> list[dict]:
        """Fetch messages from OpenSearch within lookback window."""
        filters = [
            {"term": {"doc_type": DOC_TYPE_MESSAGE}},
            {
                "range": {
                    "created_at": {
                        "gte": f"now-{lookback_hours}h",
                    }
                }
            },
        ]
        if agent_id:
            filters.append({"term": {"agent_id": agent_id}})

        try:
            # Use match_all with filters (keyword_search with "*" doesn't work as wildcard)
            body = {
                "size": 500,
                "query": {
                    "bool": {
                        "filter": filters
                    }
                },
                "sort": [{"created_at": {"order": "asc"}}],
            }
            resp = self.os_client.client.search(
                index=self.os_client.index_name,
                body=body,
            )
            hits = resp.get("hits", {}).get("hits", [])
            messages = []
            for hit in hits:
                source = hit.get("_source", {})
                source["_id"] = source.get("doc_id", hit.get("_id", ""))
                messages.append(source)

            # Sort by created_at
            messages.sort(key=lambda m: m.get("created_at", ""))
            return messages
        except Exception as e:
            logger.error("Failed to fetch recent messages: %s", e)
            return []

    def _format_conversation(self, messages: list[dict]) -> str:
        """Format messages into a conversation string for the LLM."""
        lines = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if content.strip():
                lines.append(f"[{role}]: {content}")
        return "\n".join(lines)

    def _extract_candidates(self, conversation: str) -> list[tuple[str, str]]:
        """Use LLM to extract candidate memories from a conversation."""
        prompt = EXTRACT_PROMPT.format(conversation=conversation[:8000])

        try:
            body = json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 2000,
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

            if "NONE" in text.strip().upper():
                return []

            return self._parse_candidates(text)

        except Exception as e:
            logger.error("LLM extraction failed: %s", e)
            return []

    def _parse_candidates(self, text: str) -> list[tuple[str, str]]:
        """Parse LLM output into (category, content) tuples."""
        candidates = []
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            # Match [Category] content
            if line.startswith("[") and "]" in line:
                bracket_end = line.index("]")
                category = line[1:bracket_end].strip()
                content = line[bracket_end + 1:].strip()
                if content:
                    candidates.append((category, content))
            elif line.startswith("- [") and "]" in line:
                # Handle "- [Category] content" format
                bracket_start = line.index("[")
                bracket_end = line.index("]")
                category = line[bracket_start + 1:bracket_end].strip()
                content = line[bracket_end + 1:].strip()
                if content:
                    candidates.append((category, content))
        return candidates

    def _dedup_and_store(
        self,
        content: str,
        category: str,
        agent_id: str,
        session_id: str,
    ) -> str:
        """
        Check for duplicates using embedding similarity, then store.
        Returns: "stored", "duplicate", or "mergeable".
        """
        # Generate embedding
        embedding = self.embed_client.embed_text(content)
        if embedding is None:
            # Store without dedup check
            self._store_candidate(content, category, agent_id, session_id, None)
            return "stored"

        # Search for similar existing memories
        try:
            hits = self.os_client.hybrid_search(
                query_text=content,
                query_vector=embedding,
                k=3,
                filters=[{"term": {"doc_type": DOC_TYPE_EXTRACTED}}],
            )
        except Exception:
            hits = []

        # Check similarity
        for hit in hits:
            hit_embedding = hit.get("_source", {}).get("embedding")
            if hit_embedding:
                sim = self.embed_client.cosine_similarity(embedding, hit_embedding)
                if sim > DEDUP_HIGH_SIMILARITY:
                    logger.debug(
                        "Duplicate found (sim=%.3f): %s",
                        sim, content[:50],
                    )
                    return "duplicate"
                elif sim > DEDUP_MERGE_SIMILARITY:
                    logger.debug(
                        "Mergeable found (sim=%.3f): %s",
                        sim, content[:50],
                    )
                    # Store but mark as mergeable
                    self._store_candidate(
                        content, category, agent_id, session_id,
                        embedding, mergeable_with=hit.get("_source", {}).get("doc_id", hit.get("_id", "")),
                    )
                    return "mergeable"

        # No duplicate — store
        self._store_candidate(content, category, agent_id, session_id, embedding)
        return "stored"

    def _store_candidate(
        self,
        content: str,
        category: str,
        agent_id: str,
        session_id: str,
        embedding: Optional[list[float]],
        mergeable_with: Optional[str] = None,
    ) -> str:
        """Store an extracted candidate in OpenSearch."""
        now = datetime.now(timezone.utc)
        ts_ms = int(now.timestamp() * 1000)
        doc_id = f"extracted:{agent_id}:{ts_ms}"

        body: dict[str, Any] = {
            "doc_id": doc_id,
            "doc_type": DOC_TYPE_EXTRACTED,
            "agent_id": agent_id,
            "content": content,
            "category": category,
            "phase": "light",
            "importance": 0.0,
            "recall_count": 0,
            "recall_queries": [],
            "promoted": False,
            "source_sessions": [session_id],
            "source_day": now.strftime("%Y-%m-%d"),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "day": now.strftime("%Y-%m-%d"),
            "needs_embed": embedding is None,
        }
        if embedding:
            body["embedding"] = embedding
        if mergeable_with:
            body["mergeable_with"] = mergeable_with

        try:
            self.os_client.index_document(doc_id, body)
            return doc_id
        except Exception as e:
            logger.error("Failed to store candidate: %s", e)
            return ""
