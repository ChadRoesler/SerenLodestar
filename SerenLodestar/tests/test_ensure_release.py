"""
Lodestar's ensure / release (seren_sinew.orchestration).

The chain: hippocampus => Lodestar => Observatory => start llama => the
Observatory waits until llama is up => Lodestar tells the hippocampus it is
ready. Pinned here, Lodestar's link:

- ensure picks the node, forwards to its Observatory, and the answer carries
  READY and the address to send requests to
- a second holder shares the service; it is stopped when the LAST one lets go
- a service that was already running is never stopped by a release
- failures are answers: no node has it, the node says it will not start
- a node configured by loopback hands out an address a remote caller can use
"""
from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from seren_lodestar.app import create_app
from seren_lodestar.config import LodestarConfig
from seren_sinew.orchestration import EnsureResult


class FakeAgent:
    def __init__(self, name, base, running=False, will_start=True):
        self.node_name, self.base_url = name, base
        self.running, self.will_start = running, will_start
        self.ensures, self.stops = [], 0

    async def ensure_service_async(self, service, req):
        self.ensures.append((service, req.holder, req.reason, req.wait_seconds))
        if self.running:
            return EnsureResult(ok=True, service=service, node=self.node_name, ready=True, already_running=True, port=8090)
        if not self.will_start:
            return EnsureResult.failed(service, "llama did not start: model.gguf: no such file", self.node_name)
        self.running = True
        return EnsureResult(ok=True, service=service, node=self.node_name, ready=True, started=True, port=8090,
                            waited_seconds=41.0)

    async def stop_service_async(self, service):
        self.stops += 1
        self.running = False
        return SimpleNamespace(ok=True)


class FakeCluster:
    def __init__(self, agents, has):
        self.agents, self.has, self.suspect = agents, has, []

    def choose_node_for(self, service):
        name = self.has.get(service)
        return SimpleNamespace(name=name) if name else None

    def get_agent(self, name):
        return self.agents.get(name)

    def mark_node_suspect(self, name, why):
        self.suspect.append(name)


def _client(agents, has):
    c = TestClient(create_app(LodestarConfig()))
    c.__enter__()
    c.app.state.cluster = FakeCluster(agents, has)
    return c


def test_ensure_answers_ready_with_the_address_and_release_stops_it():
    nano = FakeAgent("node-a", "http://192.0.2.101:7777/")
    c = _client({"node-a": nano}, {"llama": "node-a"})
    try:
        r = c.post("/api/v1/service/llama/ensure", json={"holder": "seren-hippocampus", "reason": "a sleep", "wait_seconds": 120})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["ok"] and d["ready"] and d["started"] and d["node"] == "node-a", d
        assert d["base_url"] == "http://192.0.2.101:8090" and d["holders"] == ["seren-hippocampus"]
        assert nano.ensures == [("llama", "seren-hippocampus", "a sleep", 120.0)]
        assert c.get("/api/v1/cluster/leases").json()["leases"] == [
            {"node": "node-a", "service": "llama", "holders": ["seren-hippocampus"], "started_by_lease": True}]

        # a second holder shares it; the first letting go does not stop it
        d2 = c.post("/api/v1/service/llama/ensure", json={"holder": "symposium"}).json()
        assert d2["ready"] and d2["already_running"] and d2["holders"] == ["seren-hippocampus", "symposium"]
        rel = c.post("/api/v1/service/llama/release", json={"holder": "seren-hippocampus"}).json()
        assert rel == {"ok": True, "service": "llama", "node": "node-a", "stopped": False, "holders": ["symposium"], "error": ""}
        assert nano.stops == 0
        rel = c.post("/api/v1/service/llama/release", json={"holder": "symposium", "reason": "done"}).json()
        assert rel["stopped"] is True and rel["holders"] == [] and nano.stops == 1 and nano.running is False
        # nothing held: a release is a quiet yes
        assert c.post("/api/v1/service/llama/release", json={"holder": "symposium"}).json()["ok"] is True
        assert nano.stops == 1
    finally:
        c.__exit__(None, None, None)


def test_a_service_someone_else_started_is_never_stopped_by_a_release():
    xavier = FakeAgent("node-b", "http://192.0.2.103:7777", running=True)
    c = _client({"node-b": xavier}, {"kokoro": "node-b"})
    try:
        d = c.post("/api/v1/service/kokoro/ensure", json={"holder": "a"}).json()
        assert d["ready"] and d["already_running"] and not d["started"]
        rel = c.post("/api/v1/service/kokoro/release", json={"holder": "a"}).json()
        assert rel["ok"] and rel["stopped"] is False and xavier.stops == 0 and xavier.running is True
    finally:
        c.__exit__(None, None, None)


def test_failures_are_answers_and_take_no_lease():
    nano = FakeAgent("node-a", "http://192.0.2.101:7777", will_start=False)
    c = _client({"node-a": nano}, {"llama": "node-a"})
    try:
        d = c.post("/api/v1/service/whisper/ensure", json={"holder": "a"}).json()
        assert d["ok"] is False and d["error"] == "no online node has 'whisper' installed"
        d = c.post("/api/v1/service/llama/ensure", json={"holder": "a"}).json()
        assert d["ok"] is False and d["ready"] is False and "model.gguf" in d["error"] and d["base_url"] == ""
        assert c.get("/api/v1/cluster/leases").json()["leases"] == []
        d = c.post("/api/v1/service/llama/ensure", json={"holder": "a", "node": "mars"}).json()
        assert d["ok"] is False and "not in the cluster config" in d["error"]
    finally:
        c.__exit__(None, None, None)


def test_a_loopback_node_hands_out_an_address_the_caller_can_use():
    nuc = FakeAgent("nuc", "http://127.0.0.1:7777")
    c = _client({"nuc": nuc}, {"llama": "nuc"})
    try:
        d = c.post("/api/v1/service/llama/ensure", json={"holder": "a"}, headers={"Host": "192.0.2.200:6361"}).json()
        assert d["base_url"] == "http://192.0.2.200:8090", "not 127.0.0.1: the caller may be on another box"
    finally:
        c.__exit__(None, None, None)
