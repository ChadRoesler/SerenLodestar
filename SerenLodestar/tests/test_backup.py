"""
Lodestar pulls every node's services' snapshots and stashes them
(seren_lodestar.backup). Design note: Lodestar does "the requesting of
them, and pulling them, and stashing them."

The node here is a fake Observatory client backed by REAL Sinew keepers, so
what travels is a real archive and what is kept is verified by its manifest.
Pinned here:

- a pull asks each service that keeps snapshots for a fresh one, fetches what
  is not stashed yet (newest first, max_per_pull), verifies, stashes under
  <dir>/<node>/<service>/<id>/, and prunes to the retention
- a service that keeps no snapshots is skipped, an offline node is skipped,
  a snapshot that does not verify is not kept, and all of it is in the report
- the routes and the schedule: GET /api/v1/backup, POST /api/v1/backup/pull,
  due(), off with backup.enabled: false
- no restore route, no delete route
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from seren_lodestar.app import create_app
from seren_lodestar.backup import BackupConfig, BackupService, pull_loop
from seren_lodestar.config import LodestarConfig, resolved_backup_dir
from seren_lodestar.dtos import NodeSnapshot
from seren_sinew.stores import Store, StoreKeeper, verify_snapshot


class FakeAgent:
    """One node's Observatory, as Lodestar's client sees it, over real keepers."""

    def __init__(self, keepers: dict[str, StoreKeeper]):
        self.keepers, self.taken, self.forged = keepers, [], set()

    async def get_service_stores_async(self, svc):
        k = self.keepers.get(svc)
        if svc == "SerenCorpusCallosum-wren":
            return {"ok": True, "stores": ["memory", "loci"]}      # the Callosum's /stores means what it federates
        return {"ok": True, **k.describe()} if k else None

    async def take_service_snapshot_async(self, svc, reason="x"):
        self.taken.append((svc, reason))
        return self.keepers[svc].snapshot(reason)

    async def list_service_snapshots_async(self, svc):
        rows = self.keepers[svc].list()
        return {"ok": True, "count": len(rows), "snapshots": rows}

    async def rehearse_service_archive_async(self, svc, data):
        return self.keepers[svc].rehearse_archive(data)

    async def get_service_snapshot_archive_async(self, svc, sid):
        data = self.keepers[svc].archive(sid)
        if data is None:
            return None
        if sid in self.forged:                                       # the wrong snapshot's archive: it does not verify as this one
            other = next(r["id"] for r in self.keepers[svc].list() if r["id"] != sid)
            return self.keepers[svc].archive(other)
        return data


class FakeCluster:
    def __init__(self, nodes: dict[str, tuple[bool, FakeAgent]]):
        self.nodes = nodes

    def get_snapshots(self):
        return {n: NodeSnapshot(online=on, installed_services=sorted(a.keepers) + ["SerenProbe-wren", "SerenCorpusCallosum-wren"], status={})
                for n, (on, a) in self.nodes.items()}

    def get_agent(self, n):
        return self.nodes[n][1] if n in self.nodes else None


def _keeper(tmp_path: Path, service: str, text: str) -> StoreKeeper:
    f = tmp_path / f"{service}.json"
    f.write_text(text)
    return StoreKeeper(service, lambda: [Store("it", "file", str(f))], tmp_path / "node-side")


def _world(tmp_path):
    mem = _keeper(tmp_path, "seren-memory", "the memories")
    loci = _keeper(tmp_path, "seren-loci", "the facts")
    agent = FakeAgent({"SerenMemory-wren": mem, "SerenLoci-wren": loci})
    cluster = FakeCluster({"desktop": (True, agent), "nano": (False, FakeAgent({}))})
    svc = BackupService(BackupConfig(every_hours=24, max_per_pull=2, keep_daily=2, keep_weekly=0),
                        cluster, tmp_path / "stash")
    return svc, agent, mem, loci


def test_a_pull_asks_fetches_verifies_and_stashes(tmp_path):
    svc, agent, mem, loci = _world(tmp_path)
    rep = asyncio.run(svc.pull("first"))
    assert rep.pulled == 2 and rep.errors == [] and rep.skipped_offline == ["nano"]
    assert agent.taken == [("SerenLoci-wren", "Lodestar pull (first)"), ("SerenMemory-wren", "Lodestar pull (first)")]
    d = rep.nodes["desktop"]
    assert d["SerenProbe-wren"] == {"skipped": "keeps no snapshots"}
    assert d["SerenCorpusCallosum-wren"] == {"skipped": "keeps no snapshots"}, "a /stores that means something else"
    assert len(d["SerenMemory-wren"]["stashed"]) == 1 and d["SerenMemory-wren"]["took_fresh"] is True
    sid = d["SerenMemory-wren"]["stashed"][0]
    stashed = tmp_path / "stash" / "desktop" / "SerenMemory-wren" / sid
    assert verify_snapshot(stashed) == [] and (stashed / "raw" / "it" / "seren-memory.json").read_text() == "the memories"
    assert json.loads((stashed / "manifest.json").read_text())["reason"] == "Lodestar pull (first)"
    # the same pull again: nothing new, nothing re-fetched
    rep2 = asyncio.run(svc.pull("again", take_fresh=False))
    assert rep2.pulled == 0 and rep2.nodes["desktop"]["SerenMemory-wren"]["already_had"] == 1
    assert svc.describe()["nodes"]["desktop"]["SerenMemory-wren"]["count"] == 1
    assert svc.describe()["last_pull"]["reason"] == "again"


def test_only_what_is_missing_newest_first_and_the_retention(tmp_path):
    svc, agent, mem, loci = _world(tmp_path)
    base = time.time() - 5 * 86400
    ids = [mem.snapshot(now=base + i * 86400)["id"] for i in range(5)]       # five old ones on the node
    rep = asyncio.run(svc.pull(take_fresh=False))
    got = rep.nodes["desktop"]["SerenMemory-wren"]
    assert got["stashed"] == [ids[4], ids[3]], "newest first, max_per_pull"
    assert got["remote"] == 5 and got["already_had"] == 0
    rep = asyncio.run(svc.pull(take_fresh=False))
    got = rep.nodes["desktop"]["SerenMemory-wren"]
    assert got["stashed"] == [ids[2], ids[1]] and got["pruned"], "the next two, and keep_daily=2 prunes the oldest"
    kept = sorted(p.name for p in (tmp_path / "stash" / "desktop" / "SerenMemory-wren").iterdir())
    assert len(kept) == 2


def test_a_snapshot_that_does_not_verify_is_not_kept(tmp_path):
    svc, agent, mem, loci = _world(tmp_path)
    good = mem.snapshot(now=time.time() - 100)["id"]
    bad = mem.snapshot()["id"]
    agent.forged.add(bad)
    rep = asyncio.run(svc.pull(take_fresh=False))
    got = rep.nodes["desktop"]["SerenMemory-wren"]
    assert got["failed"] == [bad] and got["stashed"] == [good]
    assert any("did not verify" in e for e in rep.errors)
    assert sorted(p.name for p in (tmp_path / "stash" / "desktop" / "SerenMemory-wren").iterdir()) == [good], \
        "no .partial left behind either"


def test_due_and_the_loop(tmp_path):
    svc, agent, mem, loci = _world(tmp_path)
    assert svc.due() is True, "nothing stashed"
    asyncio.run(svc.pull())
    assert svc.due() is False
    assert svc.due(now=time.time() + 25 * 3600) is True
    svc._cfg.every_hours = 0
    assert svc.due(now=time.time() + 99 * 3600) is False, "0 = only when asked"

    (tmp_path / "two").mkdir()
    svc2, *_ = _world(tmp_path / "two")

    async def run():
        t = asyncio.create_task(pull_loop(svc2, check_seconds=0.05, first_after=0.0))
        await asyncio.sleep(0.4)
        t.cancel()
    asyncio.run(run())
    assert svc2.last_pull is not None and svc2.last_pull.pulled == 2


def test_the_routes(tmp_path, monkeypatch):
    cfg = LodestarConfig()
    cfg.backup = BackupConfig(dir=str(tmp_path / "stash"), every_hours=0)
    app = create_app(cfg)
    with TestClient(app) as c:
        svc, agent, mem, loci = _world(tmp_path)
        c.app.state.backup = BackupService(cfg.backup, svc._cluster, resolved_backup_dir(cfg))
        d = c.get("/api/v1/backup").json()
        assert d["ok"] and d["dir"] == str((tmp_path / "stash").resolve()) and d["nodes"] == {}
        r = c.post("/api/v1/backup/pull", json={"reason": "from the route", "node": "desktop"})
        assert r.status_code == 200 and r.json()["ok"] and r.json()["pulled"] == 2
        assert c.get("/api/v1/backup").json()["nodes"]["desktop"]["SerenLoci-wren"]["count"] == 1
        for path in ("/api/v1/backup/restore", "/api/v1/backup/desktop/SerenLoci-wren"):
            assert c.post(path).status_code in (404, 405) and c.delete(path).status_code in (404, 405)


def test_off_says_so_and_the_default_place_is_beside_the_config(tmp_path):
    cfg = LodestarConfig()
    cfg.backup = BackupConfig(enabled=False)
    with TestClient(create_app(cfg)) as c:
        assert c.get("/api/v1/backup").status_code == 404 and c.post("/api/v1/backup/pull").status_code == 404
    cfg = LodestarConfig(config_path=str(tmp_path / "seren-lodestar.yaml"))
    assert resolved_backup_dir(cfg) == (tmp_path / "backups").resolve()
    assert BackupConfig.from_dict({"every_hours": "nope", "max_per_pull": 0}).every_hours == 24.0
    assert BackupConfig.from_dict({"max_per_pull": 0}).max_per_pull == 1


def test_a_rehearsal_proves_the_stash_on_the_node_it_came_from(tmp_path):
    """Design note: 'backups are useless if you can't validate them'.
    The stashed snapshot goes back down to the service for a dry run."""
    svc, agent, mem, loci = _world(tmp_path)
    mem.check = lambda restored, man, snap: {"read": (restored["it"] / "seren-memory.json").read_text()}
    assert asyncio.run(svc.rehearse())["ok"] is False, "nothing stashed: nothing proved, and it says to pull"
    asyncio.run(svc.pull("first"))
    rep = asyncio.run(svc.rehearse(reason="the first one"))
    assert rep["ok"] and rep["dry_run"] and (rep["rehearsed"], rep["passed"]) == (2, 2), rep
    m = rep["nodes"]["desktop"]["SerenMemory-wren"]
    assert m["ok"] and m["source"] == "sent" and m["stash_verified"] and m["check"] == {"read": "the memories"}
    assert m["live_store_touched"] is False and len(mem.list()) == 1, "nothing was restored, nothing was kept"
    assert svc.describe()["last_rehearsal"]["reason"] == "the first one"
    # one service, one snapshot; and a stash that rotted is caught here, before it is sent
    sid = m["snapshot"]
    one = asyncio.run(svc.rehearse(node="desktop", service="SerenLoci-wren"))
    assert one["rehearsed"] == 1 and one["ok"]
    assert asyncio.run(svc.rehearse(service="SerenMemory-wren", snapshot_id="nope"))["ok"] is False
    (tmp_path / "stash" / "desktop" / "SerenMemory-wren" / sid / "raw" / "it" / "seren-memory.json").write_text("rot")
    bad = asyncio.run(svc.rehearse(service="SerenMemory-wren"))
    assert bad["ok"] is False and "no longer matches its manifest" in bad["problems"][0]
    assert bad["nodes"]["desktop"]["SerenMemory-wren"]["stash_verified"] is False


def test_the_rehearse_route(tmp_path):
    cfg = LodestarConfig()
    cfg.backup = BackupConfig(dir=str(tmp_path / "stash"), every_hours=0)
    with TestClient(create_app(cfg)) as c:
        svc, agent, mem, loci = _world(tmp_path)
        c.app.state.backup = BackupService(cfg.backup, svc._cluster, resolved_backup_dir(cfg))
        c.post("/api/v1/backup/pull", json={})
        r = c.post("/api/v1/backup/rehearse", json={"service": "SerenLoci-wren", "reason": "from the route"})
        assert r.status_code == 200 and r.json()["ok"] and r.json()["rehearsed"] == 1, r.text
        assert c.get("/api/v1/backup").json()["last_rehearsal"]["reason"] == "from the route"
        assert c.post("/api/v1/backup/restore").status_code in (404, 405), "a rehearsal is not a restore"
