"""
The Transit (punch list transit, 28 Oct 2026): two bodies on one box, one
GPU. Lodestar decides, the Observatory executes, the hippocampus only asks.

Chad's chart: the hippocampus wakes and asks for its llama; another llama
holds the box; scale that one down for the drafting; nobody is asked. Pinned
here:

- an ensure of a service whose policy `displaces` a running sibling stops the
  sibling through the node's Observatory first, holds it out of routing, and
  then ensures the asked-for one; the lease book and the history say so
- a sibling someone holds is WAITED for, up to the ask's own wait; a wait that
  runs out is an answer naming the holder, and nothing is stopped; once the
  holder lets go (or its lease runs out) the next ask goes through
- the transit ends on the check after the displacing service has no holder:
  the sibling is routable again and is NOT started again (on demand brings it
  back on the next ask)
- a policy with no `displaces` changes nothing
"""
from __future__ import annotations

import asyncio
import time

from seren_lodestar.run_as_needed import Steward
from seren_sinew.orchestration import EnsureRequest, Policies

from tests.test_run_as_needed import FakeAgent, FakeCluster, _client  # noqa: F401

POLICIES = {"apps": {"hippocampus-llama": {"mode": "on_demand", "idle_minutes": 1, "displaces": ["llama"]},
                     "llama": {"mode": "on_demand", "idle_minutes": 15}}}


class HoldingCluster(FakeCluster):
    """The fake cluster with the real cluster's holds."""

    def __init__(self, agent):
        super().__init__(agent)
        self._holds = {}
        self.refreshed = 0

    def hold(self, node, service, reason):
        self._holds[(node, service)] = reason

    def unhold(self, node, service):
        self._holds.pop((node, service), None)

    def holds(self):
        return {f"{n}/{s}": why for (n, s), why in sorted(self._holds.items())}

    async def refresh_node_async(self, node):
        self.refreshed += 1


def _world(tmp_path, up=None):
    agent = FakeAgent("nuc", up or {"llama": True, "hippocampus-llama": False})
    st = Steward(HoldingCluster(agent), Policies.from_dict(POLICIES),
                 tmp_path / "orchestration" / "leases.json", lease_ttl_seconds=3600)
    return st, agent


def test_the_hippocampus_llama_displaces_the_main_one_and_the_transit_ends_when_it_lets_go(tmp_path):
    st, agent = _world(tmp_path)
    c = _client(tmp_path, st)
    try:
        d = c.post("/api/v1/service/hippocampus-llama/ensure",
                   json={"holder": "seren-hippocampus", "reason": "a sleep", "wait_seconds": 5}).json()
        assert d["ok"] and d["ready"] and d["started"], d
        assert agent.stops == ["llama"], "the main llama was stopped first, through the node"
        assert agent.up == {"llama": False, "hippocampus-llama": True}
        assert st.cluster.refreshed == 1, "a fresh look at the node before deciding"
        assert st.cluster.holds() == {"nuc/llama": "in transit for hippocampus-llama (seren-hippocampus)"}
        o = c.get("/api/v1/orchestration").json()
        assert o["transits"][0]["service"] == "llama" and o["transits"][0]["for"] == "hippocampus-llama"
        assert o["holds"] == st.cluster.holds()
        stop_row = next(r for r in o["history"] if r["action"] == "stop")
        assert stop_row["service"] == "llama" and "transit" in stop_row["reason"]
        assert ("nuc", "llama") not in st.leases.entries(), "the stopped sibling's entry is forgotten"

        # The brief is sent: the hippocampus lets go. on_demand lingers, and the
        # next check ends the transit: llama routable again, NOT restarted.
        rel = c.post("/api/v1/service/hippocampus-llama/release", json={"holder": "seren-hippocampus"}).json()
        assert rel["ok"] and rel["stopped"] is False
        done = asyncio.run(st.check())
        assert [r["action"] for r in done] == ["transit_ended"], done
        assert st.transits == {} and st.cluster.holds() == {}
        assert agent.starts == [], "nothing is restored; on demand brings llama back on the next ask"
        assert agent.up["hippocampus-llama"] is True, "its own idle minute has not passed"
    finally:
        c.__exit__(None, None, None)


def test_a_held_sibling_is_waited_for_and_a_wait_that_runs_out_names_the_holder(tmp_path, monkeypatch):
    import seren_lodestar.run_as_needed as ran
    monkeypatch.setattr(ran, "TRANSIT_POLL_S", 0.01)
    st, agent = _world(tmp_path)
    # Symposium is mid-conversation on the main llama: a lease with a short life.
    st.leases.acquire("nuc", "llama", "seren-symposium", started=False, ttl=0.3)
    c = _client(tmp_path, st)
    try:
        t0 = time.monotonic()
        d = c.post("/api/v1/service/hippocampus-llama/ensure",
                   json={"holder": "seren-hippocampus", "wait_seconds": 0.05}).json()
        assert d["ok"] is False and "held by seren-symposium" in d["error"] and "displaces it" in d["error"], d
        assert agent.stops == [] and agent.up["llama"] is True, "a turn in flight is never pre-empted"
        assert st.cluster.holds() == {} and st.transits == {}
        # The holder's lease runs out: the same ask now goes through.
        time.sleep(0.35)
        d = c.post("/api/v1/service/hippocampus-llama/ensure",
                   json={"holder": "seren-hippocampus", "wait_seconds": 1}).json()
        assert d["ok"] and d["ready"], d
        assert agent.stops == ["llama"] and time.monotonic() - t0 < 5
    finally:
        c.__exit__(None, None, None)


def test_a_wait_inside_the_ask_lets_a_release_through(tmp_path, monkeypatch):
    """The holder lets go while the hippocampus waits: the transit goes on
    without a second ask."""
    import seren_lodestar.run_as_needed as ran
    monkeypatch.setattr(ran, "TRANSIT_POLL_S", 0.01)
    st, agent = _world(tmp_path)
    st.leases.acquire("nuc", "llama", "seren-symposium", started=False, ttl=0.2)
    req = EnsureRequest(holder="seren-hippocampus", wait_seconds=2)
    err = asyncio.run(st.transit(agent, "hippocampus-llama", req, "seren-hippocampus"))
    assert err is None and agent.stops == ["llama"]
    assert ("nuc", "llama") in st.transits


def test_a_policy_with_nothing_to_displace_changes_nothing(tmp_path):
    st, agent = _world(tmp_path, up={"llama": True, "kokoro": False})
    req = EnsureRequest(holder="x", wait_seconds=1)
    assert asyncio.run(st.transit(agent, "kokoro", req, "x")) is None
    assert agent.stops == [] and st.cluster.holds() == {} and st.cluster.refreshed == 0


def test_a_sibling_that_will_not_stop_is_an_answer_and_lifts_the_hold(tmp_path):
    from types import SimpleNamespace
    st, agent = _world(tmp_path)

    async def refuse(svc):
        return SimpleNamespace(ok=False, error="access is denied")
    agent.stop_service_async = refuse
    req = EnsureRequest(holder="seren-hippocampus", wait_seconds=1)
    err = asyncio.run(st.transit(agent, "hippocampus-llama", req, "seren-hippocampus"))
    assert err and "could not stop 'llama'" in err and "access is denied" in err
    assert st.cluster.holds() == {} and st.transits == {}
