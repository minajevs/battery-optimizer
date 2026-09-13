"""Tests for DirectControl policy: outcomes, deduplication, verify ladder.

DirectControl no longer knows about services or registers — it drives a
ControlBackend. These tests therefore exercise policy against a FakeBackend;
the register-level contract lives in test_upstream_vpp_backend.py.

Covers:
- unconfirmed send -> records last-sent, schedules verification, logs WARNING
- confirmed failure -> returns False, does NOT record last-sent, no verification
- rate-limited send -> NOT applied, no failure escalation, retry scheduled
- verify-after-set mismatch -> resends once (bypassing duplicate suppression),
  then re-checks exactly once; a persistent mismatch escalates to ERROR without
  ever sending a third time (bounded ladder, no resend loop)
- verify/timeout delays are configurable and counters are exposed
- pending verification timer is superseded when a new mode is applied
"""

from __future__ import annotations

import datetime

import pytest

from battery_optimizer_lib.config import BatteryOptimizerConfig
from battery_optimizer_lib.control import (
    ControlAction,
    EffectVerdict,
    InverterState,
    SendResult,
    VerifyResult,
    VerifyVerdict,
)
from battery_optimizer_lib.direct_control import ApplyOutcome, DirectControl
from battery_optimizer_lib.models import BatteryMode, ScheduleEntry


class FakeBackend:
    """Backend double: records commands, returns scripted results."""

    name = "fake"

    def __init__(self):
        self.send_result = SendResult.CONFIRMED
        self.send_raise = None          # set to an Exception instance to raise
        self.release_result = SendResult.CONFIRMED
        self.release_raise = None
        self.sent = []                  # list of InverterCommand
        self.released = 0

        # Verification scripting
        self.verdict = VerifyVerdict.UNVERIFIABLE
        self.effect = EffectVerdict.PASS
        self.actual = "unreadable"
        self.state = None               # InverterState or None

    def send(self, command):
        self.sent.append(command)
        if self.send_raise is not None:
            raise self.send_raise
        return self.send_result

    def release(self):
        self.released += 1
        if self.release_raise is not None:
            raise self.release_raise
        return self.release_result

    def read_state(self):
        return self.state

    def verify(self, command, state):
        return VerifyResult(
            verdict=self.verdict, actual=self.actual, effect=self.effect
        )

    def get_diagnostics(self):
        return {"backend": self.name}


class FakeApp:
    """Minimal AppDaemon app double exposing the methods DirectControl uses."""

    def __init__(self):
        self.states = {}

        self._next_handle = 0
        self.run_in_calls = []  # list of (callback, delay, kwargs, handle)
        self.cancelled = []

        self.logs = []  # list of (message, level)

    def get_state(self, entity):
        return self.states.get(entity)

    def run_in(self, callback, delay, **kwargs):
        self._next_handle += 1
        handle = f"timer_{self._next_handle}"
        self.run_in_calls.append((callback, delay, kwargs, handle))
        return handle

    def cancel_timer(self, handle):
        self.cancelled.append(handle)

    def log(self, message, level="INFO"):
        self.logs.append((message, level))

    # --- test helpers ---
    def levels(self):
        return [lvl for _, lvl in self.logs]

    def fire_last_timer(self):
        """Invoke the most recently scheduled run_in callback."""
        callback, _delay, kwargs, _handle = self.run_in_calls[-1]
        callback(kwargs)


def make_dc(device_id="dev123", **overrides):
    config = BatteryOptimizerConfig(device_id=device_id, **overrides)
    app = FakeApp()
    backend = FakeBackend()
    return DirectControl(app, config, backend), app, backend


def hold_entry():
    return ScheduleEntry(
        time=datetime.datetime(2024, 1, 1, 12, 0, 0),
        mode=BatteryMode.HOLD,
        reason="test",
    )


def charge_entry():
    return ScheduleEntry(
        time=datetime.datetime(2024, 1, 1, 12, 0, 0),
        mode=BatteryMode.CHARGE,
        reason="test",
    )


