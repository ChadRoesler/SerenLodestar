"""
Info routes — GET /, GET /health.

Service info + liveness endpoint.

`/` also carries the update status. It's a PUBLIC route (no bearer), which is
fine because it already published the running version — `updates` adds the
comparison, not the disclosure. If you'd rather not advertise "and it's out of
date" to an unauthenticated caller, set `updates.enabled: false` and read the
badge from the dashboard instead.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Request

from .._version import __version__ as _fallback_version
from seren_meninges import get_version
from seren_meninges.updates import updates_payload

APP_VERSION = get_version("seren-lodestar", fallback=_fallback_version)

router = APIRouter(tags=["info"])



@router.get("/")
async def root(request: Request):
    return {
        "service": "SerenLodestar",
        "version": APP_VERSION,
        "status": "ok",
        "updates": await updates_payload(
            getattr(request.app.state, "updates", None),
            distribution="seren-lodestar", installed=APP_VERSION),
    }


@router.get("/health")
async def health():
    return {"ok": True, "ts": time.time()}
