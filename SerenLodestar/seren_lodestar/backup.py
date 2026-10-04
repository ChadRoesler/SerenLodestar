"""
seren_lodestar.backup
════════════════════════════════════════════════════════════════════════

Lodestar's half of the brain's backups: ask, pull, stash.

Every service that keeps something declares it and snapshots itself
(seren_sinew.stores: GET /stores, POST /stores/snapshot); its node's
Observatory proxies those routes, so Lodestar needs one address and one token
per node. Design note: "SerenSinew handles the backups... this gives us
a way to manage and eventually be able to have Lodestar do the requesting of
them, and pulling them, and stashing them."

A PULL, per node that is online, per service the node reports installed:

    GET  .../service/{svc}/stores              404 = keeps no snapshots: skip
    POST .../service/{svc}/stores/snapshot     a fresh one (when asked to take)
    GET  .../service/{svc}/stores/snapshots    what it has
    GET  .../stores/snapshots/{id}/archive     each one not stashed here yet,
                                              newest first, up to max_per_pull
    unpack_snapshot(...)                       verified against its manifest
                                              before it is kept

STASH LAYOUT        <backup.dir>/<node>/<service>/<snapshot id>/...
                    the snapshot exactly as the service made it: manifest,
                    raw copy, plain export. The same retention as the
                    services keep (newest keep_daily, then one a week), per
                    node and service.

A REHEARSAL (Design note: "backups are useless if you can't validate
them") is the stash proved: a stashed snapshot is verified here, sent down
through the node's Observatory to the service it came from
(POST .../service/{svc}/stores/rehearse), and the service restores it into a
scratch folder, opens the copy as it opens its store, counts it against the
manifest, replays its tombstones on the copy, and removes it. Nothing is
restored. `rehearse()` reports per node and service.

WHAT THIS IS NOT. Not a restore: nothing here writes a snapshot back into a
service. That will be its own gated step, replaying the tombstones, asked for
with a reason. Not a reach into the stash from a purge, either: a purge
leaves a tombstone, and a restore applies the tombstones; the stash is not
pruned by it (decided with Design note:).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from seren_sinew.stores import StoreKeeper, pack_snapshot, unpack_snapshot, verify_snapshot

log = logging.getLogger("seren_lodestar.backup")


@dataclass
class BackupConfig:
    """The `backup:` block."""
    enabled: bool = True
    # Where pulled snapshots go. Blank = `backups` beside the config file (or
    # ~/seren-lodestar/backups on defaults). Point it at the disk you trust.
    dir: str = ""
    # A pull whenever the newest stashed snapshot of any service is older than
    # this. 0 = only when asked (POST /api/v1/backup/pull, the tool).
    every_hours: float = 24.0
    # A pull first asks each service for a fresh snapshot. Off = only what the
    # services took on their own schedules.
    take_fresh: bool = True
    # Snapshots fetched per service per pull, newest first.
    max_per_pull: int = 3
    keep_daily: int = 14
    keep_weekly: int = 8

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "BackupConfig":
        d = d or {}

        def num(k: str, default: float) -> float:
            try:
                return float(d.get(k, default))
            except (TypeError, ValueError):
                return default
        return cls(
            enabled=bool(d.get("enabled", True)),
            dir=str(d.get("dir", "") or ""),
            every_hours=max(0.0, num("every_hours", 24.0)),
            take_fresh=bool(d.get("take_fresh", True)),
            max_per_pull=max(1, int(num("max_per_pull", 3))),
            keep_daily=max(1, int(num("keep_daily", 14))),
            keep_weekly=max(0, int(num("keep_weekly", 8))),
        )


@dataclass
class PullReport:
    started_at: float
    finished_at: float = 0.0
    reason: str = "scheduled"
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)   # node -> {service -> what happened}
    pulled: int = 0
    skipped_offline: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"started_at": self.started_at, "finished_at": self.finished_at, "reason": self.reason,
                "seconds": round(self.finished_at - self.started_at, 2), "pulled": self.pulled,
                "nodes": self.nodes, "skipped_offline": self.skipped_offline, "errors": self.errors}


class BackupService:
    """Pulls snapshots from every node and keeps them under `root`."""

    def __init__(self, cfg: BackupConfig, cluster: Any, root: Path, log_fn=None) -> None:
        self._cfg = cfg
        self._cluster = cluster
        self.root = Path(root)
        self._log = log_fn or (lambda m: log.info(m))
        self._lock = asyncio.Lock()
        self.last_pull: Optional[PullReport] = None
        self.last_rehearsal: Optional[dict[str, Any]] = None

    # ── what is stashed ───────────────────────────────────────────────────
    def _keeper(self, node: str, service: str) -> StoreKeeper:
        # A StoreKeeper over the stash folder: it lists and prunes a folder of
        # snapshots the same way the service does, and takes none of its own.
        return StoreKeeper(service, lambda: [], self.root / node, keep_daily=self._cfg.keep_daily,
                           keep_weekly=self._cfg.keep_weekly, log=self._log)

    def stashed(self) -> dict[str, dict[str, list[dict[str, Any]]]]:
        """node -> service -> snapshots (newest first), from the stash on disk."""
        out: dict[str, dict[str, list[dict[str, Any]]]] = {}
        if not self.root.is_dir():
            return out
        for node_dir in sorted(p for p in self.root.iterdir() if p.is_dir()):
            for svc_dir in sorted(p for p in node_dir.iterdir() if p.is_dir()):
                rows = self._keeper(node_dir.name, svc_dir.name).list()
                if rows:
                    out.setdefault(node_dir.name, {})[svc_dir.name] = rows
        return out

    def describe(self) -> dict[str, Any]:
        stash = self.stashed()
        newest = max((s[0]["created_at"] or 0 for node in stash.values() for s in node.values()), default=None)
        return {"dir": str(self.root), "every_hours": self._cfg.every_hours, "take_fresh": self._cfg.take_fresh,
                "keep_daily": self._cfg.keep_daily, "keep_weekly": self._cfg.keep_weekly,
                "nodes": {n: {s: {"count": len(rows), "latest": rows[0]} for s, rows in svcs.items()}
                          for n, svcs in stash.items()},
                "newest_at": newest, "last_pull": self.last_pull.as_dict() if self.last_pull else None,
                "last_rehearsal": self.last_rehearsal}

    def due(self, now: Optional[float] = None) -> bool:
        if self._cfg.every_hours <= 0:
            return False
        now = time.time() if now is None else now
        if self.last_pull and now - self.last_pull.finished_at < 600:
            return False                                   # a pull that found nothing is not retried every check
        stash = self.stashed()
        if not stash:
            return True
        newest = max((s[0]["created_at"] or 0 for node in stash.values() for s in node.values()), default=0)
        return now - float(newest) >= self._cfg.every_hours * 3600

    # ── the pull ──────────────────────────────────────────────────────────
    async def pull(self, reason: str = "scheduled", node: Optional[str] = None,
                   service: Optional[str] = None, take_fresh: Optional[bool] = None) -> PullReport:
        """Ask every online node (or one) for its services' snapshots and
        stash what is not here yet. Never raises: every failure is in the
        report, per service, and the next pull tries again."""
        if self._lock.locked():
            rep = PullReport(started_at=time.time(), reason=reason)
            rep.errors.append("a pull is already running")
            rep.finished_at = time.time()
            return rep
        async with self._lock:
            take = self._cfg.take_fresh if take_fresh is None else bool(take_fresh)
            rep = PullReport(started_at=time.time(), reason=reason)
            snaps = self._cluster.get_snapshots()
            for node_name, snap in snaps.items():
                if node and node_name != node:
                    continue
                if not snap.online:
                    rep.skipped_offline.append(node_name)
                    continue
                agent = self._cluster.get_agent(node_name)
                if agent is None:
                    continue
                for svc in sorted(snap.installed_services or []):
                    if service and svc != service:
                        continue
                    try:
                        rep.nodes.setdefault(node_name, {})[svc] = await self._pull_service(agent, node_name, svc, take, rep)
                    except Exception as e:  # noqa: BLE001 - one service's trouble is one line in the report
                        rep.nodes.setdefault(node_name, {})[svc] = {"error": f"{type(e).__name__}: {e}"}
                        rep.errors.append(f"{node_name}/{svc}: {type(e).__name__}: {e}")
            rep.finished_at = time.time()
            self.last_pull = rep
            self._log(f"pull ({reason}): {rep.pulled} snapshot(s) stashed, {len(rep.errors)} error(s)"
                      + (f", offline: {', '.join(rep.skipped_offline)}" if rep.skipped_offline else ""))
            return rep

    async def _pull_service(self, agent: Any, node: str, svc: str, take: bool, rep: PullReport) -> dict[str, Any]:
        stores = await agent.get_service_stores_async(svc)
        if not isinstance(stores, dict) or not isinstance(stores.get("snapshots"), dict) or "stores" not in stores:
            # 404, or a /stores of some other meaning: the Corpus Callosum's
            # /stores is the list of stores it federates (seen live, 3 Oct 2026)
            return {"skipped": "keeps no snapshots"}
        if take:
            fresh = await agent.take_service_snapshot_async(svc, reason=f"Lodestar pull ({rep.reason})")
            if fresh is None:
                rep.errors.append(f"{node}/{svc}: the service would not take a snapshot")
        listed = await agent.list_service_snapshots_async(svc) or {}
        remote = listed.get("snapshots") or []
        keeper = self._keeper(node, svc)
        have = {s["id"] for s in keeper.list()}
        got, failed = [], []
        for row in remote:
            if len(got) >= self._cfg.max_per_pull:
                break
            sid = str(row.get("id") or "")
            if not sid or sid in have:
                continue
            data = await agent.get_service_snapshot_archive_async(svc, sid)
            if data is None:
                failed.append(sid)
                rep.errors.append(f"{node}/{svc}: could not fetch snapshot {sid}")
                continue
            try:
                await asyncio.to_thread(unpack_snapshot, data, keeper.root, sid)
            except ValueError as e:
                failed.append(sid)
                rep.errors.append(f"{node}/{svc}: snapshot {sid} did not verify: {e}")
                continue
            got.append(sid)
            rep.pulled += 1
            self._log(f"stashed {node}/{svc}/{sid} ({len(data)} bytes)")
        pruned = keeper.prune() if got else []
        return {"stashed": got, "already_had": len(have), "remote": len(remote), "failed": failed, "pruned": pruned,
                "took_fresh": take}


    # ── the rehearsal ─────────────────────────────────────────────────────
    async def rehearse(self, node: Optional[str] = None, service: Optional[str] = None,
                       snapshot_id: Optional[str] = None, reason: str = "by hand") -> dict[str, Any]:
        """Prove the stash: for every stashed service (or one node, one
        service), verify a stashed snapshot here (the newest, or snapshot_id)
        and send it down to the service for a restore's dry run. Never
        raises; "ok" is true when every rehearsal that was asked for passed,
        and at least one ran."""
        t0 = time.time()
        out: dict[str, Any] = {"ok": False, "dry_run": True, "reason": reason, "started_at": t0, "nodes": {},
                               "rehearsed": 0, "passed": 0, "problems": []}
        stash = self.stashed()
        snaps = self._cluster.get_snapshots()
        for node_name, svcs in stash.items():
            if node and node_name != node:
                continue
            for svc, rows in svcs.items():
                if service and svc != service:
                    continue
                row = next((r for r in rows if not snapshot_id or r["id"] == snapshot_id), None)
                where = out["nodes"].setdefault(node_name, {})
                if row is None:
                    where[svc] = {"ok": False, "problems": [f"no stashed snapshot '{snapshot_id}'"]}
                elif not getattr(snaps.get(node_name), "online", False) or self._cluster.get_agent(node_name) is None:
                    where[svc] = {"ok": False, "snapshot": row["id"], "problems": ["the node is offline"]}
                else:
                    try:
                        where[svc] = await self._rehearse_one(self._cluster.get_agent(node_name), svc, Path(row["path"]))
                    except Exception as e:  # noqa: BLE001 - one service's trouble is one line in the report
                        where[svc] = {"ok": False, "snapshot": row["id"], "problems": [f"{type(e).__name__}: {e}"]}
                out["rehearsed"] += 1
                if where[svc].get("ok"):
                    out["passed"] += 1
                else:
                    out["problems"] += [f"{node_name}/{svc}: {p}" for p in (where[svc].get("problems") or ["failed"])]
        if not out["rehearsed"]:
            out["problems"].append("nothing stashed matches: pull first (POST /api/v1/backup/pull)")
        out["ok"] = bool(out["rehearsed"]) and out["passed"] == out["rehearsed"]
        out["seconds"] = round(time.time() - t0, 2)
        self.last_rehearsal = out
        self._log(f"rehearsal ({reason}): {out['passed']}/{out['rehearsed']} passed")
        return out

    async def _rehearse_one(self, agent: Any, svc: str, snap: Path) -> dict[str, Any]:
        bad = await asyncio.to_thread(verify_snapshot, snap)
        if bad:
            return {"ok": False, "snapshot": snap.name, "stash_verified": False,
                    "problems": ["the stashed copy no longer matches its manifest: " + "; ".join(bad[:5])]}
        data = await asyncio.to_thread(pack_snapshot, snap)
        rep = await agent.rehearse_service_archive_async(svc, data)
        rep = dict(rep or {"ok": False, "problems": ["the service gave no report"]})
        rep.setdefault("snapshot", snap.name)
        rep["stash_verified"], rep["sent_bytes"] = True, len(data)
        return rep


async def pull_loop(service: BackupService, check_seconds: float = 900.0, first_after: float = 120.0) -> None:
    """Lodestar's own schedule: a pull whenever one is due. Cancelled at
    shutdown; a failure is in the report and tried again at the next check."""
    await asyncio.sleep(first_after)
    while True:
        try:
            if service.due():
                await service.pull("scheduled")
        except Exception as e:  # noqa: BLE001 - a backup never takes the head down
            service._log(f"scheduled pull failed: {type(e).__name__}: {e}")
        await asyncio.sleep(check_seconds)