def discharge_entry():
    return ScheduleEntry(
        time=datetime.datetime(2024, 1, 1, 12, 0, 0),
        mode=BatteryMode.DISCHARGE,
        reason="test",
    )


def matching(backend, actual="auth=1 remote=1"):
    backend.verdict = VerifyVerdict.MATCH
    backend.actual = actual
    backend.state = InverterState(control_authority=1, remote_enabled=1)


def mismatching(backend, actual="auth=0 remote=0"):
    backend.verdict = VerifyVerdict.MISMATCH
    backend.actual = actual
    backend.state = InverterState(control_authority=0, remote_enabled=0)


# ---------------------------------------------------------------------------
# Outcome / failure detection
# ---------------------------------------------------------------------------

def test_unconfirmed_send_records_and_schedules():
    """An unconfirmed send still records last-sent and schedules verification."""
    dc, app, backend = make_dc()
    backend.send_result = SendResult.UNCONFIRMED

    result = dc.apply_mode(hold_entry())

    assert result is True
    assert dc._last_action_sent == "hold"
    assert dc._last_mode_time is not None
    assert "WARNING" in app.levels()
    assert len(app.run_in_calls) == 1
    assert app.run_in_calls[0][1] == dc._verify_delay
    assert app.run_in_calls[0][2]["attempt"] == 1


def test_failure_exception_returns_false_and_does_not_record():
    """A backend that raises is a confirmed failure."""
    dc, app, backend = make_dc()
    backend.send_raise = RuntimeError("boom")

    result = dc.apply_mode(hold_entry())

    assert result is False
    assert dc._last_action_sent is None  # not recorded -> resend not suppressed
    assert dc._last_mode_time is None
    assert len(app.run_in_calls) == 0    # no verification scheduled
    assert "ERROR" in app.levels()


def test_failure_result_returns_false_and_does_not_record():
    """An explicit FAILED result behaves like a raise."""
    dc, app, backend = make_dc()
    backend.send_result = SendResult.FAILED

    result = dc.apply_mode(hold_entry())

    assert result is False
    assert dc._last_action_sent is None
    assert len(app.run_in_calls) == 0
    assert "ERROR" in app.levels()


def test_command_carries_resolved_action_and_the_command_ttl():
    """The command handed to the backend is fully resolved policy.

    The duration is the COMMAND's TTL, not the slot length. It used to be
    slot_minutes + buffer, on the reasoning that a missed refresh would let
    the override expire and the inverter revert to its base mode. Matched
    hardware runs on 2026-09-08 disproved that: 30408 bounds the energetic
    command (1 min -> 60 s, 2 min -> 122 s) and leaves 30100/30407/30409
    exactly as they were, so expiry suppresses local logic and puts the house
    on the grid. A slot longer than the TTL is covered by RE-ARMING.
    """
    from battery_optimizer_lib.control import DEFAULT_COMMAND_TTL_MINUTES

    dc, app, backend = make_dc()

    dc.apply_mode(charge_entry())

    command = backend.sent[0]
    assert command.action is ControlAction.GRID_CHARGE
    assert command.duration_minutes == DEFAULT_COMMAND_TTL_MINUTES
    assert 1 <= command.duration_minutes <= 10, (
        "30408 is validated 1..10 minutes on this firmware")
    assert command.duration_minutes < dc.config.slot_minutes, (
        "the TTL is expected to be shorter than a slot — that is why renewal "
        "exists; if this ever flips, renewal stops being exercised")
    assert command.power_percent == dc.config.default_power_percent
    # A CHARGE slot carries the charge cutoff, never the discharge one.
    assert command.charge_cutoff_soc == int(dc.config.default_max_soc)
    assert command.discharge_cutoff_soc is None


