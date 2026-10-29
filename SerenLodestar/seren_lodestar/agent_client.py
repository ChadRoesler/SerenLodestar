"""
seren_lodestar.agent_client
=======================================================================

Typed HTTP client for one node's SerenObservatory API at /api/v1/...

Design note, 28 Oct 2026: the client is ``seren_sinew.cluster.client.
NodeClient`` now (punch list sinew-cluster): one copy of the wire, which
the Observatory's own contract test parses against. This module keeps the
name Lodestar's callers and tests use, ``JetsonAgentClient``, and the
helpers they reach for. Change the client THERE.
"""
from __future__ import annotations

from seren_sinew.cluster.client import STATUS_READ_TIMEOUT_S, NodeClient  # noqa: F401
from seren_sinew.cluster.dtos import (  # noqa: F401
    ALIASES as _ALIASES,
    coerce as _coerce,
    empty_for as _empty_for,
    from_dict as _from_dict,
)

JetsonAgentClient = NodeClient

__all__ = ["JetsonAgentClient", "NodeClient", "STATUS_READ_TIMEOUT_S", "_from_dict", "_coerce", "_empty_for",
           "_ALIASES"]
