"""
Upgrades across the cluster - /api/v1/upgrade (seren_lodestar.upgrade).

    POST /api/v1/upgrade            start a run; 202 with it (dry_run: 200 with every node's plan)
        {"nodes": ["nuc"], "services": ["memory", "loci"],   both optional: everything seren online
         "only_behind": true,                                default: leave what is on the newest alone
         "versions": {"memory": "5.2.0"},                    pins, per app; the rest go to their newest
         "version": "5.2.0",                                 a pin for a run of ONE app (else 400)
         "with": ["seren-sinew", ...],                       default: the Observatory's (sinew + meninges)
         "backup": true,                                     a fresh copy into the stash before each
         "dry_run": false, "reason": "..."}
    GET  /api/v1/upgrade            the run going now, and the recent ones
    GET  /api/v1/upgrade/behind     every seren service, its version, the index's newest
                                    (?refresh=true asks the index now; ?nodes=&services= narrow it)
    GET  /api/v1/upgrade/{run_id}   one run: every node/service step, its job, before and after
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

API_VERSION = "v1"

router = APIRouter(tags=["upgrade"])


def _service(request: Request):
    return getattr(request.app.state, "upgrades", None)


def _names(v) -> list[str] | None:
    if v is None or v == "":
        return None
    if isinstance(v, str):
        v = [v]
    return [str(x) for x in v if str(x).strip()] or None


@router.post(f"/api/{API_VERSION}/upgrade")
async def upgrade_start(request: Request, body: dict = Body(default={})):
    svc = _service(request)
    if svc is None:
        return JSONResponse({"ok": False, "error": "upgrades are not available"}, status_code=404)
    body = body or {}
    version = body.get("version") or None
    versions = body.get("versions")
    if versions is not None and not (isinstance(versions, dict)
                                     and all(isinstance(v, str) or v is None for v in versions.values())):
        return JSONResponse({"ok": False, "error": "versions is {app: version}"}, status_code=400)
    with_ = body.get("with") if isinstance(body.get("with"), list) else None
    nodes, services = _names(body.get("nodes")), _names(body.get("services"))
    only_behind = bool(body.get("only_behind", True))
    if body.get("dry_run"):
        out = await svc.plan(nodes=nodes, services=services, version=version, with_=with_,
                             versions=versions, only_behind=only_behind)
        return JSONResponse(out, status_code=200 if out.get("ok") or "nodes" in out else 400)
    status, out = svc.start(nodes=nodes, services=services, version=version, with_=with_,
                            backup=bool(body.get("backup", True)),
                            reason=str(body.get("reason") or "by hand"),
                            versions=versions, only_behind=only_behind)
    return JSONResponse(out, status_code=status)


@router.get(f"/api/{API_VERSION}/upgrade")
async def upgrade_status(request: Request):
    svc = _service(request)
    if svc is None:
        return JSONResponse({"ok": False, "error": "upgrades are not available"}, status_code=404)
    return {"ok": True, **svc.describe()}


@router.get(f"/api/{API_VERSION}/upgrade/behind")
async def upgrade_behind(request: Request, refresh: bool = False, nodes: str = "", services: str = ""):
    svc = _service(request)
    if svc is None:
        return JSONResponse({"ok": False, "error": "upgrades are not available"}, status_code=404)
    return await svc.behind(nodes=_names(nodes.split(",")) if nodes else None,
                            services=_names(services.split(",")) if services else None, refresh=refresh)


@router.get(f"/api/{API_VERSION}/upgrade/{{run_id}}")
async def upgrade_run(request: Request, run_id: str):
    svc = _service(request)
    run = svc.load(run_id) if svc else None
    if run is None:
        return JSONResponse({"ok": False, "error": f"no upgrade run {run_id}"}, status_code=404)
    return {"ok": True, "run": run}