def test_first_unverifiable_logs_warning_then_debug():
    """First cannot-verify occurrence is WARNING; subsequent ones are DEBUG."""
    dc, app, backend = make_dc()
    # backend.verdict defaults to UNVERIFIABLE

    dc.apply_mode(hold_entry())
    app.fire_last_timer()

    cannot_verify_warnings = [
        m for m, lvl in app.logs
        if lvl == "WARNING" and "cannot verify" in m
    ]
    assert len(cannot_verify_warnings) == 1

    logs_before = len(app.logs)
    dc.apply_mode(charge_entry())  # different mode -> not a duplicate
    app.fire_last_timer()
    new_logs = app.logs[logs_before:]
    assert not any(
        lvl == "WARNING" and "cannot verify" in m for m, lvl in new_logs
    )
    assert any(
        lvl == "DEBUG" and "cannot verify" in m for m, lvl in new_logs
    )


def test_failed_apply_cancels_pending_verification_timer():
    """A confirmed-failure send cancels a timer from a previous good send."""
    dc, app, backend = make_dc()

    dc.apply_mode(hold_entry())
    first_handle = app.run_in_calls[0][3]
    assert len(app.run_in_calls) == 1

    backend.send_raise = RuntimeError("boom")
    result = dc.apply_mode(charge_entry())

    assert result is False
    assert first_handle in app.cancelled
    assert len(app.run_in_calls) == 1  # no new verification scheduled


def test_failed_release_cancels_pending_verification_timer():
    """A failed release_control also cancels a previously pending timer."""
    dc, app, backend = make_dc()

    dc.apply_mode(hold_entry())
    first_handle = app.run_in_calls[0][3]

    backend.release_raise = RuntimeError("boom")
    result = dc.release_control()

    assert result is False
    assert first_handle in app.cancelled
    assert len(app.run_in_calls) == 1


def test_success_records_and_schedules_verification():
    dc, app, backend = make_dc()

    result = dc.apply_mode(hold_entry())

    assert result is True
    assert dc._last_action_sent == "hold"
    assert len(app.run_in_calls) == 1


# ---------------------------------------------------------------------------
# RATE_LIMITED: deferred, not tolerated
# ---------------------------------------------------------------------------

def test_rate_limited_is_not_applied_and_schedules_a_retry():
    """A cooldown collision means the command did NOT reach the inverter."""
    dc, app, backend = make_dc()
    backend.send_result = SendResult.RATE_LIMITED

    outcome = dc.apply_mode_with_outcome(hold_entry())

    assert outcome is ApplyOutcome.RATE_LIMITED
    assert outcome.applied is False          # must not look like success
    assert outcome.confirmed is False
    assert dc.apply_mode(charge_entry()) is False
    assert "WARNING" in app.levels()
    # A retry is scheduled rather than the command being dropped.
    assert any(
        cb == dc._retry_command for cb, _d, _k, _h in app.run_in_calls
    )
    assert dc.get_diagnostics()["rate_limited_count"] == 2


def test_rate_limited_retry_resends_and_can_succeed():
    """The deferred command is re-sent after the cooldown and then lands."""
    dc, app, backend = make_dc()
    backend.send_result = SendResult.RATE_LIMITED

    dc.apply_mode(hold_entry())
    assert len(backend.sent) == 1

    # Cooldown has passed; the retry now succeeds.
    backend.send_result = SendResult.CONFIRMED
    retry = [c for c in app.run_in_calls if c[0] == dc._retry_command][-1]
    retry[0](retry[2])

    assert len(backend.sent) == 2
    assert backend.sent[1].action is ControlAction.HOLD
    assert dc.last_apply_outcome is ApplyOutcome.SENT


def test_rate_limited_does_not_record_last_sent():
    """A deferred command must not suppress the retry as a duplicate."""
    dc, app, backend = make_dc()
    backend.send_result = SendResult.RATE_LIMITED

    dc.apply_mode(hold_entry())

    assert dc._last_action_sent is None
    assert dc._last_command is None


# ---------------------------------------------------------------------------
# verify-after-set
# ---------------------------------------------------------------------------

def test_verify_match_does_not_resend():
    dc, app, backend = make_dc()
    matching(backend)

    dc.apply_mode(hold_entry())
    sends_before = len(backend.sent)

    app.fire_last_timer()

    assert len(backend.sent) == sends_before


