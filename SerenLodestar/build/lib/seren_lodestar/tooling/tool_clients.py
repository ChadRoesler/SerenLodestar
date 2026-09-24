"""
seren_lodestar.tooling.tool_clients
=======================================================================

The three ways the chat loop and the scheduler reach a tool, behind one
two-method shape:

    list_tools_async() -> list[McpToolDefinition]
    call_tool_async(name, arguments) -> str      (text, or a JSON error object)

WHY THIS FILE REPLACED mcp_tool_client.py. The old client POSTed JSON-RPC to
"/" on a pooled httpx client that had no base URL, so httpx refused the
request before it left the process; the exception was swallowed, the tool
list came back empty, and every chat ran with zero tools while `tool_rounds`
sat at 0. Even with a URL it would have failed three more times: no bearer,
no MCP initialize handshake, and the wrong Accept header for the streamable
transport. Nothing in Lodestar had ever executed a tool.

IN-PROCESS FIRST. Lodestar's own tools live on the FastMCP server this same
process mounts at /mcp. Calling them over HTTP to ourselves - with a session
handshake, a bearer we issued, and SSE framing - is a loopback with extra
steps. `InProcessToolClient` calls the FastMCP object directly: no socket,
no token, no session, and it cannot be misconfigured.

REMOTE SECOND. The tools worth having in a chat - memory, search, the
scheduler's targets - live on Workbench and its siblings. `RemoteMcpToolClient`
uses the official `mcp` client library for the streamable-HTTP transport,
which does the initialize handshake and the session id properly, and carries
a bearer from the leaf's usual token pointers. Configured under
`tooling.remote_mcp` in seren-lodestar.yaml.

`CompositeToolClient` puts them together: one tool list, calls routed to
whichever client listed the name.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional, Sequence

from .i_tool_dialect import McpToolDefinition

log = logging.getLogger("seren_lodestar.tooling")


def _error(name: str, why: str) -> str:
    return json.dumps({"error": f"tool '{name}' {why}"})


def _render_result(result: Any) -> str:
    """Turn what a tool returned into the text the model gets back.

    FastMCP hands back either a dict (structured output), a sequence of
    content blocks (each with `.text` when textual), or a (content,
    structured) pair depending on version. The model wants text; a dict is
    JSON; blocks join with newlines; anything else is stringified.
    """
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
        return json.dumps(result[1], indent=2, default=str)
    if isinstance(result, dict):
        return json.dumps(result, indent=2, default=str)
    if isinstance(result, (list, tuple)):
        texts = []
        for block in result:
            text = getattr(block, "text", None)
            if text is None and isinstance(block, dict):
                text = block.get("text")
            if text is not None:
                texts.append(str(text))
        if texts:
            return "\n".join(texts)
        return json.dumps([str(b) for b in result])
    return str(result)


class InProcessToolClient:
    """Lodestar's own MCP tools, called without leaving the process."""

    def __init__(self, mcp_server: Any) -> None:
        self._server = mcp_server

    async def list_tools_async(self) -> list[McpToolDefinition]:
        try:
            tools = await self._server.list_tools()
        except Exception as ex:                       # noqa: BLE001 - a tool list must not 500 a chat
            log.warning("in-process tool list failed: %s: %s", type(ex).__name__, ex)
            return []
        out = []
        for t in tools:
            out.append(McpToolDefinition(
                name=t.name,
                description=getattr(t, "description", None),
                input_schema=getattr(t, "inputSchema", None) or {},
            ))
        return out

    async def call_tool_async(self, name: str, arguments: Any) -> str:
        args = arguments if isinstance(arguments, dict) else {}
        try:
            result = await self._server.call_tool(name, args)
        except Exception as ex:                       # noqa: BLE001 - the model gets the error as data
            log.warning("tool %r failed: %s: %s", name, type(ex).__name__, ex)
            return _error(name, f"failed: {type(ex).__name__}: {ex}")
        return _render_result(result)


