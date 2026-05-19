"""Chapter 3 starter — IPC primitives and the Ticket state machine.

Implement:

  write_ipc_message(directory, payload) -> Path
    Write JSON atomically (temp file + rename). Filename format:
    <millis_since_epoch>-<4-hex>.json so files sort chronologically.

  Ticket with transitions: pending -> running -> (success | skipped | error)
    On succeed(), require a verified Manifest. On fail(), record the
    structured error code.

See chapters/chapter_03_ipc/solution.py for reference exports.

Run `pytest chapters/chapter_03_ipc/tests.py -v` to check your work.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from sovereign_agent._internal.atomic import (
    atomic_write_json,
    atomic_write_text,
    compute_sha256,
    new_ipc_filename,
)
from sovereign_agent.errors import IOError as SovereignIOError
from sovereign_agent.errors import ValidationError
from sovereign_agent.ipc.watcher import IpcWatcher
from sovereign_agent.session.state import _parse_dt, now_utc

if TYPE_CHECKING:
    from sovereign_agent.session.directory import Session

CLOSE_SENTINEL_NAME = "_close"


def write_ipc_message(directory: Path, payload: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / new_ipc_filename()
    atomic_write_json(path, payload)
    return path


def write_close_sentinel(ipc_input_dir: Path) -> Path:
    ipc_input_dir.mkdir(parents=True, exist_ok=True)
    path = ipc_input_dir / CLOSE_SENTINEL_NAME
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(b"")
    tmp.replace(path)
    return path


def is_close_sentinel(name_or_path: str | Path) -> bool:
    name = name_or_path.name if isinstance(name_or_path, Path) else name_or_path
    return name == CLOSE_SENTINEL_NAME


def clear_close_sentinel(ipc_input_dir: Path) -> bool:
    path = ipc_input_dir / CLOSE_SENTINEL_NAME
    if path.exists():
        path.unlink()
        return True
    return False


def read_and_consume(
    directory: Path,
    *,
    max_age_ms: int = 100,
    archive_dir: Path | None = None,
    error_dir: Path | None = None,
) -> list[tuple[Path, dict]]:
    out: list[tuple[Path, dict]] = []
    if not directory.exists():
        return out

    archive = archive_dir or directory / "processed"
    errors = error_dir or directory.parent / "errors"
    archive.mkdir(parents=True, exist_ok=True)

    now_ms = int(time.time() * 1000)
    for entry in sorted(directory.iterdir()):
        if not entry.is_file() or entry.name == "_close" or entry.suffix == ".tmp":
            continue
        age_ms = now_ms - int(entry.stat().st_mtime * 1000)
        if age_ms < max_age_ms:
            continue

        try:
            payload = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            errors.mkdir(parents=True, exist_ok=True)
            entry.replace(errors / entry.name)
            continue

        original = entry
        entry.replace(archive / entry.name)
        out.append((original, payload))
    return out


def send_input(ipc_input_dir: Path, payload: dict) -> Path:
    if not ipc_input_dir.exists():
        raise SovereignIOError(
            code="SA_IO_NOT_FOUND",
            message=f"ipc input directory does not exist: {ipc_input_dir}",
            context={"path": str(ipc_input_dir)},
        )
    return write_ipc_message(ipc_input_dir, payload)


class TicketState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass
class OutputRecord:
    path: Path
    sha256: str
    size_bytes: int
    content_type: str = "application/json"

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "content_type": self.content_type,
        }

    @classmethod
    def from_dict(cls, data: dict) -> OutputRecord:
        return cls(
            path=Path(data["path"]),
            sha256=data["sha256"],
            size_bytes=int(data["size_bytes"]),
            content_type=data.get("content_type", "application/json"),
        )


@dataclass
class Manifest:
    ticket_id: str
    operation: str
    started_at: datetime
    completed_at: datetime
    duration_ms: int
    outputs: list[OutputRecord] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    def verify(self) -> bool:
        """Check every listed output file exists and its sha256 matches."""
        for record in self.outputs:
            if not record.path.exists():
                return False
            if compute_sha256(record.path) != record.sha256:
                return False
        return True

    def to_dict(self) -> dict:
        return {
            "ticket_id": self.ticket_id,
            "operation": self.operation,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "duration_ms": self.duration_ms,
            "outputs": [o.to_dict() for o in self.outputs],
            "metrics": self.metrics,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Manifest:
        return cls(
            ticket_id=data["ticket_id"],
            operation=data["operation"],
            started_at=_parse_dt(data["started_at"]),
            completed_at=_parse_dt(data["completed_at"]),
            duration_ms=int(data["duration_ms"]),
            outputs=[OutputRecord.from_dict(o) for o in data.get("outputs", [])],
            metrics=data.get("metrics", {}),
        )


TERMINAL_TICKET_STATES: frozenset[TicketState] = frozenset(
    {TicketState.SUCCESS, TicketState.SKIPPED, TicketState.ERROR}
)

ALLOWED_TICKET_TRANSITIONS: dict[TicketState, frozenset[TicketState]] = {
    TicketState.PENDING: frozenset({TicketState.RUNNING, TicketState.SKIPPED, TicketState.ERROR}),
    TicketState.RUNNING: frozenset({TicketState.SUCCESS, TicketState.SKIPPED, TicketState.ERROR}),
    TicketState.SUCCESS: frozenset(),
    TicketState.SKIPPED: frozenset(),
    TicketState.ERROR: frozenset(),
}


@dataclass
class TicketResult:
    ticket_id: str
    state: TicketState
    summary: str
    manifest: Manifest | None = None
    error_code: str | None = None
    error_message: str | None = None
    raw_output_path: Path | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


def is_ticket_transition_allowed(current: TicketState, proposed: TicketState) -> bool:
    return proposed in ALLOWED_TICKET_TRANSITIONS.get(current, frozenset())


def _generate_ticket_id() -> str:
    return f"tk_{secrets.token_hex(4)}"


class Ticket:
    def __init__(self, session: Session, operation: str, ticket_id: str | None = None) -> None:
        self.session = session
        self.operation = operation
        self.ticket_id = ticket_id or _generate_ticket_id()
        self.directory = session.tickets_dir / self.ticket_id
        self._started_at: datetime | None = None
        self._state = TicketState.PENDING
        self.directory.mkdir(parents=True, exist_ok=False)
        self._write_state(TicketState.PENDING, started_at=None, completed_at=None)

    @property
    def state_path(self) -> Path:
        return self.directory / "state.json"

    @property
    def manifest_path(self) -> Path:
        return self.directory / "manifest.json"

    @property
    def summary_path(self) -> Path:
        return self.directory / "summary.md"

    def _write_state(
        self,
        state: TicketState,
        *,
        started_at: datetime | None,
        completed_at: datetime | None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        atomic_write_json(
            self.state_path,
            {
                "ticket_id": self.ticket_id,
                "operation": self.operation,
                "state": state.value,
                "started_at": started_at.isoformat() if started_at else None,
                "completed_at": completed_at.isoformat() if completed_at else None,
                "error_code": error_code,
                "error_message": error_message,
            },
        )
        self._state = state

    def _enforce_transition(self, proposed: TicketState) -> None:
        if not is_ticket_transition_allowed(self._state, proposed):
            raise ValidationError(
                code="SA_VAL_INVALID_STATE_TRANSITION",
                message=f"invalid ticket transition: {self._state.value!r} -> {proposed.value!r}",
                context={
                    "ticket_id": self.ticket_id,
                    "current": self._state.value,
                    "proposed": proposed.value,
                },
            )

    def start(self) -> None:
        self._enforce_transition(TicketState.RUNNING)
        self._started_at = now_utc()
        self._write_state(TicketState.RUNNING, started_at=self._started_at, completed_at=None)

    def succeed(self, manifest: Manifest, summary: str) -> None:
        self._enforce_transition(TicketState.SUCCESS)
        if not summary.strip():
            raise ValidationError(
                code="SA_VAL_MISSING_REQUIRED_FIELD",
                message="ticket.succeed() requires a non-empty summary",
                context={"ticket_id": self.ticket_id},
            )
        if not manifest.verify():
            raise SovereignIOError(
                code="SA_IO_MANIFEST_INVALID",
                message=f"manifest for ticket {self.ticket_id} did not verify",
                context={"ticket_id": self.ticket_id},
            )
        completed = now_utc()
        atomic_write_json(self.manifest_path, manifest.to_dict())
        atomic_write_text(self.summary_path, summary)
        self._write_state(TicketState.SUCCESS, started_at=self._started_at, completed_at=completed)

    def skip(self, reason: str) -> None:
        self._enforce_transition(TicketState.SKIPPED)
        completed = now_utc()
        atomic_write_text(self.summary_path, f"Skipped: {reason}")
        self._write_state(TicketState.SKIPPED, started_at=self._started_at, completed_at=completed)

    def fail(self, error_code: str, error_message: str) -> None:
        self._enforce_transition(TicketState.ERROR)
        completed = now_utc()
        atomic_write_text(self.summary_path, f"Error [{error_code}]: {error_message}")
        self._write_state(
            TicketState.ERROR,
            started_at=self._started_at,
            completed_at=completed,
            error_code=error_code,
            error_message=error_message,
        )

    def read_state(self) -> TicketState:
        return TicketState(json.loads(self.state_path.read_text(encoding="utf-8"))["state"])

    def read_summary(self) -> str:
        if not self.summary_path.exists():
            return ""
        return self.summary_path.read_text(encoding="utf-8")

    def read_manifest(self) -> Manifest | None:
        if not self.manifest_path.exists():
            return None
        return Manifest.from_dict(json.loads(self.manifest_path.read_text(encoding="utf-8")))

    def read_result(self) -> TicketResult:
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        raw_output = self.directory / "raw_output.json"
        return TicketResult(
            ticket_id=self.ticket_id,
            state=TicketState(data["state"]),
            summary=self.read_summary(),
            manifest=self.read_manifest(),
            error_code=data.get("error_code"),
            error_message=data.get("error_message"),
            raw_output_path=raw_output if raw_output.exists() else None,
            started_at=_parse_opt_dt(data.get("started_at")),
            completed_at=_parse_opt_dt(data.get("completed_at")),
        )


def _parse_opt_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return _parse_dt(value)


def create_ticket(session: Session, operation: str) -> Ticket:
    return Ticket(session=session, operation=operation)


def list_tickets(session: Session, state_filter: TicketState | None = None) -> list[Ticket]:
    out: list[Ticket] = []
    if not session.tickets_dir.exists():
        return out
    for entry in sorted(session.tickets_dir.iterdir()):
        state_file = entry / "state.json"
        if not entry.is_dir() or not state_file.exists():
            continue
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            state = TicketState(data["state"])
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if state_filter is not None and state != state_filter:
            continue
        ticket = Ticket.__new__(Ticket)
        ticket.session = session
        ticket.operation = data["operation"]
        ticket.ticket_id = data["ticket_id"]
        ticket.directory = entry
        ticket._started_at = _parse_opt_dt(data.get("started_at"))
        ticket._state = state
        out.append(ticket)
    return out


__all__ = [
    "CLOSE_SENTINEL_NAME",
    "write_ipc_message",
    "write_close_sentinel",
    "is_close_sentinel",
    "clear_close_sentinel",
    "read_and_consume",
    "send_input",
    "IpcWatcher",
    "TicketState",
    "TERMINAL_TICKET_STATES",
    "TicketResult",
    "OutputRecord",
    "Manifest",
    "Ticket",
    "create_ticket",
    "list_tickets",
]
