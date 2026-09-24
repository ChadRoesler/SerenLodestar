"""
seren_lodestar.mcp — FastMCP server wiring for SerenLodestar.

Mounts a FastMCP server onto the FastAPI app so that LLMs can connect
to the Lodestar's MCP endpoint for cluster management tools.
"""
from __future__ import annotations

from .server import mount_mcp_routes

__all__ = ["mount_mcp_routes"]
