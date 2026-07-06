"""
seren_lodestar.tooling.i_tool_dialect
=======================================================================

Abstract interface for a model-family-specific tool-calling dialect.
Ported from SerenLodestar/Tooling/IToolDialect.cs.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass
class ParsedToolCall:
    """A tool call parsed out of model output."""
    name: str
    arguments: Any = field(default_factory=lambda: {})  # JsonElement-like dict


@dataclass
class McpToolDefinition:
    """A tool definition as returned by MCP tools/list."""
    name: str
    description: Optional[str] = None
    input_schema: Any = field(default_factory=lambda: {})  # JSON schema dict


@runtime_checkable
class IToolDialect(Protocol):
    """
    Protocol for a model-family-specific tool-calling dialect.
    """

    @property
    def name(self) -> str:
        """Family name for logging/diagnostics (e.g. 'qwen-hermes')."""
        ...

    def contains_tool_call(self, model_output: str) -> bool:
        """
        Cheap pre-check: does this chunk of model output contain at least
        one tool call?
        """
        ...

    def parse_tool_calls(self, model_output: str) -> list[ParsedToolCall]:
        """
        Extract all tool calls from model output. Returns empty when none
        are present or parsing fails.
        """
        ...

    def extract_preamble(self, model_output: str) -> str:
        """
        The text the model produced BEFORE its first tool call – its
        "thinking out loud" preamble, if any.
        """
        ...

    def format_tool_result(self, tool_name: str, result_json: str):
        """
        Format a tool's result back into a message the model expects.
        Returns (role, content) pair.
        """
        ...

    def format_tools_for_system_prompt(self, tools: list[McpToolDefinition]):
        """
        Format available tool definitions into the system-prompt block
        the model expects. Returns None/empty when there are no tools.
        """
        ...
