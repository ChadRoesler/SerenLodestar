"""
The scheduler on its own: the relative-offset and cron helpers, the task
book (add, list, remove, pause), the state file (atomic, round-trips every
field, a corrupt file starts fresh), and the tick-and-fire loop driven by
hand with a fake tool client: only due and unpaused tasks fire, a one-shot
leaves the book, a recurring cron re-arms, a tool's error lands on the
task, and a broken cron pauses the task instead of looping on it.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from seren_lodestar.scheduling import SchedulerService, ScheduledTask
from seren_lodestar.scheduling.scheduler_service import _cron_next, _parse_relative_offset


class FakeTools:
    """Stands in for the tool client: records every call, answers what it is told."""

    def __init__(self, answer: str = '{"ok": true}', raise_with: Exception | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.answer = answer
        self.raise_with = raise_with

    async def call_tool_async(self, name: str, args: dict) -> str:
        self.calls.append((name, args))
        if self.raise_with is not None:
            raise self.raise_with
        return self.answer


def _service(tmp_path, tools: FakeTools | None = None) -> tuple[SchedulerService, FakeTools, list[str]]:
    tools = tools or FakeTools()
    log: list[str] = []
    svc = SchedulerService(tool_client=tools, state_file_path=str(tmp_path / "scheduled_tasks.json"),
                           log_fn=log.append)
    return svc, tools, log


def _task(name: str, *, due_in_s: float = -1, schedule_type: str = "relative", cron: str = "",
          recurring: bool = False, args: str = "{}", paused: bool = False) -> ScheduledTask:
    return ScheduledTask(
        name=name, description=f"task {name}", tool_name="ping", schedule_type=schedule_type,
        tool_args_json=args, cron_expression=cron,
        next_fire_at=datetime.now(timezone.utc) + timedelta(seconds=due_in_s),
        recurring=recurring, paused=paused,
    )


# ── helpers ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,seconds", [
    ("90s", 90), ("30m", 1800), ("2h", 7200), ("5d", 432000), ("0m", 0),
])
def test_relative_offsets_parse(text, seconds):
    assert _parse_relative_offset(text) == seconds


@pytest.mark.parametrize("text", ["", "m", "5", "5w", "-5m", "abc", "1.5h", "h5"])
def test_bad_relative_offsets_are_none(text):
    assert _parse_relative_offset(text) is None


def test_cron_next_is_the_next_minute_for_every_minute():
    base = datetime(2026, 11, 7, 20, 0, 30, tzinfo=timezone.utc)
    nxt = _cron_next("* * * * *", base)
    assert nxt == datetime(2026, 11, 7, 20, 1, tzinfo=timezone.utc)


def test_cron_next_of_a_bad_expression_is_none():
    assert _cron_next("not a cron", datetime.now(timezone.utc)) is None
    assert _cron_next("", datetime.now(timezone.utc)) is None


# ── the book ──────────────────────────────────────────────────────────────

async def test_add_list_remove_pause_and_the_state_file(tmp_path):
    svc, _, log = _service(tmp_path)
    added = await svc.add_async(_task("a", due_in_s=3600))
    assert added.name == "a"
    listed = await svc.list_async()
    assert [t.name for t in listed] == ["a"]

    # what list hands out is a clone: mutating it never reaches the book
    listed[0].paused = True
    assert (await svc.list_async())[0].paused is False

    with pytest.raises(ValueError):
        await svc.add_async(_task("a"))

    assert await svc.set_paused_async("a", True) is True
    assert (await svc.list_async())[0].paused is True
    assert await svc.set_paused_async("nobody", True) is False

    assert await svc.remove_async("a") is True
    assert await svc.remove_async("a") is False
    assert await svc.list_async() == []

    state = tmp_path / "scheduled_tasks.json"
    assert json.loads(state.read_text(encoding="utf-8")) == []          # the remove was saved
    assert not state.with_suffix(".tmp").exists()                        # the atomic write left no temp file
    assert any("added task 'a'" in m for m in log) and any("removed task 'a'" in m for m in log)


async def test_every_field_round_trips_through_the_state_file(tmp_path):
    svc, _, _ = _service(tmp_path)
    fired = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
    t = _task("full", due_in_s=60, schedule_type="cron", cron="*/5 * * * *", recurring=True,
              args='{"x": 1}', paused=True)
    t.last_fired_at = fired
    t.fire_count = 7
    t.last_error = "it broke once"
    await svc.add_async(t)

    again, _, _ = _service(tmp_path)
    await again.start()
    try:
        [loaded] = await again.list_async()
    finally:
        await again.stop()
    for f in ("name", "description", "tool_name", "schedule_type", "tool_args_json", "cron_expression",
              "next_fire_at", "recurring", "created_at", "last_fired_at", "fire_count", "last_error", "paused"):
        assert getattr(loaded, f) == getattr(t, f), f
    assert loaded.next_fire_at.tzinfo is not None                      # datetimes stay aware across the file


async def test_a_corrupt_state_file_starts_fresh_and_says_so(tmp_path):
    (tmp_path / "scheduled_tasks.json").write_text("{not json", encoding="utf-8")
    svc, _, log = _service(tmp_path)
    await svc.start()
    try:
        assert await svc.list_async() == []
    finally:
        await svc.stop()
    assert any("could not load state" in m and "starting fresh" in m for m in log)
    assert any("shutdown" in m for m in log)
    assert json.loads((tmp_path / "scheduled_tasks.json").read_text(encoding="utf-8")) == []   # stop flushed


async def test_start_twice_keeps_one_loop_and_stop_is_idempotent(tmp_path):
    svc, _, _ = _service(tmp_path)
    await svc.start()
    first = svc._task_obj
    await svc.start()
    assert svc._task_obj is first
    await svc.stop()
    await svc.stop()
    assert svc._task_obj is None


# ── tick and fire ─────────────────────────────────────────────────────────

async def test_only_due_unpaused_tasks_fire_and_a_one_shot_leaves_the_book(tmp_path):
    svc, tools, _ = _service(tmp_path)
    await svc.add_async(_task("due", due_in_s=-5, args='{"n": 1}'))
    await svc.add_async(_task("later", due_in_s=3600))
    await svc.add_async(_task("paused", due_in_s=-5, paused=True))
    unscheduled = _task("unscheduled")
    unscheduled.next_fire_at = None
    await svc.add_async(unscheduled)

    await svc._tick()

    assert tools.calls == [("ping", {"n": 1})]
    names = sorted(t.name for t in await svc.list_async())
    assert names == ["later", "paused", "unscheduled"]                 # the one-shot is gone, the rest stay
    saved = json.loads((tmp_path / "scheduled_tasks.json").read_text(encoding="utf-8"))
    assert sorted(d["name"] for d in saved) == names


async def test_a_recurring_cron_task_re_arms_in_the_future(tmp_path):
    svc, tools, _ = _service(tmp_path)
    await svc.add_async(_task("every-minute", due_in_s=-1, schedule_type="cron", cron="* * * * *", recurring=True))
    before = datetime.now(timezone.utc)

    await svc._tick()

    [t] = await svc.list_async()
    assert tools.calls == [("ping", {})]
    assert t.fire_count == 1 and t.last_error is None and t.last_fired_at >= before
    assert t.next_fire_at.tzinfo is not None and t.next_fire_at > before
    assert t.paused is False


async def test_a_tool_error_answer_lands_on_the_task(tmp_path):
    svc, _, log = _service(tmp_path, FakeTools(answer=json.dumps({"error": "no such tool"})))
    await svc.add_async(_task("bad", due_in_s=-1, schedule_type="cron", cron="* * * * *", recurring=True))
    await svc._tick()
    [t] = await svc.list_async()
    assert t.last_error == "no such tool" and t.fire_count == 1
    assert any("fire reported error: no such tool" in m for m in log)


async def test_a_tool_that_raises_lands_its_exception_on_the_task(tmp_path):
    svc, _, _ = _service(tmp_path, FakeTools(raise_with=RuntimeError("boom")))
    await svc.add_async(_task("raises", due_in_s=-1, schedule_type="cron", cron="* * * * *", recurring=True))
    await svc._tick()
    [t] = await svc.list_async()
    assert t.last_error == "RuntimeError: boom"


async def test_arguments_that_are_not_an_object_never_reach_the_tool(tmp_path):
    svc, tools, _ = _service(tmp_path)
    await svc.add_async(_task("listy", due_in_s=-1, args="[1, 2]", schedule_type="cron", cron="* * * * *",
                              recurring=True))
    await svc._tick()
    [t] = await svc.list_async()
    assert tools.calls == []
    assert t.last_error is not None and "JSON object" in t.last_error


async def test_a_broken_cron_pauses_the_task_instead_of_looping(tmp_path):
    svc, tools, log = _service(tmp_path)
    await svc.add_async(_task("broken", due_in_s=-1, schedule_type="cron", cron="not a cron", recurring=True))
    await svc._tick()
    [t] = await svc.list_async()
    assert tools.calls == [("ping", {})]                                # it fired once
    assert t.paused is True and t.last_error == "cron reparse failed"
    assert any("broken cron, paused" in m for m in log)

    tools.calls.clear()
    await svc._tick()
    assert tools.calls == []                                            # and never again
