"""
Backups - /api/v1/backup/*.

    GET  /api/v1/backup          what is stashed, per node and service, and the last pull
    POST /api/v1/backup/pull     pull now: {"node": "...", "service": "...", "take_fresh": true, "reason": "..."}

No restore route and no delete route: see seren_lodestar.backup.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

API_VERSION = "v1"

router = APIRouter(tags=["backup"])


def _service(request: Request):
    return getattr(request.app.state, "backup", None)


@router.get(f"/api/{API_VERSION}/backup")
async def backup_status(request: Request):
    svc = _service(request)
    if svc is None:
        return JSONResponse({"ok": False, "error": "backups are off (backup.enabled: false)"}, status_code=404)
    return {"ok": True, **svc.describe()}


@router.post(f"/api/{API_VERSION}/backup/pull")
async def backup_pull(request: Request, body: dict = Body(default={})):
    svc = _service(request)
    if svc is None:
        return JSONResponse({"ok": False, "error": "backups are off (backup.enabled: false)"}, status_code=404)
    body = body or {}
    rep = await svc.pull(reason=str(body.get("reason") or "by hand")[:200], node=body.get("node") or None,
                         service=body.get("service") or None,
                         take_fresh=body.get("take_fresh") if "take_fresh" in body else None)
    status = 409 if rep.errors == ["a pull is already running"] else 200
    return JSONResponse({"ok": not rep.errors, **rep.as_dict()}, status_code=status)
