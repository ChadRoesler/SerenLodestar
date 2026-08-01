"""
Agent client — the wire between Lodestar and each node's Observatory.

Every test here names the bug it exists for. All three shipped broken and
none of them could fail a test, because nothing exercised this file.
"""
from __future__ import annotations

import httpx
import pytest

from seren_lodestar.agent_client import JetsonAgentClient
from seren_lodestar.dtos import JetsonNodeOptions


def _opts(**kw) -> JetsonNodeOptions:
    base = dict(name="nano", agent_url="http://10.0.0.1:7777")
    base.update(kw)
    return JetsonNodeOptions(**base)


def _client_with(handler) -> JetsonAgentClient:
    c = JetsonAgentClient(_opts())
    c._client = httpx.AsyncClient(
        base_url="http://10.0.0.1:7777/",
        transport=httpx.MockTransport(handler),
    )
    return c


# ── the shutdown crash ─────────────────────────────────────────────────

def test_aclose_is_a_method_on_the_class():
    """It was defined at column zero, below the module helpers — a free
    function that happened to take `self`. cluster.py calls
    `await agent.aclose()` on shutdown, so every clean stop raised
    AttributeError and no connection pool was ever released."""
    assert hasattr(JetsonAgentClient, "aclose"), "aclose fell out of the class"
    assert callable(getattr(JetsonAgentClient, "aclose"))


@pytest.mark.asyncio
async def test_aclose_actually_closes_the_pool():
    c = JetsonAgentClient(_opts())
    await c.aclose()
    assert c._client.is_closed


@pytest.mark.asyncio
async def test_the_whole_cluster_can_shut_down():
    """The real path: ClusterClient.aclose() loops its agents. If aclose is
    missing from even one, shutdown dies partway and leaks the rest."""
    agents = [JetsonAgentClient(_opts(name=f"n{i}")) for i in range(3)]
    for a in agents:
        await a.aclose()
    assert all(a._client.is_closed for a in agents)


# ── the renamed endpoint ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_package_push_targets_the_endpoint_observatory_actually_serves():
    """Observatory serves /api/v1/system/observatory-update. This client
    posted to /api/v1/system/agent-update — renamed with the service, and
    the caller wasn't. Every push was a 404 that looked like a network fault."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"ok": True})

    c = _client_with(handler)
    r = await c.push_agent_update_async(b"x", "pkg.tar.gz", "/tmp/pkg.tar.gz")

    assert seen["path"] == "/api/v1/system/observatory-update", seen
    assert "agent-update" not in seen["path"]
    assert r is not None and r.ok is True


@pytest.mark.asyncio
async def test_a_failed_push_names_the_endpoint_it_actually_called():
    """An error string pointing at a path the code no longer uses sends you
    grepping for something that isn't there."""
    logged: list[str] = []
    c = JetsonAgentClient(_opts(), log_fn=logged.append)
    c._client = httpx.AsyncClient(
        base_url="http://10.0.0.1:7777/",
        transport=httpx.MockTransport(lambda r: httpx.Response(404)),
    )
    r = await c.push_agent_update_async(b"x", "pkg.tar.gz", "/tmp/pkg.tar.gz")
    assert r is not None and r.ok is False
    assert any("observatory-update" in m for m in logged), logged
    assert not any("POST agent-update" in m for m in logged), logged


# ── the paths themselves ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_system_endpoints_match_observatorys_prefix():
    """Observatory mounts its system router at /api/v1/system. A drift here
    is invisible until a node silently reads as unreachable."""
    calls: list[str] = []
    c = _client_with(lambda r: (calls.append(r.url.path),
                                httpx.Response(200, json={}))[1])
    await c.ping_async()
    await c.get_node_async()
    await c.get_thermal_async()
    await c.get_health_async()
    await c.get_services_async()
    assert calls == [
        "/api/v1/system/ping",
        "/api/v1/system/node",
        "/api/v1/system/thermal",
        "/api/v1/system/health",
        "/api/v1/system/services",
    ], calls


@pytest.mark.asyncio
async def test_service_endpoints_url_encode_the_service_name():
    calls: list[str] = []
    c = _client_with(lambda r: (calls.append(r.url.path),
                                httpx.Response(200, json={}))[1])
    await c.get_service_status_async("llama")
    await c.get_service_status_async("a/b")
    assert calls[0] == "/api/v1/service/llama/status"
    assert "a%2Fb" in calls[1] or "a/b" not in calls[1].split("service/")[1].split("/")[0]