def test_verify_mismatch_resends_once_and_rechecks():
    """Mismatch -> resend once, then re-check ONCE (bounded ladder)."""
    dc, app, backend = make_dc()
    mismatching(backend, actual="Passthrough")

    dc.apply_mode(hold_entry())
    sends_before = len(backend.sent)

    app.fire_last_timer()

    assert len(backend.sent) == sends_before + 1
    assert "WARNING" in app.levels()
    assert len(app.run_in_calls) == 2
    assert app.run_in_calls[1][2]["attempt"] == 2
    assert app.run_in_calls[1][1] == dc._verify_recheck_delay
    assert dc.get_diagnostics()["mismatch_count"] == 1
    assert dc.get_diagnostics()["resend_count"] == 1


def test_second_verification_after_resend_matches_logs_recovery():
    dc, app, backend = make_dc()
    mismatching(backend, actual="Passthrough")

    dc.apply_mode(hold_entry())
    app.fire_last_timer()                       # attempt 1: mismatch -> resend
    sends_after_resend = len(backend.sent)

    matching(backend)                           # the read was merely lagging
    app.fire_last_timer()                       # attempt 2: match

    assert len(backend.sent) == sends_after_resend   # no third send
    assert len(app.run_in_calls) == 2                # no third timer
    diag = dc.get_diagnostics()
    assert diag["resend_recovered_count"] == 1
    assert diag["persistent_mismatch_count"] == 0
    assert any("recovered after resend" in m for m, _ in app.logs)


def test_persistent_mismatch_escalates_to_error_and_does_not_loop():
    dc, app, backend = make_dc()
    mismatching(backend, actual="Passthrough")

    dc.apply_mode(hold_entry())
    sends_after_apply = len(backend.sent)
    app.fire_last_timer()   # attempt 1: mismatch -> resend + schedule attempt 2
    app.fire_last_timer()   # attempt 2: still mismatch -> ERROR, stop

    assert len(backend.sent) == sends_after_apply + 1
    assert len(app.run_in_calls) == 2
    diag = dc.get_diagnostics()
    assert diag["persistent_mismatch_count"] == 1
    assert diag["mismatch_count"] == 2
    assert diag["resend_recovered_count"] == 0
    assert any(
        lvl == "ERROR" and "persistent mode mismatch" in m for m, lvl in app.logs
    )
    assert diag["last_mismatch"]["actual"] == "Passthrough"
    assert diag["last_mismatch"]["attempt"] == 2


def test_failed_resend_is_counted_and_stops_the_ladder():
    dc, app, backend = make_dc()
    mismatching(backend, actual="Passthrough")

    dc.apply_mode(hold_entry())
    backend.send_raise = RuntimeError("boom")
    app.fire_last_timer()

    assert dc.get_diagnostics()["resend_failed_count"] == 1
    assert len(app.run_in_calls) == 1  # no re-check after a failed resend
    assert any(lvl == "ERROR" and "resend of hold failed" in m
               for m, lvl in app.logs)


def test_rate_limited_resend_also_stops_the_ladder():
    """A resend deferred by the cooldown is not a re-check opportunity."""
    dc, app, backend = make_dc()
    mismatching(backend, actual="Passthrough")

    dc.apply_mode(hold_entry())
    backend.send_result = SendResult.RATE_LIMITED
    app.fire_last_timer()

    assert dc.get_diagnostics()["resend_failed_count"] == 1
    assert len(app.run_in_calls) == 1


def test_verify_delays_and_timeout_are_configurable():
    """apps.yaml can compensate a lagging read without a code change."""
    dc, app, backend = make_dc(
        verify_delay_seconds=30,
        verify_recheck_seconds=20,
        command_timeout_seconds=10,
    )
    mismatching(backend)

    dc.apply_mode(hold_entry())

    assert dc._command_timeout == 10
    assert app.run_in_calls[0][1] == 30

    app.fire_last_timer()
    assert app.run_in_calls[1][1] == 20


