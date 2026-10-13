"""
seren_lodestar.run_as_needed
════════════════════════════════════════════════════════════════════════

Lodestar keeps the main Seren apps running as needed. The rules are Sinew's
(seren_sinew.orchestration: RunPolicy, Leases with clocks, decide); this is
where they run.

    orchestration:
      lease_ttl_minutes: 60       # an ensure holds an app this long unless renewed
      check_seconds: 60           # how often the policies are applied
      state_file: ""              # the lease book; blank = orchestration/leases.json
                                  # beside the config (in memory when running on defaults)
      default: {mode: unmanaged, idle_minutes: 0}
      apps:
        probe:       {mode: on_demand, idle_minutes: 20}
        theatre:     on_demand
        symposium:   {mode: on_demand, idle_minutes: 60}
        hippocampus: {mode: scheduled, hours: "22:00-07:00", idle_minutes: 10}
        memory:      always

Every check: leases past their ttl are dropped; what each online node reports
running starts or resets the idle clocks; decide() says what to start (a
scheduled app inside its hours) and what to stop (an on_demand app nobody has
held for its idle minutes; an unmanaged one an ensure started, the same); the
node's Observatory does it. An on_demand or scheduled app is also marked
orchestrated on its Observatory, so being off reads as idle, not down.

The default policy is the old behaviour exactly: unmanaged, 0 idle minutes -
a release stops at once what an ensure started, and nothing else is touched.
Lodestar and the Observatory are never managed.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from seren_sinew.orchestration import (DEFAULT_TTL_SECONDS, Leases, Policies, RunPolicy, decide, resolve,
                                       short_name)

log = logging.getLogger("seren_lodestar.run_as_needed")


@dataclass
class OrchestrationConfig:
    """The `orchestration:` block."""
    enabled: bool = True
    lease_ttl_minutes: float = DEFAULT_TTL_SECONDS / 60.0
    check_seconds: float = 60.0
    state_file: str = ""
    policies: Policies = field(default_factory=Policies)

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "OrchestrationConfig":
        d = d or {}

        def num(k: str, default: float, low: float) -> float:
            try:
                return max(low, float(d.get(k, default)))
            except (TypeError, ValueError):
                return default
        return cls(enabled=bool(d.get("enabled", True)),
                   lease_ttl_minutes=num("lease_ttl_minutes", DEFAULT_TTL_SECONDS / 60.0, 1.0),
                   check_seconds=num("check_seconds", 60.0, 5.0),
                   state_file=str(d.get("state_file") or ""),
                   policies=Policies.from_dict({"default": d.get("default"), "apps": d.get("apps")}))


def resolved_state_file(cfg: Any) -> Optional[Path]:
    """orchestration.state_file, else orchestration/leases.json beside the
    config; None on defaults (the book stays in memory)."""
    oc = cfg.orchestration
    if oc.state_file.strip():
        return Path(oc.state_file.strip()).expanduser().resolve()
    if cfg.config_path:
        return Path(cfg.config_path).resolve().parent / "orchestration" / "leases.json"
    return None


class Steward:
    """The lease book, the policies, and the check that applies them."""

    def __init__(self, cluster: Any, policies: Policies, state_file: Optional[Path] = None,
                 lease_ttl_seconds: float = DEFAULT_TTL_SECONDS, check_seconds: float = 60.0, log_fn=None) -> None:
        # The cluster, or a function that returns it (the app passes one, so
        # whatever app.state.cluster is now is what the steward sees).
        self._cluster = cluster
        self.policies = policies
        self.state_file = state_file
        self.check_seconds = check_seconds
        self.leases = Leases.load(state_file, lease_ttl_seconds) if state_file else Leases(lease_ttl_seconds)
        self.history: deque[dict[str, Any]] = deque(maxlen=50)
        self.last_check: Optional[float] = None
        self._marked: set[tuple[str, str]] = set()
        self._log = log_fn or (lambda m: log.info(m))
        self._lock = asyncio.Lock()

    @property
    def cluster(self) -> Any:
        return self._cluster() if callable(self._cluster) else self._cluster

    # ── the book ──────────────────────────────────────────────────────────
    def save(self) -> None:
        if self.state_file is None:
            return
        try:
            self.leases.save(self.state_file)
        except OSError as e:
            self._log(f"could not keep the lease book at {self.state_file}: {e}")

    def policy(self, service: str) -> RunPolicy:
        return self.policies.for_service(service)

    def resolve(self, name: str) -> tuple[Optional[str], Optional[str]]:
        """(the installed service `name` means, or None and why). A name that
        is installed is itself; a short one ('probe') is the one install of
        it; two installs of it is a question back, not a guess."""
        installed = sorted(self.cluster.get_capabilities())
        found = resolve(name, installed)
        if len(found) == 1:
            return found[0], None
        if not found:
            return None, f"no online node has '{name}' installed"
        return None, f"'{name}' is ambiguous: name one of {', '.join(found)}"

    def running(self) -> dict[tuple[str, str], bool]:
        """What every online node reports, per service. A service whose status
        the node did not give is left out (unknown is not off)."""
        out: dict[tuple[str, str], bool] = {}
        for node, snap in self.cluster.get_snapshots().items():
            if not snap.online:
                continue
            for svc in snap.installed_services or []:
                st = (snap.status or {}).get(svc)
                if st is not None:
                    out[(node, svc)] = bool(getattr(st, "running", False))
        return out

    # ── the check ─────────────────────────────────────────────────────────
    async def check(self, now: Optional[float] = None, local=None) -> list[dict[str, Any]]:
        """Expire, tick, decide, act. Returns what was done this time."""
        async with self._lock:
            now = time.time() if now is None else now
            for d in self.leases.expire(now):
                self._log(f"lease ran out: {d['holder']} on {d['node']}/{d['service']}")
            running = self.running()
            self.leases.tick(running, now)
            await self._mark_orchestrated(running)
            done = []
            for dec in decide(self.leases.entries(), running, self.policies, now=now, local=local):
                agent = self.cluster.get_agent(dec.node)
                if agent is None:
                    continue
                if dec.action == "stop":
                    r = await agent.stop_service_async(dec.service)
                else:
                    r = await agent.start_service_async(dec.service)
                ok = bool(r is not None and getattr(r, "ok", False))
                if ok and dec.action == "stop":
                    self.leases.forget(dec.node, dec.service)
                row = {**dec.to_dict(), "ok": ok, "at": now,
                       **({} if ok else {"error": getattr(r, "error", None) or "the node did not answer"})}
                self.history.appendleft(row)
                done.append(row)
                self._log(f"{dec.action} {dec.node}/{dec.service}: {dec.reason}" + ("" if ok else f" - FAILED: {row['error']}"))
            self.last_check = now
            self.save()
            return done

    async def _mark_orchestrated(self, running: dict[tuple[str, str], bool]) -> None:
        """An app that is off on purpose reads as idle on its Observatory."""
        for node, svc in running:
            if (node, svc) in self._marked or self.policy(svc).mode not in ("on_demand", "scheduled"):
                continue
            agent = self.cluster.get_agent(node)
            setter = getattr(agent, "set_orchestrated_async", None)
            if setter is not None and await setter(svc, True):
                self._marked.add((node, svc))

    async def loop(self) -> None:
        """Cancelled at shutdown. A failed check is logged and tried again."""
        while True:
            await asyncio.sleep(self.check_seconds)
            try:
                await self.check()
            except Exception as e:  # noqa: BLE001 - the steward never takes the head down
                self._log(f"check failed: {type(e).__name__}: {e}")

    def describe(self) -> dict[str, Any]:
        return {"policies": self.policies.to_dict(), "check_seconds": self.check_seconds,
                "lease_ttl_seconds": self.leases.default_ttl,
                "state_file": str(self.state_file) if self.state_file else None,
                "last_check": self.last_check, "leases": self.leases.snapshot(),
                "apps": {f"{n}/{s}": {"running": up, "policy": self.policy(s).to_dict()}
                         for (n, s), up in sorted(self.running().items()) if short_name(s)},
                "history": list(self.history)}
