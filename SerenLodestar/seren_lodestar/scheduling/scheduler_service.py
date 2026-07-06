"""
seren_lodestar.scheduling.scheduler_service
=======================================================================

Background scheduler that fires MCP tool calls on a cron or relative
schedule. Ported from SerenLodestar/Scheduling/SchedulerService.cs.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .scheduled_task import ScheduledTask

# ── helpers for cron parsing (no NCrontab in Python – use croniter) ────────
try:
    from croniter import croniter
except ImportError:
    croniter = None  # type: ignore[assignment]


def _cron_next(expr: str, base: datetime) -> Optional[datetime]:
    """Compute the next fire time from a cron expression."""
    if croniter is None:
        raise RuntimeError("croniter is not installed; pip install croniter")
    try:
        it = croniter(expr, base)
        return it.get_next(datetime)
    except Exception:
        return None


def _parse_relative_offset(s: str) -> Optional[int]:
    """Parse '2h', '30m', '5d', '90s' → total seconds. Returns None on fail."""
    if len(s) < 2:
        return None
    unit = s[-1]
    try:
        n = int(s[:-1])
    except ValueError:
        return None
    if n < 0:
        return None
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if unit not in multipliers:
        return None
    return n * multipliers[unit]


class SchedulerService:
    """
    Background task scheduler that persists to a JSON file and fires
    MCP tool calls via an HTTP client factory.
    """

    def __init__(
        self,
        http_client_factory: Callable[[str], object],
        state_file_path: str,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._http_factory = http_client_factory
        self._state_path = state_file_path
        self._log = log_fn or (lambda m: print(f"[scheduler] {m}"))
        self._tasks: list[ScheduledTask] = []
        self._lock = asyncio.Lock()
        self._running = False
        self._task_obj: Optional[asyncio.Task] = None

    # ── Public API ────────────────────────────────────────────────────────

    async def list_async(self) -> list[ScheduledTask]:
        async with self._lock:
            return [self._clone(t) for t in self._tasks]

    async def add_async(self, task: ScheduledTask) -> ScheduledTask:
        async with self._lock:
            if any(t.name == task.name for t in self._tasks):
                raise ValueError(f"Task '{task.name}' already exists")
            self._tasks.append(task)
            await self._save_unsafe()
            self._log(f"added task '{task.name}' (type={task.schedule_type})")
            return self._clone(task)

    async def remove_async(self, name: str) -> bool:
        async with self._lock:
            before = len(self._tasks)
            self._tasks = [t for t in self._tasks if t.name != name]
            removed = before - len(self._tasks)
            if removed > 0:
                await self._save_unsafe()
                self._log(f"removed task '{name}'")
                return True
            return False

    async def set_paused_async(self, name: str, paused: bool) -> bool:
        async with self._lock:
            for t in self._tasks:
                if t.name == name:
                    t.paused = paused
                    await self._save_unsafe()
                    self._log(f"task '{name}' paused={paused}")
                    return True
            return False

    # ── Background loop ───────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background tick loop."""
        if self._running:
            return
        self._running = True
        await self._load_state()
        self._log(f"loaded {len(self._tasks)} scheduled tasks from {self._state_path}")
        self._task_obj = asyncio.create_task(self._tick_loop())

    async def stop(self) -> None:
        """Stop the background tick loop."""
        self._running = False
        if self._task_obj is not None:
            self._task_obj.cancel()
            try:
                await self._task_obj
            except asyncio.CancelledError:
                pass
            self._task_obj = None
        await self._save_state()
        self._log("shutdown, final state flushed")

    async def _tick_loop(self) -> None:
        """Tick every 30 seconds, fire due tasks."""
        try:
            await asyncio.sleep(5)  # warmup delay
            while self._running:
                try:
                    await self._tick()
                except Exception as ex:
                    self._log(f"tick threw: {type(ex).__name__}: {ex}")
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            pass

    async def _tick(self) -> None:
        now = datetime.now(timezone.utc)
        due: list[ScheduledTask] = []

        async with self._lock:
            for t in self._tasks:
                if not t.paused and t.next_fire_at and t.next_fire_at <= now:
                    due.append(t)

        for t in due:
            await self._fire(t)

    async def _fire(self, task: ScheduledTask) -> None:
        self._log(f"firing '{task.name}' → tool='{task.tool_name}'")
        error: Optional[str] = None

        try:
            client = self._http_factory("mcp")
            args = json.loads(task.tool_args_json) if task.tool_args_json else {}
            rpc = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": task.tool_name,
                    "arguments": args,
                },
            }

            # client is an httpx.AsyncClient or similar
            resp = await client.post("/", json=rpc)
            if not resp.is_success:
                error = f"MCP returned HTTP {resp.status_code}"
            else:
                body = resp.json()
                # Check for JSON-RPC error envelope
                if isinstance(body, dict) and "error" in body:
                    err = body["error"]
                    if isinstance(err, dict):
                        msg = err.get("message") or str(err)
                    else:
                        msg = str(err)
                    error = f"MCP JSON-RPC error: {msg}"
        except Exception as ex:
            error = f"{type(ex).__name__}: {ex}"

        if error is not None:
            self._log(f"  '{task.name}' fire reported error: {error}")

        async with self._lock:
            task.last_fired_at = datetime.now(timezone.utc)
            task.fire_count += 1
            task.last_error = error

            if task.recurring and task.schedule_type == "cron":
                next_time = _cron_next(task.cron_expression, datetime.utcnow())
                if next_time is not None:
                    task.next_fire_at = next_time.replace(tzinfo=timezone.utc)
                    self._log(f"  '{task.name}' re-armed for {task.next_fire_at.isoformat()}")
                else:
                    task.last_error = "cron reparse failed"
                    task.paused = True
                    self._log(f"  '{task.name}' broken cron, paused")
            else:
                self._tasks = [t for t in self._tasks if t.name != task.name]
                self._log(f"  '{task.name}' was one-shot, removed")

            await self._save_unsafe()

    # ── Persistence ──────────────────────────────────────────────────────

    async def _load_state(self) -> None:
        path = Path(self._state_path)
        if not path.exists():
            self._tasks = []
            return
        try:
            raw = await asyncio.to_thread(lambda: path.read_text(encoding="utf-8"))
            data = json.loads(raw)
            self._tasks = [
                self._task_from_dict(d) for d in data
            ]
        except Exception as ex:
            self._log(f"could not load state from {self._state_path}: {ex}; starting fresh")
            self._tasks = []

    async def _save_state(self) -> None:
        async with self._lock:
            await self._save_unsafe()

    async def _save_unsafe(self) -> None:
        path = Path(self._state_path)
        tmp = path.with_suffix(".tmp")
        raw = json.dumps(
            [self._task_to_dict(t) for t in self._tasks],
            indent=2,
        )
        # atomic write via tmpfile + rename
        await asyncio.to_thread(lambda: tmp.write_text(raw, encoding="utf-8"))
        await asyncio.to_thread(lambda: tmp.rename(path))

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _clone(t: ScheduledTask) -> ScheduledTask:
        return ScheduledTask(
            name=t.name,
            description=t.description,
            tool_name=t.tool_name,
            tool_args_json=t.tool_args_json,
            schedule_type=t.schedule_type,
            cron_expression=t.cron_expression,
            next_fire_at=t.next_fire_at,
            recurring=t.recurring,
            created_at=t.created_at,
            last_fired_at=t.last_fired_at,
            fire_count=t.fire_count,
            last_error=t.last_error,
            paused=t.paused,
        )

    @staticmethod
    def _task_from_dict(d: dict) -> ScheduledTask:
        def _dt(key: str) -> Optional[datetime]:
            v = d.get(key)
            if v is None:
                return None
            try:
                return datetime.fromisoformat(v)
            except Exception:
                return None

        return ScheduledTask(
            name=d.get("name", ""),
            description=d.get("description", ""),
            tool_name=d.get("tool_name", ""),
            tool_args_json=d.get("tool_args_json", "{}"),
            schedule_type=d.get("schedule_type", ""),
            cron_expression=d.get("cron_expression", ""),
            next_fire_at=_dt("next_fire_at"),
            recurring=d.get("recurring", False),
            created_at=_dt("created_at"),
            last_fired_at=_dt("last_fired_at"),
            fire_count=d.get("fire_count", 0),
            last_error=d.get("last_error"),
            paused=d.get("paused", False),
        )

    @staticmethod
    def _task_to_dict(t: ScheduledTask) -> dict:
        def _iso(dt: Optional[datetime]) -> Optional[str]:
            return dt.isoformat() if dt is not None else None

        return {
            "name": t.name,
            "description": t.description,
            "tool_name": t.tool_name,
            "tool_args_json": t.tool_args_json,
            "schedule_type": t.schedule_type,
            "cron_expression": t.cron_expression,
            "next_fire_at": _iso(t.next_fire_at),
            "recurring": t.recurring,
            "created_at": _iso(t.created_at),
            "last_fired_at": _iso(t.last_fired_at),
            "fire_count": t.fire_count,
            "last_error": t.last_error,
            "paused": t.paused,
        }
