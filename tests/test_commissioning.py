"""Slice 3: the first code in this project that can change a real inverter.

The whole point of commissioning mode is that the set of things it can do is
small, fixed, and enforced in more than one place. These tests pin that down:

* the executor's register allowlist (30200/30201/30410/30411 are unreachable);
* ``send()`` refusing every energy-moving action;
* ``build_plan`` raising rather than building one;
* a preflight that re-reads the inverter and refuses on every hazardous,
  externally owned, degraded, release-pending or incompletely read state;
* the 30476 probe always restoring what it changed.

Nothing here performs I/O.
"""

from __future__ import annotations

import pytest

from battery_optimizer_lib.config import BatteryOptimizerConfig
from battery_optimizer_lib.control import (
    COMMISSIONING_ACTIONS,
    CommissioningSession,
    ControlAction,
    HaCommissioningExecutor,
    InverterCommand,
    InverterState,
    SendResult,
    SessionState,
    UpstreamVppBackend,
    build_executor,
)
from battery_optimizer_lib.control.upstream_vpp import (
    PriorityModeCapability,
    REG_SETPOINT_MIRROR,
    REG_AC_CHARGE_ENABLE,
    REG_CHARGE_CUTOFF_SOC,
    REG_CONTROL_AUTHORITY,
    REG_DISCHARGE_CUTOFF_SOC,
    REG_EXPORT_LIMIT_ENABLE,
    REG_EXPORT_LIMIT_RATE,
    REG_PRIORITY_MODE,
    REG_REMOTE_DURATION,
    REG_REMOTE_ENABLE,
    REG_REMOTE_POWER,
    REG_TOU_NUM_PERIODS,
    RegisterWrite,
    StepResult,
)

# Still forbidden after DISCHARGE_TO_LOAD was admitted for the supervised
# discharge test. Each of these spends money in a direction nothing has yet
# observed on this hardware: grid charge buys, and both of the others sell.
FORBIDDEN_ACTIONS = [
    ControlAction.GRID_CHARGE,
    ControlAction.DISCHARGE_TO_GRID,
    ControlAction.MAX_EXPORT,
]


class FakeApp:
    """Records service calls; can be told to fail a specific register."""

    def __init__(self, registers=None):
        self.registers = dict(registers or {})
        self.logs = []
        self.service_calls = []
        self.writes = []
        self.run_in_calls = []
        self.fail_writes = {}        # register -> Exception to raise
        self.ignore_writes = set()   # register -> accepted but value unchanged
        self._handle = 0

    def log(self, message, level="INFO"):
        self.logs.append((message, level))

    def levels(self):
        return [lvl for _m, lvl in self.logs]

    def get_state(self, entity):
        return None

    def run_in(self, callback, delay, **kwargs):
        self._handle += 1
        self.run_in_calls.append((callback, delay))
        return f"timer_{self._handle}"

    def fire_last_timer(self):
        """Run the most recently scheduled callback, as AppDaemon would."""
        callback, _delay = self.run_in_calls[-1]
        callback()

    def cancel_timer(self, handle):
        pass

    def call_service(self, service, **kwargs):
        self.service_calls.append((service, kwargs))

        if service.endswith("get_register_data"):
            start = kwargs["start_address"]
            count = kwargs["count"]
            return {"success": True,
                    "values": [self.registers.get(start + i, 0)
                               for i in range(count)]}

        if service.endswith("write_register"):
            register = kwargs["register"]
            value = kwargs["value"]
            if register in self.fail_writes:
                raise self.fail_writes[register]
            self.writes.append((register, value))
            if register not in self.ignore_writes:
                self.registers[register] = value
            return None

        raise AssertionError(f"unexpected service {service}")


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make(registers=None, **overrides):
    app = FakeApp(registers)
    clock = FakeClock()
    config = BatteryOptimizerConfig(
        device_id="dev", control_mode="commissioning", **overrides)
    backend = UpstreamVppBackend(app, config,
                                 executor=build_executor(app, config),
                                 clock=clock)
    backend.clock = clock
    return backend, app, CommissioningSession(backend, log_func=app.log)


def make_waiter(backend, app):
    """A `wait(seconds)` for session_test: advance the clock, run callbacks.

    Mirrors what scripts/commission.py does with real sleeps and
    RestApp.run_due_timers -- the release lifecycle finishes on timers this
    process scheduled, so a wait that only passed time would hang at the
    settle.
    """
    def wait(seconds):
        backend.clock.advance(seconds)
        pending, app.run_in_calls = list(app.run_in_calls), []
        for callback, _delay in pending:
            callback()
    return wait


def clean():
    """A passthrough inverter with no scheduler but us: 0/0, Load First, 30411=0.

    30411 is 0 because that is what the reference WIT actually reads with
    Growatt Smart Scheduling switched off (2026-09-03, and still 0 44 h
    later). A non-zero count is now an interlock condition, not scenery — see
    ``external_scheduler()`` and the tests that use it.
    """
    return {REG_CONTROL_AUTHORITY: 0, REG_REMOTE_ENABLE: 0,
            REG_REMOTE_POWER: 0, REG_PRIORITY_MODE: 0,
            REG_TOU_NUM_PERIODS: 0}


def external_scheduler(periods=16):
    """The same inverter with somebody else's TOU schedule loaded.

    16 periods is what Smart Scheduling was observed holding on the reference
    unit; the interlock cares only that the count is above zero.
    """
    registers = clean()
    registers[REG_TOU_NUM_PERIODS] = periods
    return registers


# ---------------------------------------------------------------------------
# Mode wiring
# ---------------------------------------------------------------------------

def test_build_executor_selects_the_commissioning_executor():
    backend, _app, _session = make(clean())
    assert isinstance(backend.executor, HaCommissioningExecutor)
    assert backend.commissioning is True
    assert backend.dry_run is False          # it really can write


def test_commissioning_does_not_enable_automatic_optimizer_writes():
    """The distinction the whole slice rests on."""
    backend, _app, _session = make(clean())
    assert backend.automatic_writes_allowed is False
    assert "COMMISSIONING" in backend.control_status
    assert "does NOT drive the inverter" in backend.describe_mode()


def test_an_unrecognised_control_mode_still_falls_back_to_dry_run():
    config = BatteryOptimizerConfig(device_id="dev", control_mode="comissioning")
    assert config.control_mode == "dry_run"


# ---------------------------------------------------------------------------
# The executor allowlist — enforced below the plan
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("register", [
    REG_AC_CHARGE_ENABLE,      # 30410
    REG_TOU_NUM_PERIODS,       # 30411
    REG_CHARGE_CUTOFF_SOC,     # 30404
    REG_DISCHARGE_CUTOFF_SOC,  # 30405 — a discharge does NOT license this one
])
def test_the_executor_refuses_registers_outside_the_allowlist(register):
    app = FakeApp()
    ex = HaCommissioningExecutor(app, device_id="dev", log_func=app.log)

    result = ex.execute(RegisterWrite(register, 1))

    assert result is StepResult.FAILED
    assert app.writes == []               # nothing was transmitted
    assert "ERROR" in app.levels()


