"""
Listening - words about a sound, through the cluster.

    POST /api/v1/listen            multipart: file (a WAV), prompt?, holder?, max_new_tokens?
                                   -> {ok, text, node, seconds, clip_seconds, model}
    POST /api/v1/listen/compare    multipart: a, b (two WAVs), prompt?, holder?
                                   -> the same, about both

Hearing (`/hear`) says what was said; this says how it sounded. The
listener is an audio-language model (Qwen3-Omni, thinker only) on a node
with the memory for it; a caller sends a clip and a question - how does
this voice sound, what is this song doing, how do these two takes differ -
and gets words back. Woken the way anything is woken (ensure, a lease in
the caller's name), asked through the node's Observatory, let go after; its
run policy decides when it sleeps.
"""
from __future__ import annotations

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from .services import service_ensure, service_release

API_VERSION = "v1"
SERVICE = "listener"
LISTEN_TIMEOUT = 240.0
WAKE_SECONDS = 600              # a 30B model coming off the NVMe takes minutes the first time
MAX_CLIP_BYTES = 64 * 1024 * 1024

router = APIRouter(tags=["listening"])


async def _clip(up: UploadFile, name: str) -> tuple[bytes | None, JSONResponse | None]:
    data = await up.read()
    if not data:
        return None, JSONResponse({"error": f"send the clip as multipart `{name}`"}, status_code=400)
    if len(data) > MAX_CLIP_BYTES:
        return None, JSONResponse({"error": f"the clip is bigger than {MAX_CLIP_BYTES // 1048576} MB"}, status_code=413)
    return data, None


async def _through(request: Request, holder: str, node: str, call):
    """Wake the listener, run `call(agent)` -> (status, body), let go."""
    holder = (holder or "listen").strip()[:64]
    cluster = request.app.state.cluster
    ask = {"holder": holder, "reason": "listening", "wait_seconds": WAKE_SECONDS}
    if node:
        ask["node"] = node
    up = await service_ensure(request, SERVICE, ask)
    agent = cluster.get_agent(up.get("node") or "") if isinstance(up, dict) else None
    if not up.get("ok") or not up.get("ready") or agent is None:
        if up.get("started") and agent is not None:
            await service_release(request, SERVICE, {"holder": holder, "node": agent.node_name})
        why = up.get("error") or "the listener did not come up"
        status = 503 if "no online node" in why or "did not come up" in why else 502
        return JSONResponse({"error": why, "node": up.get("node")}, status_code=status)
    try:
        status, out = await call(agent)
    finally:
        await service_release(request, SERVICE, {"holder": holder, "node": agent.node_name})
    if status != 200 or not isinstance(out, dict) or not out.get("ok"):
        detail = out if isinstance(out, dict) else {"error": str(out)}
        return JSONResponse({"error": detail.get("error") or detail.get("detail") or "the listener could not answer",
                             "detail": detail.get("detail"), "node": agent.node_name}, status_code=502)
    return {"ok": True, "text": out.get("text", ""), "node": agent.node_name, "seconds": out.get("seconds"),
            "clip_seconds": out.get("clip_seconds"), "cut": out.get("cut", False), "model": out.get("model")}


@router.post(f"/api/{API_VERSION}/listen")
async def listen(request: Request, file: UploadFile = File(...), prompt: str = Form(""), holder: str = Form(""),
                 node: str = Form(""), max_new_tokens: int = Form(400)):
    """What does this sound like? `prompt` is the question (the listener has
    a good default: a voice's pitch, tone, timbre, pace, warmth...)."""
    data, err = await _clip(file, "file")
    if err:
        return err
    return await _through(request, holder, node, lambda agent: agent.listen_async(
        {"file": (file.filename or "clip.wav", data, "audio/wav")}, prompt, "listen", max_new_tokens, LISTEN_TIMEOUT))


@router.post(f"/api/{API_VERSION}/listen/compare")
async def listen_compare(request: Request, a: UploadFile = File(...), b: UploadFile = File(...), prompt: str = Form(""),
                         holder: str = Form(""), node: str = Form(""), max_new_tokens: int = Form(500)):
    """How do these two differ? A then B, one answer about both."""
    data_a, err = await _clip(a, "a")
    if err:
        return err
    data_b, err = await _clip(b, "b")
    if err:
        return err
    files = {"a": (a.filename or "a.wav", data_a, "audio/wav"), "b": (b.filename or "b.wav", data_b, "audio/wav")}
    return await _through(request, holder, node, lambda agent: agent.listen_async(
        files, prompt, "compare", max_new_tokens, LISTEN_TIMEOUT))