def test_get_diagnostics_shape():
    """The diagnostics sensor payload is stable and starts at zero."""
    dc, _app, _backend = make_dc()
    diag = dc.get_diagnostics()

    expected_keys = {
        "mismatch_count", "resend_count", "resend_recovered_count",
        "resend_failed_count", "persistent_mismatch_count",
        "unverifiable_count", "verified_count", "last_mismatch",
        "last_effect", "release_pending_count",
        "verify_delay_seconds", "verify_recheck_seconds",
        "command_timeout_seconds",
        # EFFECT escalation: an ACKed command that did nothing releases
        # control rather than rewriting the inverter's TOU schedule.
        "control_degraded", "degraded_reason", "degraded_family",
        "effect_failure_count", "consecutive_effect_failures",
        "consecutive_effect_failures_max", "effect_failure_limit",
        # Per-outcome tally: a dry run, a suppressed duplicate and an
        # unconfirmed timeout are all "True" from apply_mode, and only
        # sent_count means the inverter acknowledged anything. A rate-limited
        # command is none of those — it was deferred.
        "sent_count", "unconfirmed_count", "duplicate_skipped_count",
        "dry_run_count", "failed_count", "rate_limited_count",
        "last_apply_outcome",
        # Merged in from the backend.
        "backend",
    }
    assert set(diag) == expected_keys
    assert diag["last_mismatch"] is None
    assert diag["last_apply_outcome"] is None
    assert all(
        diag[k] == 0 for k in expected_keys
        if k.endswith("_count")
    )


def test_backend_diagnostics_are_merged():
    """Backend counters reach the health sensor through DirectControl."""
    dc, _app, backend = make_dc()
    backend.get_diagnostics = lambda: {"session_state": "active", "arm_failures": 2}

    diag = dc.get_diagnostics()

    assert diag["session_state"] == "active"
    assert diag["arm_failures"] == 2


def test_broken_backend_diagnostics_do_not_break_the_sensor():
    dc, _app, backend = make_dc()

    def boom():
        raise RuntimeError("nope")

    backend.get_diagnostics = boom

    diag = dc.get_diagnostics()
    assert diag["sent_count"] == 0


def test_unverifiable_reads_are_counted():
    dc, app, backend = make_dc()  # verdict defaults to UNVERIFIABLE

    dc.apply_mode(hold_entry())
    app.fire_last_timer()

    diag = dc.get_diagnostics()
    assert diag["unverifiable_count"] == 1
    assert diag["mismatch_count"] == 0
    assert diag["resend_count"] == 0


def test_unverifiable_does_not_resend():
    dc, app, backend = make_dc()

    dc.apply_mode(hold_entry())
    sends_before = len(backend.sent)

    app.fire_last_timer()

    assert len(backend.sent) == sends_before
    assert "WARNING" in app.levels()


def test_verify_mismatch_bypasses_duplicate_suppression():
    """The resend goes out even though the command matches the last send."""
    dc, app, backend = make_dc()
    mismatching(backend, actual="Passthrough")

    dc.apply_mode(hold_entry())
    assert dc._is_duplicate(dc._last_command) is True

    sends_before = len(backend.sent)
    app.fire_last_timer()
    assert len(backend.sent) == sends_before + 1


def test_effect_verdict_is_recorded_on_a_match():
    """The EFFECT level surfaces in diagnostics without gating the ladder."""
    dc, app, backend = make_dc()
    matching(backend)
    backend.effect = EffectVerdict.INDETERMINATE

    dc.apply_mode(hold_entry())
    app.fire_last_timer()

    assert dc.get_diagnostics()["last_effect"] == "indeterminate"
    assert dc.get_diagnostics()["verified_count"] == 1


# ---------------------------------------------------------------------------
# EFFECT failure: release and escalate, never rewrite the TOU schedule
# ---------------------------------------------------------------------------

def verified_effect(dc, app, backend, verdict, entry=None):
    """Run one apply + verification cycle ending in ``verdict``."""
    backend.effect = verdict
    dc._last_mode_time = None            # bypass duplicate suppression
    dc.apply_mode(entry or hold_entry())
    app.fire_last_timer()