@pytest.mark.parametrize("register", [
    REG_CONTROL_AUTHORITY, REG_REMOTE_ENABLE, REG_REMOTE_DURATION,
    REG_REMOTE_POWER, REG_PRIORITY_MODE,
    # The export limit: admitted because it can only take capability away.
    REG_EXPORT_LIMIT_ENABLE, REG_EXPORT_LIMIT_RATE,
])
def test_the_executor_permits_exactly_the_commissioning_registers(register):
    app = FakeApp()
    ex = HaCommissioningExecutor(app, device_id="dev", log_func=app.log)

    assert ex.execute(RegisterWrite(register, 1)) is StepResult.OK
    assert app.writes == [(register, 1)]


def test_the_inverter_cooldown_refusal_maps_to_rate_limited():
    """Deferred is not failed — the retry machinery depends on the difference."""
    app = FakeApp()
    app.fail_writes[REG_CONTROL_AUTHORITY] = ValueError(
        "Write to register 30100 was rate-limited (WIT cooldown)")
    ex = HaCommissioningExecutor(app, device_id="dev", log_func=app.log)

    assert ex.execute(RegisterWrite(REG_CONTROL_AUTHORITY, 1)) is StepResult.RATE_LIMITED


def test_a_write_failure_is_a_failure():
    app = FakeApp()
    app.fail_writes[REG_REMOTE_ENABLE] = ValueError("Modbus write failed: Illegal Function")
    ex = HaCommissioningExecutor(app, device_id="dev", log_func=app.log)

    assert ex.execute(RegisterWrite(REG_REMOTE_ENABLE, 1)) is StepResult.FAILED


# ---------------------------------------------------------------------------
# Commissioning cannot invoke non-commissioning actions
# ---------------------------------------------------------------------------

def test_commissioning_transmits_hold_passthrough_and_the_load_discharge_only():
    """DISCHARGE_TO_LOAD was admitted for the first energetic experiment. The
    three that buy or sell were not, and admitting one is not a precedent for
    the others: each needs its own hardware evidence first."""
    assert COMMISSIONING_ACTIONS == {ControlAction.HOLD,
                                     ControlAction.PASSTHROUGH,
                                     ControlAction.DISCHARGE_TO_LOAD}


@pytest.mark.parametrize("action", FORBIDDEN_ACTIONS)
def test_send_refuses_every_energy_moving_action(action):
    backend, app, _session = make(clean())

    result = backend.send(InverterCommand(action=action))

    assert result is SendResult.FAILED
    assert app.writes == []                       # nothing was transmitted
    assert any("not a commissioning operation" in m for m, _lvl in app.logs)
    assert backend.session_state is SessionState.NOT_ARMED


@pytest.mark.parametrize("action", FORBIDDEN_ACTIONS)
def test_build_plan_refuses_to_even_construct_a_forbidden_sequence(action):
    """The structural backstop, for a caller that bypasses send()."""
    backend, _app, _session = make(clean())

    with pytest.raises(ValueError, match="not a commissioning operation"):
        backend.build_plan(InverterCommand(action=action))


def test_the_commissioning_plan_touches_only_the_session_registers():
    backend, _app, _session = make(clean())

    plan = backend.build_plan(InverterCommand(
        action=ControlAction.HOLD, power_percent=1, duration_minutes=5))

    assert [(s.register, s.value) for s in plan.steps] == [
        (REG_REMOTE_DURATION, 5),
        (REG_REMOTE_POWER, 1),          # +1 %, never 0
        (REG_CONTROL_AUTHORITY, 1),
        (REG_REMOTE_ENABLE, 1),         # ARM last
    ]


def test_a_commissioning_hold_writes_no_policy_registers_on_real_hardware():
    backend, app, session = make(clean())

    result = session.hold(duration_minutes=5)

    assert result.ok is True
    written = [reg for reg, _v in app.writes]
    for forbidden in (REG_TOU_NUM_PERIODS, REG_EXPORT_LIMIT_ENABLE,
                      REG_EXPORT_LIMIT_RATE, REG_AC_CHARGE_ENABLE,
                      REG_CHARGE_CUTOFF_SOC):
        assert forbidden not in written
    assert written[-1] == REG_REMOTE_ENABLE
    assert backend.session_state is SessionState.ACTIVE


# ---------------------------------------------------------------------------
# The preflight interlock
# ---------------------------------------------------------------------------

def test_hold_refuses_at_one_zero_because_the_authority_is_not_ours():
    """Refused on ownership, not on a hazard diagnosis we cannot support."""
    _backend, app, session = make({REG_CONTROL_AUTHORITY: 1,
                                   REG_REMOTE_ENABLE: 0,
                                   REG_PRIORITY_MODE: 0})

    result = session.hold()

    assert result.refused is True
    assert "HARD INTERLOCK" in result.detail
    assert app.writes == []


def test_hold_refuses_after_our_own_arm_was_left_half_applied():
    backend, app, session = make({REG_CONTROL_AUTHORITY: 1,
                                  REG_REMOTE_ENABLE: 0,
                                  REG_PRIORITY_MODE: 0})
    # 1/0 that WE created: reconcile must keep it ours rather than reclassify.
    backend.session_state = SessionState.ARM_FAILED_AUTHORITY_HELD

    result = session.hold()

    assert result.refused is True
    assert "half-applied" in result.detail
    assert app.writes == []


def test_commissioning_never_recovers_from_one_zero():
    """Nothing acts on 30100=1/30407=0 alone — not even a mode that can write.

    Commissioning CAN write, so a capability-based guard was never enough on
    its own; and since 1/0 is no longer read as a fault on this hardware,
    there is nothing here to recover from in the first place.
    """
    backend, app, _session = make({REG_CONTROL_AUTHORITY: 1,
                                   REG_REMOTE_ENABLE: 0,
                                   REG_PRIORITY_MODE: 0})

    backend.reconcile()

    assert backend.session_state is SessionState.AUTHORITY_HELD_NOT_OURS
    assert app.run_in_calls == []        # no timer was armed
    assert app.writes == []
    assert any("discrepancy is unresolved" in m.lower() for m, _lvl in app.logs)


def test_release_will_not_revoke_authority_this_process_did_not_take():
    """Including at 1/0: not ours to set, not ours to clear."""
    _backend, app, session = make({REG_CONTROL_AUTHORITY: 1,
                                   REG_REMOTE_ENABLE: 0,
                                   REG_PRIORITY_MODE: 0})

    result = session.release()

    assert result.refused is True
    assert "not ours to revoke" in result.detail
    assert app.writes == []


def test_release_does_clear_authority_this_process_left_half_applied():
    """Our own failed arm IS ours to undo — the trigger is our write result."""
    backend, app, session = make(clean())
    backend.session_state = SessionState.ARM_FAILED_AUTHORITY_HELD
    app.registers[REG_CONTROL_AUTHORITY] = 1
    app.registers[REG_REMOTE_ENABLE] = 0

    result = session.release()

    assert result.ok is True
    assert (REG_CONTROL_AUTHORITY, 0) in app.writes


