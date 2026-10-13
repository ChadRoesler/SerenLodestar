"""
Lodestar keeps the main Seren apps running as needed
(seren_lodestar.run_as_needed; the rules are Sinew's and tested there).

Pinned here, Lodestar's half:

- the orchestration block parses, and the book lives beside the config (in
  memory on defaults)
- a check stops an on_demand app nobody has held for its idle minutes, through
  its node's Observatory, forgets it, and says so in the history; it marks
  on_demand apps orchestrated so off reads as idle
- a release of an on_demand app lingers for the policy instead of stopping
  at once; the default policy still stops at once (test_ensure_release)
- ensure takes a short name and finds the one install of it; two installs
  is a question back
- the book survives a Lodestar restart, and a lease past its ttl runs out
- the routes and the MCP tool
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

from seren_lodestar.app import create_app
from seren_lodestar.config import LodestarConfig
from seren_lodestar.dtos import NodeSnapshot, ServiceStatus
from seren_lodestar.run_as_needed import OrchestrationConfig, Steward, resolved_state_file
from seren_sinew.orchestration import EnsureResult, Policies

POLICIES = {"apps": {"probe": {"mode": "on_demand", "idle_minutes": 10}, "memory": "always"}}


class FakeAgent:
    def __init__(self, name, up):
        self.node_name, self.base_url, self.up = name, "http://192.0.2.200:7777/", up
        self.stops, self.starts, self.marked = [], [], []

    async def stop_service_async(self, svc):
        self.stops.append(svc)
        self.up[svc] = False
        return SimpleNamespace(ok=True)

    async def start_service_async(self, svc):
        self.starts.append(svc)
        self.up[svc] = True
        return SimpleNamespace(ok=True)

    async def set_orchestrated_async(self, svc, flag=True):
        self.marked.append(svc)
        return True

    async def ensure_service_async(self, svc, req):
        started = not self.up.get(svc)
        self.up[svc] = True
        return EnsureResult(ok=True, service=svc, node=self.node_name, ready=True, started=started, port=7400)


class FakeCluster:
    def __init__(self, agent):
        self.agent = agent

    def get_snapshots(self):
        return {"nuc": NodeSnapshot(online=True, installed_services=sorted(self.agent.up),
                                    status={s: ServiceStatus(service=s, running=on) for s, on in self.agent.up.items()})}

    def get_agent(self, n):
        return self.agent if n == "nuc" else None

    def get_capabilities(self):
        return {s: ["nuc"] for s in self.agent.up}

    def choose_node_for(self, svc):
        return SimpleNamespace(name="nuc") if svc in self.agent.up else None

    def mark_node_suspect(self, *a):
        pass


def _world(tmp_path=None, up=None):
    agent = FakeAgent("nuc", up or {"seren-probe-wren": True, "seren-memory-wren": True,
                                    "seren-observatory-wren": True, "llama": False})
    st = Steward(FakeCluster(agent), Policies.from_dict(POLICIES),
                 (tmp_path / "orchestration" / "leases.json") if tmp_path else None, lease_ttl_seconds=3600)
    return st, agent


def test_the_block_and_where_the_book_lives(tmp_path):
    oc = OrchestrationConfig.from_dict({"lease_ttl_minutes": 30, "check_seconds": 1, "default": {"idle_minutes": 5},
                                        "apps": {"theatre": "on_demand"}})
    assert oc.lease_ttl_minutes == 30 and oc.check_seconds == 5.0, "a check is never more often than 5s"
    assert oc.policies.for_service("seren-theatre-wren").mode == "on_demand"
    assert oc.policies.for_service("llama").idle_minutes == 5
    cfg = LodestarConfig(config_path=str(tmp_path / "seren-lodestar.yaml"))
    assert resolved_state_file(cfg) == (tmp_path / "orchestration" / "leases.json").resolve()
    assert resolved_state_file(LodestarConfig()) is None, "on defaults the book stays in memory"


def test_a_check_stops_an_idle_on_demand_app_and_nothing_else(tmp_path):
    st, agent = _world(tmp_path)

    async def go():
        assert await st.check(now=0) == []                    # the idle clocks start
        assert agent.marked == ["seren-probe-wren"], "on_demand: off will read as idle"
        assert await st.check(now=9 * 60) == []
        done = await st.check(now=11 * 60)
        return done
    done = asyncio.run(go())
    assert [(d["service"], d["action"], d["ok"]) for d in done] == [("seren-probe-wren", "stop", True)]
    assert agent.stops == ["seren-probe-wren"], "memory is always, the Observatory never, llama is off"
    assert ("nuc", "seren-probe-wren") not in st.leases.entries()
    assert st.describe()["history"][0]["service"] == "seren-probe-wren"
    assert (tmp_path / "orchestration" / "leases.json").is_file()


def test_held_means_kept_and_a_lease_that_runs_out_lets_go(tmp_path):
    st, agent = _world(tmp_path)

    async def go():
        st.leases.acquire("nuc", "seren-probe-wren", "seren-workbench", started=False, ttl=1800, now=0)
        await st.check(now=0)
        assert await st.check(now=25 * 60) == [], "held: kept"
        assert await st.check(now=31 * 60) == [], "the lease ran out at 30: idle from then"
        return await st.check(now=42 * 60)
    assert [d["service"] for d in asyncio.run(go())] == ["seren-probe-wren"]


def test_the_book_survives_a_restart(tmp_path):
    st, agent = _world(tmp_path)
    st.leases.acquire("nuc", "seren-probe-wren", "seren-workbench", started=True, ttl=1800, now=100)
    st.save()
    again, _ = _world(tmp_path)
    assert again.leases.node_of("seren-probe-wren", "seren-workbench") == "nuc"
    assert again.leases.entries()[("nuc", "seren-probe-wren")]["started"] is True


def _client(tmp_path, st):
    cfg = LodestarConfig(config_path=str(tmp_path / "seren-lodestar.yaml"))
    cfg.orchestration = OrchestrationConfig.from_dict(POLICIES)
    c = TestClient(create_app(cfg))
    c.__enter__()
    c.app.state.cluster = st.cluster
    c.app.state.steward = st
    return c


def test_ensure_by_short_name_and_a_release_that_lingers(tmp_path):
    st, agent = _world(tmp_path, up={"seren-probe-wren": False, "seren-memory-wren": True, "seren-memory-alice": True})
    c = _client(tmp_path, st)
    try:
        d = c.post("/api/v1/service/probe/ensure", json={"holder": "seren-workbench", "ttl_seconds": 600}).json()
        assert d["ok"] and d["ready"] and d["started"] and d["service"] == "seren-probe-wren", d
        lease = c.get("/api/v1/cluster/leases").json()["leases"][0]
        assert lease["service"] == "seren-probe-wren" and 0 < lease["expires_in"]["seren-workbench"] <= 600
        rel = c.post("/api/v1/service/probe/release", json={"holder": "seren-workbench"}).json()
        assert rel["ok"] and rel["stopped"] is False and agent.stops == [], "on_demand: the policy decides, later"
        assert st.leases.entries()[("nuc", "seren-probe-wren")]["idle_since"] is not None
        amb = c.post("/api/v1/service/memory/ensure", json={"holder": "x"}).json()
        assert amb["ok"] is False and "ambiguous" in amb["error"] and "seren-memory-alice" in amb["error"]
        o = c.get("/api/v1/orchestration").json()
        assert o["ok"] and o["policies"]["apps"]["probe"]["mode"] == "on_demand"
        assert o["apps"]["nuc/seren-probe-wren"]["policy"]["mode"] == "on_demand"
        assert c.post("/api/v1/orchestration/check").json()["ok"]
        names = {t.name for t in asyncio.run(c.app.state.tool_client.list_tools_async())}
        assert "orchestration_status" in names
    finally:
        c.__exit__(None, None, None)
