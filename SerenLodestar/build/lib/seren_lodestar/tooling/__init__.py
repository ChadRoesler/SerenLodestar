"""
seren_lodestar.tooling
=======================================================================

Model-agnostic tool-calling dialect + the tool clients the chat loop and
the scheduler call through. Ported from SerenLodestar/Tooling/*.cs, minus
the HTTP-to-ourselves client that never worked (see tool_clients.py).
"""
from __future__ import annotations

from .i_tool_dialect import IToolDialect, ParsedToolCall, McpToolDefinition
from .qwen_hermes_dialect import QwenHermesDialect
from .tool_clients import (
    CompositeToolClient, InProcessToolClient, RemoteMcpToolClient, build_tool_client,
)

__all__ = ["IToolDialect", "ParsedToolCall", "McpToolDefinition",
           "QwenHermesDialect", "CompositeToolClient", "InProcessToolClient",
           "RemoteMcpToolClient", "build_tool_client"]
