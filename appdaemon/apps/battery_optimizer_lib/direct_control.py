"""Inverter control policy — deduplication, verification, outcome accounting.

This module deliberately knows NOTHING about Modbus registers or Home Assistant
service names.  All of that lives behind a :class:`ControlBackend`
(``control/backend.py``), so swapping the underlying Growatt integration is a
constructor argument rather than a rewrite of the policy that survived several
production incidents.

What stays here, unchanged in behaviour:

* the five-way :class:`ApplyOutcome` accounting that separates "the inverter
  acknowledged" from "nothing was transmitted" from "we never found out";
* duplicate suppression within half a slot;
* the BOUNDED two-step verify-after-set ladder (max 2 checks, 2 sends);
* the diagnostics counters that make inverter-control health observable.
"""

from __future__ import annotations

import datetime
import enum
from typing import Optional

from .control import (
    DEFAULT_COMMAND_TTL_MINUTES,
    ControlAction,
    EffectVerdict,
    InverterCommand,
    SendResult,
    UpstreamVppBackend,
    resolve_ac_charge_mode,
    resolve_action,
)
from .models import BatteryMode, ScheduleEntry

# Default delay before verifying a sent command against inverter read-back.
# The coordinator refreshes on each poll (~30-60s), so 90s gives at least one
# poll cycle after the write settles. Overridable via config.verify_delay_seconds:
# a lagging read and a lost command look identical at a fixed delay.
VERIFY_DELAY_SECONDS = 90

# Default delay for the SINGLE re-check performed after a resend. Shorter than
# the first check — the resend goes out immediately, only one poll cycle is
# needed to see it.
VERIFY_RECHECK_SECONDS = 60

# Per-command timeout budget passed to the backend.
COMMAND_TIMEOUT_SECONDS = 15


def effect_family(action: ControlAction) -> str:
    """Which EFFECT failures accumulate together.

    A grid-charge that does nothing and a discharge that does nothing are two
    different faults with two different causes. Sharing one counter would latch
    control degraded after one of each — blaming a mechanism that neither of
    them showed to be broken, and hiding which one actually is. The three
    discharge actions DO share a family: they are the same forced-discharge
    mechanism differing only in where the energy is allowed to go.
    """
    if action.is_discharge:
        return "discharge"
    return action.value


class ApplyOutcome(enum.Enum):
    """What actually happened to one ``apply_mode`` command.

    The boolean returned by ``apply_mode`` cannot separate these, and three of
    them are True: a dry run, a duplicate that was never transmitted, and a
    client-side timeout nobody confirmed. Treating all three as "the inverter
    obeyed" is what let a hung integration publish climbing apply_successes
    while the "inverter is NOT following the schedule" escalation could never
    fire.

    ``RATE_LIMITED`` means the command was NOT applied because a per-register
    cooldown refused it. It is neither success nor evidence of ill health — it
    is deferred, and a retry is scheduled.
    """

    SENT = "sent"                        # confirmed by read-back or response
    UNCONFIRMED_TIMEOUT = "unconfirmed"  # client-side timeout, outcome unknown
    SKIPPED_DUPLICATE = "duplicate"      # identical command, nothing transmitted
    DRY_RUN = "dry_run"                  # nothing transmitted
    RATE_LIMITED = "rate_limited"        # refused by cooldown; retry scheduled
    FAILED = "failed"                    # confirmed failure

    @property
    def confirmed(self) -> bool:
        """True only when the inverter actually acknowledged the command."""
        return self is ApplyOutcome.SENT

    @property
    def applied(self) -> bool:
        """False whenever the inverter is definitely NOT running this command."""
        return self not in (ApplyOutcome.FAILED, ApplyOutcome.RATE_LIMITED)