def test_hold_refuses_when_authority_is_held_by_something_we_cannot_account_for():
    """A hard interlock, without claiming to know what set 30100."""
    backend, app, session = make({REG_CONTROL_AUTHORITY: 1,
                                  REG_REMOTE_ENABLE: 1,
                                  REG_PRIORITY_MODE: 0})

    result = session.hold()

    assert result.refused is True
    assert "HARD INTERLOCK" in result.detail
    assert app.writes == []
    assert backend.session_state is SessionState.AUTHORITY_HELD_NOT_OURS


def test_inherited_authority_is_never_reused_to_arm():
    """The failure this interlock exists to prevent: arming on someone else's 30100."""
    _backend, app, session = make({REG_CONTROL_AUTHORITY: 1,
                                   REG_REMOTE_ENABLE: 1,
                                   REG_PRIORITY_MODE: 0})

    session.hold()
    session.renew()

    assert app.writes == []          # not one register, on either attempt


def test_hold_refuses_when_the_inverter_is_unreadable():
    backend, app, session = make(clean())
    backend.executor.read_registers = lambda start, count: None

    result = session.hold()

    assert result.refused is True
    assert "unreadable" in result.detail
    assert app.writes == []


def test_hold_refuses_on_an_incomplete_read_of_the_control_block():
    """A partial picture is not a basis for a write."""
    backend, app, session = make(clean())
    real = backend.executor.read_registers

    def partial(start, count):
        # 30476 is read as part of the 30474..30476 tail block.
        if start == REG_SETPOINT_MIRROR:
            return None
        return real(start, count)

    backend.executor.read_registers = partial

    result = session.hold()

    assert result.refused is True
    assert "incomplete read" in result.detail
    assert "30476" in result.detail
    assert app.writes == []


def test_hold_refuses_when_a_release_is_still_pending():
    backend, app, session = make(clean())
    backend.session_state = SessionState.RELEASE_PENDING

    result = session.hold()

    assert result.refused is True
    assert "release is still pending" in result.detail
    assert app.writes == []


def test_hold_refuses_while_degraded():
    _backend, app, session = make(clean())
    session._degrade("something went wrong")

    result = session.hold()

    assert result.refused is True
    assert "degraded" in result.detail
    assert app.writes == []


def test_hold_refuses_when_the_inverter_is_not_in_passthrough():
    """Authority held without a session of ours is not a starting point."""
    backend, app, session = make(clean())
    backend.session_state = SessionState.NOT_ARMED
    app.registers[REG_REMOTE_ENABLE] = 1        # 0/1: not 0/0, not our session

    result = session.hold()

    assert result.refused is True
    assert "not in the 0/0 passthrough state" in result.detail
    assert app.writes == []


def test_every_operation_re_reads_the_inverter():
    """Preflight must not trust a cached model of the hardware."""
    _backend, app, session = make(clean())

    reads_before = len([c for c, _k in app.service_calls
                        if c.endswith("get_register_data")])
    session.hold()
    reads_after = len([c for c, _k in app.service_calls
                       if c.endswith("get_register_data")])

    assert reads_after > reads_before


# ---------------------------------------------------------------------------
# Renewal
# ---------------------------------------------------------------------------

def test_renew_inside_the_cooldown_is_deferred_not_failed():
    """Preserved from the normal path: deferred is a third outcome, not a failure."""
    backend, _app, session = make(clean())
    session.hold(duration_minutes=5)     # stamps 30408/30409/30407

    result = session.renew(duration_minutes=7)

    assert result.refused is True
    assert "cooldown" in result.detail
    assert session.degraded is False
    assert backend.session_state is SessionState.ACTIVE


def test_renew_requires_a_session_this_process_opened():
    _backend, app, session = make(clean())

    result = session.renew()

    assert result.refused is True
    assert "no session opened by this process" in result.detail
    assert app.writes == []


def test_renew_re_arms_without_re_taking_authority():
    backend, app, session = make(clean())
    session.hold(duration_minutes=5)
    writes_before = len(app.writes)
    backend.clock.advance(31)           # past the per-register write cooldown

    result = session.renew(duration_minutes=7)

    assert result.ok is True
    renewal = app.writes[writes_before:]
    assert [reg for reg, _v in renewal] == [REG_REMOTE_DURATION,
                                            REG_REMOTE_POWER,
                                            REG_REMOTE_ENABLE]
    assert (REG_REMOTE_DURATION, 7) in renewal
    assert backend.session_state is SessionState.ACTIVE


def test_reconciling_mid_session_does_not_demote_our_own_session():
    """Commissioning reconciles before EVERY operation, including on 1/1."""
    backend, _app, session = make(clean())
    session.hold()
    assert backend.session_state is SessionState.ACTIVE

    backend.reconcile()

    assert backend.session_state is SessionState.ACTIVE


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------

def test_release_revokes_authority_and_is_confirmed_by_read_back():
    backend, app, session = make(clean())
    session.hold()
    # Our own successful 30100=1 stamped the cooldown; wait it out.
    backend.clock.advance(31)

    result = session.release()

    assert result.ok is True
    assert (REG_CONTROL_AUTHORITY, 0) in app.writes
    # Authority is confirmed back, but the session is NOT finished: 30407=0
    # has not been written yet, so stopping here would strand the arm.
    assert backend.session_state is SessionState.RELEASE_SETTLING
    assert backend.session_state.safe_to_stop is False

    backend.clock.advance(36)
    app.fire_last_timer()                       # the scheduled disarm

    assert (REG_REMOTE_ENABLE, 0) in app.writes
    assert backend.session_state is SessionState.RELEASED
    assert backend.session_state.safe_to_stop is True


def test_release_deferred_by_the_cooldown_is_reported_as_in_progress():
    """The documented path: our own 30100=1 blocks the 30100=0 that undoes it."""
    backend, _app, session = make(clean())
    session.hold()                      # stamps the 30100 cooldown

    result = session.release()

    assert result.ok is True            # in progress, not a failure
    assert "rate-limited" in result.detail
    assert backend.session_state is SessionState.RELEASE_PENDING
    assert session.degraded is False    # deferral must never latch


def test_release_is_available_while_degraded():
    """A latch must never be able to trap a session open."""
    backend, app, session = make(clean())
    session.hold()
    backend.clock.advance(31)
    session._degrade("simulated")

    result = session.release()

    assert result.ok is True
    assert (REG_CONTROL_AUTHORITY, 0) in app.writes


def test_release_refuses_to_tear_down_a_session_that_is_not_ours():
    _backend, app, session = make({REG_CONTROL_AUTHORITY: 1,
                                   REG_REMOTE_ENABLE: 1,
                                   REG_PRIORITY_MODE: 0})

    result = session.release()

    assert result.refused is True
    assert "not ours to revoke" in result.detail
    assert app.writes == []


def test_release_refuses_when_one_is_already_pending():
    backend, app, session = make(clean())
    session.hold()
    session.release()                   # deferred -> RELEASE_PENDING
    writes_before = len(app.writes)

    result = session.release()

    assert result.refused is True
    assert "already pending" in result.detail
    assert len(app.writes) == writes_before


# ---------------------------------------------------------------------------
# The external-scheduler interlock (30411 > 0)
# ---------------------------------------------------------------------------

