"""The only surface from which a commissioning write can be started.

Nothing in this module is invoked by the optimizer, the scheduler, or any
timer. Every operation is called deliberately, by a person, one at a time —
that is what "supervised" means here, and it is the reason the first commands
this project ever sends to an inverter are the least energetic ones.

**Run them in this order:**

    1. ``hold()``     — open a timed VPP session at +1 %
    2. ``renew()``    — prove the watchdog can be re-armed
    3. ``release()``  — give the inverter back to its own logic

That is the minimal VPP path — 30408, 30409, 30100, 30407 and nothing else —
and it is what the first hardware run must establish on its own. None of the
three touches 30476.

``probe_priority_mode()`` is deliberately NOT part of it. It is a supervised
capability probe, and its result licenses nothing by itself: knowing that
30476 can be written is not a reason to write it. 30476 is a storage register
that changes the inverter's base mode, so it is spent only where there is a
hypothesis to test — the later grid-charge experiment, where Battery First is
one candidate explanation for a VPP charge that does not import. Run the probe
when that experiment is what is being worked on, not as a warm-up. Until
hardware evidence shows some other action needs it, ``build_plan`` writes
30476=1 for GRID_CHARGE alone, and no commissioning operation writes it at all.

None of these operations moves meaningful energy. Selling, buying and
discharging are not reachable from here at all: ``UpstreamVppBackend.send``
refuses them in commissioning mode, ``build_plan`` raises on them, and the
executor's register allowlist refuses the writes that would implement them.
Three independent layers, because this is the first code in the project that
can change a real inverter.

**Every operation re-reads and reconciles the inverter first.** A stale model
of the hardware is how a supervised session turns into an unsupervised one.
Preflight refuses whenever another scheduler's TOU schedule is loaded
(30411 > 0), the authority is not ours, our own arm was left half-applied, a
release is pending, the session is degraded, or the read came back incomplete.
``release()`` is gated by none of that: giving the inverter back must never be
the thing an interlock blocks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from .actions import ControlAction
from .backend import InverterCommand, InverterState, SendResult
from .upstream_vpp import HOLD_POWER_PERCENT, SessionState

# Short by design. A commissioning HOLD should expire on its own well inside
# the operator's attention span, so a forgotten session self-heals via the
# inverter's watchdog rather than persisting.
DEFAULT_COMMISSIONING_MINUTES = 5


@dataclass(frozen=True)
class CommissioningResult:
    """What one supervised operation did, and whether it may be followed."""

    operation: str
    ok: bool
    detail: str = ""
    refused: bool = False        # True when nothing was transmitted at all
    state: Optional[InverterState] = None

    def describe(self) -> str:
        verdict = "OK" if self.ok else ("REFUSED" if self.refused else "FAILED")
        return f"{self.operation}: {verdict} — {self.detail}"


class CommissioningSession:
    """Supervised commissioning operations against one backend.

    Refuses to act — transmitting nothing — whenever the inverter is not in a
    state this process fully understands and owns.
    """

    def __init__(self, backend, log_func=None):
        self.backend = backend
        self._log_func = log_func
        self.degraded = False
        self.degraded_reason: Optional[str] = None
        self.history: List[CommissioningResult] = []

    # --- logging ----------------------------------------------------------

    def _log(self, message: str, level: str = "INFO") -> None:
        if self._log_func is not None:
            self._log_func(f"[commissioning] {message}", level=level)
        else:
            self.backend._log(f"[commissioning] {message}", level=level)

    def _record(self, result: CommissioningResult) -> CommissioningResult:
        self.history.append(result)
        self._log(result.describe(),
                  level="INFO" if result.ok else "ERROR")
        return result

    def _refuse(self, operation: str, reason: str) -> CommissioningResult:
        return self._record(CommissioningResult(
            operation=operation, ok=False, refused=True, detail=reason))

    def _degrade(self, reason: str) -> None:
        """Latch: something left the inverter in a state we cannot reason about."""
        self.degraded = True
        self.degraded_reason = reason
        self._log(
            f"COMMISSIONING DEGRADED — {reason}. No further operations except "
            f"release() will be attempted until this is investigated and "
            f"clear_degraded() is called.",
            level="CRITICAL",
        )

    def clear_degraded(self) -> None:
        if not self.degraded:
            return
        self.degraded = False
        self.degraded_reason = None
        self._log("degraded state cleared; operations re-enabled")

    # --- the interlock ----------------------------------------------------

    def _preflight(
        self, operation: str, require_active: bool = False
    ) -> Tuple[Optional[InverterState], Optional[CommissioningResult]]:
        """Read the REAL inverter and decide whether acting is defensible.

        Returns ``(state, None)`` to proceed or ``(None, refusal)`` to stop.
        Refusal transmits nothing.
        """
        if not self.backend.commissioning:
            return None, self._refuse(
                operation,
                "backend is not in commissioning mode "
                f"(executor: {getattr(self.backend.executor, 'name', '?')})")

        if self.degraded:
            return None, self._refuse(
                operation, f"commissioning is degraded: {self.degraded_reason}")

        # Not cached, not assumed: re-read every time.
        state = self.backend.reconcile()
        if state is None:
            return None, self._refuse(
                operation, "inverter state is unreadable; refusing to act blind")

        missing = [name for name, value in (
            ("30100 control_authority", state.control_authority),
            ("30407 remote_power_enable", state.remote_enabled),
            ("30409 commanded_power", state.commanded_power),
            ("30411 tou_period_count", state.tou_period_count),
            ("30476 priority_mode", state.priority_mode),
        ) if value is None]
        if missing:
            return None, self._refuse(
                operation,
                f"incomplete read of the control block ({', '.join(missing)}); "
                f"a partial picture is not a basis for a write")

        session = self.backend.session_state

        if session is SessionState.ARM_FAILED_AUTHORITY_HELD:
            return None, self._refuse(
                operation,
                "this process took control authority and could not arm it, so "
                "its own command is half-applied. Release deliberately before "
                "starting anything else")

        # Positive evidence of a second scheduler, and the only such evidence
        # this project has. Deliberately checked BEFORE the 30100 interlock
        # below: on the reference unit Growatt Smart Scheduling holds authority
        # AND loads a schedule, so both would refuse -- but only this one can
        # say what to switch off. 30100=1 names no culprit.
        if state.external_scheduler_present:
            return None, self._refuse(
                operation,
                f"EXTERNAL SCHEDULER: 30411 reports "
                f"{state.tou_period_count} TOU period(s). Nothing in this "
                f"project writes a TOU period, in any mode, so that schedule "
                f"was authored by something else -- on the reference "
                f"installation, Growatt Smart Scheduling. Two schedulers "
                f"driving one inverter is not a supervised state. Turn it off "
                f"(30411 and all 60 period registers go to 0) and re-run")

        if session is SessionState.AUTHORITY_HELD_NOT_OURS:
            return None, self._refuse(
                operation,
                "HARD INTERLOCK: 30100=1 without this process having taken it. "
                "Authority we cannot account for is never armed on top of — "
                "establish what set it before commissioning")

        if session is SessionState.RELEASE_PENDING:
            return None, self._refuse(
                operation,
                "a release is still pending: authority is not yet confirmed "
                "revoked, and its scheduled retry is still running")

        if require_active:
            if session is not SessionState.ACTIVE:
                return None, self._refuse(
                    operation,
                    f"no session opened by this process to act on "
                    f"(session_state={session.value})")
        elif state.control_authority != 0 or state.remote_enabled != 0:
            return None, self._refuse(
                operation,
                f"inverter is not in the 0/0 passthrough state "
                f"(30100={state.control_authority}, 30407={state.remote_enabled})")

        return state, None

    # --- the four operations ---------------------------------------------

    def probe_priority_mode(self) -> CommissioningResult:
        """Find out whether 30476 is genuinely writable, then put it back."""
        operation = "priority_mode_probe"
        state, refusal = self._preflight(operation)
        if refusal is not None:
            return refusal

        capability, detail = self.backend.probe_priority_mode(state)

        restored = "RESTORE FAILED" not in detail and "unconfirmed" not in detail
        if not restored:
            self._degrade(f"30476 was not restored after the probe: {detail}")

        return self._record(CommissioningResult(
            operation=operation,
            ok=restored,
            detail=f"{capability.value}; {detail}",
            state=state,
        ))

    def hold(
        self, duration_minutes: int = DEFAULT_COMMISSIONING_MINUTES
    ) -> CommissioningResult:
        """Open a timed VPP session at +1 %: the least energetic real command.

        Proves the whole arming sequence — 30408, then 30409, then authority,
        then 30407 LAST — against real hardware, while asking the battery to do
        essentially nothing.
        """
        operation = "timed_hold"
        state, refusal = self._preflight(operation)
        if refusal is not None:
            return refusal
        return self._send(operation, state, duration_minutes)

    def renew(
        self, duration_minutes: int = DEFAULT_COMMISSIONING_MINUTES
    ) -> CommissioningResult:
        """Re-arm the watchdog on the session THIS process opened.

        Nothing upstream documents that rewriting 30408 alone renews a timed
        session, which is why the normal path re-arms every slot. This is the
        operation that finds out whether that is true.
        """
        operation = "timer_renewal"
        state, refusal = self._preflight(operation, require_active=True)
        if refusal is not None:
            return refusal
        return self._send(operation, state, duration_minutes)

    def _send(
        self, operation: str, state: InverterState, duration_minutes: int
    ) -> CommissioningResult:
        command = InverterCommand(
            action=ControlAction.HOLD,
            power_percent=HOLD_POWER_PERCENT,
            duration_minutes=int(duration_minutes),
            reason=f"commissioning: {operation}",
        )
        result = self.backend.send(command)

        if result is SendResult.CONFIRMED:
            return self._record(CommissioningResult(
                operation=operation, ok=True, state=state,
                detail=f"session armed for {duration_minutes} min at "
                       f"+{HOLD_POWER_PERCENT}% "
                       f"(session_state={self.backend.session_state.value})"))

        if result is SendResult.RATE_LIMITED:
            # Deferred, not failed: a control register is still in its cooldown.
            # No latch — the state machine is behaving exactly as designed.
            return self._record(CommissioningResult(
                operation=operation, ok=False, refused=True, state=state,
                detail="deferred: a control register is still in its 30 s "
                       "write cooldown. Nothing was applied; retry shortly"))

        self._degrade(
            f"{operation} returned {result.value}; the inverter may be in a "
            f"partially applied state (session_state="
            f"{self.backend.session_state.value})")
        return self._record(CommissioningResult(
            operation=operation, ok=False, state=state,
            detail=f"send returned {result.value}"))

    def release(self) -> CommissioningResult:
        """Give the inverter back to its own local logic.

        Deliberately NOT gated on ``self.degraded``: releasing is the way out
        of a bad state, so a latch must never be able to trap a session open.
        """
        operation = "release"

        if not self.backend.commissioning:
            return self._refuse(operation, "backend is not in commissioning mode")

        state = self.backend.reconcile()
        if state is None:
            return self._refuse(
                operation, "inverter state is unreadable; refusing to act blind")

        session = self.backend.session_state

        if session is SessionState.AUTHORITY_HELD_NOT_OURS:
            return self._refuse(
                operation,
                "HARD INTERLOCK: 30100=1 was not set by this process, so it is "
                "not ours to revoke. Clear it from wherever it came from")

        if session is SessionState.RELEASE_PENDING:
            return self._refuse(
                operation,
                "a release is already pending; its scheduled retry is running "
                "and will complete on its own")

        result = self.backend.release()

        if result is SendResult.CONFIRMED:
            return self._record(CommissioningResult(
                operation=operation, ok=True, state=state,
                detail="authority revoked and confirmed by read-back; "
                       "disarm scheduled after settle"))

        if result is SendResult.PENDING:
            # The documented, expected path: our own successful 30100=1 stamped
            # the cooldown that now blocks 30100=0. The retry is scheduled.
            return self._record(CommissioningResult(
                operation=operation, ok=True, state=state,
                detail="release in progress — revoking authority is rate-"
                       "limited; a retry is scheduled. Do NOT stop the process "
                       "until the session reads RELEASED"))

        self._degrade(f"release returned {result.value}")
        return self._record(CommissioningResult(
            operation=operation, ok=False, state=state,
            detail=f"release returned {result.value}"))

    # --- reporting --------------------------------------------------------

    def get_diagnostics(self) -> dict:
        return {
            "commissioning_degraded": self.degraded,
            "commissioning_degraded_reason": self.degraded_reason,
            "commissioning_operations": len(self.history),
            "commissioning_last": (
                self.history[-1].describe() if self.history else None
            ),
        }
