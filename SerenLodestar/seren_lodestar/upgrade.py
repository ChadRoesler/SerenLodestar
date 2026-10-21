"""
seren_lodestar.upgrade
════════════════════════════════════════════════════════════════════════

Lodestar drives upgrades across the cluster, its own included, without
losing anything on the way: snap it, snap the config, upgrade, put it back.

Each node's Observatory does the upgrading (seren_observatory.upgrades: the
service's own snapshot, the config held and put back, pip with the shared
libraries named, an import check before any restart, health after, a rollback
when anything fails). Lodestar decides what, where and in which order, and
keeps a copy of everything off the box first.

EACH APP ON ITS OWN. The Seren apps are installed apart, versioned apart and
upgraded apart: release what changed, and Lodestar moves what is behind. A
run never puts one version number on every app (a pinned version names its
app, or comes as {app: version}), and one app failing does not hold the
others back: its Observatory rolls it back, the run says so, and goes on.

PEERS. Where the apps meet is over HTTP, so that is what is checked. A reader
says in its pyproject which apps it talks to and how new they must be (the
`peers` extra; seren_observatory.upgrades has the whole rule). Before each
upgrade Lodestar collects every node's versions and sends them along; an
upgrade whose new version needs a newer Memory than the cluster runs ends
"held", nothing touched, and the run goes on. Stores go first in a run, so
when Memory and the Callosum that needs it both move, Memory is already
there by the time the Callosum is checked.

A RUN, per online node (or the ones named), per seren service the node
reports (or the ones named):

    0. behind     with only_behind (the default) a service already on the
                  newest release the index has is left alone ("current").
                  A pinned version is always installed.
    1. backup     pull a fresh snapshot of the service into Lodestar's stash
                  (seren_lodestar.backup), so a copy is off the box before the
                  box changes. A service that keeps none is a plain skip; a
                  pull that fails fails that service, untouched. With the
                  stash in hand the Observatory is asked not to take a second.
    2. upgrade    POST .../service/{svc}/upgrade with the cluster's versions,
                  then follow the job until it ends. "Not upgradable" (no pip
                  install the Observatory can see: llama, kokoro...) is a
                  skip; "held" is a peer too old, said plainly.

The run ends "done" (nothing failed), "partial" (something failed, something
moved) or "failed" (nothing moved and something failed).

ORDER. The stores first (Memory, Loci, Margin, the Hippocampus), then what
reads them, then the Observatory, last on each node: its own upgrade restarts
it, and the job is closed by the Observatory that comes back. Lodestar's own
upgrade is the last thing of all, because it ends the process running the
run: Lodestar hands it to its node's Observatory and records the hand-off.
The Lodestar that starts next reads the run, follows that job to its end and
closes the run. That is the self-upgrade: Lodestar is never the thing
restarting itself.

DRY RUN. Every Observatory's dry run (pip's own plan: what would move from
what to what, and whether its peers are new enough), in run order, each one
seeing the versions the ones before it would bring. Nothing changed.

BEHIND. Every seren service on every online node, its version, and the
newest release the index has (cached; the updates: block says where and how
often). With it, per service, the shared libraries its venv runs (seren-sinew
and seren-meninges, `libs`) against the index too: a service can be on its
newest and still run a stale library, and a run that moves only what is
behind would not touch it. 20 Oct 2026: click a service, see its own
version and the libraries' with their newest, and update any of the three
from there. A library alone is a run of that one service pinned to the
version it already has, with `with` naming the library: pip moves the
library and leaves the service where it is, and the service is still
restarted so it runs the new code.

The runs are kept as JSON beside the config (upgrades/<run id>.json) so a
restart, of Lodestar above all, does not lose one.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("seren_lodestar.upgrade")

# Stores, then their readers. Anything seren the list does not know goes after
# the readers; the Observatory is last on its node, Lodestar last of all.
ORDER = ["memory", "loci", "margin", "hippocampus", "corpuscallosum", "workbench", "probe", "theatre",
         "symposium"]
LAST_ON_NODE = "observatory"
LAST_OF_ALL = "lodestar"
JOB_TIMEOUT_S = 1800.0
POLL_S = 3.0
KEEP_RUNS = 30

# (package, force) -> (newest release or None, why not)
LatestFn = Callable[[str, bool], Awaitable[tuple[Optional[str], Optional[str]]]]


class PinError(ValueError):
    """A pinned version that does not name its app."""


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def short_name(service: str) -> Optional[str]:
    """'seren-corpus-callosum-wren' / 'SerenCorpusCallosum-wren' -> 'corpuscallosum'.
    None for anything that is not a seren service."""
    n = _norm(service)
    if not n.startswith("seren"):
        return None
    rest = n[len("seren"):]
    for key in ORDER + [LAST_ON_NODE, LAST_OF_ALL]:
        if rest.startswith(key):
            return key
    # an unknown seren service: its name up to the instance suffix
    m = re.match(r"seren-?([a-z0-9]+)", service.lower())
    return _norm(m.group(1)) if m else rest


def rank(service: str) -> int:
    s = short_name(service) or ""
    if s == LAST_OF_ALL:
        return 1000
    if s == LAST_ON_NODE:
        return 900
    return ORDER.index(s) if s in ORDER else len(ORDER)


def wanted(service: str, names: Optional[list[str]]) -> bool:
    """A service is picked by its full name or by its short one ('memory',
    'corpus-callosum')."""
    if short_name(service) is None:
        return False
    if not names:
        return True
    s = short_name(service)
    return any(_norm(n) in (_norm(service), s) for n in names)


def runs_dir(config_path: Optional[str]) -> Path:
    if config_path:
        return Path(config_path).resolve().parent / "upgrades"
    return Path.home() / "seren-lodestar" / "upgrades"


def pins(order: list[tuple[str, str]], version: Optional[str],
         versions: Optional[dict[str, str]]) -> dict[str, Optional[str]]:
    """{"node/service": version or None (the newest)}. A single `version`
    only when the run is one app; `versions` names each app it pins, and an
    app it names that the run does not have is a mistake worth saying."""
    out: dict[str, Optional[str]] = {f"{n}/{s}": None for n, s in order}
    if version in ("", "latest"):
        version = None
    if version:
        apps = {short_name(s) for _n, s in order}
        if len(apps) != 1:
            raise PinError("a version pins one app: name it in services, or pin each one in versions "
                           f"({{app: version}}); this run is {len(apps)} apps")
        out = {k: version for k in out}
    for name, v in (versions or {}).items():
        hit = [f"{n}/{s}" for n, s in order if wanted(s, [name])]
        if not hit:
            raise PinError(f"versions names {name}, which this run does not have")
        for k in hit:
            out[k] = None if v in ("", "latest", None) else str(v)
    return out


def _newer(latest: Optional[str], installed: Optional[str]) -> Optional[bool]:
    """True/False, or None when either is missing or will not parse."""
    if not (latest and installed):
        return None
    try:
        from packaging.version import Version
        return Version(latest) > Version(installed)
    except Exception:  # noqa: BLE001
        return None


class IndexLatest:
    """The newest release of each seren package, from the index the updates:
    block names, cached per package (seren_meninges.updates does the asking)."""

    def __init__(self, index_url: Optional[str] = None, ttl_seconds: float = 6 * 3600.0,
                 allow_prerelease: bool = False, enabled: bool = True) -> None:
        self.index_url, self.ttl_seconds = index_url, ttl_seconds
        self.allow_prerelease, self.enabled = allow_prerelease, enabled
        self._checkers: dict[str, Any] = {}

    async def __call__(self, package: str, force: bool = False) -> tuple[Optional[str], Optional[str]]:
        if not self.enabled:
            return None, "update checking is switched off (updates.enabled)"
        try:
            from seren_meninges.updates import UpdateChecker
        except Exception as e:  # noqa: BLE001
            return None, f"no seren_meninges.updates: {e}"
        c = self._checkers.get(package)
        if c is None:
            kw = {"index_url": self.index_url} if self.index_url else {}
            c = self._checkers[package] = UpdateChecker(package, ttl_seconds=self.ttl_seconds,
                                                        allow_prerelease=self.allow_prerelease, **kw)
        st = await c.get(force=force)
        return st.latest, (None if st.latest else (st.detail or st.status))


class UpgradeService:
    """One run at a time."""

    def __init__(self, cluster: Any, backup: Any, root: Path, log_fn=None,
                 job_timeout: float = JOB_TIMEOUT_S, poll: float = POLL_S,
                 latest: Optional[LatestFn] = None) -> None:
        self._cluster = cluster
        self._backup = backup
        self.root = Path(root)
        self._log = log_fn or (lambda m: log.info(m))
        self._task: Optional[asyncio.Task] = None
        self.current: Optional[dict[str, Any]] = None
        self.job_timeout = job_timeout
        self.poll = poll
        self.latest: LatestFn = latest or IndexLatest()

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    # ── the runs on disk ──────────────────────────────────────────────────
    def save(self, run: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        p = self.root / f"{run['id']}.json"
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(run, indent=2), encoding="utf-8")
        os.replace(tmp, p)

    def load(self, run_id: str) -> Optional[dict[str, Any]]:
        if not run_id or any(c in run_id for c in "/\\."):
            return None
        try:
            return json.loads((self.root / f"{run_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        runs = [r for r in (self.load(p.stem) for p in self.root.glob("*.json")) if r]
        runs.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
        return runs[:limit]

    def _prune(self) -> None:
        for r in self.recent(limit=10_000)[KEEP_RUNS:]:
            try:
                (self.root / f"{r['id']}.json").unlink(missing_ok=True)
            except OSError:
                pass

    # ── what a run would touch ────────────────────────────────────────────
    def targets(self, nodes: Optional[list[str]], services: Optional[list[str]]) -> tuple[list[tuple[str, str]], list[str]]:
        """([(node, service)] in run order, [offline nodes skipped])."""
        snaps = self._cluster.get_snapshots()
        found, last, offline = [], [], []
        for node in sorted(snaps):
            if nodes and node not in nodes:
                continue
            snap = snaps[node]
            if not snap.online:
                offline.append(node)
                continue
            mine = sorted((s for s in snap.installed_services or [] if wanted(s, services)), key=lambda s: (rank(s), s))
            for s in mine:
                (last if short_name(s) == LAST_OF_ALL else found).append((node, s))
        return found + last, offline

    # ── what the cluster runs ─────────────────────────────────────────────
    async def versions(self, only: Optional[str] = None) -> tuple[list[dict[str, Any]], list[str]]:
        """([{node, service, package, version, needs}] for every service an
        online Observatory can upgrade], [nodes that could not say]). only:
        one node."""
        snaps = self._cluster.get_snapshots()
        online = [n for n in sorted(snaps) if snaps[n].online and (only is None or n == only)]

        async def one(node):
            agent = self._cluster.get_agent(node)
            ask = getattr(agent, "get_upgrade_versions_async", None)
            return node, (await ask() if ask else None)
        rows, unknown = [], []
        for node, svcs in await asyncio.gather(*(one(n) for n in online)):
            if svcs is None:
                unknown.append(node)
                continue
            for name, d in sorted(svcs.items()):
                if isinstance(d, dict):
                    libs = d.get("libs") if isinstance(d.get("libs"), dict) else {}
                    rows.append({"node": node, "service": name, "package": d.get("package"),
                                 "version": d.get("version"), "needs": d.get("needs") or [],
                                 "libs": {str(k): (str(v) if v else None) for k, v in libs.items()}})
        return rows, unknown

    @staticmethod
    def _peers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{k: r.get(k) for k in ("service", "node", "package", "version")} for r in rows if r.get("package")]

    async def behind(self, nodes=None, services=None, refresh: bool = False) -> dict[str, Any]:
        """Every seren service an Observatory can upgrade, its version and the
        index's newest. `behind` lists the ones a run would move."""
        rows, unknown = await self.versions()
        rows = [r for r in rows if (not nodes or r["node"] in nodes) and wanted(r["service"], services)]
        latest: dict[str, tuple[Optional[str], Optional[str]]] = {}
        wanted_pkgs = {r["package"] for r in rows if r.get("package")}
        wanted_pkgs |= {lib for r in rows for lib in (r.get("libs") or {})}
        for pkg in sorted(wanted_pkgs):
            latest[pkg] = await self.latest(pkg, refresh)
        out, behind, libs_behind = [], [], []
        for r in sorted(rows, key=lambda r: (r["node"], rank(r["service"]), r["service"])):
            new, why = latest.get(r.get("package"), (None, "no package"))
            moves = _newer(new, r.get("version"))
            row = {**r, "latest": new, "behind": moves}
            if why:
                row["latest_error"] = why
            # The libraries the venv runs, each against the index. Not in
            # `behind`: a run moves services, and the libraries ride along.
            row["libs"] = {}
            for lib, have in (r.get("libs") or {}).items():
                lib_new, lib_why = latest.get(lib, (None, "no package"))
                lib_moves = _newer(lib_new, have)
                row["libs"][lib] = {"version": have, "latest": lib_new, "behind": lib_moves,
                                    **({"latest_error": lib_why} if lib_why else {})}
                if lib_moves:
                    libs_behind.append(f"{r['node']}/{r['service']} {lib} {have} -> {lib_new}")
            out.append(row)
            if moves:
                behind.append(f"{r['node']}/{r['service']} {r['version']} -> {new}")
        return {"ok": True, "services": out, "behind": behind, "libs_behind": libs_behind, "unknown_nodes": unknown}

    # ── dry run ───────────────────────────────────────────────────────────
    async def plan(self, nodes=None, services=None, version=None, with_=None, versions=None,
                   only_behind: bool = True) -> dict[str, Any]:
        order, offline = self.targets(nodes, services)
        try:
            pinned = pins(order, version, versions)
        except PinError as e:
            return {"ok": False, "error": str(e)}
        rows, unknown = await self.versions()
        out: dict[str, Any] = {"ok": True, "dry_run": True, "pins": {k: v for k, v in pinned.items() if v},
                               "only_behind": only_behind, "order": [f"{n}/{s}" for n, s in order],
                               "skipped_offline": offline, "unknown_nodes": unknown, "nodes": {},
                               "held": [], "current": []}
        for node, svc in order:
            key = f"{node}/{svc}"
            row = next((r for r in rows if r["node"] == node and r["service"] == svc), None)
            if only_behind and pinned[key] is None and row and row.get("package"):
                new, _why = await self.latest(row["package"], False)
                if _newer(new, row.get("version")) is False:
                    out["current"].append(key)
                    out["nodes"].setdefault(node, {})[svc] = {"current": row.get("version"), "latest": new}
                    continue
            agent = self._cluster.get_agent(node)
            body = {"dry_run": True, "version": pinned[key], "peers": self._peers(rows),
                    **({"with": with_} if with_ is not None else {})}
            status, data = await agent.start_upgrade_async(svc, body) if agent else (502, {"error": "no client"})
            if status == 409 and data.get("upgradable") is False:
                data = {"skipped": data.get("error")}
            elif status >= 400 or not data.get("ok", False):
                out["ok"] = False
            if data.get("held"):
                out["held"].append(f"{key}: " + "; ".join((data.get("needs") or {}).get("problems") or []))
            # the ones after this see what this one would become
            pkg = data.get("package")
            if row and pkg and not data.get("held"):
                for w in data.get("would_install") or []:
                    if re.sub(r"[-_.]+", "-", str(w.get("name", ""))).lower() == pkg:
                        row["version"] = w.get("version")
            out["nodes"].setdefault(node, {})[svc] = data
        return out

    # ── a run ─────────────────────────────────────────────────────────────
    def start(self, nodes=None, services=None, version=None, with_=None, backup: bool = True,
              reason: str = "by hand", versions=None, only_behind: bool = True) -> tuple[int, dict[str, Any]]:
        if self.busy:
            return 409, {"ok": False, "error": "an upgrade run is already going", "run": self.current}
        order, offline = self.targets(nodes, services)
        if not order:
            return 409, {"ok": False, "error": "nothing to upgrade: no online node reports a matching seren service",
                         "skipped_offline": offline}
        try:
            pinned = pins(order, version, versions)
        except PinError as e:
            return 400, {"ok": False, "error": str(e)}
        run = {"id": time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6], "state": "running",
               "reason": reason[:200], "only_behind": bool(only_behind), "with": with_, "backup": bool(backup),
               "created_at": time.time(), "finished_at": None, "skipped_offline": offline,
               "steps": [{"node": n, "service": s, "version": pinned[f"{n}/{s}"] or "latest", "state": "pending"}
                         for n, s in order],
               "failed": [], "held": [], "error": None}
        self.save(run)
        self.current = run
        self._task = asyncio.ensure_future(self._run(run))
        return 202, {"ok": True, "run": run}

    async def _run(self, run: dict[str, Any]) -> None:
        try:
            rows, _unknown = await self.versions()
            for step in run["steps"]:
                await self._one(run, step, rows)
                self.save(run)
                if step.get("job"):           # something moved: what that node runs now
                    fresh, _u = await self.versions(only=step["node"])
                    rows[:] = [r for r in rows if r["node"] != step["node"]] + fresh
                if run["state"] == "handed_off":
                    break
            if run["state"] == "running":
                run["state"] = self._outcome(run)
        except Exception as e:  # noqa: BLE001 - a run always ends with a state
            log.exception("upgrade run %s crashed", run["id"])
            run["state"], run["error"] = "failed", f"{type(e).__name__}: {e}"
        finally:
            if run["state"] != "handed_off":
                run["finished_at"] = time.time()
                for step in run["steps"]:
                    if step["state"] in ("pending", "running"):
                        step["state"] = "not_started"
            self.save(run)
            self._prune()
            self._log(f"run {run['id']}: {run['state']}"
                      + (f" ({run['error']})" if run.get("error") else ""))

    @staticmethod
    def _outcome(run: dict[str, Any]) -> str:
        if not run.get("failed"):
            return "done"
        return "partial" if any(s["state"] == "done" for s in run["steps"]) else "failed"

    @staticmethod
    def _fail(run: dict[str, Any], step: dict[str, Any], why: str) -> None:
        step["state"], step["error"] = "failed", why
        run.setdefault("failed", []).append(f"{step['node']}/{step['service']}: {why}")
        run["error"] = "; ".join(run["failed"])

    async def _one(self, run: dict[str, Any], step: dict[str, Any], rows: list[dict[str, Any]]) -> None:
        node, svc = step["node"], step["service"]
        agent = self._cluster.get_agent(node)
        if agent is None:
            self._fail(run, step, "no client for the node")
            return
        step["state"] = "running"
        self.save(run)
        pinned = None if step.get("version") in (None, "latest") else step["version"]

        # 0. already on the newest
        row = next((r for r in rows if r["node"] == node and r["service"] == svc), None)
        if run.get("only_behind", True) and pinned is None and row and row.get("package"):
            new, _why = await self.latest(row["package"], False)
            if _newer(new, row.get("version")) is False:
                step["state"], step["current"] = "current", row.get("version")
                return

        # 1. a copy off the box
        snapshot_on_node = True
        if run["backup"] and self._backup is not None:
            rep = await self._backup.pull(reason=f"before upgrade {run['id']}", node=node, service=svc,
                                          take_fresh=True)
            got = (rep.nodes.get(node) or {}).get(svc) or {}
            step["backup"] = got or {"errors": rep.errors}
            if rep.errors or "error" in got:
                self._fail(run, step, "the backup failed, so no upgrade: "
                           + "; ".join(rep.errors or [got.get("error", "")]))
                return
            snapshot_on_node = False          # the stash has it, or it keeps none

        # 2. the upgrade
        body = {"version": pinned, "snapshot": snapshot_on_node, "peers": self._peers(rows),
                "reason": f"Lodestar run {run['id']}: {run['reason']}"}
        if run.get("with") is not None:
            body["with"] = run["with"]
        status, data = await agent.start_upgrade_async(svc, body, timeout=60.0)
        if status == 409 and data.get("upgradable") is False:
            step["state"], step["skipped"] = "skipped", data.get("error")
            return
        if status != 202 or not (data.get("job") or {}).get("id"):
            self._fail(run, step, f"the Observatory would not start it: {data.get('error') or status}")
            return
        job = data["job"]
        step["job"] = job["id"]
        self.save(run)

        # Lodestar itself: the job ends this process. Hand it off; the next
        # Lodestar follows it (resume()).
        if short_name(svc) == LAST_OF_ALL:
            step["state"] = run["state"] = "handed_off"
            self.save(run)

        job = await self.follow(agent, job["id"])
        self._close_step(run, step, job)

    async def job_of(self, step: dict[str, Any]) -> Optional[dict[str, Any]]:
        """A step's job as its node has it now, or None (no job, no client
        for the node, or the Observatory did not answer)."""
        if not step.get("job"):
            return None
        agent = self._cluster.get_agent(step.get("node"))
        if agent is None:
            return None
        try:
            return await agent.get_upgrade_job_async(step["job"])
        except Exception:
            return None

    async def follow(self, agent: Any, job_id: str) -> dict[str, Any]:
        """The job, polled until it ends. An Observatory that does not answer
        is restarting (its own upgrade) or busy: keep asking until the time
        is up."""
        deadline = time.monotonic() + self.job_timeout
        last: dict[str, Any] = {"id": job_id, "state": "unknown"}
        while time.monotonic() < deadline:
            job = await agent.get_upgrade_job_async(job_id)
            if job:
                last = job
                if job.get("terminal"):
                    return job
            await asyncio.sleep(self.poll)
        last = dict(last)
        last["error"] = last.get("error") or f"no end to the job within {self.job_timeout:.0f}s"
        return last

    def _close_step(self, run: dict[str, Any], step: dict[str, Any], job: dict[str, Any]) -> None:
        step["job_state"] = job.get("state")
        step["before"], step["after"] = job.get("before"), job.get("after")
        if job.get("state") == "done":
            step["state"] = "done"
        elif job.get("state") == "held":
            step["state"], step["held"] = "held", job.get("error")
            run.setdefault("held", []).append(f"{step['node']}/{step['service']}: {job.get('error')}")
        else:
            self._fail(run, step, f"{job.get('state')}" + (f" - {job.get('error')}" if job.get("error") else ""))
        if run["state"] == "handed_off":
            run["state"] = self._outcome(run)

    # ── after a restart ───────────────────────────────────────────────────
    async def resume(self) -> list[str]:
        """At startup: a run this Lodestar handed off (its own upgrade) is
        followed to its end and closed. Returns the runs closed."""
        closed = []
        for run in self.recent(limit=KEEP_RUNS):
            if run.get("state") != "handed_off":
                continue
            step = next((s for s in run["steps"] if s.get("state") == "handed_off" and s.get("job")), None)
            agent = self._cluster.get_agent(step["node"]) if step else None
            if step is None or agent is None:
                run["state"], run["error"] = "failed", "handed off, but the job or its node is gone"
            else:
                self._close_step(run, step, await self.follow(agent, step["job"]))
            run["finished_at"] = time.time()
            self.save(run)
            self._log(f"run {run['id']}: closed after the hand-off, {run['state']}")
            closed.append(run["id"])
        return closed

    def describe(self) -> dict[str, Any]:
        return {"busy": self.busy, "current": self.current if self.busy else None, "dir": str(self.root),
                "runs": self.recent()}
