"""
seren_lodestar.scheduling.scheduled_task
=======================================================================

ScheduledTask dataclass – ported from
SerenLodestar/Scheduling/ScheduledTask.cs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class ScheduledTask:
    """A persisted scheduled task that fires an MCP tool call."""

    name: str
    description: str
    tool_name: str
    schedule_type: str  # "cron" or "relative"
    tool_args_json: str = "{}"
    cron_expression: str = ""
    next_fire_at: Optional[datetime] = None
    recurring: bool = False
    created_at: datetime | None = None
    last_fired_at: Optional[datetime] = None
    fire_count: int = 0
    last_error: Optional[str] = None
    paused: bool = False

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc)
