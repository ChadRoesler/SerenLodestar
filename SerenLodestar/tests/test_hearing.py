"""
Hearing through the cluster: a clip -> Lodestar -> a REAL Observatory
(in-process) -> its whisper-server (scripted) -> the words.

Pinned: whisper is woken for the clip (ensure) and let go after (release,
not put away: its run policy decides), the clip reaches the server as the
multipart it takes, the words come back with the node that heard them, a
server that fails is a 502 that says so, no node with whisper is a 503,
and an empty clip is refused before anything is woken.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from seren_lodestar.app import create_app
from seren_lodestar.config import LodestarConfig

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\0" * 20 + b"data\0\0\0\0"


@pytest.fixture
def road(tmp_path, monkeypatch):
    """Lodestar in front of a real Observatory whose whisper is scripted.
    Returns (lodestar client, box) where box records what happened."""
    obs_app = pytest.importorskip("seren_observatory.app")
    from seren_observatory import lifecycle
    from seren_observatory.services import whisper as obs_whisper
    from seren_lodestar.agent_client import JetsonAgentClient

    home = tmp_path / "home"
    (home / ".seren" / "services").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (home / ".seren" / "services" / "whisper.json").write_text(json.dumps({
        "schema_version": 1, "service": "whisper", "service_type": "pid_file", "port": 8081, "health_path": "/",
        "endpoint": "/v1/audio/transcriptions", "implementation": "whisper.cpp",
        "serviceSpecific": {"model": "small.en"}}))
    (home / ".seren" / "secrets.json").write_text(json.dumps({"observatory_token": "obs-secret"}))

    box = {"running": False, "stops": 0, "heard": [], "answer": " Hello, Wren.", "status": 200}

    async def probe(manifest):
        return {"ok": box["running"]}

    async def start(manifest):
        box["running"] = True
        return {"ok": True, "pid": 7}

    async def stop(manifest):
        box["running"] = False
        box["stops"] += 1
        return {"ok": True}

    def server(request: httpx.Request) -> httpx.Response:
        box["heard"].append({"url": str(request.url), "body": request.read(), "running": box["running"]})
        if box["status"] != 200:
            return httpx.Response(box["status"], json={"error": "no model loaded"})
        return httpx.Response(200, json={"text": box["answer"]})

    monkeypatch.setattr(lifecycle, "probe_port", probe)
    monkeypatch.setattr(lifecycle, "start", start)
    monkeypatch.setattr(lifecycle, "stop", stop)
    monkeypatch.setattr(lifecycle, "ENSURE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(obs_whisper, "TRANSPORT", httpx.MockTransport(server))
    observatory = obs_app.create_app()

    agent = JetsonAgentClient.__new__(JetsonAgentClient)
    agent._node_name = "jetson"
    agent._log = lambda m: None
    agent._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=observatory), base_url="http://192.0.2.116:7777/",
                                      headers={"Authorization": "Bearer obs-secret"})
    cluster = SimpleNamespace(
        choose_node_for=lambda svc: SimpleNamespace(name="jetson") if svc == "whisper" else None,
        get_agent=lambda name: agent if name == "jetson" else None,
        mark_node_suspect=lambda *a: None)
    with TestClient(create_app(LodestarConfig())) as c:
        c.app.state.cluster = cluster
        yield c, box


def _hear(c, data=WAV, **form):
    return c.post("/api/v1/hear", files={"file": ("clip.wav", data, "audio/wav")}, data=form)


def test_a_clip_wakes_whisper_is_heard_and_whisper_is_let_go(road):
    c, box = road
    r = _hear(c, language="en", holder="symposium")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "text": "Hello, Wren.", "node": "jetson",
                        "seconds": r.json()["seconds"], "model": "small.en"}
    (heard,) = box["heard"]
    assert heard["running"] is True, "whisper was up while it listened"
    assert heard["url"] == "http://127.0.0.1:8081/v1/audio/transcriptions"
    assert WAV in heard["body"] and b'name="language"\r\n\r\nen' in heard["body"]
    # let go, not put away: the run policy decides when whisper stops, so a
    # conversation is not a start and a stop per sentence
    leases = c.get("/api/v1/cluster/leases").json()
    assert not any(l.get("service") == "whisper" and "symposium" in (l.get("holders") or [])
                   for l in (leases.get("leases") or leases if isinstance(leases, list) else [])), leases


def test_a_whisper_that_fails_is_a_502_that_says_so(road):
    c, box = road
    box["status"] = 500
    r = _hear(c)
    assert r.status_code == 502 and r.json()["node"] == "jetson"
    assert "refused" in r.json()["error"]


def test_no_node_with_whisper_is_a_503():
    with TestClient(create_app(LodestarConfig())) as c:
        c.app.state.cluster = SimpleNamespace(choose_node_for=lambda svc: None, get_agent=lambda n: None,
                                              mark_node_suspect=lambda *a: None)
        r = _hear(c)
        assert r.status_code == 503 and "no online node" in r.json()["error"]


def test_an_empty_clip_wakes_nothing(road):
    c, box = road
    assert _hear(c, data=b"").status_code == 400
    assert box["running"] is False and not box["heard"]