def test_hold_refuses_while_another_scheduler_has_a_schedule_loaded():
    """30411 > 0 cannot be ours: no plan in this project writes a TOU period."""
    _backend, app, session = make(external_scheduler(16))

    result = session.hold()

    assert result.refused is True
    assert "EXTERNAL SCHEDULER" in result.detail
    assert "16" in result.detail
    assert app.writes == []


def test_renew_refuses_while_another_scheduler_has_a_schedule_loaded():
    """A schedule appearing mid-session stops the watchdog being re-armed.

    Not re-arming is the safe direction: the override expires on its own and
    the inverter returns to its base mode. release() stays available.
    """
    backend, app, session = make(clean())
    session.hold(duration_minutes=5)
    backend.clock.advance(31)
    app.registers[REG_TOU_NUM_PERIODS] = 16          # somebody switched it on
    writes_before = len(app.writes)

    result = session.renew(duration_minutes=7)

    assert result.refused is True
    assert "EXTERNAL SCHEDULER" in result.detail
    assert len(app.writes) == writes_before
    assert backend.session_state is SessionState.ACTIVE


def test_the_probe_obeys_the_external_scheduler_interlock_too():
    _backend, app, session = make(external_scheduler())

    result = session.probe_priority_mode()

    assert result.refused is True
    assert "EXTERNAL SCHEDULER" in result.detail
    assert app.writes == []


def test_a_single_tou_period_is_enough_to_interlock():
    """The rule is > 0, not "a schedule that looks substantial"."""
    _backend, app, session = make(external_scheduler(1))

    result = session.hold()

    assert result.refused is True
    assert "EXTERNAL SCHEDULER" in result.detail
    assert app.writes == []


def test_the_external_scheduler_refusal_names_the_culprit_before_30100_does():
    """Smart Scheduling ON is 30100=1 AND 30411>0; only one message helps.

    Observed together on the reference WIT on 2026-09-03: authority held and
    16 periods loaded. AUTHORITY_HELD_NOT_OURS is true but says nothing about
    what to switch off, so the TOU interlock is checked first.
    """
    registers = external_scheduler(16)
    registers[REG_CONTROL_AUTHORITY] = 1
    _backend, app, session = make(registers)

    result = session.hold()

    assert result.refused is True
    assert "EXTERNAL SCHEDULER" in result.detail
    assert "HARD INTERLOCK" not in result.detail
    assert app.writes == []


def test_release_is_never_blocked_by_the_external_scheduler_interlock():
    """Giving the inverter back must not be what an interlock prevents."""
    backend, app, session = make(clean())
    session.hold(duration_minutes=5)
    backend.clock.advance(31)
    app.registers[REG_TOU_NUM_PERIODS] = 16

    result = session.release()

    assert result.ok is True
    assert (REG_CONTROL_AUTHORITY, 0) in app.writes


def test_an_unread_30411_refuses_rather_than_reading_as_absent():
    """None is not evidence of absence, so it refuses on the incomplete read."""
    backend, app, session = make(clean())
    real = backend.executor.read_registers

    def partial(start, count):
        # 30411 is the last register of the 30404..30411 block.
        if start == REG_CHARGE_CUTOFF_SOC:
            return None
        return real(start, count)

    backend.executor.read_registers = partial

    result = session.hold()

    assert result.refused is True
    assert "incomplete read" in result.detail
    assert "30411" in result.detail
    assert app.writes == []


def test_a_commissioning_hold_is_clean_again_once_the_schedule_is_gone():
    """The interlock is a condition of the inverter, not a latch."""
    backend, app, session = make(external_scheduler(16))
    assert session.hold().refused is True

    app.registers[REG_TOU_NUM_PERIODS] = 0           # scheduler switched off
    result = session.hold(duration_minutes=5)

    assert result.ok is True
    assert session.degraded is False
    assert backend.session_state is SessionState.ACTIVE


# ---------------------------------------------------------------------------
# Read-back confirmation of HOLD and renew
# ---------------------------------------------------------------------------

def test_a_hold_is_confirmed_by_reading_30100_30407_30409_back():
    backend, _app, session = make(clean())

    result = session.hold(duration_minutes=5)

    assert result.ok is True
    assert "confirmed by read-back" in result.detail
    assert "auth=1" in result.detail and "remote=1" in result.detail
    assert backend.session_state is SessionState.ACTIVE


def test_a_hold_whose_arm_did_not_take_is_not_reported_as_a_session():
    """The write was accepted and the register did not move. Not armed."""
    backend, app, session = make(clean())
    app.ignore_writes.add(REG_REMOTE_ENABLE)      # accepted, value unchanged

    result = session.hold(duration_minutes=5)

    assert result.ok is False
    assert "MISMATCH" in result.detail
    assert "remote control not enabled" in result.detail
    assert session.degraded is True


def test_a_hold_whose_setpoint_did_not_take_is_not_reported_as_a_session():
    backend, app, session = make(clean())
    app.ignore_writes.add(REG_REMOTE_POWER)       # 30409 stays 0

    result = session.hold(duration_minutes=5)

    assert result.ok is False
    assert "MISMATCH" in result.detail
    assert "power" in result.detail
    assert session.degraded is True


def test_a_hold_that_cannot_be_read_back_is_not_a_confirmed_session():
    """A session that cannot be seen is not a supervised one."""
    backend, _app, session = make(clean())
    real = backend.read_state
    calls = {"n": 0}

    def read_once_then_blind():
        calls["n"] += 1
        return real() if calls["n"] <= 1 else None   # 1 = preflight reconcile

    backend.read_state = read_once_then_blind

    result = session.hold(duration_minutes=5)

    assert result.ok is False
    assert "read-back failed" in result.detail
    assert session.degraded is True


def test_a_renewal_that_did_not_take_is_not_reported_as_renewed():
    backend, app, session = make(clean())
    session.hold(duration_minutes=5)
    backend.clock.advance(31)
    app.ignore_writes.add(REG_REMOTE_DURATION)    # 30408 will not move

    result = session.renew(duration_minutes=7)

    # 30408 is not part of verify()'s three registers, so the renewal still
    # confirms -- what must NOT happen is a mismatch being reported as OK.
    assert result.ok is True
    app.ignore_writes.clear()
    app.ignore_writes.add(REG_REMOTE_ENABLE)
    app.registers[REG_REMOTE_ENABLE] = 0          # arm silently dropped
    backend.clock.advance(31)

    second = session.renew(duration_minutes=7)

    assert second.ok is False
    assert "MISMATCH" in second.detail


# ---------------------------------------------------------------------------
# The long-lived session test
# ---------------------------------------------------------------------------

def test_session_test_runs_the_whole_lifecycle_in_one_process():
    backend, app, session = make(clean())

    result = session.session_test(
        wait=make_waiter(backend, app), duration_minutes=5,
        renew_after_seconds=35)

    assert result.ok is True
    assert backend.session_state is SessionState.RELEASED
    assert backend.session_state.safe_to_stop is True

    operations = [r.operation for r in session.history]
    assert operations == ["timed_hold", "timer_renewal", "release",
                          "session_test"]

    registers = [reg for reg, _v in app.writes]
    assert registers[-1] == REG_REMOTE_ENABLE     # the disarm, last of all
    assert app.registers[REG_CONTROL_AUTHORITY] == 0
    assert app.registers[REG_REMOTE_ENABLE] == 0


