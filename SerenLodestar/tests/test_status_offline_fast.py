"""
The dashboard poll must not wait on a dead node (20 Oct 2026). With the
desktop's service stopped and its port dropping packets, every
/api/v1/system/status took 35s (the client's default timeout, three times
over, in parallel) and the dashboard called the whole cluster unreachable.
Pinned: a node the discovery snapshot calls offline is answered from the
snapshot without a live read; the live reads of an online node carry the
short STATUS_READ_TIMEOUT_S.
"""
from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

from seren_lodestar.agent_client import STATUS_READ_TIMEOUT_S, JetsonAgentClient
from seren_lodestar.app import create_app
from seren_lodestar.cluster import NodeSnapshot
from seren_lodestar.config import LodestarConfig


class SlowAgent:
    nickname = "the window"
    is_host = False

    def __init__(self):
        self.asked = 0

    async def _slow(self):
        self.asked += 1
        await asyncio.sleep(5)
        return None

    get_node_async = _slow
    get_thermal_async = _slow
    get_services_async = _slow


class FakeCluster:
    def __init__(self, agents, snaps):
        self.agents = agents
        self._snaps = snaps

    def get_snapshots(self):
        return self._snaps

    def mark_node_suspect(self, name, reason):
        pass

    def suspect_reason(self, name):
        return None


def test_an_offline_node_is_answered_from_the_snapshot_without_a_live_read(tmp_path):
    cfg = LodestarConfig(config_path=str(tmp_path / "seren-lodestar.yaml"))
    with TestClient(create_app(cfg)) as c:
        agent = SlowAgent()
        c.app.state.cluster = FakeCluster({"desktop": agent}, {
            "desktop": NodeSnapshot(online=False, installed_services=["SerenObservatory-wren"], status={},
                                    last_error="discovery timed out after 2.0s")})
        t0 = time.monotonic()
        body = c.get("/api/v1/system/status").json()
        assert time.monotonic() - t0 < 2.0, "no waiting on a dead socket"
        assert agent.asked == 0
        node = body["nodes"][0]
        assert node["online"] is False and node["last_error"] == "discovery timed out after 2.0s"
        assert node["installed_services"] == ["SerenObservatory-wren"] and node["services_detail"] is None


async def test_the_status_reads_carry_the_short_timeout(monkeypatch):
    c = JetsonAgentClient.__new__(JetsonAgentClient)
    seen = []

    class Client:
        async def get(self, path, timeout=None):
            seen.append((path, timeout))
            raise RuntimeError("no network in this test")
    c._client = Client()
    c._log = lambda m: None
    c._node_name = "x"
    await c.get_node_async(); await c.get_thermal_async(); await c.get_services_async()
    assert [t for _, t in seen] == [STATUS_READ_TIMEOUT_S] * 3
    assert STATUS_READ_TIMEOUT_S < 35.0


async def test_the_versions_read_connects_fast_and_waits_long():
    """/upgrade/behind asks every online node's venvs (slow) but must not
    hang on a node that will not even connect (20 Oct 2026: the desktop's
    port dropped packets and every behind took the full 120s)."""
    import httpx
    c = JetsonAgentClient.__new__(JetsonAgentClient)
    seen = []

    class Client:
        async def get(self, path, timeout=None):
            seen.append(timeout)
            raise RuntimeError("no network in this test")
    c._client = Client()
    c._log = lambda m: None
    c._node_name = "x"
    assert await c.get_upgrade_versions_async() is None
    (t,) = seen
    assert isinstance(t, httpx.Timeout) and t.connect == 5.0 and t.read == 120.0