def test_indeterminate_effect_never_escalates():
    """PV surplus or absorbed discharge is not a failure."""
    dc, app, backend = make_dc()
    matching(backend)

    for _ in range(5):
        verified_effect(dc, app, backend, EffectVerdict.INDETERMINATE)

    diag = dc.get_diagnostics()
    assert diag["effect_failure_count"] == 0
    assert diag["control_degraded"] is False
    assert backend.released == 0


def test_effect_failure_below_the_limit_alerts_but_keeps_control():
    dc, app, backend = make_dc()
    matching(backend)

    verified_effect(dc, app, backend, EffectVerdict.FAIL)

    diag = dc.get_diagnostics()
    assert diag["effect_failure_count"] == 1
    assert diag["control_degraded"] is False
    assert backend.released == 0
    assert "ERROR" in app.levels()
    assert any("NO EFFECT" in m for m, _lvl in app.logs)


def test_repeated_effect_failure_releases_control_and_latches_degraded():
    """ACK correct + no effect: hand the battery back, do not touch TOU."""
    dc, app, backend = make_dc()
    matching(backend)

    verified_effect(dc, app, backend, EffectVerdict.FAIL)
    verified_effect(dc, app, backend, EffectVerdict.FAIL)

    diag = dc.get_diagnostics()
    assert diag["consecutive_effect_failures"] == {"hold": 2}
    assert diag["degraded_family"] == "hold"
    assert diag["control_degraded"] is True
    assert "EFFECT failures" in diag["degraded_reason"]
    assert backend.released == 1
    assert any("CONTROL DEGRADED" in m for m, _lvl in app.logs)


def test_degraded_control_stops_commanding_until_cleared():
    dc, app, backend = make_dc()
    matching(backend)
    verified_effect(dc, app, backend, EffectVerdict.FAIL)
    verified_effect(dc, app, backend, EffectVerdict.FAIL)

    sends_before = len(backend.sent)
    dc._last_mode_time = None
    outcome = dc.apply_mode_with_outcome(charge_entry())

    assert outcome is ApplyOutcome.FAILED
    assert len(backend.sent) == sends_before      # nothing transmitted

    dc.clear_degraded()
    dc._last_mode_time = None
    dc.apply_mode(charge_entry())
    assert len(backend.sent) == sends_before + 1


def test_a_passing_effect_clears_the_failure_streak():
    """One bad reading between good ones must not accumulate into a latch."""
    dc, app, backend = make_dc()
    matching(backend)

    verified_effect(dc, app, backend, EffectVerdict.FAIL)
    verified_effect(dc, app, backend, EffectVerdict.PASS)
    verified_effect(dc, app, backend, EffectVerdict.FAIL)

    diag = dc.get_diagnostics()
    assert diag["effect_failure_count"] == 2
    assert diag["consecutive_effect_failures"] == {"hold": 1}
    assert diag["control_degraded"] is False
    assert backend.released == 0


def test_effect_failure_streaks_do_not_cross_action_families():
    """A dead grid-charge and a dead discharge are two faults, not two strikes.

    Sharing one counter would latch degraded after one of each, blaming a
    mechanism neither of them proved broken.
    """
    dc, app, backend = make_dc()
    matching(backend)

    verified_effect(dc, app, backend, EffectVerdict.FAIL, charge_entry())
    verified_effect(dc, app, backend, EffectVerdict.FAIL, discharge_entry())

    diag = dc.get_diagnostics()
    assert diag["consecutive_effect_failures"] == {"grid_charge": 1,
                                                  "discharge": 1}
    assert diag["control_degraded"] is False
    assert backend.released == 0

    # A second failure within ONE family is what latches.
    verified_effect(dc, app, backend, EffectVerdict.FAIL, charge_entry())
    assert dc.get_diagnostics()["control_degraded"] is True
    assert dc.get_diagnostics()["degraded_family"] == "grid_charge"


