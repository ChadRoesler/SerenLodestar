"""
Lodestar routes a ripple (28 Sept 2026): the hippocampus - on the Nano - asks
the main model for a brief or a review, and Lodestar, which knows the cluster,
sends it where the model lives. The hippocampus needs one address and one
token; moving the model is a change in Lodestar's config.

- target "" : off, 409, nothing forwarded
- target <node> : forwarded to that node's Observatory, and its answer (status
  and reason) comes back as it is - "not logged on" must not become a None
- an unknown node is a 404 that names it
- target "local" : run on Lodestar's own box through seren_sinew.ripple
- target "self" : 501, not built yet
- the ripple block reads from yaml
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from seren_lodestar.agent_client import JetsonAgentClient
from seren_lodestar.app import create_app
from seren_lodestar.config import LodestarConfig, RippleConfig, load_config
from seren_lodestar.dtos import JetsonNodeOptions


def _app(ripple: RippleConfig):
    return create_app(LodestarConfig(ripple=ripple))


class FakeAgent:
    def __init__(self, status=200, answer=None):
        self.status, self.answer, self.seen = status, answer or {"ok": True, "pid": 42}, []

    async def ripple_async(self, body):
        self.seen.append(body)
        return self.status, dict(self.answer)


BODY = {"event": "brief_requested", "message": "It's bedtime - want to write a brief?"}


def test_off_by_default():
    with TestClient(_app(RippleConfig())) as c:
        r = c.post("/api/v1/system/ripple", json=BODY)
    assert r.status_code == 409 and "does not route ripples" in r.json()["error"]


def test_a_node_target_forwards_to_its_observatory(monkeypatch):
    agent = FakeAgent()
    with TestClient(_app(RippleConfig(target="desktop"))) as c:
        monkeypatch.setattr(c.app.state.cluster, "get_agent", lambda n: agent if n == "desktop" else None)
        r = c.post("/api/v1/system/ripple", json=BODY)
    assert r.status_code == 200 and r.json() == {"ok": True, "pid": 42, "routed_to": "desktop"}
    assert agent.seen == [BODY]


def test_the_observatorys_refusal_comes_back_as_it_is(monkeypatch):
    agent = FakeAgent(409, {"ok": False, "error": "alice is not logged on to this box"})
    with TestClient(_app(RippleConfig(target="desktop"))) as c:
        monkeypatch.setattr(c.app.state.cluster, "get_agent", lambda n: agent)
        r = c.post("/api/v1/system/ripple", json=BODY)
    assert r.status_code == 409 and "not logged on" in r.json()["error"]


def test_an_unknown_node_is_named():
    with TestClient(_app(RippleConfig(target="ghost"))) as c:
        r = c.post("/api/v1/system/ripple", json=BODY)
    assert r.status_code == 404 and "'ghost'" in r.json()["error"]


def test_self_is_not_built_yet():
    with TestClient(_app(RippleConfig(target="self"))) as c:
        assert c.post("/api/v1/system/ripple", json=BODY).status_code == 501


def test_local_runs_on_this_box(tmp_path, monkeypatch):
    from seren_sinew import runas
    monkeypatch.setattr(runas, "whoami", lambda: ("alice", False))
    monkeypatch.setenv("HOME", str(tmp_path)); monkeypatch.setenv("USERPROFILE", str(tmp_path))
    out = tmp_path / "rippled.json"
    rec = tmp_path / "rec.py"
    rec.write_text("import json, sys\nopen(sys.argv[1], 'w').write(json.dumps(sys.argv[2:]))\n", encoding="utf-8")
    with TestClient(_app(RippleConfig(target="local", command=[sys.executable, str(rec), str(out), "{message}"]))) as c:
        r = c.post("/api/v1/system/ripple", json=BODY)
        assert r.status_code == 200 and r.json()["ok"] is True and r.json()["routed_to"] == "local", r.text
        end = time.time() + 15
        while not (out.exists() and out.stat().st_size) and time.time() < end:
            time.sleep(0.05)
        c.app.state.ripple_runner.wait()
    assert json.loads(out.read_text()) == [BODY["message"]]


async def test_the_client_passes_the_observatorys_answer_through():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(409, json={"ok": False, "error": "ripple is not set up on this node"})
    c = JetsonAgentClient(JetsonNodeOptions(name="desktop", agent_url="http://10.0.0.2:7777"))
    c._client = httpx.AsyncClient(base_url="http://10.0.0.2:7777/", transport=httpx.MockTransport(handler))
    status, answer = await c.ripple_async(BODY)
    assert status == 409 and "not set up" in answer["error"]
    assert seen == [("/api/v1/system/ripple", BODY)]
    await c._client.aclose()


def test_the_yaml_ripple_block(tmp_path, monkeypatch):
    monkeypatch.delenv("SEREN_LODESTAR_CONFIG", raising=False)
    p = tmp_path / "lodestar.yaml"
    p.write_text("ripple:\n  target: desktop\n  timeout_seconds: soon\n  command: 42\n", encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.ripple.target == "desktop"
    assert cfg.ripple.timeout_seconds == 900.0 and cfg.ripple.command == ["claude", "-p", "{message}"]