class DirectControl:
    """Applies schedule entries to the inverter through a ControlBackend."""

    def __init__(self, app, config, backend=None):
        """
        Args:
            app: AppDaemon app instance (for get_state, log, run_in, cancel_timer)
            config: BatteryOptimizerConfig instance
            backend: ControlBackend implementation. Defaults to
                :class:`UpstreamVppBackend`, which is dry-run unless given a
                live executor.
        """
        self.app = app
        self.config = config
        self.backend = backend if backend is not None else UpstreamVppBackend(app, config)

        self._last_action_sent: Optional[str] = None
        self._last_mode_time: Optional[datetime.datetime] = None
        self._last_command: Optional[InverterCommand] = None

        # Handle of the pending one-shot verification timer (from run_in), or
        # None. Superseded whenever a new mode is applied.
        self._verify_timer = None
        # Handle of a pending retry after a rate-limited command.
        self._retry_timer = None
        # Whether the "cannot verify" condition has been logged at WARNING yet.
        self._verify_unreadable_warned = False

        # Timing (configurable — a lagging read must be compensable from
        # apps.yaml, not by editing this module).
        self._verify_delay = int(
            getattr(config, "verify_delay_seconds", VERIFY_DELAY_SECONDS)
        )
        self._verify_recheck_delay = int(
            getattr(config, "verify_recheck_seconds", VERIFY_RECHECK_SECONDS)
        )
        self._command_timeout = int(
            getattr(config, "command_timeout_seconds", COMMAND_TIMEOUT_SECONDS)
        )

        # Diagnostics counters. They exist to separate "the read lags"
        # (mismatch_count high, resend_recovered_count high, persistent 0) from
        # "the inverter really drops the override" (persistent grows).
        self._mismatch_count = 0
        self._resend_count = 0
        self._resend_recovered_count = 0
        self._resend_failed_count = 0
        self._persistent_mismatch_count = 0
        self._unverifiable_count = 0
        self._verified_count = 0
        self._rate_limited_count = 0
        self._release_pending_count = 0
        self._last_mismatch: Optional[dict] = None
        self._last_effect: Optional[str] = None

        # EFFECT failure handling. An ACKed command with no effect is the one
        # failure mode registers cannot show, so it gets its own escalation:
        # release and stop, never rewrite the inverter's TOU schedule.
        self._effect_failure_count = 0
        # Streaks are per action family, never global — see effect_family().
        self._consecutive_effect_failures: dict = {}
        self._control_degraded = False
        self._degraded_reason: Optional[str] = None
        self._degraded_family: Optional[str] = None
        # Commissioning notice is loud once, then quiet: it fires every slot.
        self._commissioning_notice_logged = False
        self._effect_failure_limit = int(
            getattr(config, "effect_failure_limit", 2)
        )

        self.last_apply_outcome: Optional[ApplyOutcome] = None
        self._apply_outcome_counts: dict = {}

    @property
    def device_id(self) -> str:
        return self.config.device_id

    def _duration_for_slot(self) -> int:
        """The command's TTL — NOT the slot length.

        This used to return ``slot_minutes + buffer``, on the reasoning that
        "if the optimizer misses a refresh, the override expires and the
        inverter reverts to its panel-configured base mode". Hardware says
        otherwise. Matched runs on 2026-09-08 (31200/31201 sampled at 5 s)
        showed 30408 bounding the ENERGETIC COMMAND to within one sample —
        1 min collapsed at 60 s, 2 min at 122 s — while leaving 30100=1,
        30407=1 and the setpoint exactly where they were. Expiry does not
        revert anything: it stops the battery doing what was asked and leaves
        local logic suppressed, so the house moves onto the grid.

        So expiry is the hazard, not the fallback, and a slot longer than the
        TTL is covered by RE-ARMING (``control/renewal.py``) rather than by
        asking for a duration long enough to span it. 30408 is validated
        1..10 minutes on this firmware, and a 15 minute slot could not be
        spanned anyway.
        """
        ttl = int(getattr(self.config, "command_ttl_minutes",
                          DEFAULT_COMMAND_TTL_MINUTES))
        return max(1, min(10, ttl))

    # --- command construction --------------------------------------------

    def build_command(self, entry: ScheduleEntry) -> InverterCommand:
        """Translate one ScheduleEntry into a backend-agnostic command."""
        action = resolve_action(entry, self.config.default_power_percent)

        charge_cutoff = None
        discharge_cutoff = None
        if entry.mode == BatteryMode.CHARGE:
            charge_cutoff = self._get_max_soc()
        if entry.mode == BatteryMode.DISCHARGE:
            discharge_cutoff = self._get_min_soc()

        return InverterCommand(
            action=action,
            power_percent=self.config.default_power_percent,
            duration_minutes=self._duration_for_slot(),
            export_rate=entry.export_rate,
            ac_charge_mode=resolve_ac_charge_mode(
                entry, self._get_pv_power(), self._get_pv_threshold()
            ),
            charge_cutoff_soc=charge_cutoff,
            discharge_cutoff_soc=discharge_cutoff,
            reason=entry.reason,
        )

    # --- applying ---------------------------------------------------------

    def apply_mode(self, entry: ScheduleEntry) -> bool:
        """Send a mode command to the inverter.

        Returns:
            False when the inverter is definitely NOT running the command — a
            confirmed failure or a rate-limited (deferred) one. Callers that
            need to distinguish "acknowledged" from "nothing was transmitted"
            must use ``apply_mode_with_outcome``.
        """
        return self.apply_mode_with_outcome(entry).applied

    def apply_mode_with_outcome(self, entry: ScheduleEntry) -> ApplyOutcome:
        """Send a mode command and report what actually happened."""
        if not self.device_id:
            self.app.log(
                f"DirectControl: dry-run {entry.mode.name} ({entry.reason})"
            )
            return self._record_outcome(ApplyOutcome.DRY_RUN)

        if not self._automatic_writes_allowed():
            level = "WARNING" if not self._commissioning_notice_logged else "DEBUG"
            self._commissioning_notice_logged = True
            self.app.log(
                f"DirectControl: COMMISSIONING mode — the optimizer does not "
                f"drive the inverter. {entry.mode.name} was planned but NOT "
                f"transmitted. Use CommissioningSession for supervised "
                f"operations.",
                level=level,
            )
            return self._record_outcome(ApplyOutcome.DRY_RUN)

        if self._control_degraded:
            self.app.log(
                f"DirectControl: control is DEGRADED ({self._degraded_reason}) "
                f"— not sending {entry.mode.name}. The inverter is running its "
                f"own local logic. This needs investigation, then "
                f"clear_degraded() to resume.",
                level="ERROR",
            )
            return self._record_outcome(ApplyOutcome.FAILED)

        command = self.build_command(entry)

        # Supervised first-live-run gate. Deliberately a HARD RESTRICTION and
        # not a preference: the first deployment that can write unattended must
        # not be able to turn whatever the optimizer currently considers
        # optimal into grid charge or MAX_EXPORT. With this set, anything that
        # does not resolve to HOLD refuses and says so loudly, so the first
        # optimizer-owned session proves the LIFECYCLE — open, own, renew,
        # release — rather than a power decision.
        #
        # Remove it (config: live_test_hold_only) only once that lifecycle has
        # been proved on hardware.
        if (self.config.live_test_hold_only
                and command.action is not ControlAction.HOLD):
            self.app.log(
                f"DirectControl: REFUSING {command.action.value} — "
                f"live_test_hold_only is set, so this supervised deployment "
                f"may only transmit HOLD. The schedule wanted "
                f"{entry.mode.name}; nothing was sent. Clear the flag when the "
                f"session lifecycle has been proved on hardware.",
                level="ERROR",
            )
            # DRY_RUN because nothing was attempted: health accounting treats
            # it as neutral, which is right — a refusal is not an inverter
            # failure and must not count toward the degraded streak.
            return self._record_outcome(ApplyOutcome.DRY_RUN)

        if self._is_duplicate(command):
            self.app.log(
                f"DirectControl: skipping duplicate {command.action.value} "
                f"(last sent {self._seconds_since_last():.0f}s ago)",
                level="DEBUG",
            )
            return self._record_outcome(ApplyOutcome.SKIPPED_DUPLICATE)

        return self._dispatch(command)

    def _automatic_writes_allowed(self) -> bool:
        """False in commissioning mode, where writes are supervised only.

        Deliberately NOT ``not backend.dry_run``: commissioning CAN write, so
        that question has the wrong answer here — it would let the scheduler
        start trading the moment commissioning was switched on. Backends that
        do not publish the property (test doubles) are treated as permitted, so
        this gate can only ever tighten behaviour, never loosen it.
        """
        return bool(getattr(self.backend, "automatic_writes_allowed", True))

    def _dispatch(self, command: InverterCommand) -> ApplyOutcome:
        """Send one already-built command and account for the result."""
        # Supersede any verification pending from a previous send BEFORE we
        # send. Otherwise a confirmed failure here would return without
        # cancelling, and the stale timer could later resend an older command.
        self._cancel_verification()

        self.app.log(f"DirectControl: {command.describe()}")

        result = self._send(command)

        if result is SendResult.FAILED:
            # Confirmed failure: do NOT record last-sent, so duplicate
            # suppression can't mask an immediate resend on the next slot.
            self.app.log(
                f"DirectControl: backend reported failure for "
                f"{command.action.value}; not recording last-sent so a resend "
                f"can correct it",
                level="ERROR",
            )
            return self._record_outcome(ApplyOutcome.FAILED)

        if result is SendResult.RATE_LIMITED:
            # The command was NOT applied. Not a health signal — but it must
            # not be silently dropped either, so a retry is scheduled.
            self._rate_limited_count += 1
            self.app.log(
                f"DirectControl: {command.action.value} deferred — a control "
                f"register is still in its write cooldown. Retrying shortly.",
                level="WARNING",
            )
            self._schedule_retry(command)
            return self._record_outcome(ApplyOutcome.RATE_LIMITED)

        if result is SendResult.DRY_RUN:
            return self._record_outcome(ApplyOutcome.DRY_RUN)

        if result is SendResult.PENDING:
            # Only the release path produces this today; treat it defensively
            # as "not applied" rather than silently as a success.
            return self._record_outcome(ApplyOutcome.RATE_LIMITED)

        # CONFIRMED or UNCONFIRMED: record the last-sent marker so the schedule
        # isn't spammed with resends; verify-after-set catches genuine losses.
        self._last_action_sent = command.action.value
        self._last_mode_time = datetime.datetime.now()
        self._last_command = command

        if result is SendResult.UNCONFIRMED:
            self.app.log(
                f"DirectControl: {command.action.value} unconfirmed "
                f"(client-side timeout); will verify against inverter read-back",
                level="WARNING",
            )

        self._schedule_verification(command)
        return self._record_outcome(
            ApplyOutcome.SENT if result is SendResult.CONFIRMED
            else ApplyOutcome.UNCONFIRMED_TIMEOUT
        )

    def _send(self, command: InverterCommand) -> SendResult:
        """Call the backend, converting an unexpected raise into FAILED."""
        try:
            return self.backend.send(command)
        except Exception as e:
            self.app.log(f"DirectControl: backend send failed: {e}", level="ERROR")
            return SendResult.FAILED

    def _record_outcome(self, outcome: ApplyOutcome) -> ApplyOutcome:
        """Store and count one apply outcome, then return it unchanged."""
        self.last_apply_outcome = outcome
        self._apply_outcome_counts[outcome] = (
            self._apply_outcome_counts.get(outcome, 0) + 1
        )
        return outcome

    def release_control(self) -> bool:
        """Release all overrides — inverter reverts to local control."""
        if not self.device_id:
            self.app.log("DirectControl: dry-run release_control (passthrough)")
            return True

        # Cancel any pending verification before sending, so a failed release
        # can't leave a stale timer that resends an older mode later.
        self._cancel_verification()

        try:
            result = self.backend.release()
        except Exception as e:
            self.app.log(f"DirectControl: release failed: {e}", level="ERROR")
            return False

        if result is SendResult.FAILED:
            self.app.log("DirectControl: release failed", level="ERROR")
            return False

        if result is SendResult.RATE_LIMITED:
            self._rate_limited_count += 1
            self.app.log(
                "DirectControl: release deferred by write cooldown", level="WARNING"
            )
            return False

        if result is SendResult.PENDING:
            # The release was ACCEPTED and is completing on a scheduled retry —
            # revoking authority is commonly blocked by the cooldown our own
            # acquisition stamped. This is not a failure, but it is also not
            # done: the handover is only complete once read-back confirms
            # 30100=0, which the health sensor reports as RELEASED.
            self._release_pending_count += 1
            self.app.log(
                "DirectControl: release in progress — authority revoke is "
                "waiting on the write cooldown. The inverter is NOT released "
                "yet; wait for control_status RELEASED before stopping or "
                "reloading AppDaemon.",
                level="WARNING",
            )
            return True

        command = InverterCommand(action=ControlAction.PASSTHROUGH)
        self._last_action_sent = ControlAction.PASSTHROUGH.value
        self._last_mode_time = datetime.datetime.now()
        self._last_command = command

        if result is SendResult.UNCONFIRMED:
            self.app.log(
                "DirectControl: passthrough unconfirmed (client-side timeout); "
                "will verify against inverter read-back",
                level="WARNING",
            )
        elif result is SendResult.CONFIRMED:
            self.app.log("DirectControl: released all overrides (passthrough)")

        if result is not SendResult.DRY_RUN:
            self._schedule_verification(command)
        return True

    # --- retry after a deferred (rate-limited) command --------------------

    def _schedule_retry(self, command: InverterCommand) -> None:
        """Retry a rate-limited command once the cooldown can have expired."""
        self._cancel_retry()
        delay = int(getattr(self.config, "wit_cooldown_seconds", 30)) + 2
        try:
            self._retry_timer = self.app.run_in(
                self._retry_command, delay, command=command
            )
        except Exception as e:
            self.app.log(
                f"DirectControl: could not schedule retry: {e}", level="ERROR"
            )
            self._retry_timer = None

    def _cancel_retry(self) -> None:
        if self._retry_timer is not None:
            try:
                self.app.cancel_timer(self._retry_timer)
            except Exception:
                pass
            self._retry_timer = None

    def _retry_command(self, kwargs=None) -> None:
        """AppDaemon scheduler callback: re-send a deferred command."""
        self._retry_timer = None
        kwargs = kwargs or {}
        command = kwargs.get("command")
        if command is None:
            return
        self.app.log(
            f"DirectControl: retrying deferred {command.action.value} "
            f"after write cooldown"
        )
        self._dispatch(command)

    # --- verification -----------------------------------------------------

    def _cancel_verification(self) -> None:
        """Cancel any pending verification timer."""
        if self._verify_timer is not None:
            try:
                self.app.cancel_timer(self._verify_timer)
            except Exception:
                pass
            self._verify_timer = None

    def _schedule_verification(
        self, command: InverterCommand, attempt: int = 1
    ) -> None:
        """Schedule a one-shot verification after a command was sent.

        Supersedes any previously pending verification, so a mode applied
        between send and verify cancels the stale check.

        Args:
            attempt: 1 for the check after the original send, 2 for the single
                re-check after a resend. Capped at 2 in _verify_mode, so this
                can never become a resend loop.
        """
        self._cancel_verification()
        delay = self._verify_delay if attempt <= 1 else self._verify_recheck_delay
        try:
            self._verify_timer = self.app.run_in(
                self._verify_mode, delay, command=command, attempt=attempt
            )
        except Exception as e:
            self.app.log(
                f"DirectControl: could not schedule verification: {e}",
                level="ERROR",
            )
            self._verify_timer = None

    def _verify_mode(self, kwargs=None) -> None:
        """Verify the inverter reached the last-sent command; resend once if not.

        Attempt ladder (bounded — never a loop):
          attempt 1: mismatch -> WARNING, resend once, schedule attempt 2
          attempt 2: match    -> INFO "recovered after resend"
                     mismatch -> ERROR, NO further resend, NO further timer

        The second check is what makes the diagnostics meaningful: without it we
        never learn whether the resend helped, so a lagging read is
        indistinguishable from an inverter that genuinely drops the override.
        """
        self._verify_timer = None
        kwargs = kwargs or {}
        command = kwargs.get("command")
        attempt = int(kwargs.get("attempt", 1) or 1)
        if command is None:
            return

        action_name = command.action.value

        try:
            state = self.backend.read_state()
            result = self.backend.verify(command, state)

            if result.unverifiable:
                # Cannot verify — don't resend blindly. Warn the FIRST time so a
                # misconfiguration is visible; stay DEBUG after that.
                self._unverifiable_count += 1
                if not self._verify_unreadable_warned:
                    self._verify_unreadable_warned = True
                    self.app.log(
                        f"DirectControl: cannot verify {action_name} — inverter "
                        f"state is unreadable ({result.actual}). Verification is "
                        f"disabled until it reads a value.",
                        level="WARNING",
                    )
                else:
                    self.app.log(
                        f"DirectControl: cannot verify {action_name} — "
                        f"{result.actual}",
                        level="DEBUG",
                    )
                return

            if result.matched:
                self._verified_count += 1
                self._last_effect = result.effect.value
                if attempt > 1:
                    self._resend_recovered_count += 1
                    self.app.log(
                        f"DirectControl: {action_name} recovered after resend — "
                        f"inverter reports '{result.actual}'"
                    )
                else:
                    self.app.log(
                        f"DirectControl: verified {action_name} — "
                        f"inverter reports '{result.actual}' "
                        f"(effect: {result.effect.value})",
                        level="DEBUG",
                    )
                self._handle_effect(command, result)
                return

            self._mismatch_count += 1
            self._last_mismatch = {
                "time": datetime.datetime.now().isoformat(timespec="seconds"),
                "mode": action_name,
                "expected": command.describe(),
                "actual": result.actual,
                "attempt": attempt,
            }

            if attempt >= 2:
                # Already resent once and the inverter still disagrees. This is
                # no longer read lag — escalate and STOP (no third send).
                self._persistent_mismatch_count += 1
                self.app.log(
                    f"DirectControl: persistent mode mismatch after resend — "
                    f"expected {action_name}, inverter reports "
                    f"'{result.actual}' ({result.detail}). The inverter is not "
                    f"honouring the command; not resending again (retry happens "
                    f"next slot).",
                    level="ERROR",
                )
                return

            # Mismatch — resend the same command ONCE, bypassing duplicate
            # suppression by clearing the last-sent timestamp.
            self.app.log(
                f"DirectControl: mode mismatch — expected {action_name}, "
                f"inverter reports '{result.actual}' ({result.detail}); "
                f"resending once",
                level="WARNING",
            )
            self._last_mode_time = None  # bypass _is_duplicate
            self._resend_count += 1
            outcome = self._send(command)

            if outcome is SendResult.FAILED or outcome is SendResult.RATE_LIMITED:
                self._resend_failed_count += 1
                self.app.log(
                    f"DirectControl: resend of {action_name} failed "
                    f"({outcome.value})",
                    level="ERROR",
                )
                return

            # Record last-sent again, then re-check exactly ONCE so we learn
            # whether the resend actually took effect.
            self._last_action_sent = action_name
            self._last_mode_time = datetime.datetime.now()
            self._last_command = command
            self._schedule_verification(command, attempt=2)

        except Exception as e:
            self.app.log(
                f"DirectControl: verification error for {action_name}: {e}",
                level="ERROR",
            )

    def _handle_effect(self, command: InverterCommand, result) -> None:
        """React to the EFFECT verdict on a command the inverter ACKnowledged.

        ACK correct + EFFECT FAIL is the combination registers alone cannot
        reveal: everything reads back exactly as sent and the inverter is doing
        something else. The rejected answer to it was to rewrite the inverter's
        TOU schedule as a fallback path. That schedule is not ours — the
        reference unit was found holding 15 periods this project never authored
        — so the answer is to stop trading instead: release the session, hand
        the battery back to local logic, and escalate for a human.

        INDETERMINATE must never escalate. PV surplus explaining a charge, or
        house load absorbing a discharge, is not a failure, and treating it as
        one is how a healthy system latches a fallback it did not need.
        """
        family = effect_family(command.action)

        if result.effect is not EffectVerdict.FAIL:
            if result.effect is EffectVerdict.PASS:
                self._consecutive_effect_failures.pop(family, None)
            return

        self._effect_failure_count += 1
        streak = self._consecutive_effect_failures.get(family, 0) + 1
        self._consecutive_effect_failures[family] = streak
        action_name = command.action.value
        self.app.log(
            f"DirectControl: {action_name} was ACKNOWLEDGED but had NO EFFECT "
            f"({streak}/{self._effect_failure_limit} for the '{family}' "
            f"family) — the control registers read back correctly and the "
            f"inverter is not acting on them. Inverter reports "
            f"'{result.actual}'.",
            level="ERROR",
        )

        if streak < self._effect_failure_limit:
            return

        self._control_degraded = True
        self._degraded_family = family
        self._degraded_reason = (
            f"{streak} consecutive EFFECT failures in the '{family}' family "
            f"(last action: {action_name})"
        )
        self.app.log(
            "DirectControl: CONTROL DEGRADED — releasing the VPP session so the "
            "inverter returns to its own local logic. No further commands will "
            "be sent until this is investigated. The inverter's existing TOU "
            "schedule has NOT been modified.",
            level="ERROR",
        )
        self.release_control()

    def clear_degraded(self) -> None:
        """Resume commanding after a supervised investigation."""
        if not self._control_degraded:
            return
        self._control_degraded = False
        self._degraded_reason = None
        self._degraded_family = None
        self._consecutive_effect_failures.clear()
        self.app.log(
            "DirectControl: degraded state cleared — commanding resumes"
        )

    # --- diagnostics ------------------------------------------------------

    def get_diagnostics(self) -> dict:
        """Counters that make inverter-control health observable in HA.

        Interpretation:
          * mismatch_count high, resend_recovered_count ~= resend_count,
            persistent_mismatch_count == 0  -> the read merely LAGS.
            Raise verify_delay_seconds.
          * persistent_mismatch_count growing -> the inverter genuinely drops
            the override. A configuration/firmware problem, not a timing one.
          * resend_failed_count growing -> the backend itself is failing;
            check the Modbus connection.
          * unconfirmed_count growing while sent_count stays flat -> every
            command is hitting its client-side timeout.
          * rate_limited_count growing -> commands are colliding with the
            per-register write cooldown; they were deferred, not applied.
        """
        counts = self._apply_outcome_counts
        diagnostics = {
            "sent_count": counts.get(ApplyOutcome.SENT, 0),
            "unconfirmed_count": counts.get(ApplyOutcome.UNCONFIRMED_TIMEOUT, 0),
            "duplicate_skipped_count": counts.get(
                ApplyOutcome.SKIPPED_DUPLICATE, 0
            ),
            "dry_run_count": counts.get(ApplyOutcome.DRY_RUN, 0),
            "failed_count": counts.get(ApplyOutcome.FAILED, 0),
            "rate_limited_count": counts.get(ApplyOutcome.RATE_LIMITED, 0),
            "last_apply_outcome": (
                self.last_apply_outcome.value if self.last_apply_outcome else None
            ),
            "mismatch_count": self._mismatch_count,
            "resend_count": self._resend_count,
            "resend_recovered_count": self._resend_recovered_count,
            "resend_failed_count": self._resend_failed_count,
            "persistent_mismatch_count": self._persistent_mismatch_count,
            "unverifiable_count": self._unverifiable_count,
            "verified_count": self._verified_count,
            "release_pending_count": self._release_pending_count,
            "last_mismatch": self._last_mismatch,
            "last_effect": self._last_effect,
            "control_degraded": self._control_degraded,
            "degraded_reason": self._degraded_reason,
            "degraded_family": self._degraded_family,
            "effect_failure_count": self._effect_failure_count,
            "consecutive_effect_failures": dict(self._consecutive_effect_failures),
            "consecutive_effect_failures_max": max(
                self._consecutive_effect_failures.values(), default=0
            ),
            "effect_failure_limit": self._effect_failure_limit,
            "verify_delay_seconds": self._verify_delay,
            "verify_recheck_seconds": self._verify_recheck_delay,
            "command_timeout_seconds": self._command_timeout,
        }

        try:
            backend_diagnostics = self.backend.get_diagnostics()
        except Exception:
            backend_diagnostics = {}
        if isinstance(backend_diagnostics, dict):
            diagnostics.update(backend_diagnostics)
        return diagnostics

    # --- HA entity reads --------------------------------------------------

    def _get_pv_power(self) -> Optional[float]:
        """Read current PV power from HA sensor."""
        try:
            state = self.app.get_state(self.config.pv_power_sensor)
            if state and state not in ("unknown", "unavailable"):
                return float(state)
        except (ValueError, TypeError):
            pass
        return None

    def _get_pv_threshold(self) -> float:
        """Get current PV threshold from HA entity or config default."""
        try:
            state = self.app.get_state(self.config.pv_threshold_entity)
            if state and state not in ("unknown", "unavailable"):
                return float(state)
        except (ValueError, TypeError):
            pass
        return self.config.default_pv_threshold

    def _get_min_soc(self) -> int:
        """Get current min SOC from HA entity or config default."""
        try:
            state = self.app.get_state(self.config.min_soc_entity)
            if state and state not in ("unknown", "unavailable"):
                return int(float(state))
        except (ValueError, TypeError):
            pass
        return int(self.config.default_min_soc)

    def _get_max_soc(self) -> int:
        """Get current max SOC from HA entity or config default."""
        try:
            state = self.app.get_state(self.config.max_soc_entity)
            if state and state not in ("unknown", "unavailable"):
                return int(float(state))
        except (ValueError, TypeError):
            pass
        return int(self.config.default_max_soc)

    # --- duplicate suppression -------------------------------------------

    def _is_duplicate(self, command: InverterCommand) -> bool:
        """Check if this command is identical to the last one sent recently."""
        if self._last_command is None or self._last_mode_time is None:
            return False
        if self._last_action_sent != command.action.value:
            return False

        elapsed = (datetime.datetime.now() - self._last_mode_time).total_seconds()
        half_slot = self.config.slot_minutes * 60 / 2
        if elapsed > half_slot:
            return False  # Time to refresh even if same mode

        return command.dedup_key() == self._last_command.dedup_key()

    def _seconds_since_last(self) -> float:
        if self._last_mode_time:
            return (datetime.datetime.now() - self._last_mode_time).total_seconds()
        return float('inf')
