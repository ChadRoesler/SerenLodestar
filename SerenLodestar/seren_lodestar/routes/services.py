"""
Service routes — /api/v1/service/{name}/* lifecycle verbs.

Coordinates service lifecycle (start, stop, restart, status, health) across
the cluster nodes.

ENSURE / RELEASE (seren_sinew.orchestration):

    POST /api/v1/service/{service}/ensure     "I need this up" - Lodestar picks
         the node, the node's Observatory starts the service and waits until
         it answers, and the reply says READY and where to send requests.
    POST /api/v1/service/{service}/release    "I am done" - the service is
         stopped when its last holder lets go, if an ensure is what started it
         (or, for an app with a run policy, when the policy says).
    GET  /api/v1/cluster/leases               who holds what
    GET  /api/v1/orchestration                policies, leases, apps, what was done
    POST /api/v1/orchestration/check          apply the policies now

    A short name ('probe') ensures the one install of it; see
    seren_lodestar.run_as_needed for the policies.

    hippocampus => Lodestar => Observatory => start llama => the Observatory
    waits until llama is up => Lodestar => the hippocampus does its work.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from ..cluster import JetsonClusterClient

API_VERSION = "v1"

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
    """The node has to exist; the service has to be one that node reports.

    There used to be a hardcoded KNOWN_SERVICES set here. It still named
    'chroma' (retired) and 'agent' (renamed), and it did not name a single
    systemd or docker service the Observatory manages, so the per-node path
    404'd on searxng while the routed path worked. The node's own last
    snapshot is the list; if there is no snapshot yet, the Observatory
    answers for itself.
    """
    agent = cluster.get_agent(node)
    if agent is None:
        return None, JSONResponse(
            {"error": "unknown_node", "detail": f"'{node}' is not in the cluster config"},
            status_code=404,
        )
    snap = cluster.get_snapshots().get(node)
    if snap is not None and snap.installed_services and svc not in snap.installed_services:
        return None, JSONResponse(
            {"error": "unknown_service",
             "detail": f"'{node}' does not report a service named '{svc}'",
             "installed": sorted(snap.installed_services)},
            status_code=404,
        )
    return agent, None


# ── Ensure / release: a service is up while someone holds it ─────────────

def _steward(request: Request):
    """The lease book and the run policies (seren_lodestar.run_as_needed). An
    app built without one (a test, a bare FastAPI) gets the default: an
    in-memory book, unmanaged."""
    from seren_sinew.orchestration import Policies
    from ..run_as_needed import Steward
    st = getattr(request.app.state, "steward", None)
    if st is None:
        st = request.app.state.steward = Steward(lambda: request.app.state.cluster, Policies())
    return st


def _leases(request: Request):
    return _steward(request).leases


def _resolve_name(request: Request, cluster, service: str) -> tuple[str, str]:
    """(service, error). A name some node has is itself. Otherwise a short
    name ('probe') is the one install of it - two installs is a question
    back, not a guess."""
    if cluster.choose_node_for(service) is not None or not hasattr(cluster, "get_capabilities"):
        return service, ""
    name, err = _steward(request).resolve(service)
    return (name or service), (err or "")


def _service_base_url(agent, port: int, request: Request) -> str:
    """Where a caller reaches a service on this node: the host Lodestar
    reaches the node's Observatory on, and the service's own port. A node
    configured by loopback is this box, and a caller on another box cannot
    use 127.0.0.1 - so it gets the host IT reached Lodestar on."""
    host = urlsplit(agent.base_url).hostname or ""
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if loopback:
        host = request.url.hostname or host
    return f"http://{host}:{port}" if host and port else ""


@router.post(f"/api/{API_VERSION}/service/{{service}}/ensure")
async def service_ensure(request: Request, service: str, body: dict = Body(default={})):
    from seren_sinew.orchestration import EnsureRequest, EnsureResult
    cluster: JetsonClusterClient = request.app.state.cluster
    req = EnsureRequest.from_dict(body)
    holder = req.holder or "anonymous"
    steward = _steward(request)
    book = steward.leases
    if not req.node:
        service, err = _resolve_name(request, cluster, service)
        if err:
            return EnsureResult.failed(service, err).to_dict()
    # A holder that already has it somewhere is asked about on THAT node.
    node_name = req.node or book.node_of(service, holder)
    if node_name:
        agent = cluster.get_agent(node_name)
        if agent is None:
            return EnsureResult.failed(service, f"'{node_name}' is not in the cluster config", node_name).to_dict()
    else:
        node = cluster.choose_node_for(service)
        agent = cluster.get_agent(node.name) if node is not None else None
        if agent is None:
            return EnsureResult.failed(service, f"no online node has '{service}' installed").to_dict()
    out = await agent.ensure_service_async(service, req)
    if out.ok and out.ready:
        out.base_url = _service_base_url(agent, out.port, request)
        out.holders = book.acquire(agent.node_name, service, holder, started=out.started,
                                   ttl=req.ttl_seconds or None)
        steward.save()
    else:
        cluster.mark_node_suspect(agent.node_name, f"ensure '{service}' failed: {out.error}") \
            if "did not answer in time" in (out.error or "") else None
    return out.to_dict()


@router.post(f"/api/{API_VERSION}/service/{{service}}/release")
async def service_release(request: Request, service: str, body: dict = Body(default={})):
    from seren_sinew.orchestration import ReleaseRequest, ReleaseResult
    cluster: JetsonClusterClient = request.app.state.cluster
    req = ReleaseRequest.from_dict(body)
    holder = req.holder or "anonymous"
    steward = _steward(request)
    book = steward.leases
    if not req.node:
        service, _err = _resolve_name(request, cluster, service)
    node_name = req.node or book.node_of(service, holder)
    if not node_name:
        return ReleaseResult(ok=True, service=service).to_dict()        # nothing held: nothing to do
    # Stop now only under the default policy with no idle grace (what a
    # release always did). Anything else lingers, idle from now, and the
    # steward's next check applies the app's policy.
    policy = steward.policy(service)
    at_once = policy.mode == "unmanaged" and policy.idle_minutes <= 0
    left, should_stop = book.release(node_name, service, holder, linger=not at_once)
    steward.save()
    out = ReleaseResult(ok=True, service=service, node=node_name, holders=left)
    if should_stop and at_once:
        agent = cluster.get_agent(node_name)
        r = await agent.stop_service_async(service) if agent is not None else None
        if r is None or not getattr(r, "ok", False):
            out.ok = False
            out.error = (f"the last holder let go and '{service}' on '{node_name}' could not be stopped: "
                         f"{getattr(r, 'error', None) or getattr(r, 'stderr', None) or 'the node did not answer'}")
        else:
            out.stopped = True
    return out.to_dict()


@router.get(f"/api/{API_VERSION}/cluster/leases")
async def cluster_leases(request: Request):
    return {"ok": True, "leases": _leases(request).snapshot()}


@router.get(f"/api/{API_VERSION}/orchestration")
async def orchestration_status(request: Request):
    """The run policies, the leases, every app's state and policy, and what
    the steward started and stopped lately."""
    return {"ok": True, **_steward(request).describe()}


@router.post(f"/api/{API_VERSION}/orchestration/check")
async def orchestration_check(request: Request):
    """Apply the policies now instead of at the next check."""
    return {"ok": True, "done": await _steward(request).check()}


# ── Cluster-routed service endpoints ─────────────────────────────────────

@router.get(f"/api/{API_VERSION}/service/{{service}}/manifest")
async def service_manifest(request: Request, service: str):
    cluster: JetsonClusterClient = request.app.state.cluster
    agent, err = _resolve_agent(cluster, service)
    if agent is None:
        return err
    m = await agent.get_service_manifest_async(service)
    if m is None:
        cluster.mark_node_suspect(agent.node_name, f"manifest fetch failed for '{service}'")
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
        cluster.mark_node_suspect(agent.node_name, f"status fetch failed for '{service}'")
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
        cluster.mark_node_suspect(agent.node_name, f"health fetch failed for '{service}'")
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
