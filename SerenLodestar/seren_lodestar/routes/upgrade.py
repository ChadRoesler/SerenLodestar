"""
Upgrades across the cluster - /api/v1/upgrade (seren_lodestar.upgrade).

    POST /api/v1/upgrade            start a run; 202 with it (dry_run: 200 with every node's plan)
        {"nodes": ["nuc"], "services": ["memory", "loci"],   both optional: everything seren online
         "only_behind": true,                                default: leave what is on the newest alone
         "libs": false,                                      true: and move a current one whose sinew or
                                                             meninges is behind (pinned where it is)
         "versions": {"memory": "5.2.0"},                    pins, per app; the rest go to their newest
         "version": "5.2.0",                                 a pin for a run of ONE app (else 400)
         "with": ["seren-sinew", ...],                       default: the Observatory's (sinew + meninges)
         "backup": true,                                     a fresh copy into the stash before each
         "dry_run": false, "reason": "..."}
    GET  /api/v1/upgrade            the run going now, and the recent ones
    GET  /api/v1/upgrade/behind     every seren service, its version, the index's newest, and the
                                    sinew and meninges its venv runs against the index (libs)
                                    (?refresh=true asks the index now; ?nodes=&services= narrow it)
    GET  /api/v1/upgrade/{run_id}   one run: every node/service step, its job, before and after
    GET  /api/v1/upgrade/{run_id}/step/{i}
                                    one step, with its job as that node's Observatory has it: every
                                    note the job took (plan, snapshot, install, preflight, restart...)
                                    with the pip tail or the error - what to read when a step failed
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
    libs = bool(body.get("libs", False))
    if body.get("dry_run"):
        out = await svc.plan(nodes=nodes, services=services, version=version, with_=with_,
                             versions=versions, only_behind=only_behind, libs=libs)
        return JSONResponse(out, status_code=200 if out.get("ok") or "nodes" in out else 400)
    status, out = svc.start(nodes=nodes, services=services, version=version, with_=with_,
                            backup=bool(body.get("backup", True)),
                            reason=str(body.get("reason") or "by hand"),
                            versions=versions, only_behind=only_behind, libs=libs)
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


@router.get(f"/api/{API_VERSION}/upgrade/{{run_id}}/step/{{index}}")
async def upgrade_step(request: Request, run_id: str, index: int):
    """One step of a run and the job behind it, fetched from the node now.
    20 Oct 2026: "we should be able to click the line and view the logs
    of what happened (this will help ts upgrade failures which are a giant
    pain my ass)". The job's `steps` are the log: each note says what was
    tried, whether it went, and carries the pip tail or the error."""
    svc = _service(request)
    run = svc.load(run_id) if svc else None
    if run is None:
        return JSONResponse({"ok": False, "error": f"no upgrade run {run_id}"}, status_code=404)
    steps = run.get("steps") or []
    if index < 0 or index >= len(steps):
        return JSONResponse({"ok": False, "error": f"run {run_id} has no step {index}"}, status_code=404)
    step = steps[index]
    job, note = None, None
    if not step.get("job"):
        note = (f"no job on the node: {step.get('error')}" if step.get("error")
                else f"skipped: {step.get('skipped')}" if step.get("skipped")
                else "already on its newest" if step.get("state") == "current"
                else "the Observatory was never asked")
    else:
        job = await svc.job_of(step)
        if job is None:
            note = f"the job {step['job']} is not on {step.get('node')} any more, or the node did not answer"
    return {"ok": True, "run_id": run_id, "index": index, "step": step, "job": job, "note": note}
