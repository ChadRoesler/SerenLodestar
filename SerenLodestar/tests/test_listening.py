"""
Listening through the cluster: a clip and a question -> Lodestar -> a REAL
Observatory (in-process) -> its listener (scripted) -> words about the sound.
The same road as hearing (test_hearing.py), with a question riding along,
and a second clip for a comparison.
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
    obs_app = pytest.importorskip("seren_observatory.app")
    from seren_observatory import lifecycle
    from seren_observatory.services import listener as obs_listener
    from seren_lodestar.agent_client import JetsonAgentClient

    home = tmp_path / "home"
    (home / ".seren" / "services").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (home / ".seren" / "services" / "listener.json").write_text(json.dumps({
        "schema_version": 1, "service": "listener", "service_type": "pid_file", "port": 8086, "health_path": "/health",
        "endpoint": "/listen", "serviceSpecific": {"model": "Qwen/Qwen3-Omni-30B-A3B-Instruct"}}))
    (home / ".seren" / "secrets.json").write_text(json.dumps({"observatory_token": "obs-secret"}))
    box = {"running": False, "stops": 0, "asked": []}

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
        box["asked"].append({"url": str(request.url), "body": request.read(), "running": box["running"]})
        return httpx.Response(200, json={"ok": True, "text": "A low, warm voice, unhurried.", "seconds": 2.5,
                                         "clip_seconds": 4.0, "cut": False})

    monkeypatch.setattr(lifecycle, "probe_port", probe)
    monkeypatch.setattr(lifecycle, "start", start)
    monkeypatch.setattr(lifecycle, "stop", stop)
    monkeypatch.setattr(lifecycle, "ENSURE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(obs_listener, "TRANSPORT", httpx.MockTransport(server))
    observatory = obs_app.create_app()
    agent = JetsonAgentClient.__new__(JetsonAgentClient)
    agent._node_name = "bigbox"
    agent._log = lambda m: None
    agent._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=observatory), base_url="http://192.0.2.128:7777/",
                                      headers={"Authorization": "Bearer obs-secret"})
    cluster = SimpleNamespace(
        choose_node_for=lambda svc: SimpleNamespace(name="bigbox") if svc == "listener" else None,
        get_agent=lambda name: agent if name == "bigbox" else None,
        mark_node_suspect=lambda *a: None)
    with TestClient(create_app(LodestarConfig())) as c:
        c.app.state.cluster = cluster
        yield c, box


def test_a_clip_and_a_question_wake_the_listener_and_its_words_come_back(road):
    c, box = road
    r = c.post("/api/v1/listen", files={"file": ("me.wav", WAV, "audio/wav")},
               data={"prompt": "how does this voice sound?", "holder": "wren"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "text": "A low, warm voice, unhurried.", "node": "bigbox", "seconds": 2.5,
                        "clip_seconds": 4.0, "cut": False, "model": "Qwen/Qwen3-Omni-30B-A3B-Instruct"}
    (asked,) = box["asked"]
    assert asked["running"] and asked["url"] == "http://127.0.0.1:8086/listen"
    assert WAV in asked["body"] and b'name="prompt"\r\n\r\nhow does this voice sound?' in asked["body"]


def test_two_clips_are_compared(road):
    c, box = road
    r = c.post("/api/v1/listen/compare", files={"a": ("a.wav", WAV, "audio/wav"), "b": ("b.wav", WAV + b"B", "audio/wav")})
    assert r.status_code == 200 and r.json()["ok"]
    (asked,) = box["asked"]
    assert asked["url"] == "http://127.0.0.1:8086/compare" and b'name="b"; filename="b.wav"' in asked["body"]


def test_no_node_with_a_listener_is_a_503_and_an_empty_clip_wakes_nothing(road):
    c, box = road
    assert c.post("/api/v1/listen", files={"file": ("x.wav", b"", "audio/wav")}).status_code == 400
    assert box["running"] is False
    with TestClient(create_app(LodestarConfig())) as c2:
        c2.app.state.cluster = SimpleNamespace(choose_node_for=lambda svc: None, get_agent=lambda n: None,
                                               mark_node_suspect=lambda *a: None)
        r = c2.post("/api/v1/listen", files={"file": ("x.wav", WAV, "audio/wav")})
        assert r.status_code == 503 and "no online node" in r.json()["error"]