def test_session_test_reports_what_the_renewal_experiment_showed():
    backend, app, session = make(clean())

    session.session_test(wait=make_waiter(backend, app), duration_minutes=5,
                         renew_after_seconds=35)

    text = " ".join(session.observations)
    assert "after HOLD" in text
    assert "after RENEW" in text
    # This fake echoes 30408 rather than counting it down, and saying so is
    # the honest answer -- the experiment cannot conclude from equal readings.
    assert "renewal INCONCLUSIVE" in text


def test_the_renewal_verdict_reads_a_counting_down_30408():
    """On an inverter that counts 30408 down, the experiment can conclude."""
    _backend, _app, session = make(clean())

    verdict = session._renewal_verdict(
        InverterState(duration_minutes=5),      # after HOLD
        InverterState(duration_minutes=3),      # after waiting
        InverterState(duration_minutes=5),      # after the re-arm
        5)

    assert "renewal WORKS" in verdict


def test_the_renewal_verdict_reports_a_re_arm_that_did_not_take():
    _backend, _app, session = make(clean())

    verdict = session._renewal_verdict(
        InverterState(duration_minutes=5),
        InverterState(duration_minutes=3),
        InverterState(duration_minutes=3),      # the re-arm changed nothing
        5)

    assert "renewal DID NOT TAKE" in verdict


def test_the_renewal_verdict_refuses_to_conclude_from_an_unreadable_30408():
    _backend, _app, session = make(clean())

    verdict = session._renewal_verdict(
        InverterState(duration_minutes=5), None,
        InverterState(duration_minutes=5), 5)

    assert "renewal INCONCLUSIVE" in verdict


def test_session_test_hands_the_inverter_back_when_the_hold_does_not_confirm():
    """Every path out of the test releases what it opened."""
    backend, app, session = make(clean())
    app.ignore_writes.add(REG_REMOTE_ENABLE)      # the arm will not take

    result = session.session_test(
        wait=make_waiter(backend, app), duration_minutes=5,
        renew_after_seconds=35)

    assert result.ok is False
    assert "HOLD did not confirm" in result.detail
    assert (REG_CONTROL_AUTHORITY, 0) in app.writes
    assert app.registers[REG_CONTROL_AUTHORITY] == 0


def test_session_test_transmits_nothing_when_the_preflight_refuses():
    backend, app, session = make(external_scheduler(16))

    result = session.session_test(
        wait=make_waiter(backend, app), duration_minutes=5)

    assert result.ok is False
    assert result.refused is True
    assert "EXTERNAL SCHEDULER" in result.detail
    assert app.writes == []


def test_cleanup_continues_past_the_reporting_timeout():
    """The timeout fails the test. It does not end the handover.

    Nothing but this process can make safe_to_stop true, so a timer that
    walked away from an armed inverter would be trading a late report for a
    stranded session.
    """
    backend, app, session = make(clean())
    session.hold(duration_minutes=5)
    waiter = make_waiter(backend, app)

    # 5 s budget against a 35 s settle: the timeout is passed several polls
    # before the disarm can even clear its cooldown.
    result = session._release_and_report(
        "session_test", waiter, timeout_seconds=5, poll_seconds=5,
        summary="summary")

    assert result.ok is False
    assert "reporting timeout" in result.detail
    # ... and yet it saw the release all the way through.
    assert backend.session_state is SessionState.RELEASED
    assert backend.session_state.safe_to_stop is True
    assert any("cleanup continues" in m and lvl == "CRITICAL"
               for m, lvl in app.logs)


def test_cleanup_retries_a_release_refused_by_an_unreadable_inverter():
    """A refusal schedules no timer, so waiting would wait forever."""
    backend, app, session = make(clean())
    session.hold(duration_minutes=5)
    waiter = make_waiter(backend, app)

    real_reconcile = backend.reconcile
    blind = {"left": 2}          # the first two release attempts see nothing

    def sometimes_unreadable():
        if blind["left"] > 0:
            blind["left"] -= 1
            return None
        return real_reconcile()

    backend.reconcile = sometimes_unreadable

    result = session._release_and_report(
        "session_test", waiter, timeout_seconds=600, poll_seconds=5,
        summary="summary")

    assert blind["left"] == 0                       # both blind reads consumed
    refusals = [r for r in session.history
                if r.operation == "release" and r.refused]
    assert len(refusals) == 2
    assert all("unreadable" in r.detail for r in refusals)
    assert any("re-initiating" in m for m, _lvl in app.logs)
    assert result.ok is True
    assert backend.session_state is SessionState.RELEASED


def test_a_force_abort_stops_cleanup_and_says_what_may_be_left_armed():
    """The operator's escape from an unfinishable handover, and only theirs."""
    backend, app, session = make(clean())
    session.hold(duration_minutes=5)
    app.ignore_writes.add(REG_CONTROL_AUTHORITY)    # 30100 will keep reading 1
    waiter = make_waiter(backend, app)
    polls = {"n": 0}

    def wait(seconds):
        polls["n"] += 1
        if polls["n"] > 3:
            raise KeyboardInterrupt                 # the second interrupt
        waiter(seconds)

    result = session._release_and_report(
        "session_test", wait, timeout_seconds=5, poll_seconds=5,
        summary="summary")

    assert result.ok is False
    assert "FORCE-ABORTED" in result.detail
    assert backend.session_state.safe_to_stop is False
    assert any("FORCE-ABORT" in m and lvl == "CRITICAL"
               for m, lvl in app.logs)


def test_an_interrupt_after_the_hold_still_hands_the_inverter_back():
    """Ctrl-C mid-experiment is an operator changing their mind, not a leak."""
    backend, app, session = make(clean())
    waiter = make_waiter(backend, app)
    interrupted = {"done": False}

    def wait(seconds):
        if not interrupted["done"]:
            interrupted["done"] = True
            raise KeyboardInterrupt                 # during the renewal wait
        waiter(seconds)

    result = session.session_test(wait=wait, duration_minutes=5,
                                  renew_after_seconds=35)

    assert result.ok is False
    assert "ABORTED by KeyboardInterrupt" in result.detail
    # The session that was open is closed, and confirmed closed.
    assert backend.session_state is SessionState.RELEASED
    assert app.registers[REG_CONTROL_AUTHORITY] == 0
    assert app.registers[REG_REMOTE_ENABLE] == 0


def test_an_error_after_the_hold_still_hands_the_inverter_back():
    backend, app, session = make(clean())
    waiter = make_waiter(backend, app)
    raised = {"done": False}

    def wait(seconds):
        if not raised["done"]:
            raised["done"] = True
            raise RuntimeError("the network went away")
        waiter(seconds)

    result = session.session_test(wait=wait, duration_minutes=5,
                                  renew_after_seconds=35)

    assert result.ok is False
    assert "ABORTED by RuntimeError" in result.detail
    assert backend.session_state is SessionState.RELEASED
    assert app.registers[REG_CONTROL_AUTHORITY] == 0


# ---------------------------------------------------------------------------
# Duration and renewal-timing validation
# ---------------------------------------------------------------------------

