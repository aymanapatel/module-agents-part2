"""Chapter 2 starter — fill in the SessionQueue.

Your job is to implement a SessionQueue with three guarantees:

  1. Per-session serialization: at most one worker per session at a time.
  2. Global concurrency cap: no more than `max_concurrent` sessions running.
  3. Retry with exponential backoff: transient failures get retried at
     BASE_RETRY_S * 2^(attempt-1), up to MAX_RETRIES.

Plus idle preemption (write _close into the worker's ipc_input_dir when
higher-priority work arrives for the same session) and graceful shutdown
(detach running workers, don't kill them).

Run `pytest chapters/chapter_02_queue/tests.py -v` to check your work.
Compare to `solution.py` when you're done.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import ClassVar

from sovereign_agent.ipc.protocol import write_close_sentinel

log = logging.getLogger(__name__)


class TaskPriority(IntEnum):
    SCHEDULED = 0
    HANDOFF = 1
    EXECUTOR = 2
    PLANNER = 3


ProcessFn = Callable[[str], Awaitable[bool]]


@dataclass(order=True)
class QueuedTask:
    priority: int
    session_id: str = field(compare=False)
    kind: str = field(compare=False)
    scheduled_fn: Callable[[], Awaitable[None]] | None = field(default=None, compare=False)
    task_id: str | None = field(default=None, compare=False)
    direction: str | None = field(default=None, compare=False)


@dataclass
class _SessionState:
    active: bool = False
    idle_waiting: bool = False
    is_scheduled_task: bool = False
    pending_tasks: list[QueuedTask] = field(default_factory=list)
    ipc_input_dir: Path | None = None
    retry_count: int = 0


class SessionQueue:
    MAX_RETRIES: ClassVar[int] = 5
    BASE_RETRY_S: ClassVar[float] = 5.0

    def __init__(
        self,
        max_concurrent: int = 5,
        process_fn: ProcessFn | None = None,
    ) -> None:
        self.max_concurrent = max_concurrent
        self._process_fn = process_fn
        self._lock = asyncio.Lock()
        self._sessions: dict[str, _SessionState] = {}
        self._waiting: list[QueuedTask] = []
        self._active_count = 0
        self._shutting_down = False
        self._active_workers: dict[str, asyncio.Task] = {}

    def set_process_fn(self, fn: ProcessFn) -> None:
        self._process_fn = fn

    async def enqueue_planner(self, session_id: str) -> None:
        await self._enqueue(
            QueuedTask(priority=TaskPriority.PLANNER.value, session_id=session_id, kind="planner")
        )

    async def enqueue_executor(self, session_id: str) -> None:
        await self._enqueue(
            QueuedTask(priority=TaskPriority.EXECUTOR.value, session_id=session_id, kind="executor")
        )

    async def enqueue_handoff(self, session_id: str, direction: str) -> None:
        await self._enqueue(
            QueuedTask(
                priority=TaskPriority.HANDOFF.value,
                session_id=session_id,
                kind="handoff",
                direction=direction,
            )
        )

    async def enqueue_scheduled_task(
        self,
        session_id: str,
        task_id: str,
        fn: Callable[[], Awaitable[None]],
    ) -> None:
        await self._enqueue(
            QueuedTask(
                priority=TaskPriority.SCHEDULED.value,
                session_id=session_id,
                kind="scheduled",
                scheduled_fn=fn,
                task_id=task_id,
            )
        )

    async def _enqueue(self, task: QueuedTask) -> None:
        if self._shutting_down:
            log.info("dropping task for session %s: queue is shutting down", task.session_id)
            return

        async with self._lock:
            sess = self._sessions.setdefault(task.session_id, _SessionState())
            if sess.active:
                sess.pending_tasks.append(task)
                sess.pending_tasks.sort(key=lambda t: t.priority)
                if sess.idle_waiting:
                    self._send_close(sess)
                return

            if self._active_count >= self.max_concurrent:
                self._waiting.append(task)
                self._waiting.sort(key=lambda t: t.priority)
                return

            self._start_locked(task)

    def register_container(self, session_id: str, ipc_input_dir: Path) -> None:
        sess = self._sessions.setdefault(session_id, _SessionState())
        sess.ipc_input_dir = ipc_input_dir

    def unregister_container(self, session_id: str) -> None:
        sess = self._sessions.get(session_id)
        if sess is not None:
            sess.ipc_input_dir = None

    def notify_idle(self, session_id: str) -> None:
        """When this session's worker is idle and higher-priority work is
        waiting, write _close into its ipc_input_dir so it exits cleanly."""
        sess = self._sessions.get(session_id)
        if sess is None:
            return
        sess.idle_waiting = True
        if sess.pending_tasks:
            self._send_close(sess)

    def _send_close(self, sess: _SessionState) -> None:
        if sess.ipc_input_dir is None:
            return
        try:
            write_close_sentinel(sess.ipc_input_dir)
        except Exception:  # noqa: BLE001
            log.exception("failed to write _close sentinel")

    def _start_locked(self, task: QueuedTask) -> None:
        sess = self._sessions.setdefault(task.session_id, _SessionState())
        sess.active = True
        sess.is_scheduled_task = task.kind == "scheduled"
        self._active_count += 1
        runner = asyncio.create_task(self._run_task(task))
        self._active_workers[task.session_id] = runner

    async def _run_task(self, task: QueuedTask) -> None:
        sid = task.session_id
        sess = self._sessions[sid]
        try:
            if task.kind == "scheduled" and task.scheduled_fn is not None:
                await task.scheduled_fn()
                success = True
            else:
                if self._process_fn is None:
                    raise RuntimeError("SessionQueue: process_fn is not set")
                success = await self._process_fn(sid)
        except Exception:  # noqa: BLE001
            log.exception("task raised for session %s (kind=%s)", sid, task.kind)
            success = False

        if success:
            sess.retry_count = 0
            await self._after_task(sid)
        else:
            await self._handle_failure(sid, task)

    async def _handle_failure(self, sid: str, task: QueuedTask) -> None:
        sess = self._sessions[sid]
        sess.retry_count += 1
        if sess.retry_count > self.MAX_RETRIES:
            log.error("session %s: max retries exceeded", sid)
            sess.retry_count = 0
            await self._after_task(sid)
            return

        delay = self.BASE_RETRY_S * (2 ** (sess.retry_count - 1))
        await self._release_slot(sid)

        async def _delayed_retry() -> None:
            await asyncio.sleep(delay)
            if self._shutting_down:
                return
            if task.kind == "planner":
                await self.enqueue_planner(sid)
            elif task.kind == "executor":
                await self.enqueue_executor(sid)
            elif task.kind == "handoff" and task.direction is not None:
                await self.enqueue_handoff(sid, task.direction)
            elif task.kind == "scheduled" and task.scheduled_fn is not None and task.task_id:
                await self.enqueue_scheduled_task(sid, task.task_id, task.scheduled_fn)

        asyncio.create_task(_delayed_retry())

    async def _release_slot(self, sid: str) -> None:
        async with self._lock:
            sess = self._sessions.get(sid)
            if sess is not None:
                sess.active = False
                sess.idle_waiting = False
            self._active_count = max(0, self._active_count - 1)
            self._active_workers.pop(sid, None)
            self._maybe_start_from_waiting_locked()

    async def _after_task(self, sid: str) -> None:
        async with self._lock:
            sess = self._sessions.get(sid)
            if sess is None:
                return

            self._active_workers.pop(sid, None)
            if sess.pending_tasks:
                sess.pending_tasks.sort(key=lambda t: t.priority)
                next_task = sess.pending_tasks.pop(0)
                sess.idle_waiting = False
                sess.is_scheduled_task = next_task.kind == "scheduled"
            else:
                sess.active = False
                sess.idle_waiting = False
                self._active_count = max(0, self._active_count - 1)
                next_task = None

            if next_task is None:
                self._maybe_start_from_waiting_locked()
                return

        runner = asyncio.create_task(self._run_task(next_task))
        self._active_workers[sid] = runner

    def _maybe_start_from_waiting_locked(self) -> None:
        while self._waiting and self._active_count < self.max_concurrent:
            self._waiting.sort(key=lambda t: t.priority)
            picked: QueuedTask | None = None
            for index, task in enumerate(self._waiting):
                sess = self._sessions.get(task.session_id)
                if sess is None or not sess.active:
                    picked = task
                    del self._waiting[index]
                    break
            if picked is None:
                return
            self._start_locked(picked)

    async def shutdown(self, grace_period_s: float = 30.0) -> None:
        """Stop accepting new work. Do NOT cancel/kill running workers.
        Wait up to grace_period_s then return."""
        self._shutting_down = True
        active = list(self._active_workers.values())
        if not active:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*active, return_exceptions=True),
                timeout=grace_period_s,
            )
        except TimeoutError:
            log.info(
                "shutdown: grace period elapsed; %d worker(s) still running",
                sum(1 for task in active if not task.done()),
            )


__all__ = ["SessionQueue", "TaskPriority", "QueuedTask"]