class RemoteMcpToolClient:
    """One remote MCP server over streamable HTTP, via the official client.

    A session is opened per call. That is three round trips (initialize,
    initialized, the call) where a persistent session would be one, and it is
    deliberately the simple version: a persistent session is a background
    task that has to be torn down on shutdown and re-opened when the far end
    restarts, and getting that wrong looks exactly like the bug this file
    replaced. Make it persistent when a tool is called often enough to
    notice.
    """

    def __init__(self, name: str, url: str, token: str = "", timeout_s: float = 30.0) -> None:
        self.name = name
        self.url = url
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._timeout = timeout_s

    async def list_tools_async(self) -> list[McpToolDefinition]:
        try:
            from mcp.client.session import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
            async with streamablehttp_client(self.url, headers=self._headers,
                                             timeout=self._timeout) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
        except Exception as ex:                       # noqa: BLE001
            log.warning("remote MCP %r at %s: tool list failed: %s: %s",
                        self.name, self.url, type(ex).__name__, ex)
            return []
        return [McpToolDefinition(name=t.name, description=t.description,
                                  input_schema=t.inputSchema or {})
                for t in listed.tools]

    async def call_tool_async(self, name: str, arguments: Any) -> str:
        args = arguments if isinstance(arguments, dict) else {}
        try:
            from mcp.client.session import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
            async with streamablehttp_client(self.url, headers=self._headers,
                                             timeout=self._timeout) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(name, args)
        except Exception as ex:                       # noqa: BLE001
            log.warning("remote MCP %r: tool %r failed: %s: %s",
                        self.name, name, type(ex).__name__, ex)
            return _error(name, f"failed on {self.name}: {type(ex).__name__}: {ex}")
        if getattr(result, "isError", False):
            return _error(name, f"reported an error on {self.name}: {_render_result(result.content)}")
        structured = getattr(result, "structuredContent", None)
        if structured:
            return json.dumps(structured, indent=2, default=str)
        return _render_result(result.content)


class CompositeToolClient:
    """One tool list from several clients; calls go to whoever listed the name."""

    def __init__(self, clients: Sequence[Any]) -> None:
        self._clients = list(clients)
        self._owner: dict[str, Any] = {}

    async def list_tools_async(self) -> list[McpToolDefinition]:
        merged: list[McpToolDefinition] = []
        owner: dict[str, Any] = {}
        for client in self._clients:
            for tool in await client.list_tools_async():
                if tool.name in owner:
                    log.warning("tool %r offered by two servers; keeping the first", tool.name)
                    continue
                owner[tool.name] = client
                merged.append(tool)
        self._owner = owner
        return merged

    async def call_tool_async(self, name: str, arguments: Any) -> str:
        client = self._owner.get(name)
        if client is None:
            # A model can name a tool the list has not been refreshed for;
            # look once more before saying no.
            await self.list_tools_async()
            client = self._owner.get(name)
        if client is None:
            return _error(name, "is not a tool this Lodestar offers")
        return await client.call_tool_async(name, arguments)


def build_tool_client(mcp_server: Any, remotes: Sequence[Any] = ()) -> CompositeToolClient:
    """The one the app hangs on `app.state.tool_client`.

    `remotes` are config rows with name/url and the family's token pointers;
    a row with no url is skipped with a log line rather than an exception,
    because a mistyped block must not stop the head from starting.
    """
    clients: list[Any] = []
    if mcp_server is not None:
        clients.append(InProcessToolClient(mcp_server))
    for row in remotes:
        url = str(getattr(row, "url", "") or "")
        if not url:
            log.warning("tooling.remote_mcp entry %r has no url - skipped", getattr(row, "name", "?"))
            continue
        token = ""
        resolver: Optional[Callable[[], str]] = getattr(row, "resolve_bearer", None)
        if callable(resolver):
            token = resolver()
        clients.append(RemoteMcpToolClient(str(getattr(row, "name", url)), url, token))
    return CompositeToolClient(clients)
