"""
OpenClaw Memory System v2 — MCP Tool Definitions
==================================================
Defines all 8 MCP tools:
  - aws_memory_write
  - aws_memory_search
  - aws_memory_get
  - aws_memory_pin
  - aws_memory_forget
  - aws_memory_update
  - aws_memory_index
  - aws_memory_stats
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config import (
    DEFAULT_API_VERSION,
    DEFAULT_TOP_K,
    DOC_TYPE_EXTRACTED,
    EXCEPTION_AGENT_LIST,
    WORKSPACE_ROOT,
)

logger = logging.getLogger(__name__)


# ─── Tool Schema Definitions ──────────────────────

TOOL_DEFINITIONS = [
    {
        "name": "aws_memory_write",
        "description": (
            "Write a message to memory. Auto-generates embedding for immediate "
            "searchability. Returns immediately (<1ms), actual persistence is async."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Current session ID"},
                "agent_id": {"type": "string", "description": "Agent identifier"},
                "role": {
                    "type": "string",
                    "enum": ["user", "assistant", "system"],
                    "description": "Message role",
                },
                "content": {"type": "string", "description": "Message content"},
                "api_version": {
                    "type": "string",
                    "default": "v1",
                    "description": "API version",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Optional dedup key for idempotent writes",
                },
                "conversation_ref": {
                    "type": "string",
                    "description": "Framework-agnostic conversation reference",
                },
                "client_type": {"type": "string", "description": "Client framework"},
                "client_version": {"type": "string", "description": "Client version"},
            },
            "required": ["session_id", "agent_id", "role", "content"],
        },
    },
    {
        "name": "aws_memory_search",
        "description": (
            "Hybrid search over all memory types. Combines BM25 keyword matching "
            "with semantic vector search. Supports filtering by time, type, and agent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query (natural language)",
                },
                "agent_id": {"type": "string", "description": "Filter by agent"},
                "session_id": {
                    "type": "string",
                    "description": "Limit to specific session",
                },
                "top_k": {
                    "type": "integer",
                    "default": 5,
                    "description": "Number of results",
                },
                "doc_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter: message, file_chunk, extracted, session_summary",
                },
                "days_back": {
                    "type": "integer",
                    "description": "Only search within last N days",
                },
                "min_score": {
                    "type": "number",
                    "description": "Minimum relevance score (0-1)",
                },
                "api_version": {
                    "type": "string",
                    "default": "v1",
                    "description": "API version",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "aws_memory_get",
        "description": (
            "Read a specific memory file or document by path or ID."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to workspace (e.g. MEMORY.md)",
                },
                "doc_id": {
                    "type": "string",
                    "description": "OpenSearch document ID",
                },
                "from": {
                    "type": "number",
                    "description": "Start line (1-indexed)",
                },
                "lines": {
                    "type": "number",
                    "description": "Number of lines to read",
                },
                "api_version": {
                    "type": "string",
                    "default": "v1",
                    "description": "API version",
                },
            },
        },
    },
    {
        "name": "aws_memory_pin",
        "description": (
            "Pin a memory directly to long-term storage. Skips Dreaming phases, "
            "sets importance=1.0. Use for critical facts the user explicitly wants remembered."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Memory content to pin",
                },
                "agent_id": {"type": "string", "description": "Agent identifier"},
                "category": {
                    "type": "string",
                    "description": "Category: Preference, Fact, Decision, Skill, Goal, Lesson",
                },
                "api_version": {
                    "type": "string",
                    "default": "v1",
                    "description": "API version",
                },
            },
            "required": ["content", "agent_id"],
        },
    },
    {
        "name": "aws_memory_forget",
        "description": (
            "Mark memories as forgotten. Soft mode hides from search; "
            "hard mode permanently deletes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query to find memories to forget",
                },
                "doc_id": {
                    "type": "string",
                    "description": "Specific document ID to forget",
                },
                "agent_id": {"type": "string", "description": "Agent identifier"},
                "mode": {
                    "type": "string",
                    "enum": ["soft", "hard"],
                    "default": "soft",
                    "description": "soft=hide, hard=delete permanently",
                },
                "api_version": {
                    "type": "string",
                    "default": "v1",
                    "description": "API version",
                },
            },
            "required": ["agent_id"],
        },
    },
    {
        "name": "aws_memory_update",
        "description": (
            "Update the content of an existing memory entry. "
            "Re-generates embedding, preserves metadata."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string", "description": "Document ID to update"},
                "new_content": {"type": "string", "description": "New content"},
                "agent_id": {"type": "string", "description": "Agent identifier"},
                "api_version": {
                    "type": "string",
                    "default": "v1",
                    "description": "API version",
                },
            },
            "required": ["doc_id", "new_content", "agent_id"],
        },
    },
    {
        "name": "aws_memory_index",
        "description": (
            "Trigger reindexing of workspace memory files into OpenSearch. "
            "Scans MEMORY.md and memory/*.md, chunks and indexes changes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "force": {
                    "type": "boolean",
                    "default": False,
                    "description": "Force full reindex (ignore hashes)",
                },
                "agent_id": {"type": "string", "description": "Agent identifier"},
                "api_version": {
                    "type": "string",
                    "default": "v1",
                    "description": "API version",
                },
            },
        },
    },
    {
        "name": "aws_memory_stats",
        "description": (
            "Get memory system statistics: document counts by type, "
            "queue health, index status, last dreaming run."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent identifier"},
                "api_version": {
                    "type": "string",
                    "default": "v1",
                    "description": "API version",
                },
            },
        },
    },
]


class ToolHandler:
    """
    Dispatches MCP tool calls to the appropriate engine methods.
    """

    def __init__(self, ingester, searcher, os_client, embed_client, indexer=None):
        self.ingester = ingester
        self.searcher = searcher
        self.os_client = os_client
        self.embed_client = embed_client
        self.indexer = indexer

    def handle(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Route a tool call to the appropriate handler."""
        handlers = {
            "aws_memory_write": self._handle_write,
            "aws_memory_search": self._handle_search,
            "aws_memory_get": self._handle_get,
            "aws_memory_pin": self._handle_pin,
            "aws_memory_forget": self._handle_forget,
            "aws_memory_update": self._handle_update,
            "aws_memory_index": self._handle_index,
            "aws_memory_stats": self._handle_stats,
        }

        handler = handlers.get(tool_name)
        if not handler:
            return {"error": f"Unknown tool: {tool_name}"}

        try:
            return handler(arguments)
        except Exception as e:
            logger.error("Tool %s failed: %s", tool_name, e, exc_info=True)
            return {"error": str(e), "alerts": self.ingester._check_alerts()}

    # ─── Individual Tool Handlers ─────────────────────

    def _handle_write(self, args: dict) -> dict:
        """Handle aws_memory_write."""
        return self.ingester.write(
            session_id=args["session_id"],
            agent_id=args["agent_id"],
            role=args["role"],
            content=args["content"],
            api_version=args.get("api_version", DEFAULT_API_VERSION),
            idempotency_key=args.get("idempotency_key"),
            conversation_ref=args.get("conversation_ref"),
            client_type=args.get("client_type"),
            client_version=args.get("client_version"),
        )

    def _handle_search(self, args: dict) -> dict:
        """Handle aws_memory_search."""
        return self.searcher.search(
            query=args["query"],
            agent_id=args.get("agent_id"),
            session_id=args.get("session_id"),
            top_k=args.get("top_k", DEFAULT_TOP_K),
            doc_types=args.get("doc_types"),
            days_back=args.get("days_back"),
            min_score=args.get("min_score", 0.0),
            api_version=args.get("api_version", DEFAULT_API_VERSION),
        )

    def _handle_get(self, args: dict) -> dict:
        """Handle aws_memory_get — read by file path or doc_id."""
        alerts = self.ingester._check_alerts()

        # Read by doc_id from OpenSearch
        if args.get("doc_id"):
            doc = self.os_client.get_document(args["doc_id"])
            if doc is None:
                return {"error": "Document not found", "alerts": alerts}
            return {
                "doc_id": args["doc_id"],
                "content": doc.get("content", ""),
                "metadata": {
                    k: v for k, v in doc.items()
                    if k not in ("content", "embedding")
                },
                "alerts": alerts,
            }

        # Read by file path
        if args.get("path"):
            workspace = WORKSPACE_ROOT
            if not workspace:
                return {"error": "WORKSPACE_ROOT not configured", "alerts": alerts}

            filepath = Path(workspace) / args["path"]
            if not filepath.exists():
                return {"error": f"File not found: {args['path']}", "alerts": alerts}

            text = filepath.read_text(encoding="utf-8")
            lines = text.splitlines()

            start = max(0, int(args.get("from", 1)) - 1)
            count = int(args.get("lines", len(lines)))
            selected = lines[start : start + count]

            return {
                "path": args["path"],
                "content": "\n".join(selected),
                "total_lines": len(lines),
                "alerts": alerts,
            }

        return {"error": "Provide either 'path' or 'doc_id'", "alerts": alerts}

    def _handle_pin(self, args: dict) -> dict:
        """
        Handle aws_memory_pin — pin directly to long-term memory.
        Skips Dreaming, importance=1.0.
        """
        content = args["content"]
        agent_id = args["agent_id"]
        category = args.get("category", "Fact")

        # Generate embedding
        embedding = self.embed_client.embed_text(content)

        now = datetime.now(timezone.utc)
        ts_ms = int(now.timestamp() * 1000)
        doc_id = f"{agent_id}:pinned:{ts_ms}"

        body = {
            "doc_id": doc_id,
            "doc_type": DOC_TYPE_EXTRACTED,
            "agent_id": agent_id,
            "content": content,
            "category": category,
            "importance": 1.0,
            "recall_count": 0,
            "recall_queries": [],
            "promoted": True,
            "promoted_at": now.isoformat(),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "day": now.strftime("%Y-%m-%d"),
            "phase": "pinned",
            "needs_embed": embedding is None,
        }
        if embedding:
            body["embedding"] = embedding

        try:
            self.os_client.index_document(doc_id, body)
            return {
                "status": "pinned",
                "doc_id": doc_id,
                "alerts": self.ingester._check_alerts(),
            }
        except Exception as e:
            return {
                "status": "error",
                "error": str(e),
                "alerts": self.ingester._check_alerts(),
            }

    def _handle_forget(self, args: dict) -> dict:
        """Handle aws_memory_forget — soft or hard delete."""
        agent_id = args["agent_id"]
        mode = args.get("mode", "soft")
        doc_id = args.get("doc_id")
        query = args.get("query")
        alerts = self.ingester._check_alerts()

        forgotten = []

        if doc_id:
            # Forget specific document
            if mode == "hard":
                self.os_client.delete_document(doc_id)
            else:
                self.os_client.update_document(doc_id, {
                    "importance": -1.0,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
            forgotten.append(doc_id)

        elif query:
            # Search and forget matching documents
            results = self.searcher.search(
                query=query,
                agent_id=agent_id,
                top_k=10,
            )
            for r in results.get("results", []):
                rid = r.get("doc_id", "")
                if not rid:
                    continue
                if mode == "hard":
                    self.os_client.delete_document(rid)
                else:
                    self.os_client.update_document(rid, {
                        "importance": -1.0,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    })
                forgotten.append(rid)

        # Mark forgotten in-memory for immediate search filtering
        # (bridges AOSS eventual consistency gap)
        if forgotten:
            self.ingester.mark_forgotten(forgotten)

        return {
            "status": "forgotten",
            "mode": mode,
            "count": len(forgotten),
            "doc_ids": forgotten,
            "alerts": alerts,
        }

    def _handle_update(self, args: dict) -> dict:
        """Handle aws_memory_update — update content, re-embed."""
        doc_id = args["doc_id"]
        new_content = args["new_content"]
        agent_id = args["agent_id"]
        alerts = self.ingester._check_alerts()

        # Verify document exists
        existing = self.os_client.get_document(doc_id)
        if existing is None:
            return {"error": "Document not found", "alerts": alerts}

        # Check ownership
        if existing.get("agent_id") != agent_id:
            if agent_id not in EXCEPTION_AGENT_LIST:
                return {"error": "Permission denied: agent mismatch", "alerts": alerts}

        # Generate new embedding
        embedding = self.embed_client.embed_text(new_content)

        update_body: dict[str, Any] = {
            "content": new_content,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "needs_embed": embedding is None,
        }
        if embedding:
            update_body["embedding"] = embedding

        try:
            self.os_client.update_document(doc_id, update_body)
            return {
                "status": "updated",
                "doc_id": doc_id,
                "alerts": alerts,
            }
        except Exception as e:
            return {"error": str(e), "alerts": alerts}

    def _handle_index(self, args: dict) -> dict:
        """Handle aws_memory_index — trigger file reindexing."""
        alerts = self.ingester._check_alerts()

        if self.indexer is None:
            return {"error": "Indexer not available", "alerts": alerts}

        force = args.get("force", False)
        try:
            result = self.indexer.run_once(force=force)
            return {
                "status": "indexed",
                "files_scanned": result.get("files_scanned", 0),
                "files_changed": result.get("files_changed", 0),
                "chunks_indexed": result.get("chunks_indexed", 0),
                "alerts": alerts,
            }
        except Exception as e:
            return {"error": str(e), "alerts": alerts}

    def _handle_stats(self, args: dict) -> dict:
        """Handle aws_memory_stats — system statistics."""
        alerts = self.ingester._check_alerts()
        agent_id = args.get("agent_id")

        try:
            # Count by doc_type
            stats: dict[str, Any] = {"by_type": {}}
            for doc_type in ["message", "file_chunk", "extracted", "session_summary", "daily_note"]:
                query = {"term": {"doc_type": doc_type}}
                if agent_id and agent_id not in EXCEPTION_AGENT_LIST:
                    query = {
                        "bool": {
                            "must": [query],
                            "filter": [{"term": {"agent_id": agent_id}}],
                        }
                    }
                stats["by_type"][doc_type] = self.os_client.count(query)

            stats["total"] = sum(stats["by_type"].values())
            stats["pending_queue_size"] = self.ingester.queue_size
            stats["opensearch_healthy"] = self.os_client.ping()
            stats["alerts"] = alerts

            return stats
        except Exception as e:
            return {"error": str(e), "alerts": alerts}