def test_the_three_discharge_actions_share_one_family():
    """They are the same forced-discharge mechanism, differing only in routing."""
    from battery_optimizer_lib.direct_control import effect_family
    from battery_optimizer_lib.control import ControlAction

    assert effect_family(ControlAction.DISCHARGE_TO_LOAD) == "discharge"
    assert effect_family(ControlAction.DISCHARGE_TO_GRID) == "discharge"
    assert effect_family(ControlAction.MAX_EXPORT) == "discharge"
    assert effect_family(ControlAction.GRID_CHARGE) == "grid_charge"
    assert effect_family(ControlAction.HOLD) == "hold"


# ---------------------------------------------------------------------------
# Commissioning: the optimizer does not drive the inverter
# ---------------------------------------------------------------------------

def test_optimizer_transmits_nothing_when_writes_are_supervised_only():
    """control_mode: commissioning must not become "the scheduler can trade"."""
    dc, app, backend = make_dc()
    backend.automatic_writes_allowed = False

    outcome = dc.apply_mode_with_outcome(charge_entry())

    assert outcome is ApplyOutcome.DRY_RUN
    assert backend.sent == []                     # nothing reached the backend
    assert any("COMMISSIONING mode" in m for m, _lvl in app.logs)
    assert "WARNING" in app.levels()


def test_the_commissioning_notice_is_loud_once_then_quiet():
    """It fires every slot; it must not drown the log."""
    dc, app, backend = make_dc()
    backend.automatic_writes_allowed = False

    dc.apply_mode(charge_entry())
    dc.apply_mode(charge_entry())
    dc.apply_mode(charge_entry())

    warnings = [m for m, lvl in app.logs
                if lvl == "WARNING" and "COMMISSIONING mode" in m]
    debugs = [m for m, lvl in app.logs
              if lvl == "DEBUG" and "COMMISSIONING mode" in m]
    assert len(warnings) == 1
    assert len(debugs) == 2
    assert backend.sent == []


def test_a_backend_without_the_property_is_still_allowed_to_write():
    """The gate may only ever tighten behaviour, never loosen it."""
    dc, _app, backend = make_dc()
    assert not hasattr(backend, "automatic_writes_allowed")

    dc.apply_mode(charge_entry())

    assert len(backend.sent) == 1


# ---------------------------------------------------------------------------
# Timer superseding
# ---------------------------------------------------------------------------

def test_new_apply_supersedes_pending_verification():
    dc, app, backend = make_dc()

    dc.apply_mode(hold_entry())
    first_handle = app.run_in_calls[0][3]

    dc.apply_mode(charge_entry())

    assert first_handle in app.cancelled
    assert len(app.run_in_calls) == 2


# ---------------------------------------------------------------------------
# release_control (passthrough)
# ---------------------------------------------------------------------------

def test_release_control_schedules_verification():
    dc, app, backend = make_dc()

    result = dc.release_control()

    assert result is True
    assert backend.released == 1
    assert dc._last_action_sent == "passthrough"
    assert len(app.run_in_calls) == 1
    assert app.run_in_calls[0][2]["command"].action is ControlAction.PASSTHROUGH


def test_release_control_unconfirmed_is_still_true():
    dc, app, backend = make_dc()
    backend.release_result = SendResult.UNCONFIRMED

    result = dc.release_control()

    assert result is True
    assert dc._last_action_sent == "passthrough"
    assert "WARNING" in app.levels()
    assert len(app.run_in_calls) == 1


def test_release_control_failure_returns_false():
    dc, app, backend = make_dc()
    backend.release_raise = RuntimeError("boom")

    result = dc.release_control()

    assert result is False
    assert dc._last_action_sent is None
    assert len(app.run_in_calls) == 0


def test_release_control_rate_limited_returns_false():
    """A deferred release must not be reported as a completed handover."""
    dc, app, backend = make_dc()
    backend.release_result = SendResult.RATE_LIMITED

    assert dc.release_control() is False
    assert dc._last_action_sent is None


# ---------------------------------------------------------------------------
# apply_mode_with_outcome: the boolean's flavours of "True"
# ---------------------------------------------------------------------------