def test_a_zero_duration_hold_is_refused():
    """30408=0 would assert a timeout semantic this hardware never showed at
    any value -- the watchdog test found 30407 still 1 well past the window.
    Nothing here is a substitute for the release that actually ends a session."""
    _backend, app, session = make(clean())

    result = session.hold(duration_minutes=0)

    assert result.refused is True
    assert "has not demonstrated" in result.detail
    assert app.writes == []


@pytest.mark.parametrize("minutes", [-5, 0, 11, 60, 1440])
def test_durations_outside_the_commissioning_range_are_refused(minutes):
    _backend, app, session = make(clean())

    assert session.hold(duration_minutes=minutes).refused is True
    assert session.session_test(
        wait=lambda s: None, duration_minutes=minutes).refused is True
    assert app.writes == []


@pytest.mark.parametrize("minutes", [1, 5, 10])
def test_the_bounded_range_itself_is_accepted(minutes):
    _backend, app, session = make(clean())

    result = session.hold(duration_minutes=minutes)

    assert result.ok is True
    assert (REG_REMOTE_DURATION, minutes) in app.writes


def test_a_non_integer_duration_is_refused_before_any_write():
    _backend, app, session = make(clean())

    assert session.hold(duration_minutes=2.5).refused is True
    assert session.hold(duration_minutes=True).refused is True
    assert app.writes == []


def test_a_renewal_inside_the_write_cooldown_is_refused():
    """It would measure the cooldown, not the watchdog."""
    _backend, app, session = make(clean())

    result = session.session_test(
        wait=lambda s: None, duration_minutes=5, renew_after_seconds=20)

    assert result.refused is True
    assert "cooldown" in result.detail
    assert app.writes == []


def test_a_renewal_after_the_session_would_have_expired_is_refused():
    _backend, app, session = make(clean())

    result = session.session_test(
        wait=lambda s: None, duration_minutes=1, renew_after_seconds=90)

    assert result.refused is True
    assert "already have expired" in result.detail
    assert app.writes == []


def test_renewal_timing_is_validated_before_a_session_is_opened():
    """Refusals must not be the thing that leaves an inverter armed."""
    backend, app, session = make(clean())

    session.session_test(wait=lambda s: None, duration_minutes=5,
                         renew_after_seconds=5)

    assert app.writes == []
    assert backend.session_state is SessionState.NOT_ARMED


def test_a_second_process_cannot_take_over_a_session_it_did_not_open():
    """Why the lifecycle has to run in one process.

    The first backend opens a session. A second one, standing in for the next
    CLI invocation, sees 30100=1 it cannot account for and refuses — it does
    not adopt the session, and nothing on disk lets it.
    """
    backend, app, session = make(clean())
    session.hold(duration_minutes=5)
    assert backend.session_state is SessionState.ACTIVE

    second_backend, _app2, second_session = make(app.registers)
    second_backend.clock.advance(31)

    renewed = second_session.renew(duration_minutes=5)
    released = second_session.release()

    assert renewed.refused is True
    # It never even reaches "no session of ours to renew": the authority it
    # can see is not accounted for, and that refuses first.
    assert "HARD INTERLOCK" in renewed.detail
    assert released.refused is True
    assert "not ours to revoke" in released.detail
    assert second_backend.session_state is SessionState.AUTHORITY_HELD_NOT_OURS


# ---------------------------------------------------------------------------
# The 30476 capability probe
# ---------------------------------------------------------------------------

def test_the_probe_changes_the_value_reads_it_back_and_restores_it():
    backend, app, session = make(clean())

    result = session.probe_priority_mode()

    assert result.ok is True
    assert backend.priority_mode_capability is PriorityModeCapability.CONFIRMED_WRITABLE
    priority_writes = [v for reg, v in app.writes if reg == REG_PRIORITY_MODE]
    assert priority_writes == [1, 0]                  # probed, then restored
    assert app.registers[REG_PRIORITY_MODE] == 0      # exactly as found


def test_a_write_that_does_not_change_the_value_is_not_writable():
    """Write-same proves the address accepts FC06, nothing more."""
    backend, app, session = make(clean())
    app.ignore_writes.add(REG_PRIORITY_MODE)          # accepted, value unchanged

    result = session.probe_priority_mode()

    assert backend.priority_mode_capability is PriorityModeCapability.WRITE_ACCEPTED
    assert result.ok is True                          # nothing to restore
    assert app.registers[REG_PRIORITY_MODE] == 0


def test_a_rejected_probe_leaves_the_inverter_untouched():
    backend, app, session = make(clean())
    app.fail_writes[REG_PRIORITY_MODE] = ValueError("Illegal Function")

    result = session.probe_priority_mode()

    assert backend.priority_mode_capability is PriorityModeCapability.REJECTED
    assert app.writes == []
    assert result.ok is True                          # nothing was changed


def test_a_failed_restore_is_critical_and_latches_degraded():
    """Leaving the base mode changed is the one outcome that must be loud."""
    backend, app, session = make(clean())

    original_execute = backend.executor.execute
    seen = {"n": 0}

    def execute(step):
        if step.register == REG_PRIORITY_MODE:
            seen["n"] += 1
            if seen["n"] == 2:                        # the restore write
                return StepResult.FAILED
        return original_execute(step)

    backend.executor.execute = execute

    result = session.probe_priority_mode()

    assert result.ok is False
    assert session.degraded is True
    assert "CRITICAL" in app.levels()
    assert any("NOT RESTORED" in m for m, _lvl in app.logs)

    # And the latch holds: nothing further is attempted except release.
    assert session.hold().refused is True


def test_the_probe_obeys_the_same_interlock_as_every_other_operation():
    _backend, app, session = make({REG_CONTROL_AUTHORITY: 1,
                                   REG_REMOTE_ENABLE: 1,
                                   REG_PRIORITY_MODE: 0})

    result = session.probe_priority_mode()

    assert result.refused is True
    assert app.writes == []


# ---------------------------------------------------------------------------
# Guard rails on the session object itself
# ---------------------------------------------------------------------------

def test_a_confirmed_writable_30476_does_not_change_what_hold_writes():
    """Writability establishes a fact, not a licence to spend it.

    The first supervised hold must exercise the minimal VPP path — 30408,
    30409, 30100, 30407 — and nothing else. 30476 is reserved for the later
    grid-charge experiment, which is the only place a hypothesis needs it.
    """
    backend, app, session = make(clean())
    probe = session.probe_priority_mode()
    assert probe.ok is True
    assert backend.priority_mode_capability is PriorityModeCapability.CONFIRMED_WRITABLE
    writes_before = len(app.writes)

    result = session.hold(duration_minutes=5)

    assert result.ok is True
    assert [reg for reg, _v in app.writes[writes_before:]] == [
        REG_REMOTE_DURATION,
        REG_REMOTE_POWER,
        REG_CONTROL_AUTHORITY,
        REG_REMOTE_ENABLE,
    ]


def test_a_session_refuses_everything_on_a_non_commissioning_backend():
    app = FakeApp(clean())
    config = BatteryOptimizerConfig(device_id="dev", control_mode="read_only")
    backend = UpstreamVppBackend(app, config,
                                 executor=build_executor(app, config))
    session = CommissioningSession(backend, log_func=app.log)

    for result in (session.hold(), session.renew(),
                   session.probe_priority_mode(), session.release()):
        assert result.refused is True
        assert "not in commissioning mode" in result.detail
    assert app.writes == []


