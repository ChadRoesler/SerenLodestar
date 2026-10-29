"""
seren_lodestar.cluster
=======================================================================

Cluster-wide capability router and discovery service.

Design note, 28 Oct 2026: both live in ``seren_sinew.cluster.discovery``
now (punch list sinew-cluster). This module keeps the names Lodestar's
app and tests use. Change the router THERE.
"""
from __future__ import annotations

from seren_sinew.cluster.discovery import ClusterClient, DiscoveryService  # noqa: F401
from seren_sinew.cluster.dtos import (  # noqa: F401
    ClusterOptions,
    JetsonNodeOptions,
    NodeSnapshot,
    RefreshSummary,
    RoutedService,
    ServiceManifest,
)

JetsonClusterClient = ClusterClient
JetsonDiscoveryService = DiscoveryService

__all__ = ["JetsonClusterClient", "JetsonDiscoveryService", "ClusterClient", "DiscoveryService",
           "ClusterOptions", "JetsonNodeOptions", "NodeSnapshot", "RefreshSummary", "RoutedService",
           "ServiceManifest"]