def test_outcome_confirmed_send_is_sent():
    dc, app, backend = make_dc()

    assert dc.apply_mode_with_outcome(hold_entry()) is ApplyOutcome.SENT
    assert dc.last_apply_outcome is ApplyOutcome.SENT
    assert ApplyOutcome.SENT.confirmed is True
    assert dc.get_diagnostics()["sent_count"] == 1
    assert dc.get_diagnostics()["last_apply_outcome"] == "sent"


def test_outcome_timeout_is_unconfirmed_not_sent():
    dc, app, backend = make_dc()
    backend.send_result = SendResult.UNCONFIRMED

    outcome = dc.apply_mode_with_outcome(hold_entry())

    assert outcome is ApplyOutcome.UNCONFIRMED_TIMEOUT
    assert outcome.confirmed is False
    # Backward compatible: still "not a failure" for the boolean caller.
    assert dc.apply_mode(charge_entry()) is True
    assert dc.get_diagnostics()["unconfirmed_count"] == 2
    assert dc.get_diagnostics()["sent_count"] == 0


def test_outcome_duplicate_is_skipped_and_nothing_is_transmitted():
    dc, app, backend = make_dc()

    assert dc.apply_mode_with_outcome(hold_entry()) is ApplyOutcome.SENT
    assert (dc.apply_mode_with_outcome(hold_entry())
            is ApplyOutcome.SKIPPED_DUPLICATE)

    assert len(backend.sent) == 1  # the duplicate never went out
    assert dc.get_diagnostics()["duplicate_skipped_count"] == 1


def test_outcome_dry_run_when_no_device_id():
    dc, app, backend = make_dc(device_id="")

    assert dc.apply_mode_with_outcome(hold_entry()) is ApplyOutcome.DRY_RUN

    assert backend.sent == []
    assert dc.get_diagnostics()["dry_run_count"] == 1
    assert dc.get_diagnostics()["sent_count"] == 0


def test_outcome_failed_result_is_failed():
    dc, app, backend = make_dc()
    backend.send_result = SendResult.FAILED

    assert dc.apply_mode_with_outcome(hold_entry()) is ApplyOutcome.FAILED
    assert dc.apply_mode(hold_entry()) is False  # boolean wrapper agrees
    assert dc.get_diagnostics()["failed_count"] == 2


def test_outcome_exception_is_failed():
    dc, app, backend = make_dc()
    backend.send_raise = RuntimeError("boom")

    assert dc.apply_mode_with_outcome(hold_entry()) is ApplyOutcome.FAILED
    assert dc.get_diagnostics()["failed_count"] == 1


def test_dry_run_backend_result_is_reported_as_dry_run():
    """A dry-run backend must not look like a confirmed send."""
    dc, app, backend = make_dc()
    backend.send_result = SendResult.DRY_RUN

    outcome = dc.apply_mode_with_outcome(hold_entry())

    assert outcome is ApplyOutcome.DRY_RUN
    assert outcome.confirmed is False
    assert dc.get_diagnostics()["sent_count"] == 0


# ---------------------------------------------------------------------------
# Release lifecycle: pending is accepted-but-not-done
# ---------------------------------------------------------------------------

def test_pending_release_is_accepted_but_warns_it_is_not_finished():
    """The handover is only complete once read-back confirms 30100=0."""
    dc, app, backend = make_dc()
    backend.release_result = SendResult.PENDING

    result = dc.release_control()

    assert result is True                     # accepted, retrying
    assert "WARNING" in app.levels()
    assert any("NOT released yet" in m for m, _lvl in app.logs)
    assert any("RELEASED before stopping" in m for m, _lvl in app.logs)
    assert dc.get_diagnostics()["release_pending_count"] == 1
    # A pending release must not be recorded as a completed passthrough.
    assert dc._last_action_sent is None


def test_pending_send_is_treated_as_not_applied():
    dc, app, backend = make_dc()
    backend.send_result = SendResult.PENDING

    outcome = dc.apply_mode_with_outcome(hold_entry())

    assert outcome.applied is False