# ---------------------------------------------------------------------------
# The watchdog test: the session nobody renews
# ---------------------------------------------------------------------------

def expiring_waiter(backend, app, window_seconds, clear_authority=True):
    """A waiter whose inverter ends the session itself once the window passes.

    The plain FakeApp never expires anything -- it only holds what was written
    -- so proving the CONFIRMED branch needs hardware that behaves like a
    watchdog. ``clear_authority`` covers both shapes the expiry could take:
    30407 alone, or 30100 with it.
    """
    base = make_waiter(backend, app)
    elapsed = {"seconds": 0}

    def wait(seconds):
        base(seconds)
        elapsed["seconds"] += seconds
        if elapsed["seconds"] >= window_seconds:
            app.registers[REG_REMOTE_ENABLE] = 0
            if clear_authority:
                app.registers[REG_CONTROL_AUTHORITY] = 0
    return wait


def test_the_watchdog_test_never_renews_the_session_it_opened():
    """The whole point. A renewal would measure the wrong thing entirely."""
    backend, app, session = make(clean())

    session.watchdog_test(wait=make_waiter(backend, app), duration_minutes=1,
                          observe_seconds=90, poll_seconds=5)

    assert [r.operation for r in session.history] == [
        "timed_hold", "release", "watchdog_test"]
    assert (REG_REMOTE_DURATION, 1) in app.writes
    # One arm, and no second one: the duration register is written exactly
    # once, by the hold.
    assert [reg for reg, _v in app.writes].count(REG_REMOTE_DURATION) == 1


def test_a_session_that_expires_on_its_own_is_the_watchdog_confirmed():
    backend, app, session = make(clean())

    result = session.watchdog_test(
        wait=expiring_waiter(backend, app, window_seconds=60),
        duration_minutes=1, observe_seconds=90, poll_seconds=5)

    assert result.ok is True
    assert "watchdog CONFIRMED" in result.detail
    assert app.registers[REG_REMOTE_ENABLE] == 0
    assert app.registers[REG_CONTROL_AUTHORITY] == 0


def test_a_session_that_never_expires_is_reported_as_unproven():
    """The finding that would matter most, and the one this fake produces:
    nothing ends the session but us."""
    backend, app, session = make(clean())

    result = session.watchdog_test(wait=make_waiter(backend, app),
                                   duration_minutes=1, observe_seconds=90,
                                   poll_seconds=5)

    assert "watchdog DID NOT FIRE" in result.detail
    # The observation completed, so the test itself did its job -- and the
    # inverter is still handed back by us rather than left armed.
    assert result.ok is True
    assert backend.session_state is SessionState.RELEASED
    assert app.registers[REG_REMOTE_ENABLE] == 0
    assert app.registers[REG_CONTROL_AUTHORITY] == 0


def test_an_expiry_that_leaves_authority_set_is_still_released_by_us():
    """1/0 after expiry is the VPP standby hazard, not a finished session."""
    backend, app, session = make(clean())

    result = session.watchdog_test(
        wait=expiring_waiter(backend, app, window_seconds=60,
                             clear_authority=False),
        duration_minutes=1, observe_seconds=90, poll_seconds=5)

    assert "watchdog CONFIRMED" in result.detail
    assert (REG_CONTROL_AUTHORITY, 0) in app.writes
    assert app.registers[REG_CONTROL_AUTHORITY] == 0
    assert backend.session_state is SessionState.RELEASED


def test_the_observation_must_outlast_the_window_it_watches():
    backend, app, session = make(clean())

    result = session.watchdog_test(wait=make_waiter(backend, app),
                                   duration_minutes=1, observe_seconds=60)

    assert result.refused is True
    assert "cannot see a 60s session expire" in result.detail
    assert app.writes == []


def test_the_watchdog_test_refuses_a_zero_duration_too():
    backend, app, session = make(clean())

    result = session.watchdog_test(wait=make_waiter(backend, app),
                                   duration_minutes=0, observe_seconds=90)

    assert result.refused is True
    assert "has not demonstrated" in result.detail
    assert app.writes == []


def test_the_watchdog_test_obeys_the_external_scheduler_interlock():
    backend, app, session = make(external_scheduler(16))

    result = session.watchdog_test(wait=make_waiter(backend, app),
                                   duration_minutes=1, observe_seconds=90)

    assert result.refused is True
    assert "EXTERNAL SCHEDULER" in result.detail
    assert app.writes == []


def test_an_interrupted_watchdog_test_still_hands_the_inverter_back():
    backend, app, session = make(clean())
    base = make_waiter(backend, app)
    calls = {"n": 0}

    def wait(seconds):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt()
        base(seconds)

    result = session.watchdog_test(wait=wait, duration_minutes=1,
                                   observe_seconds=90, poll_seconds=5)

    assert result.ok is False
    assert "ABORTED by KeyboardInterrupt" in result.detail
    assert app.registers[REG_CONTROL_AUTHORITY] == 0
    assert app.registers[REG_REMOTE_ENABLE] == 0


def test_every_observation_carries_the_measured_effect():
    """The registers say what the inverter was told; only these say what it
    did. Sampling them costs nothing while the test is waiting anyway, and it
    is the only evidence of what +1% does physically."""
    backend, app, session = make(clean())

    session.watchdog_test(wait=make_waiter(backend, app), duration_minutes=1,
                          observe_seconds=90, poll_seconds=45)

    polled = [line for line in session.observations if line.startswith("t=")]
    assert len(polled) == 3           # t=0, t=45, t=90
    for line in polled:
        assert "bat=" in line and "grid=" in line and "soc=" in line


def test_unreadable_power_sensors_are_named_rather_than_reported_as_zero():
    _backend, _app, session = make(clean())

    described = session._describe_power(InverterState(battery_power_w=-410.0,
                                                      grid_import_power_w=57.0))

    assert "bat=-410W" in described
    assert "grid=57/?" in described    # export unread, not 0
    assert "soc=?" in described


def test_the_watchdog_verdict_needs_an_armed_session_to_watch():
    _backend, _app, session = make(clean())

    verdict = session._watchdog_verdict(
        [(0, InverterState(remote_enabled=0, control_authority=0)),
         (5, InverterState(remote_enabled=0, control_authority=0))], 60)

    assert "watchdog INCONCLUSIVE" in verdict
    assert "never read 1" in verdict


def test_the_watchdog_verdict_refuses_to_conclude_from_an_unreadable_inverter():
    _backend, _app, session = make(clean())

    verdict = session._watchdog_verdict([(0, None), (5, None)], 60)

    assert "watchdog INCONCLUSIVE" in verdict


def test_the_watchdog_verdict_brackets_the_disarm_and_names_the_overshoot():
    _backend, _app, session = make(clean())

    verdict = session._watchdog_verdict(
        [(0, InverterState(remote_enabled=1, control_authority=1)),
         (60, InverterState(remote_enabled=1, control_authority=1)),
         (65, InverterState(remote_enabled=0, control_authority=0))], 60)

    assert "watchdog CONFIRMED" in verdict
    assert "t=60s and t=65s" in verdict
    assert "+5s" in verdict
    assert "30100 dropped with it" in verdict


