"""
Tests for tools.py — MCP tool handler dispatch.
"""
import json
import time
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from tools import ToolHandler, TOOL_DEFINITIONS


class MockIngester:
    def __init__(self):
        self.writes = []
        self._queue_size = 0

    def write(self, **kwargs):
        self.writes.append(kwargs)
        return {
            "status": "queued",
            "doc_id": f"test:{kwargs['session_id']}:{int(time.time()*1000)}",
            "alerts": [],
        }

    @property
    def queue_size(self):
        return self._queue_size

    def _check_alerts(self):
        return []


class MockSearcher:
    def __init__(self, results=None):
        self._results = results or []

    def search(self, **kwargs):
        return {
            "results": self._results,
            "query": kwargs.get("query", ""),
            "total": len(self._results),
            "alerts": [],
        }


class MockOSClient:
    def __init__(self):
        self.indexed = {}
        self.deleted = []
        self.updated = {}

    def index_document(self, doc_id, body):
        self.indexed[doc_id] = body
        return {"result": "created"}

    def get_document(self, doc_id):
        return self.indexed.get(doc_id)

    def update_document(self, doc_id, partial):
        self.updated[doc_id] = partial
        return {"result": "updated"}

    def delete_document(self, doc_id):
        self.deleted.append(doc_id)
        return {"result": "deleted"}

    def count(self, query=None):
        return 42

    def ping(self):
        return True


class MockEmbedClient:
    def embed_text(self, text, max_retries=3):
        return [0.1] * 1024


class TestToolDefinitions:
    def test_all_tools_have_aws_prefix(self):
        """All tool names start with aws_memory_."""
        for tool in TOOL_DEFINITIONS:
            assert tool["name"].startswith("aws_memory_"), \
                f"Tool '{tool['name']}' missing aws_memory_ prefix"

    def test_eight_tools_defined(self):
        """Exactly 8 tools defined."""
        assert len(TOOL_DEFINITIONS) == 8

    def test_all_tools_have_input_schema(self):
        """All tools have inputSchema."""
        for tool in TOOL_DEFINITIONS:
            assert "inputSchema" in tool, f"Tool '{tool['name']}' missing inputSchema"

    def test_api_version_in_all_tools(self):
        """All tools accept api_version parameter."""
        for tool in TOOL_DEFINITIONS:
            props = tool["inputSchema"].get("properties", {})
            assert "api_version" in props, \
                f"Tool '{tool['name']}' missing api_version parameter"


class TestToolHandler:
    def setup_method(self):
        self.ingester = MockIngester()
        self.searcher = MockSearcher()
        self.os_client = MockOSClient()
        self.embed_client = MockEmbedClient()
        self.handler = ToolHandler(
            ingester=self.ingester,
            searcher=self.searcher,
            os_client=self.os_client,
            embed_client=self.embed_client,
        )

    def test_unknown_tool(self):
        """Unknown tool returns error."""
        result = self.handler.handle("aws_memory_nonexistent", {})
        assert "error" in result

    def test_write_dispatches(self):
        """aws_memory_write dispatches to ingester."""
        result = self.handler.handle("aws_memory_write", {
            "session_id": "s1",
            "agent_id": "a1",
            "role": "user",
            "content": "test",
        })
        assert result["status"] == "queued"
        assert len(self.ingester.writes) == 1

    def test_search_dispatches(self):
        """aws_memory_search dispatches to searcher."""
        result = self.handler.handle("aws_memory_search", {
            "query": "hello",
            "agent_id": "a1",
        })
        assert "results" in result
        assert result["total"] == 0

    def test_pin_creates_extracted_doc(self):
        """aws_memory_pin creates an extracted doc with importance=1.0."""
        result = self.handler.handle("aws_memory_pin", {
            "content": "important fact",
            "agent_id": "a1",
            "category": "Fact",
        })
        assert result["status"] == "pinned"
        assert "doc_id" in result
        # Verify the indexed document
        doc = self.os_client.indexed[result["doc_id"]]
        assert doc["importance"] == 1.0
        assert doc["promoted"] is True
        assert doc["phase"] == "pinned"

    def test_forget_soft(self):
        """aws_memory_forget soft mode sets importance=-1."""
        # Pre-populate a doc
        self.os_client.indexed["doc-1"] = {
            "content": "to forget",
            "agent_id": "a1",
        }
        result = self.handler.handle("aws_memory_forget", {
            "doc_id": "doc-1",
            "agent_id": "a1",
            "mode": "soft",
        })
        assert result["status"] == "forgotten"
        assert result["count"] == 1
        assert self.os_client.updated["doc-1"]["importance"] == -1.0

    def test_forget_hard(self):
        """aws_memory_forget hard mode deletes the doc."""
        result = self.handler.handle("aws_memory_forget", {
            "doc_id": "doc-1",
            "agent_id": "a1",
            "mode": "hard",
        })
        assert result["status"] == "forgotten"
        assert "doc-1" in self.os_client.deleted

    def test_update_rerequires_doc(self):
        """aws_memory_update fails if doc not found."""
        result = self.handler.handle("aws_memory_update", {
            "doc_id": "nonexistent",
            "new_content": "updated",
            "agent_id": "a1",
        })
        assert "error" in result

    def test_update_success(self):
        """aws_memory_update updates content and re-embeds."""
        self.os_client.indexed["doc-u"] = {
            "content": "old content",
            "agent_id": "a1",
        }
        result = self.handler.handle("aws_memory_update", {
            "doc_id": "doc-u",
            "new_content": "new content",
            "agent_id": "a1",
        })
        assert result["status"] == "updated"
        assert self.os_client.updated["doc-u"]["content"] == "new content"
        assert self.os_client.updated["doc-u"]["needs_embed"] is False

    def test_get_by_doc_id(self):
        """aws_memory_get by doc_id."""
        self.os_client.indexed["doc-g"] = {
            "content": "get me",
            "doc_type": "message",
        }
        result = self.handler.handle("aws_memory_get", {"doc_id": "doc-g"})
        assert result["content"] == "get me"

    def test_stats(self):
        """aws_memory_stats returns counts."""
        result = self.handler.handle("aws_memory_stats", {"agent_id": "a1"})
        assert "by_type" in result
        assert "total" in result
        assert result["opensearch_healthy"] is True
