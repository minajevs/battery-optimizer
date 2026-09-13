"""30408 is an energetic TTL, so a long slot must RE-ARM before it expires.

Proved by matched runs on 2026-09-08, sampling 31200/31201 at 5 s:

    30408 = 1 min  ->  battery effect collapsed at t=60 s   (ratio 1.00)
    30408 = 2 min  ->  battery effect collapsed at t=122 s  (ratio 1.02)

The collapse tracks the duration field to within one sample. What it does NOT
do is end the session: 30100 stays 1, 30407 stays 1, 30409 keeps its setpoint,
local battery logic stays suppressed, and the house moves onto the grid. The
earlier code chose the override duration as ``slot_minutes + buffer`` on the
stated reasoning that "if the optimizer misses a refresh, the override expires
and the inverter reverts to its panel-configured base mode". That is exactly
backwards, and it is why this exists.

**The renewal clock is the TTL's, not the poll interval's.** Renewing "every
60 s because scan_interval is 60 s" ties a hardware deadline to an unrelated
setting; if either moves the margin silently changes. Renewal is due at a
conservative fraction of the TTL, so the margin scales with the thing it
protects.

**An unconfirmed renewal is a fault, not a shrug.** If a renewal cannot be
confirmed before the command expires, the honest position is that the inverter
is no longer doing what was asked — so this reports a fault and the caller must
stop commanding and release, rather than carry on believing a command whose
effect has stopped. Failing safe means letting the TTL run out under a session
we then give back, not assuming the write probably landed.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional


class RenewalState(enum.Enum):
    #: Nothing is armed; there is nothing to renew.
    IDLE = "idle"
    #: A command is armed and inside its TTL.
    ARMED = "armed"
    #: A renewal is in flight. Re-entrancy here would race two arms.
    RENEWING = "renewing"
    #: A renewal did not confirm in time. The command's effect has stopped or
    #: is about to, while the session stays armed: a control fault.
    FAULT = "fault"


#: Renew halfway through the TTL. Chosen to leave a whole second half for
#: retries: a renewal that fails at 50 % can be retried and still confirm
#: before the effect stops, which a 90 % trigger could not promise.
DEFAULT_RENEW_FRACTION = 0.5

#: 30408 is validated 1..10 minutes on this firmware, so a slot longer than
#: the TTL is normal and re-arming is the only way to cover it.
DEFAULT_COMMAND_TTL_MINUTES = 5


@dataclass(frozen=True)
class RenewalVerdict:
    state: RenewalState
    detail: str

    @property
    def is_fault(self) -> bool:
        return self.state is RenewalState.FAULT


class CommandRenewal:
    """Tracks one armed command's TTL and says when to re-arm.

    Deliberately free of AppDaemon, the backend and the clock: it takes a
    ``clock`` and is driven by whoever owns the slot, so the same object is
    exercised by tests, by the orchestrator, and by any supervised runner.
    """

    def __init__(self, ttl_minutes: int = DEFAULT_COMMAND_TTL_MINUTES,
                 renew_fraction: float = DEFAULT_RENEW_FRACTION,
                 clock=None, log_func=None):
        if not 1 <= int(ttl_minutes) <= 10:
            raise ValueError(
                f"command TTL {ttl_minutes} is outside the 1..10 minute range "
                f"30408 accepts on this firmware")
        if not 0.1 <= float(renew_fraction) <= 0.9:
            raise ValueError(
                f"renew fraction {renew_fraction} leaves no usable margin; "
                f"0.5 renews halfway through the TTL")
        self.ttl_seconds = float(int(ttl_minutes) * 60)
        self.renew_fraction = float(renew_fraction)
        self._clock = clock
        self._log_func = log_func

        self.state = RenewalState.IDLE
        self.armed_at: Optional[float] = None
        self.renewals = 0
        self.failures = 0
        self.fault_reason: Optional[str] = None
        # Every armed command gets a GENERATION, and every scheduled renewal
        # callback carries the one it was scheduled for. A timer cannot be
        # un-scheduled reliably across a slot change, a release or a restart,
        # so the callback must be able to recognise that the world moved on.
        # Stale-timer resurrection -- an old callback re-arming a command that
        # was already released -- is the failure mode this exists to prevent.
        self.generation = 0

    # --- clock ------------------------------------------------------------

    def _now(self) -> float:
        if self._clock is None:
            import time
            return time.monotonic()
        return self._clock()

    def _log(self, message: str, level: str = "INFO") -> None:
        if self._log_func is not None:
            self._log_func(f"[renewal] {message}", level=level)

    # --- lifecycle --------------------------------------------------------

    def record_armed(self, at: Optional[float] = None) -> int:
        """A command was confirmed armed; its TTL starts now.

        Returns the generation this arming belongs to. Whoever schedules the
        renewal callback must carry that value and hand it back, so a callback
        belonging to a superseded command can be recognised and dropped.
        """
        self.armed_at = self._now() if at is None else at
        self.state = RenewalState.ARMED
        self.fault_reason = None
        self.generation += 1
        return self.generation

    def cancel(self, reason: str = "") -> int:
        """Invalidate any renewal in flight or scheduled. Returns the new generation.

        Called BEFORE any other state transition on a new slot, a disable, a
        shutdown, a lifecycle change, a release or a superseding command. The
        ordering matters more than the bookkeeping: a timer cancelled after the
        state has already moved can still fire against the new state.
        """
        self.generation += 1
        self.state = RenewalState.IDLE
        self.armed_at = None
        self.fault_reason = None
        if reason:
            self._log(f"renewal cancelled: {reason} "
                      f"(generation now {self.generation})")
        return self.generation

    def released(self) -> None:
        """The session was released; there is nothing left to renew."""
        self.state = RenewalState.IDLE
        self.armed_at = None
        self.fault_reason = None
        self.generation += 1

    # --- questions --------------------------------------------------------

    def age(self) -> Optional[float]:
        if self.armed_at is None:
            return None
        return max(0.0, self._now() - self.armed_at)

    def seconds_until_expiry(self) -> Optional[float]:
        age = self.age()
        return None if age is None else self.ttl_seconds - age

    def due(self) -> bool:
        """Is it time to re-arm? False unless a command is actually armed."""
        if self.state is not RenewalState.ARMED:
            return False
        age = self.age()
        return age is not None and age >= self.ttl_seconds * self.renew_fraction

    def expired(self) -> bool:
        """Has the TTL run out? The command's effect has stopped if so."""
        age = self.age()
        return age is not None and age >= self.ttl_seconds

    # --- acting -----------------------------------------------------------

    def renew(self, send, generation: Optional[int] = None) -> RenewalVerdict:
        """Re-arm via ``send()``, which must return True only on a CONFIRMED arm.

        ``send`` is the caller's re-arm — the same command, through the same
        backend, so it goes through the same lock and the same read-back
        verification as the original. Renewal is not a special write path.
        """
        if generation is not None and generation != self.generation:
            return RenewalVerdict(
                self.state,
                f"ignoring a renewal for generation {generation}: the current "
                f"command is generation {self.generation}. This callback "
                f"belongs to a command that has already been superseded or "
                f"released")

        if self.state is RenewalState.RENEWING:
            return RenewalVerdict(
                self.state, "a renewal is already in flight; not starting a "
                            "second one against the same registers")
        if self.state is not RenewalState.ARMED:
            return RenewalVerdict(
                self.state, f"nothing to renew (state={self.state.value})")

        before = self.state
        self.state = RenewalState.RENEWING
        try:
            confirmed = bool(send())
        except BaseException as e:  # noqa: BLE001 - a fault, never a crash
            self.failures += 1
            self.state = RenewalState.FAULT
            self.fault_reason = f"renewal raised {type(e).__name__}: {e}"
            self._log(self.fault_reason, level="CRITICAL")
            return RenewalVerdict(self.state, self.fault_reason)

        if confirmed:
            self.renewals += 1
            self.record_armed()
            detail = (f"re-armed; the command has a fresh "
                      f"{self.ttl_seconds / 60:.0f} min TTL "
                      f"(renewal #{self.renewals})")
            self._log(detail)
            return RenewalVerdict(self.state, detail)

        self.failures += 1
        remaining = self.seconds_until_expiry() or 0.0
        if remaining > 0:
            # Still inside the TTL: the effect has not stopped yet, so the
            # caller may try again. State returns to ARMED so `due()` keeps
            # saying yes.
            self.state = before
            detail = (f"renewal NOT confirmed, {remaining:.0f}s of TTL left; "
                      f"will retry before the command expires")
            self._log(detail, level="WARNING")
            return RenewalVerdict(self.state, detail)

        self.state = RenewalState.FAULT
        self.fault_reason = (
            "renewal did not confirm and the TTL has run out: the energetic "
            "command has stopped while the session is STILL ARMED, which means "
            "local battery logic stays suppressed and the house is on the "
            "grid. Release — do not keep commanding")
        self._log(self.fault_reason, level="CRITICAL")
        return RenewalVerdict(self.state, self.fault_reason)

    def status(self) -> dict:
        """Flat and boring: this is read in a UI and in logs."""
        return {
            "state": self.state.value,
            "ttl_seconds": self.ttl_seconds,
            "renew_fraction": self.renew_fraction,
            "age_seconds": self.age(),
            "seconds_until_expiry": self.seconds_until_expiry(),
            "due": self.due(),
            "expired": self.expired(),
            "renewals": self.renewals,
            "failures": self.failures,
            "fault_reason": self.fault_reason,
            "generation": self.generation,
        }


class RenewalAction(enum.Enum):
    """What a renewal callback should do, having checked the whole world."""

    #: Do nothing, and stop rescheduling. The command this callback belongs to
    #: is gone, or this process is no longer allowed to drive the inverter.
    DROP = "drop"
    #: Not yet due. Nothing to do, keep watching.
    WAIT = "wait"
    #: Re-arm now, through the ordinary send path.
    RENEW = "renew"
    #: The command's TTL has run out without a confirmed renewal. Release.
    RELEASE = "release"


@dataclass(frozen=True)
class RenewalDecision:
    action: RenewalAction
    reason: str

    @property
    def writes(self) -> bool:
        """Will acting on this decision touch the inverter?"""
        return self.action in (RenewalAction.RENEW, RenewalAction.RELEASE)


def assess_renewal(
    *,
    renewal: "CommandRenewal",
    generation: Optional[int],
    lifecycle_ready: bool,
    automatic_writes_allowed: bool,
    session_active: bool,
    scheduled_command=None,
    active_command=None,
) -> RenewalDecision:
    """Decide what a fired renewal timer may do. Writes nothing itself.

    Every condition is a reason to DROP, never a reason to try harder. A
    renewal callback runs minutes after it was scheduled, and in between the
    slot may have changed, the app may have been disabled, the lifecycle may
    have left READY, the mode may have been reduced, or the session may have
    been released — by a person, by the reaper, or by this app's own fault
    handling. A callback that "tries to be helpful" in any of those cases
    re-arms an inverter nobody is expecting to be armed.

    The generation check is the load-bearing one, because it is the only one
    that catches a superseded command whose replacement looks identical.
    """
    if generation is not None and generation != renewal.generation:
        return RenewalDecision(
            RenewalAction.DROP,
            f"stale callback: scheduled for generation {generation}, current "
            f"is {renewal.generation}")

    if renewal.state is RenewalState.FAULT:
        return RenewalDecision(
            RenewalAction.DROP,
            f"the renewal already faulted ({renewal.fault_reason}); the "
            f"session is being released and must not be re-armed")

    if renewal.state is RenewalState.IDLE:
        return RenewalDecision(RenewalAction.DROP,
                               "nothing is armed; there is no command to renew")

    if not lifecycle_ready:
        return RenewalDecision(
            RenewalAction.DROP,
            "the optimizer is no longer READY, so it may not command the "
            "inverter — startup recovery or a fault owns it now")

    if not automatic_writes_allowed:
        return RenewalDecision(
            RenewalAction.DROP,
            "automatic writes are not allowed in this mode; a renewal is a "
            "write like any other")

    if not session_active:
        return RenewalDecision(
            RenewalAction.DROP,
            "the session is no longer active — released, reaped or taken over "
            "— so there is nothing of ours to renew")

    if (scheduled_command is not None and active_command is not None
            and scheduled_command != active_command):
        return RenewalDecision(
            RenewalAction.DROP,
            "the active command changed since this renewal was scheduled; the "
            "new one has a renewal of its own")

    if renewal.expired():
        return RenewalDecision(
            RenewalAction.RELEASE,
            "the command TTL ran out without a confirmed renewal: the battery "
            "has stopped doing what was asked while the session is still "
            "armed. Release rather than command something the inverter is no "
            "longer doing")

    if renewal.due():
        return RenewalDecision(RenewalAction.RENEW,
                               "the command is past its renewal point")

    return RenewalDecision(RenewalAction.WAIT, "not yet due")
