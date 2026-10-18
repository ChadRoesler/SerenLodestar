"""
Hearing - words from a clip, through the cluster.

    POST /api/v1/hear      multipart: file (a WAV), language?, holder?
                           -> {ok, text, node, seconds, model}

The ears of the house. A caller (Symposium's push-to-talk, a Workbench tool,
the hippocampus one day) sends a clip; Lodestar wakes whisper the way any
service is woken (ensure, a lease held by the caller), has the node's
Observatory hand the clip to its whisper-server, and lets go - whisper's run
policy decides when it is put away, so a conversation is not a start and a
stop per sentence.

What comes back is the words and nothing else. The clip itself is the
caller's to keep: Symposium keeps it beside the chat, so what was said is
not only what whisper made of it.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from .services import service_ensure, service_release

API_VERSION = "v1"
SERVICE = "whisper"
HEAR_TIMEOUT = 120.0
WAKE_SECONDS = 120
MAX_CLIP_BYTES = 64 * 1024 * 1024

router = APIRouter(tags=["hearing"])


@router.post(f"/api/{API_VERSION}/hear")
async def hear(request: Request, file: UploadFile = File(...), language: str = Form(""),
               holder: str = Form(""), node: str = Form("")):
    """Transcribe `file` on the first node with whisper that wakes. Holds
    whisper there as `holder` (default "hear") while it listens, and lets
    go after."""
    data = await file.read()
    if not data:
        return JSONResponse({"error": "send the clip as multipart `file`"}, status_code=400)
    if len(data) > MAX_CLIP_BYTES:
        return JSONResponse({"error": f"the clip is bigger than {MAX_CLIP_BYTES // 1048576} MB"}, status_code=413)
    holder = (holder or "hear").strip()[:64]
    cluster = request.app.state.cluster
    ask = {"holder": holder, "reason": "listening", "wait_seconds": WAKE_SECONDS}
    if node:
        ask["node"] = node
    up = await service_ensure(request, SERVICE, ask)
    agent = cluster.get_agent(up.get("node") or "") if isinstance(up, dict) else None
    if not up.get("ok") or not up.get("ready") or agent is None:
        if up.get("started") and agent is not None:
            await service_release(request, SERVICE, {"holder": holder, "node": agent.node_name})
        why = up.get("error") or "whisper did not come up"
        status = 503 if "no online node" in why or "did not come up" in why else 502
        return JSONResponse({"error": why, "node": up.get("node")}, status_code=status)
    try:
        status, out = await agent.transcribe_async(data, file.filename or "clip.wav", (language or "").strip()[:8],
                                                   timeout=HEAR_TIMEOUT)
    finally:
        await service_release(request, SERVICE, {"holder": holder, "node": agent.node_name})
    if status != 200 or not isinstance(out, dict) or not out.get("ok"):
        detail = out if isinstance(out, dict) else {"error": str(out)}
        return JSONResponse({"error": detail.get("error") or detail.get("detail") or "whisper could not hear it",
                             "detail": detail.get("detail"), "node": agent.node_name}, status_code=502)
    return {"ok": True, "text": out.get("text", ""), "node": agent.node_name,
            "seconds": out.get("seconds"), "model": out.get("model")}
