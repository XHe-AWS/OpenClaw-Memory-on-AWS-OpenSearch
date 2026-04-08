"""
OpenClaw Memory System v2 — MCP Server
========================================
MCP (Model Context Protocol) server using stdio JSON-RPC transport.
Integrates all components: ingester, searcher, tools, and (optionally)
indexer and dreaming.

Usage:
    python mcp_server.py
    
    Or in openclaw.json:
    "mcp": {
      "servers": {
        "openclaw-memory": {
          "command": "python",
          "args": ["/path/to/mcp_server.py"]
        }
      }
    }
"""

import json
import logging
import os
import signal
import sys
from typing import Any, Optional

from config import (
    OPENSEARCH_ENDPOINT,
    LOG_LEVEL,
    LOG_FILE,
    WORKSPACE_ROOT,
)

# ─── Logging Setup ─────────────────────────────────
# Log to file (stderr is used by MCP for diagnostics, stdout for JSON-RPC)
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger("mcp_server")


# ─── JSON-RPC Helpers ─────────────────────────────
def jsonrpc_response(id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id, "result": result}


def jsonrpc_error(id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": id, "error": err}


# ─── MCP Protocol Constants ───────────────────────
MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "openclaw-memory-v2"
SERVER_VERSION = "0.1.0"


class MCPServer:
    """
    Stdio-based MCP server implementing the Model Context Protocol.
    Handles initialize, tools/list, tools/call, and shutdown.
    """

    def __init__(self):
        self.initialized = False
        self.tool_handler = None
        self.ingester = None
        self.indexer = None

    def setup_components(self) -> None:
        """Initialize all memory system components."""
        from opensearch_client import OpenSearchClient
        from embedding import EmbeddingClient
        from ingester import Ingester
        from searcher import Searcher
        from tools import ToolHandler, TOOL_DEFINITIONS

        os_client = OpenSearchClient()
        embed_client = EmbeddingClient()

        self.ingester = Ingester(os_client, embed_client)
        self.ingester.start()

        searcher = Searcher(os_client, embed_client, ingester=self.ingester)

        # Indexer is optional (P1)
        indexer = None
        try:
            from indexer import Indexer
            if WORKSPACE_ROOT:
                indexer = Indexer(os_client, embed_client, WORKSPACE_ROOT)
                self.indexer = indexer
                logger.info("Indexer initialized for workspace: %s", WORKSPACE_ROOT)
        except ImportError:
            logger.info("Indexer module not available (P1)")

        self.tool_handler = ToolHandler(
            ingester=self.ingester,
            searcher=searcher,
            os_client=os_client,
            embed_client=embed_client,
            indexer=indexer,
        )

        self.tool_definitions = TOOL_DEFINITIONS
        logger.info("Memory system components initialized")

    def handle_message(self, message: dict) -> Optional[dict]:
        """Process a single JSON-RPC message and return a response (or None for notifications)."""
        method = message.get("method", "")
        msg_id = message.get("id")
        params = message.get("params", {})

        # Notifications (no id) don't need a response
        if msg_id is None and method.startswith("notifications/"):
            logger.debug("Received notification: %s", method)
            return None

        if method == "initialize":
            return self._handle_initialize(msg_id, params)
        elif method == "tools/list":
            return self._handle_tools_list(msg_id, params)
        elif method == "tools/call":
            return self._handle_tools_call(msg_id, params)
        elif method == "ping":
            return jsonrpc_response(msg_id, {})
        elif method == "shutdown":
            return self._handle_shutdown(msg_id)
        else:
            # Unknown method — return error for requests, ignore notifications
            if msg_id is not None:
                return jsonrpc_error(msg_id, -32601, f"Method not found: {method}")
            return None

    def _handle_initialize(self, msg_id: Any, params: dict) -> dict:
        """Handle the initialize request."""
        logger.info("Initializing MCP server...")

        # Set up components on first initialize
        if not self.initialized:
            try:
                self.setup_components()
                self.initialized = True
            except Exception as e:
                logger.error("Failed to initialize components: %s", e, exc_info=True)
                return jsonrpc_error(msg_id, -32603, f"Initialization failed: {e}")

        return jsonrpc_response(msg_id, {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {
                "tools": {},
            },
            "serverInfo": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
            },
        })

    def _handle_tools_list(self, msg_id: Any, params: dict) -> dict:
        """Handle tools/list — return all available tools."""
        if not self.initialized:
            return jsonrpc_error(msg_id, -32002, "Server not initialized")

        return jsonrpc_response(msg_id, {
            "tools": self.tool_definitions,
        })

    def _handle_tools_call(self, msg_id: Any, params: dict) -> dict:
        """Handle tools/call — execute a tool."""
        if not self.initialized:
            return jsonrpc_error(msg_id, -32002, "Server not initialized")

        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        logger.info("Tool call: %s", tool_name)
        logger.debug("Arguments: %s", json.dumps(arguments, ensure_ascii=False)[:500])

        result = self.tool_handler.handle(tool_name, arguments)

        # MCP tools/call expects content array
        return jsonrpc_response(msg_id, {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, ensure_ascii=False, default=str),
                }
            ],
        })

    def _handle_shutdown(self, msg_id: Any) -> dict:
        """Handle shutdown — flush and stop."""
        logger.info("Shutdown requested")
        if self.ingester:
            self.ingester.shutdown()
        return jsonrpc_response(msg_id, {})

    def run(self) -> None:
        """Main event loop — read from stdin, write to stdout."""
        logger.info("MCP server starting (stdio transport)")

        # Handle graceful shutdown
        def on_signal(signum, frame):
            logger.info("Signal %d received, shutting down...", signum)
            if self.ingester:
                self.ingester.shutdown()
            sys.exit(0)

        signal.signal(signal.SIGINT, on_signal)
        signal.signal(signal.SIGTERM, on_signal)

        # Read JSON-RPC messages from stdin (newline-delimited)
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                message = json.loads(line)
            except json.JSONDecodeError as e:
                logger.error("Invalid JSON: %s", e)
                error_resp = jsonrpc_error(None, -32700, f"Parse error: {e}")
                self._write_response(error_resp)
                continue

            response = self.handle_message(message)
            if response is not None:
                self._write_response(response)

    def _write_response(self, response: dict) -> None:
        """Write a JSON-RPC response to stdout."""
        line = json.dumps(response, ensure_ascii=False, default=str)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def main():
    """Entry point."""
    server = MCPServer()
    server.run()


if __name__ == "__main__":
    main()
