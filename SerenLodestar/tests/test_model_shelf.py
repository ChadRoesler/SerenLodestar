"""
The model shelf through Lodestar: fetching a model onto the node that runs
the service and changing which one it serves. Lodestar only carries the
asking - the node pulls the file itself - and the node's own answers,
refusals included, come back as they were.
"""
from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from seren_lodestar.app import create_app
from seren_lodestar.config import LodestarConfig


class ShelfAgent:
    node_name, base_url = "node-a", "http://192.0.2.8:7777/"

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    async def service_models_call_async(self, method, service, sub, body=None):
        self.calls.append((method, service, sub, body))
        return self.answers.get((method, sub), (0, None))


class Cluster:
    def __init__(self, agent):
        self.agent = agent

    def choose_node_for(self, service):
        return SimpleNamespace(name="node-a") if service == "llama" else None

    def get_agent(self, name):
        return self.agent if name == "node-a" else None


def _client(agent):
    c = TestClient(create_app(LodestarConfig()))
    c.__enter__()
    c.app.state.cluster = Cluster(agent)
    return c


def test_the_asking_goes_to_the_node_that_has_it_and_its_answers_come_back():
    agent = ShelfAgent({
        ("POST", "models/fetch"): (200, {"ok": True, "fetch": {"state": "fetching", "name": "c.gguf"}}),
        ("GET", "models/fetch"): (200, {"state": "done", "name": "c.gguf", "bytes": 10}),
        ("PUT", "model"): (200, {"ok": True, "serving": {"current": "/m/c.gguf", "previous": "/m/q.gguf"}}),
        ("GET", "model"): (200, {"serving": {"current": "/m/c.gguf", "previous": "/m/q.gguf"}}),
        ("DELETE", "models/q.gguf"): (409, {"detail": "q.gguf is the one it serves; switch first"}),
    })
    c = _client(agent)
    try:
        r = c.post("/api/v1/service/llama/models/fetch", json={"url": "http://spark/x", "name": "c.gguf"})
        assert r.status_code == 200 and r.json()["node"] == "node-a" and r.json()["fetch"]["state"] == "fetching"
        assert agent.calls[0] == ("POST", "llama", "models/fetch", {"url": "http://spark/x", "name": "c.gguf"})
        assert c.get("/api/v1/service/llama/models/fetch").json()["state"] == "done"
        assert c.put("/api/v1/service/llama/model", json={"name": "c.gguf"}).json()["serving"]["previous"] == "/m/q.gguf"
        assert c.get("/api/v1/service/llama/model").json()["serving"]["current"] == "/m/c.gguf"
        r = c.delete("/api/v1/service/llama/models/q.gguf")
        assert r.status_code == 409 and "switch first" in r.json()["detail"], "the node's refusal, as it was"
    finally:
        c.__exit__(None, None, None)


def test_no_node_and_no_answer_are_said():
    c = _client(ShelfAgent({}))
    try:
        assert c.get("/api/v1/service/whisper/model").status_code == 503
        r = c.get("/api/v1/service/llama/model")
        assert r.status_code == 503 and r.json()["error"] == "agent_unreachable"
    finally:
        c.__exit__(None, None, None)
