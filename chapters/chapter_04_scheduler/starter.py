"""Chapter 4 starter — drift-corrected scheduler.

The interesting function is compute_next_run. It has two non-obvious
behaviors:

  1. ANCHORING: for interval tasks, anchor to task.next_run, NOT wall-clock
     now(). A task scheduled at 12:00:00 with interval 60s, computed at
     12:00:03, should return 12:01:00 — not 12:01:03.

  2. SKIP-AHEAD: when the system sleeps through many intervals, advance
     to the next FUTURE interval in one jump. Do not run missed intervals
     back-to-back.

Run `pytest chapters/chapter_04_scheduler/tests.py -v` to check.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

log = logging.getLogger(__name__)

ScheduleType = Literal["once", "interval", "cron"]

TaskFn = Callable[[], Awaitable[None]]


@dataclass
class ScheduledTask:
    id: str
    schedule_type: ScheduleType
    fn: TaskFn | None = None
    interval_s: int | None = None
    cron_expr: str | None = None
    timezone: str = "UTC"
    next_run: datetime | None = None
    enabled: bool = True
    session_id: str | None = None
    metadata: dict = field(default_factory=dict)


def compute_next_run(task: ScheduledTask, now: datetime | None = None) -> datetime | None:
    """Compute the next run time for `task`.

    - once: returns task.next_run if it's still in the future, else None.
    - interval: anchor to task.next_run; add interval_s repeatedly until
      the result is strictly in the future.
    - cron: use croniter with the task's timezone.
    """
    now = now or datetime.now(tz=UTC)

    if task.schedule_type == "once":
        if task.next_run is None:
            return None
        if task.next_run > now:
            return task.next_run
        return None

    if task.schedule_type == "interval":
        if task.interval_s is None or task.interval_s <= 0:
            raise ValueError(f"interval task {task.id!r} must have a positive interval_s")
        if task.next_run is None:
            return now + timedelta(seconds=task.interval_s)

        nxt = task.next_run + timedelta(seconds=task.interval_s)
        while nxt <= now:
            nxt += timedelta(seconds=task.interval_s)
        return nxt

    if task.schedule_type == "cron":
        if task.cron_expr is None:
            raise ValueError(f"cron task {task.id!r} must have a cron_expr")
        try:
            from croniter import croniter
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("croniter is required for cron scheduled tasks") from exc
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(task.timezone)
        except Exception:  # pragma: no cover
            tz = UTC

        local_now = now.astimezone(tz)
        next_local = croniter(task.cron_expr, local_now).get_next(datetime)
        return next_local.astimezone(UTC)

    raise ValueError(f"unknown schedule_type: {task.schedule_type!r}")


class DriftCorrectedScheduler:
    def __init__(self, poll_interval_s: float = 1.0) -> None:
        self.tasks: dict[str, ScheduledTask] = {}
        self.poll_interval_s = poll_interval_s
        self._running = False

    def register(self, task: ScheduledTask) -> None:
        if task.next_run is None:
            task.next_run = compute_next_run(task)
        self.tasks[task.id] = task
        log.info(
            "scheduler: registered %s (type=%s, next_run=%s)",
            task.id,
            task.schedule_type,
            task.next_run,
        )

    def unregister(self, task_id: str) -> None:
        self.tasks.pop(task_id, None)

    async def run(self) -> None:
        self._running = True
        try:
            while self._running:
                await self._tick()
                await asyncio.sleep(self.poll_interval_s)
        finally:
            self._running = False

    async def _tick(self) -> None:
        now = datetime.now(tz=UTC)
        for task in list(self.tasks.values()):
            if not task.enabled:
                continue
            if task.next_run is None or task.next_run > now:
                continue
            asyncio.create_task(self._fire(task))
            if task.schedule_type == "once":
                self.unregister(task.id)
            else:
                task.next_run = compute_next_run(task, now)

    async def _fire(self, task: ScheduledTask) -> None:
        if task.fn is None:
            return
        try:
            await task.fn()
        except Exception:  # noqa: BLE001
            log.exception("scheduled task %s failed", task.id)

    async def shutdown(self) -> None:
        self._running = False


__all__ = [
    "ScheduleType",
    "ScheduledTask",
    "DriftCorrectedScheduler",
    "compute_next_run",
]
