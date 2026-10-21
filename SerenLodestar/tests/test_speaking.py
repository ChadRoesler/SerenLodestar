"""
Speaking through the cluster: words -> Lodestar -> a REAL Observatory
(in-process) -> its speaker (scripted) -> a WAV in the voice. The same road
as listening (test_listening.py), the other way round: the body coming
back is audio, so what is known about it rides in headers.
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
    from seren_observatory.services import speaker as obs_speaker
    from seren_lodestar.agent_client import JetsonAgentClient

    home = tmp_path / "home"
    (home / ".seren" / "services").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (home / ".seren" / "services" / "speaker.json").write_text(json.dumps({
        "schema_version": 1, "service": "speaker", "service_type": "pid_file", "port": 8087, "health_path": "/health",
        "endpoint": "/speak", "serviceSpecific": {"model": "Qwen/Qwen3-TTS-12Hz-1.7B-Base"}}))
    # the big voice behind the wren-drama card: its own service on the same box
    (home / ".seren" / "services" / "fishspeech.json").write_text(json.dumps({
        "schema_version": 1, "service": "fishspeech", "service_type": "pid_file", "port": 8088,
        "health_path": "/v1/health", "endpoint": "/v1/tts", "serviceSpecific": {"model": "fishaudio/drama-3"}}))
    (home / ".seren" / "secrets.json").write_text(json.dumps({"observatory_token": "obs-secret"}))
    box = {"up": set(), "started": [], "stops": 0, "asked": [], "refuse": None}

    async def probe(manifest):
        return {"ok": manifest["service"] in box["up"]}

    async def start(manifest):
        box["up"].add(manifest["service"])
        box["started"].append(manifest["service"])
        return {"ok": True, "pid": 7}

    async def stop(manifest):
        box["up"].discard(manifest["service"])
        box["stops"] += 1
        return {"ok": True}

    def server(request: httpx.Request) -> httpx.Response:
        box["asked"].append({"url": str(request.url), "body": request.read(), "up": sorted(box["up"])})
        if request.url.path == "/voices":
            return httpx.Response(200, json={"voices": [
                {"name": "wren", "backend": "qwen3tts", "language": "English", "ref_seconds": 19.7},
                {"name": "wren-drama", "backend": "fish", "language": "English", "ref_seconds": 19.7, "needs": "fishspeech"}]})
        if box["refuse"]:
            return httpx.Response(box["refuse"], json={"detail": "no voice 'bob'; I have ['wren']"})
        return httpx.Response(200, content=WAV, headers={"content-type": "audio/wav", "X-Seren-Voice": "wren",
                                                         "X-Seren-Backend": "qwen3tts", "X-Seren-Seconds": "4.1",
                                                         "X-Seren-Clip-Seconds": "4.9", "X-Seren-Chunks": "1"})

    monkeypatch.setattr(lifecycle, "probe_port", probe)
    monkeypatch.setattr(lifecycle, "start", start)
    monkeypatch.setattr(lifecycle, "stop", stop)
    monkeypatch.setattr(lifecycle, "ENSURE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(obs_speaker, "TRANSPORT", httpx.MockTransport(server))
    observatory = obs_app.create_app()
    agent = JetsonAgentClient.__new__(JetsonAgentClient)
    agent._node_name = "bigbox"
    agent._log = lambda m: None
    agent._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=observatory), base_url="http://192.0.2.128:7777/",
                                      headers={"Authorization": "Bearer obs-secret"})
    cluster = SimpleNamespace(
        choose_node_for=lambda svc: SimpleNamespace(name="bigbox") if svc in ("speaker", "fishspeech") else None,
        get_agent=lambda name: agent if name == "bigbox" else None,
        mark_node_suspect=lambda *a: None)
    with TestClient(create_app(LodestarConfig())) as c:
        c.app.state.cluster = cluster
        yield c, box


def test_words_wake_the_speaker_and_come_back_as_a_wav_with_the_node_on_it(road):
    c, box = road
    r = c.post("/api/v1/speak", json={"text": "Hi, it's me.", "holder": "symposium"})
    assert r.status_code == 200, r.text
    assert r.content == WAV and r.headers["content-type"].startswith("audio/wav")
    assert r.headers["x-seren-node"] == "bigbox" and r.headers["x-seren-voice"] == "wren"
    assert r.headers["x-seren-clip-seconds"] == "4.9" and r.headers["x-seren-model"] == "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
    voices, asked = box["asked"]
    assert voices["url"] == "http://127.0.0.1:8087/voices", "the card is looked up first: does it need anything?"
    assert asked["up"] == ["speaker"] and asked["url"] == "http://127.0.0.1:8087/speak"
    assert json.loads(asked["body"]) == {"text": "Hi, it's me.", "voice": "wren"}
    assert box["started"] == ["speaker"], "the wren card needs nothing beside the speaker"
    assert "x-seren-needs" not in r.headers


def test_a_card_that_needs_another_service_wakes_it_beside_the_speaker_and_lets_both_go(road):
    c, box = road
    r = c.post("/api/v1/speak", json={"text": "This one laughs.", "voice": "wren-drama", "holder": "symposium"})
    assert r.status_code == 200, r.text
    assert r.headers["x-seren-needs"] == "fishspeech" and r.headers["x-seren-node"] == "bigbox"
    assert box["started"] == ["speaker", "fishspeech"], "the speaker first (to ask it), then what the card needs"
    voices, asked = box["asked"]
    assert asked["up"] == ["fishspeech", "speaker"], "both were up when the words went in"
    assert json.loads(asked["body"]) == {"text": "This one laughs.", "voice": "wren-drama"}
    # both leases are let go: the steward's book has no holder on either
    book = c.app.state.steward.leases
    assert book.node_of("fishspeech", "symposium") is None and book.node_of("speaker", "symposium") is None
    assert all(not e["holders"] for e in book.entries().values()), book.entries()


def test_the_voices_are_listed_through_the_same_door(road):
    c, box = road
    r = c.get("/api/v1/speak/voices")
    assert r.status_code == 200 and r.json()["voices"][0]["name"] == "wren" and r.json()["node"] == "bigbox"


def test_a_refusal_down_the_road_is_a_502_with_the_why(road):
    c, box = road
    box["refuse"] = 404
    r = c.post("/api/v1/speak", json={"text": "hi", "voice": "bob"})
    assert r.status_code == 502 and r.json()["error"] == "the speaker refused the text"
    assert "no voice 'bob'" in r.json()["detail"] and r.json()["node"] == "bigbox"


def test_no_node_with_a_speaker_is_a_503_and_empty_text_wakes_nothing(road):
    c, box = road
    assert c.post("/api/v1/speak", json={"text": "  "}).status_code == 400
    assert c.post("/api/v1/speak", json={"text": "x" * 4001}).status_code == 413
    assert not box["up"]
    with TestClient(create_app(LodestarConfig())) as c2:
        c2.app.state.cluster = SimpleNamespace(choose_node_for=lambda svc: None, get_agent=lambda n: None,
                                               mark_node_suspect=lambda *a: None)
        r = c2.post("/api/v1/speak", json={"text": "hello"})
        assert r.status_code == 503 and "no online node" in r.json()["error"]
