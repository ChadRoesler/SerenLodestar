"""
Speaking - words said in a voice, through the cluster.

    POST /api/v1/speak            JSON {text, voice?="wren", holder?, node?}
                                  -> audio/wav, with X-Seren-Node, X-Seren-Voice,
                                     X-Seren-Backend, X-Seren-Model, X-Seren-Seconds
                                     (to make it), X-Seren-Clip-Seconds
                                  | JSON {error, node} with 400/413/502/503
    GET  /api/v1/speak/voices     -> {ok, voices: [{name, backend, language,
                                     ref_seconds}], node, model}

Hearing (`/hear`) is words from a sound and listening (`/listen`) is words
about one; this is the other direction. The speaker is a voice on a node
(a reference clip and its transcript, cloned by a text-to-speech backend
for every sentence). Woken the way anything is woken (ensure, a lease in
the caller's name), asked through the node's Observatory, let go after;
its run policy decides when it sleeps. Kokoro stays the voice for a node
that has no speaker, and callers fall back to it on a 503.

A voice card may say it NEEDS another service on the same box (the
speaker's `wren-drama` card runs on Fish Audio Drama 3, 41 GB behind its
own server, `fishspeech`, which the speaker only talks to). Then that
service is woken beside the speaker, in the same holder's name, with the
longer wake a big model takes, and let go with it. The speaker's voice
list says which cards need what (18 Oct 2026).
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from .services import service_ensure, service_release

API_VERSION = "v1"
SERVICE = "speaker"
SPEAK_TIMEOUT = 600.0           # a big backend says 15 s of speech in 20; a long answer takes minutes
WAKE_SECONDS = 240              # a 1.7B model and a voice prompt: under a minute off the NVMe
NEEDS_WAKE_SECONDS = 540        # a 35B voice (fishspeech) takes five and a half minutes off the NVMe
MAX_TEXT_CHARS = 4000

router = APIRouter(tags=["speaking"])


class SpeakBody(BaseModel):
    text: str
    voice: str = "wren"
    holder: str = ""
    node: str = ""


async def _wake(request: Request, holder: str, node: str):
    """(agent, holder, error_response). Wakes the speaker and holds it."""
    holder = (holder or "speak").strip()[:64]
    cluster = request.app.state.cluster
    ask = {"holder": holder, "reason": "speaking", "wait_seconds": WAKE_SECONDS}
    if node:
        ask["node"] = node
    up = await service_ensure(request, SERVICE, ask)
    agent = cluster.get_agent(up.get("node") or "") if isinstance(up, dict) else None
    if not up.get("ok") or not up.get("ready") or agent is None:
        if up.get("started") and agent is not None:
            await service_release(request, SERVICE, {"holder": holder, "node": agent.node_name})
        why = up.get("error") or "the speaker did not come up"
        status = 503 if "no online node" in why or "did not come up" in why else 502
        return None, holder, JSONResponse({"error": why, "node": up.get("node")}, status_code=status)
    return agent, holder, None


async def _needs(agent, voice: str) -> str:
    """The service the voice card `voice` needs beside the speaker, or "".
    From the speaker's own voice list; a speaker that cannot be asked, or a
    voice it does not list, needs nothing extra (the speak will say why)."""
    status, out = await agent.relay_json_async("api/v1/service/speaker/voices", timeout=30.0)
    if status != 200 or not isinstance(out, dict):
        return ""
    for card in out.get("voices") or []:
        if isinstance(card, dict) and card.get("name") == voice:
            return str(card.get("needs") or "")
    return ""


async def _wake_needed(request: Request, holder: str, agent, needed: str):
    """Wake `needed` on the speaker's node, in the same holder's name.
    Returns an error response, or None when it is up."""
    up = await service_ensure(request, needed, {"holder": holder, "reason": f"speaking ({needed})",
                                               "wait_seconds": NEEDS_WAKE_SECONDS, "node": agent.node_name})
    if up.get("ok") and up.get("ready"):
        return None
    if up.get("started"):
        await service_release(request, needed, {"holder": holder, "node": agent.node_name})
    why = up.get("error") or f"{needed} did not come up"
    return JSONResponse({"error": f"that voice needs {needed}: {why}", "node": agent.node_name},
                        status_code=503 if "did not come up" in why or "no online node" in why else 502)


@router.post(f"/api/{API_VERSION}/speak")
async def speak(request: Request, body: SpeakBody):
    """Say this, in that voice. The WAV comes back as the body; who said it
    and how long it took ride in the headers."""
    text = (body.text or "").strip()
    if not text:
        return JSONResponse({"error": "send `text` to say"}, status_code=400)
    if len(text) > MAX_TEXT_CHARS:
        return JSONResponse({"error": f"that is {len(text)} characters; at most {MAX_TEXT_CHARS} in one go"}, status_code=413)
    voice = (body.voice or "wren").strip()[:64]
    agent, holder, err = await _wake(request, body.holder, body.node)
    if err:
        return err
    needed = ""
    try:
        needed = await _needs(agent, voice)
        if needed:
            err = await _wake_needed(request, holder, agent, needed)
            if err:
                return err
        status, out, headers = await agent.speak_async(text, voice, SPEAK_TIMEOUT)
    finally:
        if needed:
            await service_release(request, needed, {"holder": holder, "node": agent.node_name})
        await service_release(request, SERVICE, {"holder": holder, "node": agent.node_name})
    if status != 200 or not isinstance(out, (bytes, bytearray)):
        detail = out if isinstance(out, dict) else {"error": str(out)}
        return JSONResponse({"error": detail.get("error") or detail.get("detail") or "the speaker could not say it",
                             "detail": detail.get("detail"), "node": agent.node_name},
                            status_code=503 if status in (0, 503) else 502)
    headers = {k: v for k, v in headers.items() if k.lower().startswith("x-seren-")}
    headers["X-Seren-Node"] = agent.node_name
    if needed:
        headers["X-Seren-Needs"] = needed
    return Response(content=bytes(out), media_type="audio/wav", headers=headers)


@router.get(f"/api/{API_VERSION}/speak/voices")
async def speak_voices(request: Request, node: str = ""):
    """The voices the speaker has. Wakes it, since only it knows."""
    agent, holder, err = await _wake(request, "speak-voices", node)
    if err:
        return err
    try:
        status, out = await agent.relay_json_async("api/v1/service/speaker/voices", timeout=30.0)
    finally:
        await service_release(request, SERVICE, {"holder": holder, "node": agent.node_name})
    if status != 200 or not isinstance(out, dict) or not out.get("ok"):
        detail = out if isinstance(out, dict) else {"error": str(out)}
        return JSONResponse({"error": detail.get("error") or "the speaker did not list its voices",
                             "detail": detail.get("detail"), "node": agent.node_name}, status_code=502)
    return {"ok": True, "voices": out.get("voices", []), "node": agent.node_name, "model": out.get("model")}
