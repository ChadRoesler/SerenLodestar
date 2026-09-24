"""
System routes — /api/v1/system/* endpoints.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..cluster import JetsonClusterClient

API_VERSION = "v1"

router = APIRouter(tags=["system"])


async def _safe_get(coro):
    try:
        return await coro
    except Exception:
        return None


def _register_system_routes(app_state):
    """These routes need cluster client from app state, wired in lifespan."""
    pass


@router.get(f"/api/{API_VERSION}/system/ping")
async def system_ping():
    return {"ok": True, "ts": int(datetime.now(timezone.utc).timestamp())}


@router.get(f"/api/{API_VERSION}/system/version")
async def system_version(request: Request):
    from .info import APP_VERSION
    return {
        "runtime_version": APP_VERSION,
        "api_version": API_VERSION,
    }


@router.get(f"/api/{API_VERSION}/system/status")
async def system_status(request: Request):
    cluster: JetsonClusterClient = request.app.state.cluster

    async def fetch_node(name: str, agent):
        snap = cluster.get_snapshots().get(name)
        node_task = _safe_get(agent.get_node_async())
        thermal_task = _safe_get(agent.get_thermal_async())
        services_task = _safe_get(agent.get_services_async())
        results = await asyncio.gather(node_task, thermal_task, services_task)
        all_failed = all(r is None for r in results)
        snap_online = snap.online if snap else False
        online = snap_online and not all_failed
        if all_failed and snap_online:
            cluster.mark_node_suspect(name, "all status fetches failed during aggregate query")
        return {
            "name": name,
            "nickname": agent.nickname,
            "is_host": agent.is_host,
            "online": online,
            "suspect": cluster.suspect_reason(name),
            "last_probed": snap.last_probed if snap else None,
            "last_error": snap.last_error if snap else None,
            "installed_services": snap.installed_services if snap else [],
            "agent_node": results[0],
            "thermal": results[1],
            "services_detail": results[2].services if results[2] else None,
        }

    per_node = await asyncio.gather(
        *[fetch_node(name, agent) for name, agent in cluster.agents.items()]
    )
    return {
        "nodes": list(per_node),
        "node_count": len(per_node),
        "online_count": sum(1 for n in per_node if n["online"]),
    }


@router.get(f"/api/{API_VERSION}/system/health")
async def system_health(request: Request):
    cluster: JetsonClusterClient = request.app.state.cluster
    per_node = await asyncio.gather(
        *[_safe_get(agent.ping_async()) for agent in cluster.agents.values()]
    )
    node_names = list(cluster.agents.keys())
    unreachable = [
        node_names[i] for i, r in enumerate(per_node) if r is None or not r.ok
    ]
    all_ok = len(unreachable) == 0
    payload = {
        "ok": all_ok,
        "status": "ok" if all_ok else "degraded",
        "total": len(per_node),
        "reachable": len(per_node) - len(unreachable),
        "unreachable": unreachable,
    }
    health_strict = request.app.state.config.cluster.health_strict_mode
    if not all_ok and health_strict:
        return JSONResponse(payload, status_code=503)
    return JSONResponse(payload)


async def _optional_json(request: Request) -> dict:
    """The body, if one was sent and it parses; {} otherwise. Never raises."""
    try:
        length = int(request.headers.get("content-length") or 0)
    except ValueError:
        length = 0
    if length <= 0:
        return {}
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


@router.post(f"/api/{API_VERSION}/system/reclaim")
async def system_reclaim(request: Request):
    """Ask every node (or `nodes`) to give the GPU back.

    Body (optional), forwarded to each Observatory:
        exclude: [str]   never stop these
        include: [str]   also stop these non-GPU (systemd / docker) services
        all:     bool    stop everything stoppable except `exclude` and the
                         Observatory itself - what this used to do silently
        nodes:   [str]   only these nodes

    The default stops only the GPU daemons on each node. It used to stop the
    entire constellation - Lodestar included, from Lodestar's own button.
    """
    cluster: JetsonClusterClient = request.app.state.cluster
    body = await _optional_json(request)
    exclude = body.get("exclude")
    include = body.get("include")
    everything = bool(body.get("all", False))
    nodes_filter = body.get("nodes")
    targets = list(cluster.agents.items())
    if nodes_filter:
        targets = [(n, a) for n, a in targets if n in nodes_filter]
    per_node = await asyncio.gather(
        *[_safe_get(agent.reclaim_async(exclude, include=include, everything=everything))
          for _, agent in targets]
    )
    results = []
    all_ok = True
    for (name, _), resp in zip(targets, per_node):
        if resp is None:
            all_ok = False
        results.append({
            "node": name,
            "ok": resp is not None,
            "stopped": resp.stopped if resp else [],
            "kept": resp.kept if resp else [],
            "failed": resp.failed if resp else [],
        })
    return {"ok": all_ok, "nodes": results}


@router.post(f"/api/{API_VERSION}/system/reboot/{{node}}")
async def reboot_node(request: Request, node: str):
    cluster: JetsonClusterClient = request.app.state.cluster
    agent = cluster.get_agent(node)
    if agent is None:
        return JSONResponse({"error": f"unknown node '{node}'"}, status_code=404)
    delay = 1
    body = await _optional_json(request)
    if "delay_minutes" in body:
        try:
            delay = int(body["delay_minutes"])
        except (TypeError, ValueError):
            return JSONResponse({"error": f"delay_minutes must be a whole number, got {body['delay_minutes']!r}"},
                                status_code=400)
    resp = await agent.reboot_async(delay)
    if resp is None:
        return JSONResponse({
            "node": node, "scheduled": False, "error": "agent unreachable or returned no body",
        }, status_code=502)
    return {
        "node": node,
        "scheduled": resp.scheduled,
        "scheduled_at": resp.scheduled_at,
        "delay_minutes": resp.delay_minutes,
        "method": resp.method,
        "error": resp.error,
        "hint": resp.hint,
    }


@router.post(f"/api/{API_VERSION}/system/reboot/{{node}}/cancel")
async def reboot_cancel(request: Request, node: str):
    cluster: JetsonClusterClient = request.app.state.cluster
    agent = cluster.get_agent(node)
    if agent is None:
        return JSONResponse({"error": f"unknown node '{node}'"}, status_code=404)
    resp = await agent.reboot_cancel_async()
    if resp is None:
        return JSONResponse({
            "node": node, "cancelled": False, "error": "agent unreachable or returned no body",
        }, status_code=502)
    return {"node": node, "cancelled": resp.cancelled, "error": resp.error}
