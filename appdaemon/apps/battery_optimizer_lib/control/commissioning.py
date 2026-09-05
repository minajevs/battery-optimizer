"""The only surface from which a commissioning write can be started.

Nothing in this module is invoked by the optimizer, the scheduler, or any
timer. Every operation is called deliberately, by a person, one at a time —
that is what "supervised" means here, and it is the reason the first commands
this project ever sends to an inverter are the least energetic ones.

**``session_test()`` is the operation to run first.** It performs the whole
lifecycle — ``hold()`` -> a timed renewal experiment -> ``release()`` — against
one backend, one session and one process, and stays alive until the release is
confirmed in both halves.

That grouping is not a convenience. Ownership of a VPP session is
process-local by design: it comes from this process's own successful writes
(``OWN_AUTHORITY_STATES``) and is never reconstructed from register values,
because a register cannot say who wrote it. A one-operation-per-invocation CLI
therefore *cannot* renew or release a session an earlier invocation opened —
the later process would find 30100=1 it did not set and refuse, correctly.
Persisting ownership to disk would fix that by trusting a file over the
hardware, and adopting 30100 on the strength of it is exactly the inference
this module exists to refuse. So the lifecycle runs in one process instead.

    1. ``hold()``     — open a timed VPP session at +1 %
    2. ``renew()``    — prove the watchdog can be re-armed
    3. ``release()``  — give the inverter back to its own logic

That is the minimal VPP path — 30408, 30409, 30100, 30407 and nothing else —
and it is what the first hardware run must establish on its own. None of the
three touches 30476. ``hold()`` and ``renew()`` are confirmed by reading
30100/30407/30409 back: a write that did not raise is not an armed session.

``hold`` and ``renew`` are NOT offered as standalone CLI operations, for the
same reason: an operation that can only open a session the next process
cannot close has no safe use. They remain here because ``session_test`` is
built from them.

**``watchdog_test()`` is the second experiment**, and it asks the opposite
question: left alone, does a session end by itself? It holds for the shortest
window the inverter accepts, never renews, and polls the control block and the
measured power until well past the expiry. The whole safety argument for a
bounded session — "if this process dies, the override expires on its own" —
rests on a watchdog that nothing had yet observed firing. It is a separate
operation rather than a flag on ``session_test`` because that one re-arms on
purpose, and an option that quietly suppressed the renewal would leave one
function whose meaning depends on an argument.

Three rules govern the cleanup, and they are the reason this is safe to run
against real hardware:

* **It always runs.** Whatever leaves the experiment — a failure, an
  exception, a KeyboardInterrupt — leaves it through the same ``finally``.
* **It is persistent.** A release refused because the inverter was momentarily
  unreadable schedules no retry timer, so waiting would wait forever; the
  cleanup re-initiates instead of waiting on a timer that does not exist.
* **A timeout reports, it does not abandon.** Exceeding the budget marks the
  test failed and says so at CRITICAL, but cleanup continues while
  ``safe_to_stop`` is False, because nothing but this process can make it
  True. Only an explicit operator force-abort (a second interrupt) stops it.

The watchdog window is validated before anything is written: 30408=0 is not
"no timeout" but a session nothing would ever end, and the accepted range is
``MIN_COMMISSIONING_MINUTES``..``MAX_COMMISSIONING_MINUTES``.

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
from .upstream_vpp import HOLD_POWER_PERCENT, SessionState, UNFINISHED_STATES

# Short by design. A commissioning HOLD should expire on its own well inside
# the operator's attention span, so a forgotten session self-heals via the
# inverter's watchdog rather than persisting.
DEFAULT_COMMISSIONING_MINUTES = 5

# 30408 is the watchdog window, and it is the only thing that returns the
# inverter to its base mode if this process dies mid-session. Both ends are
# enforced, not advisory:
#
#   0 is NOT "no timeout" -- it is a session with no watchdog at all, exactly
#     the state a supervised operation must never be able to create.
#   10 minutes is the upper bound because a commissioning session should
#     expire well inside the operator's attention span.
MIN_COMMISSIONING_MINUTES = 1
MAX_COMMISSIONING_MINUTES = 10

# The renewal experiment must re-arm AFTER the per-register write cooldown has
# expired, or it measures the cooldown instead of the watchdog.
DEFAULT_RENEW_AFTER_SECONDS = 35

# The watchdog test asks the OPPOSITE question from the renewal experiment:
# left alone, does the session end by itself? So it holds for the shortest
# window the inverter accepts and then does nothing at all, and the observation
# has to outlast the window or it cannot see the expiry it is looking for.
DEFAULT_WATCHDOG_MINUTES = MIN_COMMISSIONING_MINUTES
DEFAULT_WATCHDOG_OBSERVE_SECONDS = 90
DEFAULT_WATCHDOG_POLL_SECONDS = 5

# How long the session test will keep waiting for a release to reach RELEASED.
# The lifecycle is two confirmed halves plus a settle, so this is minutes, not
# seconds: 30100=0 can be rate-limited for 30 s, then the 30407=0 cleanup is
# scheduled release_settle_seconds later and read back after that.
DEFAULT_RELEASE_TIMEOUT_SECONDS = 240


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
        self.observations: List[str] = []

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

    # --- input validation -------------------------------------------------

    def _validate_duration(
        self, operation: str, duration_minutes
    ) -> Optional[CommissioningResult]:
        """Refuse an unusable watchdog window before touching the inverter.

        Returns a refusal, or None when the value is fit to write. Checked
        here rather than at the CLI because the library is what actually
        writes 30408, and a caller that skipped the CLI would otherwise skip
        the check with it.
        """
        if isinstance(duration_minutes, bool) or not isinstance(
                duration_minutes, int):
            return self._refuse(
                operation,
                f"duration must be a whole number of minutes, got "
                f"{duration_minutes!r}")

        if duration_minutes < MIN_COMMISSIONING_MINUTES:
            return self._refuse(
                operation,
                f"duration {duration_minutes} min would write 30408="
                f"{duration_minutes}. That is not 'no timeout', it is a "
                f"session with NO WATCHDOG: nothing would return the inverter "
                f"to its base mode if this process died holding it. Minimum "
                f"is {MIN_COMMISSIONING_MINUTES} min")

        if duration_minutes > MAX_COMMISSIONING_MINUTES:
            return self._refuse(
                operation,
                f"duration {duration_minutes} min exceeds the commissioning "
                f"maximum of {MAX_COMMISSIONING_MINUTES} min. A supervised "
                f"session must expire inside the operator's attention span")

        return None

    def _validate_renewal_timing(
        self, operation: str, duration_minutes: int, renew_after_seconds
    ) -> Optional[CommissioningResult]:
        """The renewal must land after the cooldown and inside the window."""
        cooldown = int(getattr(self.backend.cooldown, "cooldown_seconds", 30))

        if not isinstance(renew_after_seconds, int) or isinstance(
                renew_after_seconds, bool):
            return self._refuse(
                operation,
                f"renewal delay must be a whole number of seconds, got "
                f"{renew_after_seconds!r}")

        if renew_after_seconds <= cooldown:
            return self._refuse(
                operation,
                f"renewal delay {renew_after_seconds}s is inside the {cooldown}s "
                f"per-register write cooldown, so the re-arm would be refused "
                f"by the cooldown and the experiment would measure that "
                f"instead of the watchdog")

        window = duration_minutes * 60
        if renew_after_seconds >= window:
            return self._refuse(
                operation,
                f"renewal delay {renew_after_seconds}s is not inside the "
                f"{window}s session it is meant to renew: the override would "
                f"already have expired")

        return None

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
        invalid = self._validate_duration(operation, duration_minutes)
        if invalid is not None:
            return invalid
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
        invalid = self._validate_duration(operation, duration_minutes)
        if invalid is not None:
            return invalid
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
            # A write that did not raise is not an armed session. Read
            # 30100/30407/30409 back and let the registers say whether the
            # command actually took, before this is reported as an open
            # session the operator can act on.
            verified = self.backend.verify(command, self.backend.read_state())

            if verified.unverifiable:
                self._degrade(
                    f"{operation} was sent but the inverter could not be read "
                    f"back, so there is no evidence the session is armed. A "
                    f"session that cannot be seen is not supervised")
                return self._record(CommissioningResult(
                    operation=operation, ok=False, state=state,
                    detail="sent, but read-back failed: 30100/30407/30409 "
                           "unknown. Release before doing anything else"))

            if not verified.matched:
                self._degrade(
                    f"{operation} was accepted but read back wrong "
                    f"({verified.detail}); the inverter is not in the state "
                    f"this operation asked for")
                return self._record(CommissioningResult(
                    operation=operation, ok=False, state=state,
                    detail=f"read-back MISMATCH: {verified.detail} "
                           f"(actual {verified.actual})"))

            return self._record(CommissioningResult(
                operation=operation, ok=True, state=state,
                detail=f"session armed for {duration_minutes} min at "
                       f"+{HOLD_POWER_PERCENT}% — confirmed by read-back of "
                       f"30100/30407/30409 ({verified.actual}); "
                       f"session_state={self.backend.session_state.value}"))

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

        if session is SessionState.RELEASE_SETTLING:
            return self._refuse(
                operation,
                "a release is already settling: 30100=0 is confirmed and the "
                "30407=0 cleanup is scheduled. Keep this process alive until "
                "the session reads RELEASED")

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

    # --- the long-lived supervised session test ---------------------------

    def _observe(self, label: str,
                 with_power: bool = False) -> Optional[InverterState]:
        """Read and record the four session registers, for the operator.

        ``with_power`` appends the measured effect — battery power (normalized,
        + = charging), the two directional grid readings and SOC. Registers say
        what the inverter was TOLD; only these say what it did, which is the
        second question the watchdog test answers for free while it waits.
        """
        state = self.backend.read_state()
        if state is None:
            line = f"{label}: inverter UNREADABLE"
        else:
            line = (f"{label}: 30100={state.control_authority} "
                    f"30407={state.remote_enabled} "
                    f"30408={state.duration_minutes} "
                    f"30409={state.commanded_power}")
            if with_power:
                line = f"{line} | {self._describe_power(state)}"
        self.observations.append(line)
        self._log(line)
        return state

    @staticmethod
    def _describe_power(state: InverterState) -> str:
        """Measured effect, with unreadable sensors named rather than zeroed."""
        def number(value, unit, sign=False):
            if value is None:
                return "?"
            return f"{value:+.0f}{unit}" if sign else f"{value:.0f}{unit}"

        return (f"bat={number(state.battery_power_w, 'W', sign=True)} "
                f"grid={number(state.grid_import_power_w, '')}/"
                f"{number(state.grid_export_power_w, 'W')} "
                f"soc={number(state.soc_percent, '%')}")

    @staticmethod
    def _renewal_verdict(opened, elapsed, renewed, duration_minutes) -> str:
        """What the 30408 readings do and do not establish.

        The experiment only means something if 30408 counts DOWN while a
        session runs. On an inverter that simply echoes what was written, the
        three readings are identical and prove nothing — which is reported as
        inconclusive rather than dressed up as a pass.
        """
        readings = [s.duration_minutes if s is not None else None
                    for s in (opened, elapsed, renewed)]
        if any(r is None for r in readings):
            return (f"renewal INCONCLUSIVE: 30408 could not be read "
                    f"throughout ({readings})")

        after_open, after_wait, after_renew = readings
        if after_wait == after_open:
            return (f"renewal INCONCLUSIVE: 30408 did not count down "
                    f"({after_open} -> {after_wait}), so it echoes the last "
                    f"written value rather than the remaining window. This "
                    f"run cannot say whether re-arming renews the watchdog")
        if after_renew > after_wait:
            return (f"renewal WORKS: 30408 counted down {after_open} -> "
                    f"{after_wait} and the re-arm put it back to "
                    f"{after_renew}")
        return (f"renewal DID NOT TAKE: 30408 went {after_open} -> "
                f"{after_wait} and the re-arm left it at {after_renew}, "
                f"not {duration_minutes}")

    def session_test(
        self,
        wait,
        duration_minutes: int = DEFAULT_COMMISSIONING_MINUTES,
        renew_after_seconds: int = DEFAULT_RENEW_AFTER_SECONDS,
        release_timeout_seconds: int = DEFAULT_RELEASE_TIMEOUT_SECONDS,
        poll_seconds: int = 5,
    ) -> CommissioningResult:
        """HOLD -> timed renewal experiment -> RELEASE, in ONE process.

        Ownership of a VPP session is deliberately process-local: it comes
        from this process's own successful writes and is never reconstructed
        from register values, so a one-operation-per-invocation CLI can open a
        session it is then structurally unable to renew or release. Rather
        than persist ownership (which would mean trusting a file over the
        hardware, and adopting 30100 on the strength of it), the whole
        lifecycle runs here against one backend, one session and one process.

        ``wait(seconds)`` is supplied by the caller and must both let real
        time pass AND run any scheduled callbacks that fall due — the release
        lifecycle finishes on timers this process owns. Nothing in this module
        sleeps on its own.

        The inverter is handed back on every path out of here, including the
        failing ones. If this process dies mid-test the watchdog expiry
        returns the inverter to its base mode on its own, which is why the
        hold is minutes long.
        """
        operation = "session_test"
        self.observations = []

        invalid = self._validate_duration(operation, duration_minutes)
        if invalid is not None:
            return invalid
        invalid = self._validate_renewal_timing(
            operation, duration_minutes, renew_after_seconds)
        if invalid is not None:
            return invalid

        opened = self.hold(duration_minutes=duration_minutes)
        if opened.refused:
            # Nothing was transmitted, so there is nothing to hand back.
            return self._record(CommissioningResult(
                operation=operation, ok=False, refused=True,
                detail=f"refused before opening a session: {opened.detail}"))

        if not opened.ok:
            return self._release_and_report(
                operation, wait, release_timeout_seconds, poll_seconds,
                f"HOLD did not confirm ({opened.detail}); handing the "
                f"inverter back without attempting the renewal",
                steps_ok=False)

        # From here a session is OPEN, so every path out of this block ends in
        # the same cleanup -- including the ones nobody planned for. A
        # KeyboardInterrupt during the experiment is an operator changing
        # their mind, and it must hand the inverter back rather than abandon
        # an armed session at the shell prompt.
        summary = "no renewal verdict"
        steps_ok = False
        try:
            after_open = self._observe("after HOLD")

            wait(renew_after_seconds)
            after_wait = self._observe(
                f"after {renew_after_seconds}s of session")

            renewed = self.renew(duration_minutes=duration_minutes)
            after_renew = self._observe("after RENEW")

            summary = self._renewal_verdict(
                after_open, after_wait, after_renew, duration_minutes)
            if not renewed.ok:
                summary = f"renew operation did not confirm ({renewed.detail})"
            steps_ok = renewed.ok
            self.observations.append(summary)
            self._log(summary)
        except BaseException as exc:   # noqa: BLE001 - KeyboardInterrupt too
            summary = (f"ABORTED by {type(exc).__name__}: {exc}. A session was "
                       f"open, so it is being handed back before this returns")
            steps_ok = False
            self._log(summary, level="ERROR")
        finally:
            result = self._release_and_report(
                operation, wait, release_timeout_seconds, poll_seconds,
                summary, steps_ok=steps_ok)

        return result

    # --- the watchdog test ------------------------------------------------

    def _validate_observation_window(
        self, operation: str, duration_minutes: int, observe_seconds
    ) -> Optional[CommissioningResult]:
        """Refuse an observation that stops before the expiry it is watching for."""
        if not isinstance(observe_seconds, int) or isinstance(
                observe_seconds, bool):
            return self._refuse(
                operation,
                f"observation window must be a whole number of seconds, got "
                f"{observe_seconds!r}")

        window = duration_minutes * 60
        if observe_seconds <= window:
            return self._refuse(
                operation,
                f"observing for {observe_seconds}s cannot see a {window}s "
                f"session expire: the test would end while the session is "
                f"still legitimately armed and prove nothing either way")

        return None

    @staticmethod
    def _watchdog_verdict(samples, window_seconds: int) -> str:
        """What the polled 30407 readings establish about the watchdog.

        Only the DISARM matters. 30408 was already shown to echo the last
        written value rather than count down, so the question is no longer what
        the register says but whether the inverter ends the session on its own.
        """
        readable = [(t, s) for t, s in samples if s is not None]
        if not readable:
            return ("watchdog INCONCLUSIVE: the inverter was unreadable for "
                    "the whole observation")

        armed = [(t, s) for t, s in readable if s.remote_enabled == 1]
        if not armed:
            return ("watchdog INCONCLUSIVE: 30407 never read 1 after the hold, "
                    "so no armed session was observed to expire")

        dropped = next((t for t, s in readable if t > armed[0][0]
                        and s.remote_enabled == 0), None)
        last_t = readable[-1][0]

        if dropped is None:
            return (f"watchdog DID NOT FIRE: 30407 was still 1 at t={last_t}s "
                    f"with a {window_seconds}s window. The session does NOT "
                    f"self-expire, so a process that dies holding one leaves "
                    f"the inverter armed until something clears it by hand — "
                    f"treat the watchdog as unproven and keep every session "
                    f"attended")

        state_at_drop = next(s for t, s in readable if t == dropped)
        authority = ("and 30100 dropped with it"
                     if state_at_drop.control_authority == 0
                     else f"while 30100 stayed at "
                          f"{state_at_drop.control_authority}")
        overshoot = dropped - window_seconds
        return (f"watchdog CONFIRMED: 30407 cleared itself between "
                f"t={dropped - DEFAULT_WATCHDOG_POLL_SECONDS}s and t={dropped}s "
                f"({overshoot:+d}s against the {window_seconds}s window), "
                f"{authority}")

    def watchdog_test(
        self,
        wait,
        duration_minutes: int = DEFAULT_WATCHDOG_MINUTES,
        observe_seconds: int = DEFAULT_WATCHDOG_OBSERVE_SECONDS,
        poll_seconds: int = DEFAULT_WATCHDOG_POLL_SECONDS,
        release_timeout_seconds: int = DEFAULT_RELEASE_TIMEOUT_SECONDS,
    ) -> CommissioningResult:
        """HOLD for the shortest window -> DO NOT renew -> watch it expire.

        The session test proved the lifecycle works while a process drives it.
        This asks what happens when nothing does: the whole safety argument for
        a bounded session — "if this process dies, the override expires by
        itself" — rests on a watchdog nothing has yet observed firing.

        Deliberately NOT a mode of ``session_test``. That operation re-arms on
        purpose; this one must not, and an option that silently suppressed the
        renewal would make the two experiments one function whose meaning
        depends on a flag.

        A release is still issued at the end, whatever the registers say. If
        the watchdog fired, it costs one refused write and confirms 0/0 by
        read-back; if it did not, that release is the only thing that ends the
        session — which is exactly the case this test exists to find.
        """
        operation = "watchdog_test"
        self.observations = []

        invalid = self._validate_duration(operation, duration_minutes)
        if invalid is not None:
            return invalid
        invalid = self._validate_observation_window(
            operation, duration_minutes, observe_seconds)
        if invalid is not None:
            return invalid

        opened = self.hold(duration_minutes=duration_minutes)
        if opened.refused:
            return self._record(CommissioningResult(
                operation=operation, ok=False, refused=True,
                detail=f"refused before opening a session: {opened.detail}"))

        if not opened.ok:
            return self._release_and_report(
                operation, wait, release_timeout_seconds, poll_seconds,
                f"HOLD did not confirm ({opened.detail}); nothing was left "
                f"running to observe",
                steps_ok=False)

        window_seconds = duration_minutes * 60
        summary = "no watchdog verdict"
        steps_ok = False
        try:
            samples = [(0, self._observe("t=0s", with_power=True))]
            elapsed = 0
            while elapsed < observe_seconds:
                step = min(poll_seconds, observe_seconds - elapsed)
                wait(step)
                elapsed += step
                samples.append(
                    (elapsed, self._observe(f"t={elapsed}s", with_power=True)))

            summary = self._watchdog_verdict(samples, window_seconds)
            # The verdict is about the inverter, not about the run: an
            # observation that completed has done its job even when what it
            # observed is that the watchdog never fired.
            steps_ok = True
            self.observations.append(summary)
            self._log(summary,
                      level="INFO" if "CONFIRMED" in summary else "WARNING")
        except BaseException as exc:   # noqa: BLE001 - KeyboardInterrupt too
            summary = (f"ABORTED by {type(exc).__name__}: {exc}. A session was "
                       f"open, so it is being handed back before this returns")
            steps_ok = False
            self._log(summary, level="ERROR")
        finally:
            result = self._release_and_report(
                operation, wait, release_timeout_seconds, poll_seconds,
                summary, steps_ok=steps_ok)

        # A watchdog that fired has already handed the inverter back, so the
        # release finds nothing of this process's to revoke and the session
        # ends at NOT_ARMED rather than RELEASED. For every other operation
        # that is a failure worth reporting; here it is precisely the result
        # being looked for -- but only once the registers say 0/0, because
        # "nothing to revoke" must never be inferred from the absence of a
        # release rather than from the state of the inverter.
        if not result.ok and "watchdog CONFIRMED" in summary:
            final = self.backend.read_state()
            if (final is not None and final.control_authority == 0
                    and final.remote_enabled == 0):
                result = self._record(CommissioningResult(
                    operation=operation, ok=True, state=final,
                    detail=f"{summary}; the inverter had already returned "
                           f"itself to 0/0, so the release found nothing of "
                           f"this process's left to revoke — confirmed by "
                           f"read-back"))

        return result

    def _release_and_report(
        self, operation: str, wait, timeout_seconds: int, poll_seconds: int,
        summary: str, steps_ok: bool = True,
    ) -> CommissioningResult:
        """Hand the inverter back and wait out the whole release lifecycle.

        RELEASED needs both halves confirmed by read-back — 30100=0 and then
        the delayed 30407=0 — so this keeps the process alive through the
        settle rather than exiting on the first confirmation.

        ``steps_ok`` carries the verdict of what came before: a clean handover
        does not redeem a hold that never armed, so a successful release is
        reported as a successful RELEASE, not a successful test.
        """
        elapsed = 0
        timed_out = False
        forced = False
        last = self.release()

        # The loop is governed by safe_to_stop, NOT by the timeout. Exceeding
        # the timeout is a REPORTING event: it makes the test a failure and
        # says so loudly, and cleanup carries on regardless, because the only
        # thing that can finish a handover is this process. The operator's
        # escape is an explicit force-abort (a second interrupt), never a
        # timer deciding on its own to walk away from an armed inverter.
        while not self.backend.session_state.safe_to_stop:
            session = self.backend.session_state

            if session not in UNFINISHED_STATES:
                # Nothing is in flight: the last attempt was REFUSED, so no
                # retry timer exists and waiting would wait forever. The
                # commonest cause is a momentarily unreadable inverter, which
                # makes the release preflight refuse to act blind -- transient,
                # and answered by asking again rather than by giving up.
                self._log(
                    f"release has not started (session_state={session.value}: "
                    f"{last.detail}); re-initiating rather than waiting on a "
                    f"timer that was never scheduled",
                    level="WARNING",
                )
                last = self.release()

            try:
                wait(poll_seconds)
            except KeyboardInterrupt:
                forced = True
                self._log(
                    "FORCE-ABORT during cleanup. The inverter may still hold "
                    "an armed session that only this process could close — "
                    "check 30100/30407 by hand NOW "
                    "(scripts/commission.py --operation state).",
                    level="CRITICAL",
                )
                break

            elapsed += poll_seconds
            if elapsed >= timeout_seconds and not timed_out:
                timed_out = True
                self._log(
                    f"release has not confirmed within {timeout_seconds}s. "
                    f"The TEST is a failure from here, but cleanup continues: "
                    f"safe_to_stop is False and nothing but this process can "
                    f"make it True. Interrupt again to force-abort.",
                    level="CRITICAL",
                )

        self._observe("after RELEASE")
        final = self.backend.session_state

        if forced:
            return self._record(CommissioningResult(
                operation=operation, ok=False,
                detail=f"{summary}; cleanup FORCE-ABORTED by the operator at "
                       f"session_state={final.value} "
                       f"(safe_to_stop={final.safe_to_stop}). Verify "
                       f"30100/30407 by hand"))

        if final is SessionState.RELEASED:
            detail = (f"{summary}; released and confirmed "
                      f"(30100=0 and 30407=0 both read back)")
            if timed_out:
                detail = (f"{detail} — but only after the {timeout_seconds}s "
                          f"reporting timeout had passed")
            return self._record(CommissioningResult(
                operation=operation, ok=steps_ok and not timed_out,
                detail=detail))

        # safe_to_stop without RELEASED: nothing of ours is outstanding (the
        # authority turned out not to be ours to revoke, or was never taken).
        return self._record(CommissioningResult(
            operation=operation, ok=False,
            detail=f"{summary}; ended at session_state={final.value} rather "
                   f"than released ({last.detail}). Nothing of this process's "
                   f"is left outstanding, but verify the inverter by hand"))

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
