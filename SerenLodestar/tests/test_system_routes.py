"""
The reboot routes and the reclaim fan on Lodestar's side, with a fake
cluster standing in for the nodes: an unknown node is a 404, a bad delay a
400, an unreachable Observatory a 502 that says so, and a good answer is
passed through field by field. Reclaim reports every node it asked, the
unreachable one as not ok with empty lists, and honours a `nodes` filter.
(The wire shapes themselves are pinned by test_wire_contract against a real
Observatory; this is the head's handling of them.)
"""
from __future__ import annotations

from types import SimpleNamespace


class FakeAgent:
    def __init__(self, reboot=None, cancel=None, reclaim=None):
        self._reboot, self._cancel, self._reclaim = reboot, cancel, reclaim
        self.calls: list[tuple] = []

    async def reboot_async(self, delay):
        self.calls.append(("reboot", delay))
        return self._reboot

    async def reboot_cancel_async(self):
        self.calls.append(("cancel",))
        return self._cancel

    async def reclaim_async(self, exclude, include=None, everything=False):
        self.calls.append(("reclaim", exclude, include, everything))
        if isinstance(self._reclaim, Exception):
            raise self._reclaim
        return self._reclaim


class FakeCluster:
    def __init__(self, agents: dict):
        self.agents = agents

    def get_agent(self, name):
        return self.agents.get(name)


def _wire(client, agents: dict) -> FakeCluster:
    cluster = FakeCluster(agents)
    client.app.state.cluster = cluster
    return cluster


def test_reboot_unknown_node_is_404(client):
    _wire(client, {})
    r = client.post("/api/v1/system/reboot/ghost")
    assert r.status_code == 404
    assert "ghost" in r.json()["error"]


def test_reboot_passes_the_answer_through_and_defaults_the_delay(client):
    agent = FakeAgent(reboot=SimpleNamespace(scheduled=True, scheduled_at="2026-11-07T21:00:00Z",
                                             delay_minutes=1, method="shutdown -r", error=None, hint=None))
    _wire(client, {"nano": agent})
    r = client.post("/api/v1/system/reboot/nano")
    assert r.status_code == 200
    assert r.json() == {"node": "nano", "scheduled": True, "scheduled_at": "2026-11-07T21:00:00Z",
                        "delay_minutes": 1, "method": "shutdown -r", "error": None, "hint": None}
    assert agent.calls == [("reboot", 1)]                 # no body: one minute


def test_reboot_takes_delay_minutes_from_the_body(client):
    agent = FakeAgent(reboot=SimpleNamespace(scheduled=True, scheduled_at=None, delay_minutes=5,
                                             method="m", error=None, hint=None))
    _wire(client, {"nano": agent})
    assert client.post("/api/v1/system/reboot/nano", json={"delay_minutes": 5}).status_code == 200
    assert agent.calls == [("reboot", 5)]


def test_reboot_rejects_a_delay_that_is_not_a_whole_number(client):
    agent = FakeAgent()
    _wire(client, {"nano": agent})
    r = client.post("/api/v1/system/reboot/nano", json={"delay_minutes": "soon"})
    assert r.status_code == 400
    assert "whole number" in r.json()["error"]
    assert agent.calls == []                              # refused before the node was asked


def test_reboot_unreachable_node_is_502_and_says_so(client):
    _wire(client, {"nano": FakeAgent(reboot=None)})
    r = client.post("/api/v1/system/reboot/nano")
    assert r.status_code == 502
    body = r.json()
    assert body["scheduled"] is False and "unreachable" in body["error"]


def test_reboot_cancel_routes(client):
    ok = FakeAgent(cancel=SimpleNamespace(cancelled=True, error=None))
    down = FakeAgent(cancel=None)
    _wire(client, {"nano": ok, "xavier": down})
    assert client.post("/api/v1/system/reboot/ghost/cancel").status_code == 404
    r = client.post("/api/v1/system/reboot/nano/cancel")
    assert r.status_code == 200 and r.json() == {"node": "nano", "cancelled": True, "error": None}
    r = client.post("/api/v1/system/reboot/xavier/cancel")
    assert r.status_code == 502 and r.json()["cancelled"] is False


def test_reclaim_fans_to_every_node_and_reports_the_one_that_did_not_answer(client):
    good = FakeAgent(reclaim=SimpleNamespace(stopped=["llama"], kept=["observatory"], failed=[]))
    dead = FakeAgent(reclaim=RuntimeError("no route to host"))
    _wire(client, {"nano": good, "xavier": dead})
    r = client.post("/api/v1/system/reclaim")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False                            # one node did not answer
    by = {n["node"]: n for n in body["nodes"]}
    assert by["nano"] == {"node": "nano", "ok": True, "stopped": ["llama"], "kept": ["observatory"], "failed": []}
    assert by["xavier"] == {"node": "xavier", "ok": False, "stopped": [], "kept": [], "failed": []}
    assert good.calls == [("reclaim", None, None, False)]  # the default: GPU daemons only


def test_reclaim_body_is_forwarded_and_nodes_filter_narrows_the_fan(client):
    nano = FakeAgent(reclaim=SimpleNamespace(stopped=[], kept=[], failed=[]))
    xavier = FakeAgent(reclaim=SimpleNamespace(stopped=[], kept=[], failed=[]))
    _wire(client, {"nano": nano, "xavier": xavier})
    r = client.post("/api/v1/system/reclaim",
                    json={"exclude": ["llama"], "include": ["comfy"], "all": True, "nodes": ["xavier"]})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert [n["node"] for n in r.json()["nodes"]] == ["xavier"]
    assert nano.calls == []
    assert xavier.calls == [("reclaim", ["llama"], ["comfy"], True)]
