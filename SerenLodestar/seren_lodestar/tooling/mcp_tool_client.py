"""
seren_lodestar.tooling.mcp_tool_client
=======================================================================

Thin MCP client for the chat loop: lists tools and calls them over the
MCP server's JSON-RPC-over-HTTP transport (Streamable HTTP, POST /).
Ported from SerenLodestar/Tooling/McpToolClient.cs.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

from .i_tool_dialect import McpToolDefinition


class McpToolClient:
    """
    A thin MCP client that lists tools and calls them over HTTP POST /.
    """

    def __init__(
        self,
        http_client_factory: Callable[[str], Any],
    ) -> None:
        self._http_factory = http_client_factory

    async def list_tools_async(self) -> list[McpToolDefinition]:
        """List the tools the MCP server currently exposes."""
        try:
            client = self._http_factory("mcp")
            rpc = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {},
            }
            resp = await client.post("/", json=rpc)
            if not resp.is_success:
                return []

            doc = await self._read_rpc_async(resp)
            if doc is None:
                return []

            # JSON-RPC envelope: { result: { tools: [...] } }
            result = doc.get("result")
            if not isinstance(result, dict):
                return []
            tools_list = result.get("tools")
            if not isinstance(tools_list, list):
                return []

            tools: list[McpToolDefinition] = []
            for t in tools_list:
                if not isinstance(t, dict):
                    continue
                name = t.get("name", "")
                if not name:
                    continue
                desc = t.get("description")
                schema = t.get("inputSchema", {})
                tools.append(McpToolDefinition(name=name, description=desc, input_schema=schema))
            return tools
        except Exception:
            return []

    async def call_tool_async(
        self, name: str, arguments: Any,
    ) -> str:
        """Call a tool by name with the given arguments. Returns result JSON string."""
        try:
            client = self._http_factory("mcp")
            rpc = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": name,
                    "arguments": arguments,
                },
            }
            resp = await client.post("/", json=rpc)
            if not resp.is_success:
                return json.dumps({"error": f"tool '{name}' returned HTTP {resp.status_code}"})

            doc = await self._read_rpc_async(resp)
            if doc is None:
                return json.dumps({"error": f"tool '{name}' returned an unreadable response"})

            # JSON-RPC: { result: { content: [ {type:"text", text:"..."} ], isError?: bool } }
            result = doc.get("result")
            if isinstance(result, dict):
                # Prefer the text content blocks (MCP standard result shape)
                content = result.get("content")
                if isinstance(content, list):
                    texts = []
                    for block in content:
                        if isinstance(block, dict):
                            txt = block.get("text")
                            if txt is not None:
                                texts.append(str(txt))
                    if texts:
                        return "\n".join(texts)
                # Fall back to raw result JSON
                return json.dumps(result, indent=2)

            # JSON-RPC error envelope
            err = doc.get("error")
            if err is not None:
                return json.dumps({"error": str(err)})

            return json.dumps({"error": f"tool '{name}' returned no result"})
        except Exception as ex:
            return json.dumps({"error": f"tool '{name}' failed: {ex}"})

    @staticmethod
    async def _read_rpc_async(resp: Any) -> Optional[dict]:
        """Read and parse JSON-RPC response, handling SSE framing."""
        try:
            body = resp.json()
            if isinstance(body, dict):
                return body
        except Exception:
            pass
        # If body is a string (SSE framed), parse it
        try:
            text = resp.text
        except Exception:
            return None
        return McpToolClient._parse_sse(text) if "data:" in text else None

    @staticmethod
    def _parse_sse(body: str) -> Optional[dict]:
        """Extract JSON from SSE framing like 'data: {json}\n\n'."""
        import re
        # Take the last data: line
        payload: Optional[str] = None
        for line in body.split("\n"):
            trimmed = line.rstrip("\r")
            if trimmed.startswith("data:"):
                payload = trimmed[5:].strip()
        if payload is None:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None
