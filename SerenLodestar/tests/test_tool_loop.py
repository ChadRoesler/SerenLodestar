"""
The chat loop calls a tool. End to end, through the real app.

A fake llama answers the first request with a tool call for Lodestar's own
`scheduler_list` and the second with a sentence that quotes the tool
result. The tool runs in-process on the FastMCP server the app mounted, so
what is under test is the whole seam: dialect -> tool client -> FastMCP ->
dialect -> model. None of it had ever executed before this test existed.
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from seren_lodestar.app import create_app
from seren_lodestar.config import LodestarConfig
from seren_lodestar.dtos import RoutedService, ServiceManifest

TOOL_CALL = '<tool_call>{"name": "scheduler_list", "arguments": {}}</tool_call>'


class FakeLlama:
    """Answers like llama-server would. Records every request it saw."""

    def __init__(self, first: str, second: str = "There are no scheduled tasks right now."):
        self.first, self.second = first, second
        self.requests: list[dict] = []

    def _answer(self, body: dict) -> str:
        saw_tool_result = any("<tool_response>" in (m.get("content") or "")
                              for m in body["messages"])
        return self.second if saw_tool_result else self.first

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        text = self._answer(body)
        if body.get("stream"):
            # llama-server streams in pieces; split the text so a tag can
            # straddle chunk boundaries the way it would in real life.
            pieces = [text[i:i + 7] for i in range(0, len(text), 7)]
            lines = [
                "data: " + json.dumps({"model": "fake", "choices": [{"delta": {"content": p}}]})
                for p in pieces
            ] + ["data: [DONE]"]
            return httpx.Response(200, content="\n".join(lines).encode(),
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={
            "model": "fake",
            "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"total_tokens": 5},
        })


@pytest.fixture
def wired(monkeypatch):
    """A running app whose llama is a fake and whose cluster routes to it."""
    app = create_app(LodestarConfig())
    with TestClient(app) as client:
        state = client.app.state
        llama = FakeLlama(TOOL_CALL)
        fake_client = httpx.AsyncClient(transport=httpx.MockTransport(llama.handler))
        real_factory = state.http_client_factory
        state.http_client_factory = (lambda name: fake_client if name == "llama-upstream"
                                     else real_factory(name))

        async def routed(capability):
            return RoutedService(node_name="fake-node", capability=capability,
                                 manifest=ServiceManifest(service="llama", port=8090),
                                 base_url="http://fake-llama:8090")
        monkeypatch.setattr(state.cluster, "get_service_url_async", routed)
        yield client, llama


def test_the_tools_are_actually_listed(wired):
    client, _ = wired
    r = client.post("/api/v1/chat/inspect", json={"prompt": "hi"})
    assert r.status_code == 200
    names = r.json()["tool_names"]
    assert "scheduler_list" in names and "service_control" in names
    assert r.json()["has_tools_block"] is True


def test_a_chat_runs_a_tool_and_answers_from_its_result(wired):
    client, llama = wired
    r = client.post("/api/v1/chat", json={"prompt": "what's scheduled?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tool_rounds"] == 1
    assert "no scheduled tasks" in body["response"]
    assert len(llama.requests) == 2
    second = llama.requests[1]["messages"]
    tool_msgs = [m for m in second if "<tool_response>" in (m.get("content") or "")]
    assert tool_msgs, "the tool result never went back to the model"
    assert '"tasks": []' in tool_msgs[0]["content"], "scheduler_list ran and answered"


def test_streaming_runs_the_tool_with_one_generation_per_round(wired):
    client, llama = wired
    r = client.post("/api/v1/chat/stream", json={"prompt": "what's scheduled?"})
    assert r.status_code == 200
    events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    kinds = [e["type"] for e in events]
    assert "tool_status" in kinds and kinds[-1] == "done"
    assert events[-1]["tool_rounds"] == 1
    assert "no scheduled tasks" in events[-1]["response"]
    assert len(llama.requests) == 2, "one streamed generation per round - no probe call first"
    assert all(req.get("stream") for req in llama.requests)


def test_a_streamed_tool_call_never_leaks_its_tag_to_the_client(wired, monkeypatch):
    client, llama = wired
    llama.first = "Let me check the schedule. " + TOOL_CALL
    r = client.post("/api/v1/chat/stream", json={"prompt": "what's scheduled?"})
    events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    chunks = "".join(e["content"] for e in events if e["type"] == "chunk")
    assert "Let me check the schedule." in chunks
    assert "<tool_call" not in chunks and "scheduler_list" not in chunks.split("no scheduled")[0]


def test_a_slow_generation_is_a_504_and_the_node_stays_in_the_cluster(wired, monkeypatch):
    client, _ = wired
    state = client.app.state

    def timeout_handler(request):
        raise httpx.ReadTimeout("model is thinking", request=request)
    slow = httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler))
    state.http_client_factory = lambda name: slow
    marked = []
    monkeypatch.setattr(state.cluster, "mark_node_suspect", lambda n, r, **k: marked.append((n, r)))
    r = client.post("/api/v1/chat", json={"prompt": "write me a novel"})
    assert r.status_code == 504
    assert marked == [], "a timeout is a slow model, not a dead node"


def test_a_refused_connection_marks_the_node_suspect_not_offline_forever(wired, monkeypatch):
    client, _ = wired
    state = client.app.state

    def refused(request):
        raise httpx.ConnectError("connection refused", request=request)
    state.http_client_factory = lambda name: httpx.AsyncClient(transport=httpx.MockTransport(refused))
    marked = []
    monkeypatch.setattr(state.cluster, "mark_node_suspect", lambda n, r, **k: marked.append(n))
    r = client.post("/api/v1/chat", json={"prompt": "hi"})
    assert r.status_code == 502
    assert marked == ["fake-node"]
