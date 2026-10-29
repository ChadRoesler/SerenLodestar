"""
Data-transfer objects matching the Seren Observatory API responses.

Design note, 28 Oct 2026: the shapes live in ``seren_sinew.cluster.dtos``
now, so the Observatory's routes and tests can import the same dataclasses
the head parses with, and a contract test on each side holds the wire still
(punch list sinew-cluster). This module re-exports them under the names
Lodestar has always used; add fields THERE, not here.
"""
from __future__ import annotations

from seren_sinew.cluster.dtos import (  # noqa: F401
    ALIASES,
    AgentUpdateNodeResult,
    AgentUpdateResponse,
    ClusterOptions,
    HealthResponse,
    JetsonNodeOptions,
    LogsResponse,
    NodeManifest,
    NodeOptions,
    NodeResponse,
    NodeRuntime,
    NodeSnapshot,
    PingResponse,
    PortHealth,
    RebootCancelResponse,
    RebootRequest,
    RebootResponse,
    ReclaimFailure,
    ReclaimRequest,
    ReclaimResponse,
    RefreshSummary,
    RoutedService,
    ServiceEntry,
    ServiceLifecycleResponse,
    ServiceManifest,
    ServiceStatus,
    ServicesResponse,
    ThermalResponse,
    ThermalZone,
    VersionResponse,
    WireDrift,
    drift,
    from_dict,
    to_dict,
)

__all__ = [
    "ALIASES", "AgentUpdateNodeResult", "AgentUpdateResponse", "ClusterOptions", "HealthResponse",
    "JetsonNodeOptions", "LogsResponse", "NodeManifest", "NodeOptions", "NodeResponse", "NodeRuntime",
    "NodeSnapshot", "PingResponse", "PortHealth", "RebootCancelResponse", "RebootRequest", "RebootResponse",
    "ReclaimFailure", "ReclaimRequest", "ReclaimResponse", "RefreshSummary", "RoutedService", "ServiceEntry",
    "ServiceLifecycleResponse", "ServiceManifest", "ServiceStatus", "ServicesResponse", "ThermalResponse",
    "ThermalZone", "VersionResponse", "WireDrift", "drift", "from_dict", "to_dict",
]
