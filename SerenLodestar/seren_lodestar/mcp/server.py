"""
seren_lodestar.mcp.server
════════════════════════════════════════════════════════════════════════

Mount the SerenLodestar MCP server onto an existing FastAPI app.

Uses the mcp package (≥1.3.0) to create a FastMCP server and then mounts
its Starlette ASGI app at /mcp with transport security configured via the
shared ServerConfig/TlsConfig from seren_meninges.

Follows the same pattern as seren_loci.mcp.server, seren_memory.mcp.server,
seren_probe.mcp.server, and seren_workbench.mcp.server.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI

from ..config import LodestarConfig

log = logging.getLogger("seren_lodestar.mcp")


def mount_mcp_routes(app: FastAPI) -> Any:
    """Mount a FastMCP server at /mcp on the given FastAPI app.

    Returns the FastMCP server instance (or None on failure) so the caller
    can access session_manager for the lifespan task group.

    Requires the ``mcp`` package (>=1.3.0) — if missing, this function
    raises ImportError, which the caller catches gracefully.
    """
    from mcp.server.fastmcp import FastMCP

    # ── Create the FastMCP server ─────────────────────────────────────────
    mcp_server = FastMCP("SerenLodestar", log_level="WARNING")

    # ── Tool: cluster-refresh ────────────────────────────────────────────
    @mcp_server.tool()
    async def cluster_refresh(target_node: str | None = None) -> dict:
        """Refresh the cluster topology — all nodes or a single named node.

        Args:
            target_node: Optional node name to refresh. If None, refreshes all.
        Returns:
            A dict with node status, installed services, and errors.
        """
        cluster = getattr(app.state, "cluster", None)
        if cluster is None:
            return {"ok": False, "error": "cluster client not initialized"}

        if target_node:
            snap = await cluster.refresh_node_async(target_node)
            if snap is None:
                return {"ok": False, "error": f"unknown node: {target_node}"}
            return {
                "ok": True,
                "node": target_node,
                "online": snap.online,
                "services": snap.installed_services,
                "last_error": snap.last_error,
            }
        else:
            summary = await cluster.refresh_async()
            return {
                "ok": True,
                "total": summary.total_nodes,
                "online": summary.online_nodes,
                "nodes": {
                    name: {"online": snap.online,
                           "services": snap.installed_services,
                           "last_error": snap.last_error}
                    for name, snap in summary.per_node.items()
                },
            }

    # ── Tool: service-lifecycle ──────────────────────────────────────────
    @mcp_server.tool()
    async def service_control(
        action: str,
        service: str,
        target_node: str | None = None,
    ) -> dict:
        """Start, stop, or restart a service across the cluster.

        Args:
            action: One of 'start', 'stop', 'restart', 'status', 'health'.
            service: Service name (llama, kokoro, comfy, whisper, coral, etc.)
            target_node: Optional node name. If None, uses cluster routing.
        Returns:
            Result dict with node name and action outcome.
        """
        from ..routes.services import _resolve_agent, _resolve_per_node_agent
        cluster = getattr(app.state, "cluster", None)
        if cluster is None:
            return {"ok": False, "error": "cluster client not initialized"}

        if action not in ("start", "stop", "restart", "status", "health"):
            return {"ok": False, "error": f"unknown action '{action}'"}

        if target_node:
            agent, err = _resolve_per_node_agent(cluster, target_node, service)
        else:
            agent, err = _resolve_agent(cluster, service)

        if agent is None:
            return {"ok": False, "error": err.status_code, "detail": err.body.get("detail")}

        actions = {
            "start": agent.start_service_async,
            "stop": agent.stop_service_async,
            "restart": agent.restart_service_async,
            "status": agent.get_service_status_async,
            "health": agent.get_service_health_async,
        }
        result = await actions[action](service)
        if result is None:
            return {"ok": False, "error": f"agent on '{agent.node_name}' did not respond"}
        return {"ok": True, "node": agent.node_name, "action": action, "result": result}

    # ── Tool: scheduler ──────────────────────────────────────────────────
    @mcp_server.tool()
    async def scheduler_list() -> dict:
        """List all scheduled tasks."""
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is None:
            return {"ok": False, "error": "scheduler not initialized"}
        tasks = await scheduler.list_async()
        return {
            "ok": True,
            "tasks": [
                {
                    "name": t.name,
                    "tool_name": t.tool_name,
                    "schedule_type": t.schedule_type,
                    "next_fire_at": t.next_fire_at.isoformat() if t.next_fire_at else None,
                    "recurring": t.recurring,
                    "paused": t.paused,
                }
                for t in tasks
            ],
        }

    @mcp_server.tool()
    async def scheduler_add(
        name: str,
        tool_name: str,
        schedule_type: str,
        cron_expression: str = "",
        relative_offset: str = "",
        description: str = "",
        tool_args_json: str = "{}",
    ) -> dict:
        """Add a scheduled task.

        Args:
            name: Unique task name.
            tool_name: Which tool to call when the task fires.
            schedule_type: 'cron' or 'relative'.
            cron_expression: Cron expression (required if schedule_type='cron').
            relative_offset: Relative offset like '2h', '30m' (required if relative).
            description: Optional description.
            tool_args_json: JSON string of tool arguments.
        """
        from datetime import datetime, timezone, timedelta
        from ..scheduling import ScheduledTask
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is None:
            return {"ok": False, "error": "scheduler not initialized"}

        now = datetime.now(timezone.utc)

        if schedule_type == "cron":
            if not cron_expression:
                return {"ok": False, "error": "cron_expression required"}
            try:
                from croniter import croniter
                it = croniter(cron_expression, now)
                next_fire = it.get_next(datetime).replace(tzinfo=timezone.utc)
            except Exception as ex:
                return {"ok": False, "error": f"invalid cron: {ex}"}
            recurring = True
        elif schedule_type == "relative":
            from ..routes.scheduler import _parse_offset
            total_seconds = _parse_offset(relative_offset)
            if total_seconds is None or total_seconds <= 0:
                return {"ok": False, "error": f"can't parse offset '{relative_offset}'"}
            next_fire = now + timedelta(seconds=total_seconds)
            recurring = False
        else:
            return {"ok": False, "error": f"unknown schedule_type '{schedule_type}'"}

        task = ScheduledTask(
            name=name,
            description=description,
            tool_name=tool_name,
            tool_args_json=tool_args_json,
            schedule_type=schedule_type,
            cron_expression=cron_expression,
            next_fire_at=next_fire,
            recurring=recurring,
        )
        try:
            created = await scheduler.add_async(task)
            return {"ok": True, "task": {"name": created.name,
                                         "next_fire_at": created.next_fire_at.isoformat()}}
        except ValueError as ex:
            return {"ok": False, "error": str(ex)}

    @mcp_server.tool()
    async def scheduler_remove(name: str) -> dict:
        """Remove a scheduled task by name."""
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is None:
            return {"ok": False, "error": "scheduler not initialized"}
        removed = await scheduler.remove_async(name)
        if not removed:
            return {"ok": False, "error": f"no task named '{name}'"}
        return {"ok": True, "removed": name}

    # ── Tool: cluster-capabilities ───────────────────────────────────────
    @mcp_server.tool()
    async def cluster_capabilities() -> dict:
        """Return the capability map — which services are on which nodes."""
        cluster = getattr(app.state, "cluster", None)
        if cluster is None:
            return {"ok": False, "error": "cluster client not initialized"}
        caps = cluster.get_capabilities()
        return {"ok": True, "capabilities": caps}

    # -- the three FastMCP-into-FastAPI transport fixes (same as Workbench) --
    if hasattr(mcp_server.settings, "streamable_http_path"):
        mcp_server.settings.streamable_http_path = "/"
    _apply_transport_security(mcp_server)

    asgi_app = mcp_server.streamable_http_app()   # creates session_manager; app.py enters it
    app.mount("/mcp", asgi_app)
    log.info("MCP server mounted at /mcp")
    return mcp_server

def _apply_transport_security(mcp_server) -> None:
    """DNS-rebinding host check from env, defaulting OFF for trusted-LAN."""
    if not hasattr(mcp_server.settings, "transport_security"):
        return
    try:
        from mcp.server.transport_security import TransportSecuritySettings
    except Exception:
        return
    def _split(name):
        import os
        return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]
    hosts = _split("SEREN_LODESTAR_ALLOWED_HOSTS")
    origins = _split("SEREN_LODESTAR_ALLOWED_ORIGINS")
    if hosts or origins:
        if not origins:
            origins = [f"http://{h}" for h in hosts] + [f"https://{h}" for h in hosts]
        mcp_server.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)
    else:
        mcp_server.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False)