"""Clean up a session the previous instance left armed, BEFORE claiming to be alive.

`session_reaper.py` has always documented that "the durable lease and startup
recovery clean it up". The lease existed, `RECOVERABLE_LEASE` existed, and
`recover()` existed — but nothing ever called it at startup, so the sentence
described an intention rather than a behaviour.

**Order is the whole design here.** The old heartbeat is the only evidence that
the PREVIOUS owner is gone, and stamping a fresh one destroys it. The startup
sequence must therefore be:

    construct the heartbeat        (do NOT stamp)
    reconcile the inverter and the lease
    if RECOVERABLE_LEASE: recover, judging liveness by the OLD heartbeat
    only then stamp, and only then allow scheduled control

Stamp first and the failure is quiet and permanent: the app comes up, writes a
fresh heartbeat, and the reaper — correctly — reads OWNER_ALIVE and refuses
forever. The inverter stays armed, the optimizer sits there unable to arm over
`RECOVERABLE_LEASE`, and nothing in the system is wrong enough to complain.

Recovery is the SAME fenced primitive the reaper uses (proved on hardware
2026-09-10), never a second implementation of it. The only difference is which
heartbeat is offered as evidence.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional

from .upstream_vpp import SessionState


class OptimizerLifecycle(enum.Enum):
    """Whether scheduled control may run yet, and why not when it may not."""

    #: Nothing was left behind, or what was left behind has been released.
    READY = "ready"
    #: A recoverable lease was found and the release did not complete. The app
    #: is alive and will retry; it must not command the inverter meanwhile.
    RECOVERY_FAILED = "recovery_failed"
    #: A recoverable lease was found but the previous owner CANNOT be shown to
    #: be gone — its heartbeat is still fresh, which at startup means another
    #: instance is running. Releasing would tear down a live session.
    RECOVERY_BLOCKED = "recovery_blocked"
    #: Authority is held with no lease accounting for it. Not ours to release
    #: and not ours to build on.
    FOREIGN_AUTHORITY = "foreign_authority"


@dataclass(frozen=True)
class StartupRecovery:
    lifecycle: OptimizerLifecycle
    detail: str
    recovered: bool = False

    @property
    def ready(self) -> bool:
        """May scheduled control command the inverter?"""
        return self.lifecycle is OptimizerLifecycle.READY

    @property
    def should_retry(self) -> bool:
        """Is this a state the app can get itself out of by trying again?"""
        return self.lifecycle is OptimizerLifecycle.RECOVERY_FAILED


def recover_previous_session(
    backend,
    session,
    heartbeat,
    wait,
    stale_after_seconds: float = 90.0,
    log=None,
) -> StartupRecovery:
    """Reconcile, and release anything a previous instance left armed.

    MUST be called before the heartbeat is stamped: ``heartbeat`` is read here
    as evidence about the PREVIOUS owner, and a fresh stamp would make that
    owner look alive.
    """
    def say(message, level="INFO"):
        if log is not None:
            log(f"[startup] {message}", level=level)

    backend.reconcile()
    state = backend.session_state

    if state is SessionState.AUTHORITY_HELD_NOT_OURS:
        detail = ("30100=1 with no lease accounting for it. This is not ours "
                  "to release and not ours to build on, so scheduled control "
                  "stays off until someone establishes what set it")
        say(detail, level="CRITICAL")
        return StartupRecovery(OptimizerLifecycle.FOREIGN_AUTHORITY, detail)

    if state is not SessionState.RECOVERABLE_LEASE:
        detail = f"nothing to recover (session_state={state.value})"
        say(detail)
        return StartupRecovery(OptimizerLifecycle.READY, detail)

    # A previous instance left a session armed. Whether it is safe to release
    # rests entirely on the OLD heartbeat, which is why nothing has stamped yet.
    age = heartbeat.age()
    if age is not None and age <= stale_after_seconds:
        detail = (
            f"a recoverable lease is present but the previous owner's "
            f"heartbeat is only {age:.0f}s old (stale after "
            f"{stale_after_seconds:.0f}s), so it cannot be shown to be gone. "
            f"At startup that means ANOTHER INSTANCE is probably running. "
            f"Not releasing — that would tear down a live session — and not "
            f"commanding the inverter either")
        say(detail, level="CRITICAL")
        return StartupRecovery(OptimizerLifecycle.RECOVERY_BLOCKED, detail)

    say(f"a previous instance left a session armed and its heartbeat is "
        f"{'never stamped' if age is None else f'{age:.0f}s old'}. Releasing "
        f"it before this app does anything else — it will NOT be resumed.",
        level="CRITICAL")

    result = session.recover(wait=wait, heartbeat=heartbeat,
                             stale_after_seconds=stale_after_seconds)

    if result.ok and backend.session_state is SessionState.RELEASED:
        detail = f"previous session released at startup: {result.detail}"
        say(detail)
        return StartupRecovery(OptimizerLifecycle.READY, detail, recovered=True)

    detail = (f"startup recovery did NOT complete: {result.detail}. The "
              f"inverter may still be armed. Scheduled control stays off and "
              f"this will be retried")
    say(detail, level="CRITICAL")
    return StartupRecovery(OptimizerLifecycle.RECOVERY_FAILED, detail)
