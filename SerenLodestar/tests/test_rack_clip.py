"""
The Rack's leftovers (29 Oct 2026): clip a carabiner onto a node from the
shelf, unclip one, and move Seren's wakes from the page.

- a clip sends the shelf's file (its bytes, its hash, its version) to the
  node's Observatory; a carabiner the shelf lacks is an answer, not a 500
- an unclip is carried to its node and the node's answer comes back whole
- the wake target is a node of this cluster, 'local' or blank; it is written
  into Lodestar's yaml with the comments kept and taken up at once
"""
from __future__ import annotations

import hashlib

import yaml
from fastapi.testclient import TestClient

from seren_lodestar.app import create_app
from seren_lodestar.config import LodestarConfig, RippleConfig


class FakeAgent:
    def __init__(self):
        self.seen = []

    async def carabiner_call_async(self, method, path, body=None, timeout=90.0):
        self.seen.append((method, path, body))
        if path == "/api/v1/carabiners":
            return 200, {"ok": True, "carabiners": []}
        if path.endswith("/clip"):
            return 200, {"ok": True, "carabiner": "claude", "was": "", "now": body["version"], "changed": True,
                         "node": "DESKTOP-XYZ"}
        if path.endswith("/unclip"):
            return 200, {"ok": True, "carabiner": "claude", "was": "5.1.4", "kept": "/kbh/claude.kbh.unclipped-5.1.4",
                         "yaml_kept": True, "note": "still registered", "node": "DESKTOP-XYZ"}
        return 200, {"ok": True}


class FakeShelf:
    repo, root = "seren/carabiners", "/shelf"

    def entries(self):
        return {"claude": {"carabiner": "claude", "version": "5.1.4", "source": "release v5.1.4", "sha256": "ab"}}

    def get(self, name):
        if name != "claude":
            raise LookupError(f"no {name}.kbh on the shelf")
        return b"PK-ish bytes", self.entries()["claude"]


def _client(monkeypatch, agents, cfg=None):
    c = TestClient(create_app(cfg or LodestarConfig(ripple=RippleConfig(target="desktop"))))
    c.__enter__()
    monkeypatch.setattr(c.app.state.cluster, "_agents", dict(agents))
    monkeypatch.setattr(c.app.state.cluster, "get_snapshots", lambda: {})
    c.app.state.shelf = FakeShelf()
    return c


def test_a_clip_sends_the_shelfs_file_to_the_node(monkeypatch):
    desk = FakeAgent()
    c = _client(monkeypatch, {"desktop": desk})
    r = c.post("/api/v1/carabiner/claude/clip", json={"node": "desktop", "reason": "from the Rack"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["now"] == "5.1.4" and r.json()["node"] == "desktop"
    method, path, body = desk.seen[-1]
    assert (method, path) == ("POST", "/api/v1/carabiner/claude/clip")
    assert body["sha256"] == hashlib.sha256(b"PK-ish bytes").hexdigest() and body["version"] == "5.1.4"
    assert body["holder"] == "lodestar-rack" and body["reason"] == "from the Rack" and body["source"] == "release v5.1.4"

    r = c.post("/api/v1/carabiner/codex/clip", json={"node": "desktop"})
    assert r.status_code == 200 and not r.json()["ok"] and "codex.kbh" in r.json()["error"]
    assert c.post("/api/v1/carabiner/claude/clip", json={"node": "toaster"}).status_code == 404
    assert c.post("/api/v1/carabiner/claude/clip", json={}).status_code == 400
    c.__exit__(None, None, None)


def test_an_unclip_is_carried_and_the_nodes_answer_comes_back(monkeypatch):
    desk = FakeAgent()
    c = _client(monkeypatch, {"desktop": desk})
    r = c.post("/api/v1/carabiner/claude/unclip", json={"node": "desktop", "reason": "done with it"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["kept"].endswith("unclipped-5.1.4")
    assert r.json()["node"] == "desktop", "Lodestar's name, not the hostname"
    method, path, body = desk.seen[-1]
    assert (method, path) == ("POST", "/api/v1/carabiner/claude/unclip")
    assert body == {"node": "desktop", "holder": "lodestar-rack", "reason": "done with it"}
    c.__exit__(None, None, None)


def test_the_wake_target_is_moved_from_the_page_and_written_with_the_comments_kept(monkeypatch, tmp_path):
    y = tmp_path / "seren-lodestar.yaml"
    y.write_text("# the head\nripple:\n  target: desktop  # where the model lives\n  command: claude\n", encoding="utf-8")
    cfg = LodestarConfig(ripple=RippleConfig(target="desktop"), config_path=str(y))
    c = _client(monkeypatch, {"desktop": FakeAgent(), "nuc": FakeAgent()}, cfg)
    assert c.get("/api/v1/rack").json()["wakes"] == {"target": "desktop"}

    r = c.post("/api/v1/rack/wakes", json={"target": "nuc"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["was"] == "desktop" and r.json()["written"] == str(y)
    text = y.read_text(encoding="utf-8")
    assert "  target: nuc  # where the model lives\n" in text and "# the head" in text and "command: claude" in text
    assert yaml.safe_load(text)["ripple"]["target"] == "nuc"
    assert c.get("/api/v1/rack").json()["wakes"] == {"target": "nuc"}, "taken up at once"
    assert (tmp_path / "seren-lodestar.yaml.seren-bak").exists()

    r = c.post("/api/v1/rack/wakes", json={"target": "toaster"})
    assert r.status_code == 404 and "toaster" in r.json()["error"]
    assert c.get("/api/v1/rack").json()["wakes"] == {"target": "nuc"}, "a refused target changes nothing"

    r = c.post("/api/v1/rack/wakes", json={"target": ""})
    assert r.json()["ok"] and r.json()["target"] == "" and yaml.safe_load(y.read_text(encoding="utf-8"))["ripple"]["target"] == ""
    r = c.post("/api/v1/rack/wakes", json={"target": "local"})
    assert r.json()["ok"] and c.get("/api/v1/rack").json()["wakes"] == {"target": "local"}
    c.__exit__(None, None, None)


def test_with_no_yaml_the_change_holds_in_memory_and_says_so(monkeypatch):
    c = _client(monkeypatch, {"desktop": FakeAgent()})
    r = c.post("/api/v1/rack/wakes", json={"target": "local"})
    assert r.json()["ok"] and r.json()["written"] == "" and "until it restarts" in r.json()["note"]
    assert c.get("/api/v1/rack").json()["wakes"] == {"target": "local"}
    c.__exit__(None, None, None)
