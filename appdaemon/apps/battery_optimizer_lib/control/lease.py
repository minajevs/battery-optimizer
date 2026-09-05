"""Durable evidence that a process started a session and may not have finished it.

The hardware watchdog does not exist. A session armed with 30408=1 was observed
still armed at t=90s (reference WIT, 2026-09-05), and only an explicit release
ended it. So the failure this file exists for is real and unbounded: a process
that dies holding 30100=1 / 30407=1 leaves the inverter executing its last
command until a human intervenes.

**What a lease is NOT.** It is not proof of ownership, and it never promotes a
session back to ACTIVE. Ownership stays what it has always been — this
process's own confirmed writes — because a file cannot tell you what a register
means. A lease says exactly one thing:

    a previous instance of this process started a session against THIS device
    and has no record of finishing its cleanup

and it grants exactly one permission: **release**. Never resume, never re-arm,
never "continue where we left off". Every path out of recovery makes the
inverter less controlled than it found it.

Without a lease, 30100=1 remains AUTHORITY_HELD_NOT_OURS and is not touched.
That rule is unchanged; the lease only carves out the case where the evidence
is our own.

**Ordering.** The lease is written BEFORE the authority write and removed only
after both halves of the release are confirmed. That order is the whole design:
a crash between "lease written" and "authority taken" leaves a lease with
nothing to clean up, which recovery detects from the registers and discards. The
opposite order would leave the case this file exists to prevent — authority
taken, no record of it.

Writes are atomic (temp file + ``os.replace``) and fsynced, because the crash
being defended against can land between any two instructions.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Optional

# Lease lifecycle. ACQUIRING is written before the first control write and is
# just as recoverable as ACTIVE -- more so, if anything: it covers the arm that
# was half-applied, which is the state with authority held and nothing armed.
ACQUIRING = "acquiring"
ACTIVE = "active"
RELEASING = "releasing"


@dataclass(frozen=True)
class LeaseRecord:
    """What a previous instance knew when it last wrote to disk."""

    session_id: str
    device_id: str
    state: str
    started_at: float
    action: str = ""
    setpoint_percent: Optional[int] = None
    duration_minutes: Optional[int] = None
    updated_at: float = 0.0
    pid: Optional[int] = None

    def describe(self) -> str:
        age = max(0, int(time.time() - self.started_at))
        return (f"lease {self.session_id[:8]} state={self.state} "
                f"action={self.action or '?'} "
                f"setpoint={self.setpoint_percent} "
                f"duration={self.duration_minutes} "
                f"age={age}s pid={self.pid}")


class SessionLease:
    """A single JSON file recording an unfinished session, or nothing at all.

    ``path`` may be None, which disables persistence entirely: every operation
    becomes a no-op and ``read()`` returns None. That is deliberate — a missing
    path must not be an error at write time, because the alternative is a
    process that refuses to release an inverter it has already armed.
    """

    def __init__(self, path: Optional[str], log_func=None, clock=time.time):
        self.path = path
        self._log_func = log_func
        self._clock = clock
        self.last_error: Optional[str] = None

    # --- plumbing ---------------------------------------------------------

    def _log(self, message: str, level: str = "INFO") -> None:
        if self._log_func is not None:
            self._log_func(f"[lease] {message}", level=level)

    @property
    def enabled(self) -> bool:
        return bool(self.path)

    def read(self) -> Optional[LeaseRecord]:
        """The lease on disk, or None. Never raises."""
        if not self.path:
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return None
        except Exception as e:  # noqa: BLE001 - a corrupt lease is a finding
            self.last_error = str(e)
            self._log(
                f"could not read the lease at {self.path}: {e}. It is being "
                f"treated as ABSENT, which means a stranded session would not "
                f"be recognised as ours — check 30100/30407 by hand",
                level="ERROR")
            return None

        try:
            return LeaseRecord(
                session_id=str(data["session_id"]),
                device_id=str(data["device_id"]),
                state=str(data["state"]),
                started_at=float(data["started_at"]),
                action=str(data.get("action", "")),
                setpoint_percent=data.get("setpoint_percent"),
                duration_minutes=data.get("duration_minutes"),
                updated_at=float(data.get("updated_at", 0.0)),
                pid=data.get("pid"),
            )
        except Exception as e:  # noqa: BLE001 - same reasoning
            self.last_error = str(e)
            self._log(f"lease at {self.path} is malformed ({e}); treated as "
                      f"ABSENT", level="ERROR")
            return None

    def _write(self, record: LeaseRecord) -> bool:
        """Atomically replace the lease file. Returns False if it did not land."""
        if not self.path:
            return False
        payload = json.dumps(asdict(record), indent=2, sort_keys=True)
        temp = f"{self.path}.{os.getpid()}.tmp"
        try:
            directory = os.path.dirname(os.path.abspath(self.path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(temp, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            return True
        except Exception as e:  # noqa: BLE001 - callers decide what to do
            self.last_error = str(e)
            self._log(f"could not write the lease to {self.path}: {e}",
                      level="ERROR")
            try:
                os.unlink(temp)
            except OSError:
                pass
            return False

    # --- lifecycle --------------------------------------------------------

    def open(self, device_id: str, action: str = "",
             setpoint_percent: Optional[int] = None,
             duration_minutes: Optional[int] = None) -> Optional[LeaseRecord]:
        """Record the intent to take authority. Called BEFORE the first write."""
        record = LeaseRecord(
            session_id=uuid.uuid4().hex,
            device_id=str(device_id),
            state=ACQUIRING,
            started_at=self._clock(),
            action=action,
            setpoint_percent=setpoint_percent,
            duration_minutes=duration_minutes,
            updated_at=self._clock(),
            pid=os.getpid(),
        )
        if not self._write(record):
            return None
        self._log(f"opened: {record.describe()}")
        return record

    def mark(self, record: Optional[LeaseRecord],
             state: str) -> Optional[LeaseRecord]:
        """Advance a lease's state (ACQUIRING -> ACTIVE -> RELEASING)."""
        if record is None:
            return None
        updated = LeaseRecord(**{**asdict(record), "state": state,
                                 "updated_at": self._clock()})
        if not self._write(updated):
            return record
        self._log(f"{state}: {updated.describe()}", level="DEBUG")
        return updated

    def close(self) -> bool:
        """Remove the lease. Only ever after a release confirmed in both halves."""
        if not self.path:
            return True
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            return True
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            self._log(
                f"could not remove the lease at {self.path}: {e}. The session "
                f"IS released; the next start will find a lease with nothing "
                f"stranded and discard it",
                level="WARNING")
            return False
        self._log("closed: the session is released and nothing is outstanding")
        return True
