"""
seren_lodestar.tooling
=======================================================================

Model-agnostic tool-calling dialect + MCP client for the chat loop.
Ported from SerenLodestar/Tooling/*.cs.
"""
from __future__ import annotations

from .i_tool_dialect import IToolDialect, ParsedToolCall, McpToolDefinition
from .qwen_hermes_dialect import QwenHermesDialect
from .mcp_tool_client import McpToolClient

__all__ = ["IToolDialect", "ParsedToolCall", "McpToolDefinition",
           "QwenHermesDialect", "McpToolClient"]