# ---------------------------------------------------------------------------
# The supervised discharge: the first operation with real power in it
# ---------------------------------------------------------------------------

def discharging(watts=-500.0, export=0.0, soc=80.0):
    """Telemetry for a FakeApp: the sensors the verdict reads."""
    return InverterState(battery_power_w=watts, grid_export_power_w=export,
                         grid_import_power_w=0.0, soc_percent=soc)


def make_metered(registers=None, battery_w=-500.0, export_w=0.0, soc=80.0):
    """A backend whose power sensors actually report something.

    `positive_is_charging` so the numbers in these tests are the numbers the
    backend sees; the reference WIT's own polarity is the opposite, and is
    normalized in the backend and covered by its own tests.
    """
    backend, app, session = make(
        registers,
        soc_sensor="sensor.soc",
        battery_power_sensor="sensor.battery",
        battery_power_direction="positive_is_charging",
        grid_import_power_sensor="sensor.grid_import",
        grid_export_power_sensor="sensor.grid_export",
    )
    readings = {"sensor.soc": soc, "sensor.battery": battery_w,
                "sensor.grid_import": 0.0, "sensor.grid_export": export_w}
    app.get_state = lambda entity: readings.get(entity)
    return backend, app, session, readings


def test_the_discharge_plan_writes_the_export_limit_and_nothing_else():
    """30200=1 / 30201=0 constrain the experiment; 30405, 30410 and 30476 are
    not part of it, and a discharge does not license them."""
    backend, _app, _session = make(clean())

    plan = backend.build_plan(InverterCommand(
        action=ControlAction.DISCHARGE_TO_LOAD, power_percent=5,
        duration_minutes=1, export_rate=0))

    assert [(s.register, s.value) for s in plan.steps] == [
        (REG_EXPORT_LIMIT_ENABLE, 1),
        (REG_EXPORT_LIMIT_RATE, 0),
        (REG_REMOTE_DURATION, 1),
        (REG_REMOTE_POWER, -5),          # the sign is the whole experiment
        (REG_CONTROL_AUTHORITY, 1),
        (REG_REMOTE_ENABLE, 1),          # ARM last, as always
    ]


def test_the_discharge_setpoint_is_written_negative():
    backend, app, session, _readings = make_metered(clean())

    session.discharge_test(wait=make_waiter(backend, app), power_percent=5,
                           duration_minutes=1, observe_seconds=10,
                           poll_seconds=5)

    assert (REG_REMOTE_POWER, -5) in app.writes


@pytest.mark.parametrize("percent", [11, 50, 100])
def test_a_supervised_discharge_is_bounded(percent):
    backend, app, session, _readings = make_metered(clean())

    result = session.discharge_test(wait=make_waiter(backend, app),
                                    power_percent=percent, duration_minutes=1,
                                    observe_seconds=10)

    assert result.refused is True
    assert "exceeds the supervised discharge maximum" in result.detail
    assert app.writes == []


def test_a_discharge_below_the_soc_floor_is_refused():
    """A battery near its reserve is not a test of anything."""
    backend, app, session, _readings = make_metered(clean(), soc=25.0)

    result = session.discharge_test(wait=make_waiter(backend, app),
                                    power_percent=5, duration_minutes=1,
                                    observe_seconds=10, min_soc_percent=30.0)

    assert result.refused is True
    assert "below the 30% floor" in result.detail
    assert app.writes == []


def test_an_unreadable_soc_refuses_rather_than_discharging_blind():
    backend, app, session, _readings = make_metered(clean(), soc=None)

    result = session.discharge_test(wait=make_waiter(backend, app),
                                    power_percent=5, duration_minutes=1,
                                    observe_seconds=10)

    assert result.refused is True
    assert "SOC is unreadable" in result.detail
    assert app.writes == []


def test_the_discharge_obeys_the_external_scheduler_interlock():
    backend, app, session, _readings = make_metered(external_scheduler(16))

    result = session.discharge_test(wait=make_waiter(backend, app),
                                    power_percent=5, duration_minutes=1,
                                    observe_seconds=10)

    assert result.refused is True
    assert "EXTERNAL SCHEDULER" in result.detail
    assert app.writes == []


def test_the_discharge_always_hands_the_inverter_back():
    backend, app, session, _readings = make_metered(clean())

    session.discharge_test(wait=make_waiter(backend, app), power_percent=5,
                           duration_minutes=1, observe_seconds=10,
                           poll_seconds=5)

    assert backend.session_state is SessionState.RELEASED
    assert app.registers[REG_CONTROL_AUTHORITY] == 0
    assert app.registers[REG_REMOTE_ENABLE] == 0


def test_an_interrupted_discharge_still_hands_the_inverter_back():
    backend, app, session, _readings = make_metered(clean())
    base = make_waiter(backend, app)
    calls = {"n": 0}

    def wait(seconds):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt()
        base(seconds)

    result = session.discharge_test(wait=wait, power_percent=5,
                                    duration_minutes=1, observe_seconds=60)

    assert result.ok is False
    assert "ABORTED by KeyboardInterrupt" in result.detail
    assert app.registers[REG_CONTROL_AUTHORITY] == 0
    assert app.registers[REG_REMOTE_ENABLE] == 0


# --- the verdict ----------------------------------------------------------

def verdict_for(before_w, during_w, export_w=0.0):
    _backend, _app, session = make(clean())
    samples = [(t, discharging(watts=during_w, export=export_w))
               for t in (0, 5, 10)]
    return session._discharge_verdict(
        discharging(watts=before_w), samples, 5,
        effect_threshold_w=150.0, export_tolerance_w=50.0)


def test_a_battery_that_discharges_harder_is_the_result_we_wanted():
    assert "discharge OBSERVED" in verdict_for(-500.0, -1100.0)


def test_a_command_that_changed_nothing_is_said_plainly():
    """Accepted, read back, and followed by nothing. That is a finding, and
    dressing it up as a pass would be the worst outcome available."""
    verdict = verdict_for(-500.0, -540.0)

    assert "NO MATERIAL CHANGE" in verdict
    assert "accepted and read back" in verdict


def test_a_battery_that_charges_instead_says_the_sign_is_wrong():
    assert "discharge INVERTED" in verdict_for(-500.0, +300.0)


def test_export_under_a_zero_export_limit_is_reported_as_our_misunderstanding():
    """Not a failure of the discharge command: the battery did what it was
    asked. It means 30200/30201 do not mean what this project assumes."""
    verdict = verdict_for(-500.0, -1100.0, export_w=800.0)

    assert "discharge OBSERVED" in verdict
    assert "EXPORT REACHED 800 W" in verdict
    assert "Do not raise the power" in verdict


def test_negligible_export_is_reported_as_negligible():
    assert "export stayed negligible" in verdict_for(-500.0, -1100.0,
                                                     export_w=12.0)


def test_the_verdict_refuses_to_conclude_without_readings():
    _backend, _app, session = make(clean())

    verdict = session._discharge_verdict(
        None, [(0, None)], 5, effect_threshold_w=150.0,
        export_tolerance_w=50.0)

    assert "discharge INCONCLUSIVE" in verdict
