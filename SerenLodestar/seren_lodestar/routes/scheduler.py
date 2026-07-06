"""
Scheduler routes — /api/v1/scheduler/* CRUD for scheduled tasks.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..scheduling import ScheduledTask, SchedulerService

API_VERSION = "v1"

# ── cron parsing (croniter) ────────────────────────────────────────────
try:
    from croniter import croniter
except ImportError:
    croniter = None  # type: ignore[assignment]

router = APIRouter(tags=["scheduler"])


def _parse_offset(s: str) -> Optional[int]:
    """Parse '2h', '30m', '5d', '90s' → total seconds."""
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
    return n * multipliers.get(unit, 0) or None


@router.get(f"/api/{API_VERSION}/scheduler/tasks")
async def list_tasks(request: Request):
    scheduler: SchedulerService = request.app.state.scheduler
    tasks = await scheduler.list_async()
    return {
        "tasks": [
            {
                "name": t.name,
                "description": t.description,
                "tool_name": t.tool_name,
                "tool_args_json": t.tool_args_json,
                "schedule_type": t.schedule_type,
                "cron_expression": t.cron_expression,
                "next_fire_at": t.next_fire_at.isoformat() if t.next_fire_at else None,
                "recurring": t.recurring,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "last_fired_at": t.last_fired_at.isoformat() if t.last_fired_at else None,
                "fire_count": t.fire_count,
                "last_error": t.last_error,
                "paused": t.paused,
            }
            for t in tasks
        ],
    }


@router.post(f"/api/{API_VERSION}/scheduler/tasks")
async def add_task(request: Request):
    scheduler: SchedulerService = request.app.state.scheduler
    try:
        body = await request.json()
    except Exception as ex:
        return JSONResponse({"error": f"malformed body: {ex}"}, status_code=400)
    if body is None or not isinstance(body, dict):
        return JSONResponse({"error": "empty body"}, status_code=400)
    name = body.get("name")
    tool_name = body.get("tool_name")
    schedule_type = body.get("schedule_type")
    if not name:
        return JSONResponse({"error": "'name' required"}, status_code=400)
    if not tool_name:
        return JSONResponse({"error": "'tool_name' required"}, status_code=400)
    if not schedule_type:
        return JSONResponse({"error": "'schedule_type' required (cron|relative)"}, status_code=400)

    schedule_type = schedule_type.lower()
    now = datetime.now(timezone.utc)

    if schedule_type == "cron":
        cron_expr = body.get("cron_expression")
        if not cron_expr:
            return JSONResponse(
                {"error": "cron schedule requires 'cron_expression'"},
                status_code=400,
            )
        if croniter is None:
            return JSONResponse(
                {"error": "croniter not installed; pip install croniter"},
                status_code=500,
            )
        try:
            it = croniter(cron_expr, now)
            next_fire = it.get_next(datetime)
        except Exception as ex:
            return JSONResponse(
                {"error": f"invalid cron: {ex}"},
                status_code=400,
            )
        next_fire = next_fire.replace(tzinfo=timezone.utc)
        recurring = True
    elif schedule_type == "relative":
        offset_str = body.get("relative_offset")
        if not offset_str:
            return JSONResponse(
                {"error": "relative schedule requires 'relative_offset' like '2h', '30m', '5d'"},
                status_code=400,
            )
        total_seconds = _parse_offset(offset_str)
        if total_seconds is None or total_seconds <= 0:
            return JSONResponse(
                {"error": f"can't parse offset '{offset_str}'; use Nh, Nm, Nd"},
                status_code=400,
            )
        next_fire = now + timedelta(seconds=total_seconds)
        recurring = False
        cron_expr = ""
    else:
        return JSONResponse(
            {"error": f"unknown schedule_type '{schedule_type}'; use 'cron' or 'relative'"},
            status_code=400,
        )

    task = ScheduledTask(
        name=name,
        description=body.get("description", ""),
        tool_name=tool_name,
        tool_args_json=body.get("tool_args_json", "{}"),
        schedule_type=schedule_type,
        cron_expression=cron_expr,
        next_fire_at=next_fire,
        recurring=recurring,
    )
    try:
        created = await scheduler.add_async(task)
        return {
            "task": {
                "name": created.name,
                "description": created.description,
                "tool_name": created.tool_name,
                "tool_args_json": created.tool_args_json,
                "schedule_type": created.schedule_type,
                "cron_expression": created.cron_expression,
                "next_fire_at": created.next_fire_at.isoformat() if created.next_fire_at else None,
                "recurring": created.recurring,
                "created_at": created.created_at.isoformat() if created.created_at else None,
                "fire_count": created.fire_count,
                "last_error": created.last_error,
                "paused": created.paused,
            }
        }
    except ValueError as ex:
        return JSONResponse({"error": str(ex)}, status_code=409)


@router.delete(f"/api/{API_VERSION}/scheduler/tasks/{{name}}")
async def delete_task(request: Request, name: str):
    scheduler: SchedulerService = request.app.state.scheduler
    removed = await scheduler.remove_async(name)
    if not removed:
        return JSONResponse(
            {"error": f"no task named '{name}'"},
            status_code=404,
        )
    return {"removed": name}


@router.post(f"/api/{API_VERSION}/scheduler/tasks/{{name}}/pause")
async def pause_task(request: Request, name: str):
    scheduler: SchedulerService = request.app.state.scheduler
    ok = await scheduler.set_paused_async(name, True)
    if not ok:
        return JSONResponse(
            {"error": f"no task named '{name}'"},
            status_code=404,
        )
    return {"paused": name}


@router.post(f"/api/{API_VERSION}/scheduler/tasks/{{name}}/resume")
async def resume_task(request: Request, name: str):
    scheduler: SchedulerService = request.app.state.scheduler
    ok = await scheduler.set_paused_async(name, False)
    if not ok:
        return JSONResponse(
            {"error": f"no task named '{name}'"},
            status_code=404,
        )
    return {"resumed": name}
