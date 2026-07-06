"""
Info routes — GET /, GET /health.

Service info + liveness endpoint.
"""
from __future__ import annotations

import time

from fastapi import APIRouter

from .._version import __version__ as _fallback_version
from seren_meninges import get_version

APP_VERSION = get_version("seren-lodestar", fallback=_fallback_version)

router = APIRouter(tags=["info"])


@router.get("/")
async def root():
    return {
        "service": "SerenLodestar",
        "version": APP_VERSION,
        "status": "ok",
    }


@router.get("/health")
async def health():
    return {"ok": True, "ts": time.time()}
