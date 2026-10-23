"""
The Rack (22 Oct 2026): Lodestar carries a carabiner's settings to the
node's Observatory and back, the road ensure takes. Nothing on the road
knows what a setting means; the carabiner's own refusals come back whole.

- /api/v1/rack: every node's carabiners with their settings; an offline node
  or one whose carabiners are off says so instead of failing the page
- a change names its node; an unknown node is a 404 that names it
- the request is a SettingsRequest by the time it leaves (holder filled in)
- the node in an answer is the name Lodestar knows the node by
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from seren_lodestar.app import create_app
from seren_lodestar.config import LodestarConfig, RippleConfig
from seren_lodestar.dtos import NodeSnapshot

MANIFEST = {"ok": True, "carabiner": "claude", "display": "Claude Code", "version": "5.3.0", "node": "DESKTOP-XYZ",
            "fields": [{"key": "session.model", "kind": "choice", "value": "claude-haiku-5-5"}], "connections": []}


class FakeAgent:
    def __init__(self, listing=(200, {"ok": True, "carabiners": [{"carabiner": "claude", "configured": True}]})):
        self.listing, self.seen = listing, []

    async def carabiner_call_async(self, method, path, body=None, timeout=90.0):
        self.seen.append((method, path, body))
        if path == "/api/v1/carabiners":
            return self.listing
        if method == "POST":
            if body["values"].get("session.permission_mode") == "yolo":
                return 200, {"ok": False, "refused": {"session.permission_mode": "not one of: manual, plan"},
                             "error": "nothing was written"}
            return 200, {"ok": True, "applied": list(body["values"]), "node": "DESKTOP-XYZ"}
        if path.endswith("/belay"):
            return 200, {"ok": True, "checks": []}
        return 200, dict(MANIFEST)


def _client(monkeypatch, agents, snapshots=None):
    c = TestClient(create_app(LodestarConfig(ripple=RippleConfig(target="desktop"))))
    c.__enter__()
    monkeypatch.setattr(c.app.state.cluster, "_agents", dict(agents))
    monkeypatch.setattr(c.app.state.cluster, "get_snapshots", lambda: dict(snapshots or {}))
    return c


def test_the_rack_is_every_nodes_carabiners_with_their_settings(monkeypatch):
    off = FakeAgent(listing=(409, {"ok": False, "error": "carabiners are not set up on this node"}))
    c = _client(monkeypatch, {"desktop": FakeAgent(), "nuc": off, "spark": FakeAgent()},
                {"spark": NodeSnapshot(online=False, installed_services=[], status={})})
    r = c.get("/api/v1/rack").json()
    by = {n["node"]: n for n in r["nodes"]}
    assert r["wakes"] == {"target": "desktop"}
    assert by["desktop"]["set_up"] and by["desktop"]["carabiners"][0]["manifest"]["version"] == "5.3.0"
    assert by["desktop"]["carabiners"][0]["manifest"]["node"] == "desktop", "Lodestar's name, not the hostname"
    assert not by["nuc"]["set_up"] and "not set up" in by["nuc"]["error"]
    assert by["spark"]["online"] is False and by["spark"]["carabiners"] == []
    c.__exit__(None, None, None)


def test_a_change_is_carried_to_its_node_and_the_carabiners_answer_comes_back(monkeypatch):
    desk = FakeAgent()
    c = _client(monkeypatch, {"desktop": desk})
    r = c.post("/api/v1/carabiner/claude/settings",
               json={"node": "desktop", "values": {"session.model": "claude-haiku-5-5"}, "sneaky": "x"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["node"] == "desktop"
    method, path, body = desk.seen[-1]
    assert (method, path) == ("POST", "/api/v1/carabiner/claude/settings")
    assert body["holder"] == "lodestar-rack" and "sneaky" not in body and body["values"] == {"session.model": "claude-haiku-5-5"}

    r = c.post("/api/v1/carabiner/claude/settings", json={"node": "desktop", "values": {"session.permission_mode": "yolo"}})
    assert not r.json()["ok"] and r.json()["refused"] == {"session.permission_mode": "not one of: manual, plan"}
    c.__exit__(None, None, None)


def test_a_change_must_name_a_node_this_cluster_has(monkeypatch):
    c = _client(monkeypatch, {"desktop": FakeAgent()})
    assert c.post("/api/v1/carabiner/claude/settings", json={"values": {}}).status_code == 400
    r = c.post("/api/v1/carabiner/claude/settings", json={"node": "toaster", "values": {}})
    assert r.status_code == 404 and "toaster" in r.json()["error"]
    assert c.get("/api/v1/carabiner/claude/settings?node=desktop").json()["fields"][0]["key"] == "session.model"
    assert c.get("/api/v1/carabiner/claude/belay?node=desktop").json()["ok"]
    c.__exit__(None, None, None)
