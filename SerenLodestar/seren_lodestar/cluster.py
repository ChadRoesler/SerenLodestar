"""
seren_lodestar.cluster
=======================================================================

Cluster-wide capability router and discovery service.
Ported from SerenCluster/JetsonClusterClient.cs and
JetsonDiscoveryService.cs.

Owns one JetsonAgentClient per configured node and a capability map.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from .agent_client import JetsonAgentClient
from .dtos import (
    ClusterOptions,
    JetsonNodeOptions,
    NodeSnapshot,
    RefreshSummary,
    RoutedService,
    ServiceManifest,
)

log = logging.getLogger("seren_lodestar.cluster")


class JetsonClusterClient:
    """Cluster-wide capability router.

    Owns one JetsonAgentClient per configured node and a capability map.
    Workers ask get_service_url() "where does X live right now" and connect
    directly to the resolved URL.
    """

    def __init__(self, options: ClusterOptions, log_fn=None):
        self._options = options
        self._log = log_fn or (lambda msg: log.info(f"[cluster] {msg}"))

        if not options.nodes:
            self._log("no nodes configured — cluster starts empty (zero-config mode)")

        # Sanity: node names must be unique
        names = [n.name for n in options.nodes]
        if len(names) != len(set(names)):
            dupes = {n for n in names if names.count(n) > 1}
            raise ValueError(
                f"Duplicate node name(s) in cluster config: {', '.join(sorted(dupes))}"
            )

        self._agents: dict[str, JetsonAgentClient] = {
            n.name: JetsonAgentClient(n, log_fn=self._log)
            for n in options.nodes
        }

        # Per-node capability snapshots
        self._snapshots: dict[str, NodeSnapshot] = {}

    @property
    def node_names(self) -> list[str]:
        return [n.name for n in self._options.nodes]

    def get_agent(self, node_name: str) -> Optional[JetsonAgentClient]:
        return self._agents.get(node_name)

    @property
    def agents(self) -> dict[str, JetsonAgentClient]:
        return dict(self._agents)

    # ── discovery ───────────────────────────────────────────────────────────

    async def refresh_async(self) -> RefreshSummary:
        """Re-query every configured node's services in parallel."""
        tasks = [
            self._probe_node(agent)
            for agent in self._agents.values()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        per_node: dict[str, NodeSnapshot] = {}
        online_count = 0
        for name, result in zip(self._agents.keys(), results):
            if isinstance(result, Exception):
                log.warning("refresh failed for %s: %s", name, result)
                snap = NodeSnapshot(
                    online=False,
                    installed_services=[],
                    status={},
                    last_error=str(result),
                    last_probed=datetime.now(timezone.utc).isoformat(),
                )
            else:
                snap = result
            self._snapshots[name] = snap
            per_node[name] = snap
            if snap.online:
                online_count += 1

        self._log(
            f"refresh: {online_count}/{len(self._agents)} nodes online"
        )
        return RefreshSummary(
            total_nodes=len(self._agents),
            online_nodes=online_count,
            per_node=per_node,
        )

    async def refresh_node_async(
        self, node_name: str
    ) -> Optional[NodeSnapshot]:
        """Refresh just one node."""
        agent = self._agents.get(node_name)
        if agent is None:
            return None

        try:
            snap = await self._probe_node(agent)
            self._snapshots[node_name] = snap
            self._log(
                f"refresh-node {node_name}: online={snap.online} "
                f"services={len(snap.installed_services)}"
            )
            return snap
        except Exception as ex:
            self._log(f"refresh-node {node_name} threw: {ex}")
            return None

    def mark_node_offline(self, node_name: str, reason: str) -> None:
        """Mark a node offline without probing."""
        prev = self._snapshots.get(node_name)
        if prev is not None:
            self._snapshots[node_name] = NodeSnapshot(
                online=False,
                installed_services=prev.installed_services,
                status=prev.status,
                last_error=reason,
                last_probed=datetime.now(timezone.utc).isoformat(),
            )
            self._log(f"marked offline: {node_name} ({reason})")

    async def _probe_node(
        self, agent: JetsonAgentClient
    ) -> NodeSnapshot:
        """Probe one node for its services."""
        try:
            services = await asyncio.wait_for(
                agent.get_services_async(),
                timeout=self._options.discovery_timeout_seconds,
            )
            if services is None:
                return NodeSnapshot(
                    online=False,
                    installed_services=[],
                    status={},
                    last_error="agent returned null (unreachable or auth failed)",
                    last_probed=datetime.now(timezone.utc).isoformat(),
                )

            installed = list(services.services.keys())
            status_map = {
                svc: entry.status
                for svc, entry in services.services.items()
            }
            return NodeSnapshot(
                online=True,
                installed_services=installed,
                status=status_map,
                last_error=None,
                last_probed=datetime.now(timezone.utc).isoformat(),
            )
        except asyncio.TimeoutError:
            return NodeSnapshot(
                online=False,
                installed_services=[],
                status={},
                last_error=(
                    f"discovery timed out after "
                    f"{self._options.discovery_timeout_seconds}s"
                ),
                last_probed=datetime.now(timezone.utc).isoformat(),
            )

    # ── routing ─────────────────────────────────────────────────────────────

    async def get_service_url_async(
        self, capability: str
    ) -> Optional[RoutedService]:
        """Resolve 'where is service X right now?'."""
        node = self._choose_node_for(capability)
        if node is None:
            self._log(
                f"no online node available for capability '{capability}'"
            )
            return None

        snapshot = self._snapshots.get(node.name)
        if snapshot is None or capability not in snapshot.installed_services:
            snapshot = await self.refresh_node_async(node.name)
            if snapshot is None or not snapshot.online or capability not in snapshot.installed_services:
                return None

        agent = self._agents.get(node.name)
        if agent is None:
            return None

        manifest = await agent.get_service_manifest_async(capability)
        if manifest is None:
            return None

        if manifest.port <= 0:
            return RoutedService(
                node_name=node.name,
                capability=capability,
                base_url=None,
                manifest=manifest,
                library_mode=True,
            )

        # Build URL from agent's host + service port
        base = agent.base_url.rstrip("/")
        scheme_host = base.split("://", 1)[-1].split(":")[0]
        # Extract scheme from base
        scheme = "http"
        if "://" in base:
            scheme = base.split("://")[0]
        service_url = f"{scheme}://{scheme_host}:{manifest.port}"

        return RoutedService(
            node_name=node.name,
            capability=capability,
            base_url=service_url,
            manifest=manifest,
            library_mode=False,
        )

    def choose_node_for(self, capability: str) -> Optional[JetsonNodeOptions]:
        """Public alias for the preferred-node selection logic."""
        return self._choose_node_for(capability)

    def _choose_node_for(self, capability: str) -> Optional[JetsonNodeOptions]:
        """Pick the best node for a capability."""
        # First tier: preferred_for
        for node in self._options.nodes:
            if capability in node.preferred_for and self._is_online_with(node.name, capability):
                self._log(
                    f"routing '{capability}' -> '{node.name}' (preferred)"
                )
                return node

        # Second tier: installed but not preferred
        for node in self._options.nodes:
            if capability not in node.preferred_for and self._is_online_with(node.name, capability):
                self._log(
                    f"routing '{capability}' -> '{node.name}' (fallback)"
                )
                return node

        self._log(
            f"routing '{capability}' -> unavailable (no online node has it)"
        )
        return None

    def _is_online_with(self, node_name: str, capability: str) -> bool:
        snap = self._snapshots.get(node_name)
        if snap is None:
            return False
        return snap.online and capability in snap.installed_services

    def get_snapshots(self) -> dict[str, NodeSnapshot]:
        return dict(self._snapshots)

    def get_capabilities(self) -> dict[str, list[str]]:
        """Build inverse: capability -> [nodes that have it]."""
        caps: dict[str, list[str]] = {}
        for node_name, snap in self._snapshots.items():
            if not snap.online:
                continue
            for svc in snap.installed_services:
                caps.setdefault(svc, []).append(node_name)
        return caps

    async def aclose(self) -> None:
        """Close every agent's HTTP client. Called on app shutdown."""
        for agent in self._agents.values():
            try:
                await agent.aclose()
            except Exception as ex:
                self._log(f"error closing agent client: {ex}")


# ── Discovery background service ────────────────────────────────────────────

class JetsonDiscoveryService:
    """Background service that drives periodic refresh.

    Ported from SerenCluster/JetsonDiscoveryService.cs.
    """

    def __init__(
        self,
        cluster: JetsonClusterClient,
        options: ClusterOptions,
        log_fn=None,
    ):
        self._cluster = cluster
        self._interval = options.refresh_interval_seconds
        self._log = log_fn or (lambda msg: log.info(f"[discovery] {msg}"))
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the background discovery loop."""
        self._log("startup eager refresh")
        try:
            await self._cluster.refresh_async()
        except Exception as ex:
            self._log(
                f"startup refresh threw: {type(ex).__name__}: {ex}"
            )

        self._log(
            f"periodic refresh every {self._interval / 60:.0f} minutes"
        )

        self._task = asyncio.create_task(self._run_loop())

    async def _run_loop(self):
        while True:
            try:
                await asyncio.sleep(self._interval)
                await self._cluster.refresh_async()
            except asyncio.CancelledError:
                break
            except Exception as ex:
                self._log(
                    f"periodic refresh threw: {type(ex).__name__}: {ex}"
                )

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass