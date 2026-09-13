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

    def record_armed(self, at: Optional[float] = None) -> None:
        """A command was confirmed armed; its TTL starts now."""
        self.armed_at = self._now() if at is None else at
        self.state = RenewalState.ARMED
        self.fault_reason = None

    def released(self) -> None:
        """The session was released; there is nothing left to renew."""
        self.state = RenewalState.IDLE
        self.armed_at = None
        self.fault_reason = None

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

    def renew(self, send) -> RenewalVerdict:
        """Re-arm via ``send()``, which must return True only on a CONFIRMED arm.

        ``send`` is the caller's re-arm — the same command, through the same
        backend, so it goes through the same lock and the same read-back
        verification as the original. Renewal is not a special write path.
        """
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
        }
