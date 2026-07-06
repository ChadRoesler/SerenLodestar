"""
Service routes — /api/v1/service/{name}/* lifecycle verbs.

Coordinates service lifecycle (start, stop, restart, status, health) across
the cluster nodes.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..cluster import JetsonClusterClient

API_VERSION = "v1"

# Known service names that Lodestar routes
KNOWN_SERVICES = {
    "llama", "kokoro", "comfy", "chroma", "whisper", "coral", "agent",
}

router = APIRouter(tags=["services"])


def _resolve_agent(cluster, service: str):
    node = cluster.choose_node_for(service)
    if node is None:
        return None, JSONResponse(
            {"error": "service_unavailable",
             "detail": f"no online node has '{service}' installed"},
            status_code=503,
        )
    agent = cluster.get_agent(node.name)
    if agent is None:
        return None, JSONResponse(
            {"error": "service_unavailable",
             "detail": f"no online node has '{service}' installed"},
            status_code=503,
        )
    return agent, None


def _resolve_per_node_agent(cluster, node: str, svc: str):
    if svc not in KNOWN_SERVICES:
        return None, JSONResponse(
            {"error": "unknown_service", "detail": f"'{svc}' is not a known service"},
            status_code=404,
        )
    agent = cluster.get_agent(node)
    if agent is None:
        return None, JSONResponse(
            {"error": "unknown_node", "detail": f"'{node}' is not in the cluster config"},
            status_code=404,
        )
    return agent, None


# ── Cluster-routed service endpoints ─────────────────────────────────────

@router.get(f"/api/{API_VERSION}/service/{{service}}/manifest")
async def service_manifest(request: Request, service: str):
    cluster: JetsonClusterClient = request.app.state.cluster
    agent, err = _resolve_agent(cluster, service)
    if agent is None:
        return err
    m = await agent.get_service_manifest_async(service)
    if m is None:
        cluster.mark_node_offline(agent.node_name, f"manifest fetch failed for '{service}'")
        return JSONResponse(
            {"error": "agent_unreachable",
             "detail": f"agent on '{agent.node_name}' did not respond"},
            status_code=503,
        )
    return {"node": agent.node_name, "manifest": m}


@router.get(f"/api/{API_VERSION}/service/{{service}}/status")
async def service_status(request: Request, service: str):
    cluster: JetsonClusterClient = request.app.state.cluster
    agent, err = _resolve_agent(cluster, service)
    if agent is None:
        return err
    s = await agent.get_service_status_async(service)
    if s is None:
        cluster.mark_node_offline(agent.node_name, f"status fetch failed for '{service}'")
        return JSONResponse(
            {"error": "agent_unreachable",
             "detail": f"agent on '{agent.node_name}' did not respond"},
            status_code=503,
        )
    return {"node": agent.node_name, "status": s}


@router.get(f"/api/{API_VERSION}/service/{{service}}/health")
async def service_health(request: Request, service: str):
    cluster: JetsonClusterClient = request.app.state.cluster
    agent, err = _resolve_agent(cluster, service)
    if agent is None:
        return err
    h = await agent.get_service_health_async(service)
    if h is None:
        cluster.mark_node_offline(agent.node_name, f"health fetch failed for '{service}'")
        return JSONResponse(
            {"error": "agent_unreachable",
             "detail": f"agent on '{agent.node_name}' did not respond"},
            status_code=503,
        )
    return {"node": agent.node_name, "health": h}


@router.post(f"/api/{API_VERSION}/service/{{service}}/start")
async def service_start(request: Request, service: str):
    cluster: JetsonClusterClient = request.app.state.cluster
    agent, err = _resolve_agent(cluster, service)
    if agent is None:
        return err
    r = await agent.start_service_async(service)
    if r is None:
        return JSONResponse(
            {"error": "agent_unreachable",
             "detail": f"agent on '{agent.node_name}' did not respond"},
            status_code=503,
        )
    return {"node": agent.node_name, "result": r}


@router.post(f"/api/{API_VERSION}/service/{{service}}/stop")
async def service_stop(request: Request, service: str):
    cluster: JetsonClusterClient = request.app.state.cluster
    agent, err = _resolve_agent(cluster, service)
    if agent is None:
        return err
    r = await agent.stop_service_async(service)
    if r is None:
        return JSONResponse(
            {"error": "agent_unreachable",
             "detail": f"agent on '{agent.node_name}' did not respond"},
            status_code=503,
        )
    return {"node": agent.node_name, "result": r}


@router.post(f"/api/{API_VERSION}/service/{{service}}/restart")
async def service_restart(request: Request, service: str):
    cluster: JetsonClusterClient = request.app.state.cluster
    agent, err = _resolve_agent(cluster, service)
    if agent is None:
        return err
    r = await agent.restart_service_async(service)
    if r is None:
        return JSONResponse(
            {"error": "agent_unreachable",
             "detail": f"agent on '{agent.node_name}' did not respond"},
            status_code=503,
        )
    return {"node": agent.node_name, "result": r}


@router.get(f"/api/{API_VERSION}/service/{{service}}/logs")
async def service_logs(request: Request, service: str, lines: int = 100):
    cluster: JetsonClusterClient = request.app.state.cluster
    agent, err = _resolve_agent(cluster, service)
    if agent is None:
        return err
    lines = max(1, min(lines, 10_000))
    l = await agent.get_service_logs_async(service, lines)
    if l is None:
        return JSONResponse(
            {"error": "agent_unreachable",
             "detail": f"agent on '{agent.node_name}' did not respond"},
            status_code=503,
        )
    return {"node": agent.node_name, "logs": l}


@router.get(f"/api/{API_VERSION}/service/{{service}}/models")
async def service_models(request: Request, service: str):
    cluster: JetsonClusterClient = request.app.state.cluster
    agent, err = _resolve_agent(cluster, service)
    if agent is None:
        return err
    models = await agent.get_service_models_async(service)
    if models is None:
        return JSONResponse(
            {"error": "agent_unreachable",
             "detail": f"agent on '{agent.node_name}' did not respond"},
            status_code=503,
        )
    return {"node": agent.node_name, "models": models}


# ── Per-node service endpoints ──────────────────────────────────────────

@router.get(f"/api/{API_VERSION}/node/{{node}}/service/{{svc}}/manifest")
async def node_service_manifest(request: Request, node: str, svc: str):
    cluster: JetsonClusterClient = request.app.state.cluster
    agent, err = _resolve_per_node_agent(cluster, node, svc)
    if agent is None:
        return err
    m = await agent.get_service_manifest_async(svc)
    if m is None:
        return JSONResponse(
            {"error": "agent_unreachable", "detail": f"agent on '{node}' did not respond"},
            status_code=503,
        )
    return {"node": node, "manifest": m}


@router.get(f"/api/{API_VERSION}/node/{{node}}/service/{{svc}}/status")
async def node_service_status(request: Request, node: str, svc: str):
    cluster: JetsonClusterClient = request.app.state.cluster
    agent, err = _resolve_per_node_agent(cluster, node, svc)
    if agent is None:
        return err
    s = await agent.get_service_status_async(svc)
    if s is None:
        return JSONResponse(
            {"error": "agent_unreachable", "detail": f"agent on '{node}' did not respond"},
            status_code=503,
        )
    return {"node": node, "status": s}


@router.get(f"/api/{API_VERSION}/node/{{node}}/service/{{svc}}/health")
async def node_service_health(request: Request, node: str, svc: str):
    cluster: JetsonClusterClient = request.app.state.cluster
    agent, err = _resolve_per_node_agent(cluster, node, svc)
    if agent is None:
        return err
    h = await agent.get_service_health_async(svc)
    if h is None:
        return JSONResponse(
            {"error": "agent_unreachable", "detail": f"agent on '{node}' did not respond"},
            status_code=503,
        )
    return {"node": node, "health": h}


@router.get(f"/api/{API_VERSION}/node/{{node}}/service/{{svc}}/logs")
async def node_service_logs(request: Request, node: str, svc: str, lines: int = 100):
    cluster: JetsonClusterClient = request.app.state.cluster
    agent, err = _resolve_per_node_agent(cluster, node, svc)
    if agent is None:
        return err
    lines = max(1, min(lines, 10_000))
    l = await agent.get_service_logs_async(svc, lines)
    if l is None:
        return JSONResponse(
            {"error": "agent_unreachable", "detail": f"agent on '{node}' did not respond"},
            status_code=503,
        )
    return {"node": node, "logs": l}


@router.get(f"/api/{API_VERSION}/node/{{node}}/service/{{svc}}/models")
async def node_service_models(request: Request, node: str, svc: str):
    cluster: JetsonClusterClient = request.app.state.cluster
    agent, err = _resolve_per_node_agent(cluster, node, svc)
    if agent is None:
        return err
    models = await agent.get_service_models_async(svc)
    if models is None:
        return JSONResponse(
            {"error": "agent_unreachable", "detail": f"agent on '{node}' did not respond"},
            status_code=503,
        )
    return {"node": node, "models": models}


@router.post(f"/api/{API_VERSION}/node/{{node}}/service/{{svc}}/start")
async def node_service_start(request: Request, node: str, svc: str):
    cluster: JetsonClusterClient = request.app.state.cluster
    agent, err = _resolve_per_node_agent(cluster, node, svc)
    if agent is None:
        return err
    r = await agent.start_service_async(svc)
    if r is None:
        return JSONResponse(
            {"error": "agent_unreachable", "detail": f"agent on '{node}' did not respond"},
            status_code=503,
        )
    return {"node": node, "result": r}


@router.post(f"/api/{API_VERSION}/node/{{node}}/service/{{svc}}/stop")
async def node_service_stop(request: Request, node: str, svc: str):
    cluster: JetsonClusterClient = request.app.state.cluster
    agent, err = _resolve_per_node_agent(cluster, node, svc)
    if agent is None:
        return err
    r = await agent.stop_service_async(svc)
    if r is None:
        return JSONResponse(
            {"error": "agent_unreachable", "detail": f"agent on '{node}' did not respond"},
            status_code=503,
        )
    return {"node": node, "result": r}


@router.post(f"/api/{API_VERSION}/node/{{node}}/service/{{svc}}/restart")
async def node_service_restart(request: Request, node: str, svc: str):
    cluster: JetsonClusterClient = request.app.state.cluster
    agent, err = _resolve_per_node_agent(cluster, node, svc)
    if agent is None:
        return err
    r = await agent.restart_service_async(svc)
    if r is None:
        return JSONResponse(
            {"error": "agent_unreachable", "detail": f"agent on '{node}' did not respond"},
            status_code=503,
        )
    return {"node": node, "result": r}
