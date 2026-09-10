"""Durable evidence that a process started a session and may not have finished it.

No expiry ends a SESSION. 30408 bounds the energetic command -- proved by
matched runs on 2026-09-08, where the effect collapsed at 60s and 122s for
30408 of 1 and 2 minutes -- but a session armed with 30408=1 was still armed at
t=90s (2026-09-05), and only an explicit release ended it. So the failure this
file exists for is real and unbounded, and it is worse than a stuck command: a
process that dies holding 30100=1 / 30407=1 leaves the inverter with its local
battery logic suppressed and the house drawing from the grid, indefinitely,
after the command itself has stopped doing anything.

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

import errno
import fcntl
import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from typing import Optional

# Lease lifecycle. ACQUIRING is written before the first control write and is
# just as recoverable as ACTIVE -- more so, if anything: it covers the arm that
# was half-applied, which is the state with authority held and nothing armed.
ACQUIRING = "acquiring"
ACTIVE = "active"
RELEASING = "releasing"
# A reaper has FENCED this lease for recovery. While a lease is in this state
# no new session may be opened against it: the point is not to notice that a
# new owner appeared, it is to make one impossible while a release is in
# flight. See claim_for_recovery().
RECOVERING = "recovering"

# How long a recovery claim is honoured. A reaper that dies mid-recovery must
# not lock the optimizer out forever, so the claim expires -- generously,
# because a real recovery waits out the inverter's 30 s cooldown and then a
# settle, and breaking a live claim is far worse than waiting.
CLAIM_TTL_SECONDS = 300.0


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
    # When the previous instance last wrote 30100, as a wall-clock timestamp.
    # A recovering process has no memory of the write cooldowns another
    # process stamped, so without this it revokes authority into a refusal it
    # could have predicted -- observed on hardware as ten rejected writes
    # before the 30 s window cleared.
    authority_written_at: Optional[float] = None
    # When a reaper fenced this lease for recovery, and WHICH recovery attempt
    # did it. The claim id is not the session id: session_id names the control
    # session, so an unclaim carrying only that could remove a LATER attempt's
    # valid fence — claim A expires after CLAIM_TTL_SECONDS, claim B fences the
    # same session, then a resumed process A unclaims B's fence. Only meaningful
    # while state == RECOVERING.
    claimed_at: Optional[float] = None
    recovery_claim_id: Optional[str] = None
    # What to restore on unclaim. A fence is a temporary state, not a
    # destination, so undoing it must put back what was actually there rather
    # than assuming ACTIVE.
    previous_state: Optional[str] = None

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

    @contextmanager
    def _exclusive(self):
        """Hold an inter-process lock across a read-modify-write of the lease.

        os.replace() makes each WRITE atomic, which is not the same thing: the
        dangerous sequence here is read-decide-write, and two processes can
        interleave inside it. The reaper deciding "this lease is still L1" and
        the optimizer deciding "there is no lease, I may open L2" are exactly
        that pair, and the window between them is the ~30 s cooldown a recovery
        must wait out.

        The lock lives in a sidecar file, never the lease itself, because the
        lease is replaced by rename and a lock held on the old inode would
        protect nothing. If locking is unavailable the operation still runs --
        an unlocked lease is what this project had before, so degrading to it
        is not a new hazard -- but it says so.
        """
        if not self.path:
            yield False
            return
        lock_path = f"{self.path}.lock"
        handle = None
        try:
            directory = os.path.dirname(os.path.abspath(lock_path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            handle = open(lock_path, "a+")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield True
        except OSError as e:
            if handle is not None and e.errno not in (errno.EACCES, errno.EAGAIN):
                self._log(f"lease lock unavailable ({e}); proceeding UNLOCKED",
                          level="WARNING")
            yield False
        finally:
            if handle is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                handle.close()

    def _claim_is_live(self, record: Optional[LeaseRecord]) -> bool:
        """Is this lease fenced for a recovery that is still plausibly running?"""
        if record is None or record.state != RECOVERING:
            return False
        if record.claimed_at is None:
            return True
        return (self._clock() - record.claimed_at) < CLAIM_TTL_SECONDS

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
                authority_written_at=data.get("authority_written_at"),
                claimed_at=data.get("claimed_at"),
                recovery_claim_id=data.get("recovery_claim_id"),
                previous_state=data.get("previous_state"),
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
        """Record the intent to take authority. Called BEFORE the first write.

        REFUSES while the existing lease is fenced for recovery. That refusal
        is the whole point of the fence: a reaper that has decided to release
        session L1 spends the inverter's 30 s cooldown getting there, and if a
        restarted optimizer could open L2 inside that window the reaper's
        release would land on a live session. Detecting the new owner
        afterwards is not enough -- by then both have acted.

        Returns None when refused, and the caller MUST NOT arm on None. An
        enabled lease that could not be opened means there is no durable record
        of what is about to be done, which is the exact failure this file
        exists to prevent.
        """
        with self._exclusive():
            existing = self.read()
            if self._claim_is_live(existing):
                age = ("unknown" if existing.claimed_at is None
                       else f"{self._clock() - existing.claimed_at:.0f}s")
                self.last_error = "fenced for recovery"
                self._log(
                    f"REFUSED to open a lease: session {existing.session_id} "
                    f"is fenced for recovery (claimed {age} ago). Something is "
                    f"releasing it right now, and arming on top would give it "
                    f"a live session to tear down. Not arming.",
                    level="CRITICAL")
                return None
            if existing is not None and existing.state == RECOVERING:
                self._log(
                    f"a recovery claim on session {existing.session_id} has "
                    f"expired (older than {CLAIM_TTL_SECONDS:.0f}s); treating "
                    f"the lease as abandoned and opening a new one. Whatever "
                    f"claimed it did not finish — check for a stranded "
                    f"session.", level="ERROR")
            return self._open_locked(device_id, action, setpoint_percent,
                                     duration_minutes)

    def claim_for_recovery(self, session_id: str) -> Optional[LeaseRecord]:
        """Fence a lease for recovery, but ONLY if it is still the one decided on.

        A compare-and-swap: the claim lands only when the lease on disk still
        carries ``session_id``. That makes the id a fencing token rather than
        one more precondition — after this returns a record, no new session can
        be opened until the claim is released or expires, so the "is it still
        the same dead session?" question cannot change its answer underneath
        the release.

        Returns None when the lease is gone, belongs to a different session, or
        is already claimed by someone else.
        """
        with self._exclusive():
            existing = self.read()
            if existing is None:
                self._log(f"cannot claim {session_id}: there is no lease",
                          level="WARNING")
                return None
            if existing.session_id != session_id:
                self._log(
                    f"REFUSING to claim {session_id}: the lease now carries "
                    f"{existing.session_id}. A different session exists, so "
                    f"the one that was assessed is gone and nothing here is "
                    f"ours to release.", level="ERROR")
                return None
            if self._claim_is_live(existing) :
                self._log(
                    f"session {session_id} is already fenced for recovery; "
                    f"leaving it to whoever claimed it", level="WARNING")
                return None

            fields = {**asdict(existing), "state": RECOVERING,
                      "claimed_at": self._clock(),
                      "recovery_claim_id": uuid.uuid4().hex,
                      "previous_state": existing.state,
                      "updated_at": self._clock()}
            claimed = LeaseRecord(**fields)
            if not self._write(claimed):
                return None
            self._log(f"FENCED for recovery: {claimed.describe()} "
                      f"(claim {claimed.recovery_claim_id[:8]}). No new "
                      f"session may be opened until this is released.")
            return claimed

    def unclaim(self, session_id: str, recovery_claim_id: str) -> bool:
        """Undo a fence THIS recovery attempt took, restoring the prior state.

        The counterpart to claim_for_recovery, and CAS in the same way: it
        restores ``previous_state`` only when the lease still carries this
        session AND is still RECOVERING AND still carries this claim id. Any
        mismatch means the fence being asked about is not the one this attempt
        took, so it does nothing and says why.

        Called on every abort path. Without it a refused recovery leaves the
        lease fenced for the full CLAIM_TTL_SECONDS, and the worst case is
        perverse: the abort reason "the owner came back" means the optimizer is
        alive and now blocked from arming by the very fence that was protecting
        it. A loud abort must not latch for five minutes.
        """
        with self._exclusive():
            existing = self.read()
            if existing is None:
                self._log(f"nothing to unclaim: the lease is gone "
                          f"(session {session_id})", level="DEBUG")
                return False
            if existing.session_id != session_id:
                self._log(
                    f"NOT unclaiming: the lease carries "
                    f"{existing.session_id}, not {session_id}. A different "
                    f"session holds it now", level="WARNING")
                return False
            if existing.state != RECOVERING:
                self._log(f"nothing to unclaim: the lease reads "
                          f"{existing.state}, not {RECOVERING}", level="DEBUG")
                return False
            if existing.recovery_claim_id != recovery_claim_id:
                self._log(
                    f"NOT unclaiming: the fence belongs to recovery attempt "
                    f"{existing.recovery_claim_id}, not {recovery_claim_id}. "
                    f"Removing it would drop somebody else's valid fence",
                    level="ERROR")
                return False

            restored = existing.previous_state or ACTIVE
            fields = {**asdict(existing), "state": restored,
                      "claimed_at": None, "recovery_claim_id": None,
                      "previous_state": None,
                      "updated_at": self._clock()}
            if not self._write(LeaseRecord(**fields)):
                return False
            self._log(f"fence released: session {session_id} is back to "
                      f"{restored} and may be armed again")
            return True

    def verify_claim(self, session_id: str) -> bool:
        """Is this still our claim, immediately before we act on it?

        Defence in depth behind the fence: ``open()`` already refuses while the
        claim is live, so this should never fail in practice. It exists because
        the write it guards is 30100=0 on hardware, and "should never" is not
        the standard that write is held to.
        """
        with self._exclusive():
            existing = self.read()
            if existing is None:
                self._log(f"claim on {session_id} is GONE: the lease no longer "
                          f"exists", level="ERROR")
                return False
            if existing.session_id != session_id:
                self._log(f"claim on {session_id} is STALE: the lease now "
                          f"carries {existing.session_id}", level="ERROR")
                return False
            if existing.state != RECOVERING:
                self._log(f"claim on {session_id} was released: the lease "
                          f"reads {existing.state}", level="ERROR")
                return False
            return True

    def _open_locked(self, device_id: str, action: str,
                     setpoint_percent: Optional[int],
                     duration_minutes: Optional[int]) -> Optional[LeaseRecord]:
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

    def mark(self, record: Optional[LeaseRecord], state: str,
             authority_written_at: Optional[float] = None
             ) -> Optional[LeaseRecord]:
        """Advance a lease's state (ACQUIRING -> ACTIVE -> RELEASING)."""
        if record is None:
            return None
        fields = {**asdict(record), "state": state, "updated_at": self._clock()}
        if authority_written_at is not None:
            fields["authority_written_at"] = authority_written_at
        updated = LeaseRecord(**fields)
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
