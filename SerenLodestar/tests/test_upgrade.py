"""
Lodestar drives upgrades across the cluster (seren_lodestar.upgrade); each
node's Observatory does them (seren_observatory.upgrades, tested there).

The nodes here are fake Observatory clients whose jobs end the way the test
says. Pinned here:

- the order: stores first, then their readers, the Observatory last on its
  node, Lodestar last of all; only seren services; offline nodes skipped
- each app on its own: to its own newest, or pinned per app; one version for
  a run of several apps is refused; an app already on its newest is left
  alone (only_behind), a pinned one is always installed
- before each upgrade a fresh snapshot is pulled into the stash, and the
  Observatory is then told not to take a second one; every node's versions
  go along as peers
- "not upgradable" (no pip install there) is a skip; a job that fails fails
  its app and the run goes on with the rest ("partial"); "held" (a peer too
  old) is said, not failed
- a backup that fails fails that app before it is touched, and only that app
- behind: every seren service, its version and the index's newest
- Lodestar's own upgrade is handed off; the Lodestar that starts next follows
  the job and closes the run
- the dry run is every Observatory's plan, nothing started
- the routes and the MCP tools
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

from seren_lodestar.app import create_app
from seren_lodestar.backup import BackupConfig, PullReport
from seren_lodestar.config import LodestarConfig
from seren_lodestar.dtos import NodeSnapshot
from seren_lodestar.upgrade import PinError, UpgradeService, pins, rank, short_name, wanted

NUC = ["seren-observatory-wren", "seren-lodestar-wren", "seren-corpus-callosum-wren", "seren-loci-wren",
       "seren-memory-wren", "seren-workbench-wren", "searxng"]


class FakeAgent:
    def __init__(self, ends=None, not_upgradable=(), services=NUC):
        self.ends = ends or {}                # service -> how its job ends
        self.not_upgradable = set(not_upgradable)
        self.started: list[tuple[str, dict]] = []
        self.planned: list[tuple[str, dict]] = []
        self.jobs: dict[str, dict] = {}
        self.silent: set[str] = set()         # jobs the Observatory cannot answer for (restarting)
        self.ver = {s: "5.0.0" for s in services if s.startswith("seren-") and s not in self.not_upgradable}

    async def get_upgrade_versions_async(self):
        return {s: {"package": s.rsplit("-", 1)[0], "version": v, "needs": [],
                    "libs": {"seren-sinew": "5.0.0", "seren-meninges": "4.9.0"}} for s, v in self.ver.items()}

    async def start_upgrade_async(self, svc, body, timeout=900.0):
        if body.get("dry_run"):
            self.planned.append((svc, body))
            if svc in self.not_upgradable:
                return 409, {"ok": False, "upgradable": False, "error": "no install record"}
            pkg = svc.rsplit("-", 1)[0]
            return 200, {"ok": True, "dry_run": True, "package": pkg, "before": {svc: "5.0.0"},
                         "would_install": [{"name": pkg, "version": "5.1.0"}]}
        self.started.append((svc, body))
        if svc in self.not_upgradable:
            return 409, {"ok": False, "upgradable": False, "error": "no install record"}
        jid = f"job-{len(self.started)}"
        end = self.ends.get(svc, "done")
        why = {"held": "seren-corpus-callosum 5.1.0 needs seren-memory>=5.2; seren-memory-wren is 5.0.0"}
        self.jobs[jid] = {"id": jid, "service": svc, "state": end, "terminal": True,
                          "before": {svc: "5.0.0"}, "after": {svc: "5.0.1"},
                          **({"error": why.get(end, "it did not come back healthy")} if end != "done" else {})}
        if end == "done":
            self.ver[svc] = body.get("version") or "5.0.1"
        return 202, {"ok": True, "job": {"id": jid, "state": "queued"}}

    async def get_upgrade_job_async(self, jid):
        return None if jid in self.silent else self.jobs.get(jid)


class FakeBackup:
    def __init__(self, fails=()):
        self.pulls: list[tuple[str, str]] = []
        self.fails = set(fails)

    async def pull(self, reason="x", node=None, service=None, take_fresh=None):
        self.pulls.append((node, service))
        rep = PullReport(started_at=0.0, reason=reason)
        if service in self.fails:
            rep.errors.append(f"{node}/{service}: the service would not take a snapshot")
            rep.nodes[node] = {service: {"stashed": []}}
        else:
            rep.nodes[node] = {service: {"stashed": ["20261012-0200"], "took_fresh": take_fresh}}
        return rep


class FakeCluster:
    def __init__(self, nodes):
        self.nodes = nodes

    def get_snapshots(self):
        return {n: NodeSnapshot(online=on, installed_services=list(svcs), status={})
                for n, (on, svcs, _a) in self.nodes.items()}

    def get_agent(self, n):
        return self.nodes[n][2] if n in self.nodes else None


NANO = ["seren-observatory-nano", "llama"]


def _latest(newest=None):
    newest = newest or {}

    async def latest(pkg, force=False):
        return newest.get(pkg, "5.0.1"), None
    return latest


def _world(tmp_path, nuc=None, nano=None, backup=None, newest=None, **kw):
    nuc = nuc or FakeAgent(not_upgradable={"searxng"})
    nano = nano or FakeAgent(services=NANO)
    cluster = FakeCluster({"nuc": (True, NUC, nuc), "nano": (True, NANO, nano),
                           "xavier": (False, ["seren-observatory-xavier"], FakeAgent(services=[]))})
    svc = UpgradeService(cluster, backup if backup is not None else FakeBackup(), tmp_path / "upgrades",
                         poll=0.01, latest=_latest(newest), **kw)
    return svc, nuc, nano


async def _until_done(svc):
    for _ in range(500):
        if not svc.busy:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the run never ended")


def test_names_and_order():
    assert short_name("seren-corpus-callosum-wren") == "corpuscallosum"
    assert short_name("SerenCorpusCallosum-wren") == "corpuscallosum"
    assert short_name("llama") is None and short_name("searxng") is None
    by = sorted(NUC[:-1], key=rank)
    assert by == ["seren-memory-wren", "seren-loci-wren", "seren-corpus-callosum-wren", "seren-workbench-wren",
                  "seren-observatory-wren", "seren-lodestar-wren"]
    assert wanted("seren-corpus-callosum-wren", ["corpus-callosum"]) and not wanted("seren-loci-wren", ["memory"])
    assert wanted("seren-memory-wren", ["seren-memory-wren"]) and not wanted("llama", None)


def test_pins_name_their_app():
    order = [("nuc", "seren-memory-wren"), ("nuc", "seren-corpus-callosum-wren")]
    assert pins(order, None, None) == {"nuc/seren-memory-wren": None, "nuc/seren-corpus-callosum-wren": None}
    assert pins(order, None, {"memory": "5.2.0"})["nuc/seren-memory-wren"] == "5.2.0"
    assert pins(order[:1], "5.2.0", None) == {"nuc/seren-memory-wren": "5.2.0"}
    for bad in ((order, "5.2.0", None), (order, None, {"loci": "5.2.0"})):
        try:
            pins(*bad)
        except PinError:
            continue
        raise AssertionError(f"{bad} should not pin")


def test_a_run_backs_up_then_upgrades_in_order_and_lodestar_goes_last(tmp_path):
    backup = FakeBackup()
    svc, nuc, nano = _world(tmp_path, backup=backup)

    async def go():
        assert svc.start(version="5.0.1")[0] == 400, "one number on every app: no"
        status, out = svc.start(reason="the round")
        assert status == 202 and out["run"]["skipped_offline"] == ["xavier"]
        await _until_done(svc)
        return svc.load(out["run"]["id"])
    run = asyncio.run(go())
    assert run["state"] == "done", run
    order = [(s["node"], s["service"]) for s in run["steps"]]
    assert order == [("nano", "seren-observatory-nano"),
                     ("nuc", "seren-memory-wren"), ("nuc", "seren-loci-wren"), ("nuc", "seren-corpus-callosum-wren"),
                     ("nuc", "seren-workbench-wren"), ("nuc", "seren-observatory-wren"),
                     ("nuc", "seren-lodestar-wren")], "no llama, no searxng, the Observatory last, Lodestar last of all"
    assert backup.pulls == order, "a copy off the box before each one"
    assert all(b["snapshot"] is False and b["version"] is None for _s, b in nuc.started), "each to its newest"
    callosum = dict(nuc.started)["seren-corpus-callosum-wren"]["peers"]
    assert {"service": "seren-memory-wren", "node": "nuc", "package": "seren-memory", "version": "5.0.1"} \
        in callosum, "the peers are what the cluster runs by then: Memory already moved"
    assert all(s["state"] == "done" and s["after"] for s in run["steps"])


def test_one_failure_does_not_hold_the_others_back(tmp_path):
    svc, nuc, nano = _world(tmp_path, nuc=FakeAgent(ends={"seren-loci-wren": "rolled_back",
                                                          "seren-corpus-callosum-wren": "held"}))

    async def go():
        _s, out = svc.start(nodes=["nuc"])
        await _until_done(svc)
        return svc.load(out["run"]["id"])
    run = asyncio.run(go())
    assert run["state"] == "partial" and run["failed"] == ["nuc/seren-loci-wren: rolled_back - it did not "
                                                           "come back healthy"]
    states = {s["service"]: s["state"] for s in run["steps"]}
    assert states == {"seren-memory-wren": "done", "seren-loci-wren": "failed", "seren-corpus-callosum-wren": "held",
                      "seren-workbench-wren": "done", "seren-observatory-wren": "done", "seren-lodestar-wren": "done"}
    assert "needs seren-memory>=5.2" in run["held"][0], "held is said, and is not a failure"
    assert nano.started == [], "--nodes nuc"


def test_what_is_on_its_newest_is_left_alone_and_a_pin_always_goes(tmp_path):
    backup = FakeBackup()
    svc, nuc, nano = _world(tmp_path, backup=backup, newest={"seren-loci": "5.0.0", "seren-memory": "5.0.0"})

    async def go():
        _s, out = svc.start(services=["memory", "loci", "workbench"], versions={"memory": "4.9.0"})
        await _until_done(svc)
        return svc.load(out["run"]["id"])
    run = asyncio.run(go())
    states = {s["service"]: s["state"] for s in run["steps"]}
    assert states == {"seren-memory-wren": "done", "seren-loci-wren": "current", "seren-workbench-wren": "done"}
    assert run["state"] == "done" and ("nuc", "seren-loci-wren") not in backup.pulls, "current: not even a backup"
    assert dict(nuc.started)["seren-memory-wren"]["version"] == "4.9.0", "pinned: installed, newest or not"


def test_no_copy_off_the_box_no_upgrade(tmp_path):
    svc, nuc, nano = _world(tmp_path, backup=FakeBackup(fails={"seren-loci-wren"}))

    async def go():
        _s, out = svc.start(services=["memory", "loci", "workbench"])
        await _until_done(svc)
        return svc.load(out["run"]["id"])
    run = asyncio.run(go())
    assert run["state"] == "partial" and "the backup failed" in run["error"]
    assert [s for s, _b in nuc.started] == ["seren-memory-wren", "seren-workbench-wren"], "loci never touched"


def test_without_backups_the_observatory_takes_its_own_snapshot(tmp_path):
    svc, nuc, nano = _world(tmp_path, backup=False)
    svc._backup = None

    async def go():
        _s, out = svc.start(services=["memory"])
        await _until_done(svc)
        return svc.load(out["run"]["id"])
    assert asyncio.run(go())["state"] == "done"
    assert nuc.started[0][1]["snapshot"] is True and nuc.started[0][1]["version"] is None


def test_lodestar_hands_its_own_upgrade_off_and_the_next_one_closes_the_run(tmp_path):
    nuc = FakeAgent()
    svc, _nuc, nano = _world(tmp_path, nuc=nuc)

    async def first_life():
        nuc.silent.add("job-2")                         # the Observatory is busy restarting Lodestar
        _s, out = svc.start(nodes=["nuc"], services=["memory", "lodestar"])
        for _ in range(500):
            run = svc.load(out["run"]["id"])
            if run["state"] == "handed_off":
                break
            await asyncio.sleep(0.01)
        svc._task.cancel()                              # this process ends here
        return out["run"]["id"]
    rid = asyncio.run(first_life())
    run = svc.load(rid)
    assert run["state"] == "handed_off" and run["finished_at"] is None
    step = run["steps"][-1]
    assert step["service"] == "seren-lodestar-wren" and step["state"] == "handed_off" and step["job"] == "job-2"

    nuc.silent.clear()                                  # the new Lodestar is up
    again, _n, _m = _world(tmp_path, nuc=nuc)
    assert asyncio.run(again.resume()) == [rid]
    run = again.load(rid)
    assert run["state"] == "done" and run["steps"][-1]["state"] == "done" and run["finished_at"]


def test_the_dry_run_is_every_plan_and_starts_nothing(tmp_path):
    svc, nuc, nano = _world(tmp_path, newest={"seren-loci": "5.0.0"})
    assert asyncio.run(svc.plan(version="5.0.1"))["ok"] is False, "one version, many apps"
    out = asyncio.run(svc.plan())
    assert out["ok"] and out["dry_run"] and out["order"][-1] == "nuc/seren-lodestar-wren"
    assert out["nodes"]["nuc"]["seren-memory-wren"]["before"] == {"seren-memory-wren": "5.0.0"}
    assert out["current"] == ["nuc/seren-loci-wren"]
    callosum = dict(nuc.planned)["seren-corpus-callosum-wren"]["peers"]
    assert {"service": "seren-memory-wren", "node": "nuc", "package": "seren-memory", "version": "5.1.0"} \
        in callosum, "each plan sees what the ones before it would bring"
    assert nuc.started == [] and nano.started == []


def test_behind_is_every_service_against_the_index(tmp_path):
    svc, nuc, nano = _world(tmp_path, newest={"seren-loci": "5.0.0", "seren-memory": "5.2.0"})
    out = asyncio.run(svc.behind(services=["memory", "loci", "observatory"]))
    rows = {(r["node"], r["service"]): (r["version"], r["latest"], r["behind"]) for r in out["services"]}
    assert rows == {("nano", "seren-observatory-nano"): ("5.0.0", "5.0.1", True),
                    ("nuc", "seren-memory-wren"): ("5.0.0", "5.2.0", True),
                    ("nuc", "seren-loci-wren"): ("5.0.0", "5.0.0", False),
                    ("nuc", "seren-observatory-wren"): ("5.0.0", "5.0.1", True)}
    assert "nuc/seren-memory-wren 5.0.0 -> 5.2.0" in out["behind"] and out["unknown_nodes"] == []


def test_behind_carries_the_libraries_each_venv_runs_against_the_index(tmp_path):
    """20 Oct 2026: click a service, see its own version and the sinew
    and meninges it runs with their newest. The libraries are not in
    `behind` (a run moves services; the libraries ride along), they have
    their own list."""
    svc, nuc, nano = _world(tmp_path, newest={"seren-memory": "5.0.0", "seren-meninges": "5.0.0", "seren-sinew": "5.0.0"})
    out = asyncio.run(svc.behind(nodes=["nuc"], services=["memory"]))
    (row,) = out["services"]
    assert row["behind"] is False, "memory itself is on its newest"
    assert row["libs"] == {"seren-sinew": {"version": "5.0.0", "latest": "5.0.0", "behind": False},
                           "seren-meninges": {"version": "4.9.0", "latest": "5.0.0", "behind": True}}
    assert out["behind"] == [] and out["libs_behind"] == ["nuc/seren-memory-wren seren-meninges 4.9.0 -> 5.0.0"]


def test_a_library_alone_is_a_run_of_its_service_pinned_where_it_is(tmp_path):
    """The Update button on a library row: the service pinned to the version
    it has, `with` naming the library. pip moves the library, the service
    stays, and the Observatory still restarts it so it runs the new code."""
    svc, nuc, nano = _world(tmp_path)

    async def go():
        status, out = svc.start(nodes=["nuc"], services=["memory"], version="5.0.0", with_=["seren-meninges"],
                                only_behind=False, reason="pkg modal")
        assert status == 202, out
        await _until_done(svc)
        return out
    out = asyncio.run(go())
    (started,) = nuc.started
    assert started[0] == "seren-memory-wren"
    assert started[1]["version"] == "5.0.0" and started[1]["with"] == ["seren-meninges"]
    assert svc.load(out["run"]["id"])["steps"][0]["state"] == "done"


def test_the_routes_and_the_tools(tmp_path):
    cfg = LodestarConfig(config_path=str(tmp_path / "seren-lodestar.yaml"))
    cfg.backup = BackupConfig(dir=str(tmp_path / "stash"), every_hours=0)
    with TestClient(create_app(cfg)) as c:
        assert c.app.state.upgrades.root == (tmp_path / "upgrades").resolve()
        svc, nuc, nano = _world(tmp_path)
        c.app.state.upgrades = svc
        p = c.post("/api/v1/upgrade", json={"dry_run": True, "services": "memory"}).json()
        assert p["order"] == ["nuc/seren-memory-wren"]
        assert c.post("/api/v1/upgrade", json={"services": ["memory", "loci"], "version": "5.0.1"}).status_code == 400
        assert c.post("/api/v1/upgrade", json={"dry_run": True, "version": "5.0.1"}).status_code == 400
        assert c.get("/api/v1/upgrade/behind?services=memory").json()["behind"] == ["nuc/seren-memory-wren 5.0.0 -> 5.0.1"]
        r = c.post("/api/v1/upgrade", json={"services": ["memory"], "version": "5.0.1"})
        assert r.status_code == 202
        rid = r.json()["run"]["id"]
        for _ in range(500):
            if c.get(f"/api/v1/upgrade/{rid}").json()["run"]["state"] != "running":
                break
        assert c.get(f"/api/v1/upgrade/{rid}").json()["run"]["state"] == "done"
        assert c.get("/api/v1/upgrade").json()["runs"][0]["id"] == rid
        # One step with the job behind it, from the node (20 Oct 2026: the
        # viewer's run modal reads the job's notes as the log of a failure).
        st = c.get(f"/api/v1/upgrade/{rid}/step/0").json()
        assert st["ok"] and st["step"]["service"] == "seren-memory-wren" and st["note"] is None
        assert st["job"]["id"] == st["step"]["job"] and st["job"]["state"] == "done"
        assert c.get(f"/api/v1/upgrade/{rid}/step/9").status_code == 404
        assert c.get("/api/v1/upgrade/nope/step/0").status_code == 404
        nuc.silent.add(st["step"]["job"])
        gone = c.get(f"/api/v1/upgrade/{rid}/step/0").json()
        assert gone["job"] is None and "did not answer" in gone["note"]
        assert c.post("/api/v1/upgrade", json={"services": ["nothing-here"]}).status_code == 409
        assert c.get("/api/v1/upgrade/nope").status_code == 404
        names = {t.name for t in asyncio.run(c.app.state.tool_client.list_tools_async())}
        assert {"upgrade_plan", "upgrade_start", "upgrade_status", "upgrade_behind"} <= names
