"""Register-level contract for UpstreamVppBackend.

This is where the risk lives. Each documented hardware fact below cost a design
revision, and each has a test so it cannot be lost again:

* 30407 is ALWAYS the last write (arming order, wit-guide.md:145-155);
* authority is acquired LATE, and a failure to arm afterwards is the documented
  VPP standby hazard (30100=1 / 30407=0) that must never be left standing;
* the rollback out of that state CANNOT be immediate, because our own
  successful 30100=1 stamps the 30 s cooldown;
* HOLD is +1 %, never 0 (0 = "suspend forced cycle");
* battery polarity is normalized once, from declared configuration, so no
  per-model sign convention reaches the trading logic;
* grid direction comes from the always-positive import/export sensors, so the
  integration's `invert_grid_power` option cannot flip a verdict;
* 30411 (the inverter's own TOU schedule) is never written.

Nothing here performs I/O: the backend is driven through a recording executor.
"""

from __future__ import annotations

import pytest

from battery_optimizer_lib.config import BatteryOptimizerConfig
from battery_optimizer_lib.control import (
    ControlAction,
    EffectVerdict,
    InverterCommand,
    InverterState,
    SendResult,
    UpstreamVppBackend,
    VerifyVerdict,
    decode_signed,
    encode_unsigned,
)
from battery_optimizer_lib.control.upstream_vpp import (
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
    DryRunExecutor,
    HaReadOnlyExecutor,
    PriorityModeCapability,
    build_executor,
    RegisterWrite,
    SessionState,
    StepResult,
)


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class RecordingExecutor:
    """Executes nothing real; records planned steps and can fail on demand.

    Declares can_write so the backend treats its plans as applied — that is
    what lets the write-path state machine be tested without any I/O.
    """

    name = "recording"
    can_write = True
    can_read = True

    def __init__(self):
        self.executed = []
        self.fail_on = set()          # register numbers that fail
        self.registers = {}           # simulated read-back store

    def execute(self, step):
        self.executed.append(step)
        if isinstance(step, RegisterWrite) and step.register in self.fail_on:
            return StepResult.FAILED
        if isinstance(step, RegisterWrite):
            self.registers[step.register] = step.value
        return StepResult.OK

    def read_registers(self, start, count):
        return [self.registers.get(start + i, 0) for i in range(count)]

    # --- helpers ---
    def writes(self):
        return [s for s in self.executed if isinstance(s, RegisterWrite)]

    def sequence(self):
        return [(w.register, w.value) for w in self.writes()]

    def registers_written(self):
        return [w.register for w in self.writes()]


class FakeApp:
    def __init__(self):
        self.logs = []
        self.states = {}
        self._handle = 0
        self.run_in_calls = []

    def log(self, message, level="INFO"):
        self.logs.append((message, level))

    def get_state(self, entity):
        return self.states.get(entity)

    def run_in(self, callback, delay, **kwargs):
        self._handle += 1
        handle = f"timer_{self._handle}"
        self.run_in_calls.append((callback, delay, kwargs, handle))
        return handle

    def cancel_timer(self, handle):
        pass

    def levels(self):
        return [lvl for _m, lvl in self.logs]

    def fire_last_timer(self):
        callback, _delay, kwargs, _handle = self.run_in_calls[-1]
        callback(kwargs)


def make_backend(clock=None, **overrides):
    config = BatteryOptimizerConfig(device_id="dev123", **overrides)
    app = FakeApp()
    executor = RecordingExecutor()
    backend = UpstreamVppBackend(app, config, executor=executor, clock=clock)
    return backend, app, executor


def command(action, **kwargs):
    kwargs.setdefault("power_percent", 100)
    kwargs.setdefault("duration_minutes", 20)
    return InverterCommand(action=action, **kwargs)


# ---------------------------------------------------------------------------
# Per-action sequences
# ---------------------------------------------------------------------------

def test_grid_charge_sequence():
    backend, _app, ex = make_backend()

    assert backend.send(command(ControlAction.GRID_CHARGE,
                                charge_cutoff_soc=90)) is SendResult.CONFIRMED

    assert ex.sequence() == [
        (REG_AC_CHARGE_ENABLE, 1),        # grid charging needs AC charge on
        (REG_CHARGE_CUTOFF_SOC, 90),
        (REG_EXPORT_LIMIT_ENABLE, 0),
        (REG_REMOTE_DURATION, 20),
        (REG_REMOTE_POWER, 100),
        (REG_CONTROL_AUTHORITY, 1),
        (REG_REMOTE_ENABLE, 1),
    ]


def test_hold_uses_plus_one_percent_never_zero():
    """0 is documented as "suspend forced cycle" and clipped PV; +1 is HOLD."""
    backend, _app, ex = make_backend()

    backend.send(command(ControlAction.HOLD))

    power = [v for r, v in ex.sequence() if r == REG_REMOTE_POWER]
    assert power == [1]
    # HOLD must not enable AC charging.
    assert (REG_AC_CHARGE_ENABLE, 0) in ex.sequence()


def test_discharge_to_load_limits_export_to_zero():
    backend, _app, ex = make_backend()

    backend.send(command(ControlAction.DISCHARGE_TO_LOAD,
                         discharge_cutoff_soc=15))

    seq = ex.sequence()
    assert (REG_DISCHARGE_CUTOFF_SOC, 15) in seq
    assert (REG_EXPORT_LIMIT_ENABLE, 1) in seq
    assert (REG_EXPORT_LIMIT_RATE, 0) in seq        # zero export
    assert (REG_REMOTE_POWER, -100) in seq


def test_discharge_to_grid_uses_the_requested_export_rate():
    backend, _app, ex = make_backend()

    backend.send(command(ControlAction.DISCHARGE_TO_GRID, export_rate=60,
                         power_percent=80))

    seq = ex.sequence()
    assert (REG_EXPORT_LIMIT_ENABLE, 1) in seq
    assert (REG_EXPORT_LIMIT_RATE, 60) in seq
    assert (REG_REMOTE_POWER, -80) in seq


def test_max_export_removes_the_export_limit_and_drives_full_negative():
    backend, _app, ex = make_backend()

    backend.send(command(ControlAction.MAX_EXPORT, export_rate=100))

    seq = ex.sequence()
    assert (REG_EXPORT_LIMIT_ENABLE, 0) in seq
    assert (REG_REMOTE_POWER, -100) in seq


def test_export_rate_is_clamped_to_the_safe_range():
    """Negative 30201 triggers WIT warning 401; it must never be written."""
    backend, _app, ex = make_backend()

    backend.send(command(ControlAction.DISCHARGE_TO_GRID, export_rate=-50))

    rates = [v for r, v in ex.sequence() if r == REG_EXPORT_LIMIT_RATE]
    assert rates == [0]


def test_passthrough_releases_authority_and_does_not_leave_standby():
    clock = FakeClock()
    backend, _app, ex = make_backend(clock=clock)
    backend.send(command(ControlAction.HOLD))
    ex.executed.clear()
    clock.advance(31)          # a real slot is 15 min away

    assert backend.release() is SendResult.CONFIRMED

    assert (REG_CONTROL_AUTHORITY, 0) in ex.sequence()
    assert backend.session_state is SessionState.RELEASED


def test_passthrough_disarm_is_scheduled_not_slept_on():
    """The ~35 s settle must never block the AppDaemon callback thread."""
    clock = FakeClock()
    backend, app, ex = make_backend(clock=clock)
    backend.send(command(ControlAction.HOLD))
    clock.advance(31)
    backend.release()

    # 30407=0 has NOT been written yet — it is on a timer.
    assert (REG_REMOTE_ENABLE, 0) not in ex.sequence()
    assert app.run_in_calls, "disarm must be scheduled"
    assert app.run_in_calls[-1][1] == backend.config.release_settle_seconds

    app.fire_last_timer()
    assert (REG_REMOTE_ENABLE, 0) in ex.sequence()


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", [
    ControlAction.GRID_CHARGE,
    ControlAction.HOLD,
    ControlAction.DISCHARGE_TO_LOAD,
    ControlAction.DISCHARGE_TO_GRID,
    ControlAction.MAX_EXPORT,
])
def test_arming_is_always_the_last_write(action):
    """30407 last, every time. Arming first applies a stale 30409."""
    backend, _app, ex = make_backend()

    backend.send(command(action, export_rate=50))

    assert ex.sequence()[-1] == (REG_REMOTE_ENABLE, 1)


@pytest.mark.parametrize("action", [
    ControlAction.GRID_CHARGE,
    ControlAction.HOLD,
    ControlAction.DISCHARGE_TO_LOAD,
])
def test_duration_precedes_power_precedes_arm(action):
    """The documented write order: 30408 -> 30409 -> 30407."""
    backend, _app, ex = make_backend()

    backend.send(command(action))

    order = ex.registers_written()
    assert order.index(REG_REMOTE_DURATION) < order.index(REG_REMOTE_POWER)
    assert order.index(REG_REMOTE_POWER) < order.index(REG_REMOTE_ENABLE)


def test_authority_is_acquired_late_after_all_setpoints():
    """A partial failure must leave the override un-armed, not mis-armed."""
    backend, _app, ex = make_backend()

    backend.send(command(ControlAction.GRID_CHARGE, charge_cutoff_soc=90))

    order = ex.registers_written()
    authority_at = order.index(REG_CONTROL_AUTHORITY)
    for setpoint in (REG_REMOTE_DURATION, REG_REMOTE_POWER,
                     REG_AC_CHARGE_ENABLE, REG_CHARGE_CUTOFF_SOC):
        assert order.index(setpoint) < authority_at
    assert order.index(REG_REMOTE_ENABLE) == authority_at + 1


def test_policy_registers_are_applied_before_the_power_command():
    """Family transition: policy first, power command last."""
    clock = FakeClock()
    backend, _app, ex = make_backend(clock=clock)
    backend.send(command(ControlAction.GRID_CHARGE))
    ex.executed.clear()
    clock.advance(31)

    backend.send(command(ControlAction.HOLD))

    order = ex.registers_written()
    # AC charge must be turned off BEFORE the new power target is applied.
    assert order.index(REG_AC_CHARGE_ENABLE) < order.index(REG_REMOTE_POWER)


# ---------------------------------------------------------------------------
# Steady state / re-arm / write-on-change
# ---------------------------------------------------------------------------

def test_steady_state_rewrites_only_the_timed_registers():
    """Same action twice: policy registers are not rewritten."""
    clock = FakeClock()
    backend, _app, ex = make_backend(clock=clock)
    backend.send(command(ControlAction.DISCHARGE_TO_LOAD,
                         discharge_cutoff_soc=15))
    ex.executed.clear()
    clock.advance(31)

    backend.send(command(ControlAction.DISCHARGE_TO_LOAD,
                         discharge_cutoff_soc=15))

    assert ex.registers_written() == [
        REG_REMOTE_DURATION, REG_REMOTE_POWER, REG_REMOTE_ENABLE
    ]


def test_every_slot_rearms_even_when_the_session_is_active():
    """Nothing documents that rewriting 30408 renews a timed session."""
    clock = FakeClock()
    backend, _app, ex = make_backend(clock=clock)
    backend.send(command(ControlAction.HOLD))
    assert backend.session_state is SessionState.ACTIVE
    ex.executed.clear()
    clock.advance(31)

    backend.send(command(ControlAction.HOLD))

    assert (REG_REMOTE_ENABLE, 1) in ex.sequence()
    assert ex.sequence()[-1] == (REG_REMOTE_ENABLE, 1)


def test_authority_is_not_reacquired_while_the_session_is_active():
    clock = FakeClock()
    backend, _app, ex = make_backend(clock=clock)
    backend.send(command(ControlAction.HOLD))
    ex.executed.clear()
    clock.advance(31)

    backend.send(command(ControlAction.DISCHARGE_TO_LOAD))

    assert REG_CONTROL_AUTHORITY not in ex.registers_written()


def test_changed_policy_register_is_rewritten():
    clock = FakeClock()
    backend, _app, ex = make_backend(clock=clock)
    backend.send(command(ControlAction.DISCHARGE_TO_GRID, export_rate=50))
    ex.executed.clear()
    clock.advance(31)

    backend.send(command(ControlAction.DISCHARGE_TO_GRID, export_rate=70))

    assert (REG_EXPORT_LIMIT_RATE, 70) in ex.sequence()


# ---------------------------------------------------------------------------
# Signed values
# ---------------------------------------------------------------------------

def test_encode_unsigned_is_twos_complement():
    assert encode_unsigned(-100) == 65436
    assert encode_unsigned(-1) == 65535
    assert encode_unsigned(100) == 100


def test_decode_signed_round_trips():
    for value in (-100, -1, 0, 1, 100):
        assert decode_signed(encode_unsigned(value)) == value


def test_discharge_power_is_written_signed_negative():
    backend, _app, ex = make_backend()

    backend.send(command(ControlAction.DISCHARGE_TO_LOAD, power_percent=45))

    power = [v for r, v in ex.sequence() if r == REG_REMOTE_POWER]
    assert power == [-45]
    assert encode_unsigned(power[0]) == 65491


def test_read_state_decodes_signed_commanded_power():
    backend, _app, ex = make_backend()
    ex.registers[REG_REMOTE_POWER] = 65436   # -100 on the wire

    state = backend.read_state()

    assert state.commanded_power == -100


# ---------------------------------------------------------------------------
# Arming failure: OUR OWN command left half-applied
# ---------------------------------------------------------------------------

def test_arming_failure_after_authority_is_recovered_from():
    clock = FakeClock()
    backend, app, ex = make_backend(clock=clock)
    ex.fail_on = {REG_REMOTE_ENABLE}

    result = backend.send(command(ControlAction.HOLD))

    assert result is SendResult.FAILED
    assert backend.session_state is SessionState.ARM_FAILED_AUTHORITY_HELD
    assert "ERROR" in app.levels()
    assert backend.get_diagnostics()["arm_failures"] == 1
    # This is the ONE automatic recovery that survives: the trigger is our own
    # write result (30100 landed, 30407 did not), never an inference drawn
    # from finding two registers in a particular combination.
    assert app.run_in_calls


def test_arming_failure_rollback_is_scheduled_not_immediate():
    """Our own successful 30100=1 stamped the cooldown; 30100=0 is refused."""
    clock = FakeClock()
    backend, app, ex = make_backend(clock=clock)
    ex.fail_on = {REG_REMOTE_ENABLE}

    backend.send(command(ControlAction.HOLD))

    # No immediate revoke was attempted — it could not have landed.
    assert (REG_CONTROL_AUTHORITY, 0) not in ex.sequence()
    # A rollback is scheduled past the remaining cooldown.
    assert app.run_in_calls
    delay = app.run_in_calls[-1][1]
    assert delay >= backend.cooldown.seconds_remaining(REG_CONTROL_AUTHORITY)


def test_rollback_lands_after_the_cooldown_and_is_confirmed_by_read_back():
    clock = FakeClock()
    backend, app, ex = make_backend(clock=clock)
    ex.fail_on = {REG_REMOTE_ENABLE}
    backend.send(command(ControlAction.HOLD))

    clock.advance(35)          # cooldown has expired
    app.fire_last_timer()

    assert (REG_CONTROL_AUTHORITY, 0) in ex.sequence()
    assert backend.session_state is SessionState.RELEASED
    assert backend.get_diagnostics()["rollback_confirmed"] == 1


def test_rollback_retries_while_still_rate_limited():
    """A rollback attempted too early must reschedule, not give up."""
    clock = FakeClock()
    backend, app, ex = make_backend(clock=clock)
    ex.fail_on = {REG_REMOTE_ENABLE}
    backend.send(command(ControlAction.HOLD))

    app.fire_last_timer()      # fires immediately; cooldown still active

    assert backend.session_state is SessionState.ARM_FAILED_AUTHORITY_HELD
    assert backend.get_diagnostics()["rollback_attempts"] == 1
    assert len(app.run_in_calls) >= 2      # rescheduled

    clock.advance(35)
    app.fire_last_timer()
    assert backend.session_state is SessionState.RELEASED


def test_rollback_is_not_believed_without_read_back_confirmation():
    """A write that merely did not raise is not proof the hazard is cleared."""
    clock = FakeClock()
    backend, app, ex = make_backend(clock=clock)
    ex.fail_on = {REG_REMOTE_ENABLE}
    backend.send(command(ControlAction.HOLD))
    clock.advance(35)

    # Read-back keeps reporting authority held despite the write "succeeding".
    original_read = ex.read_registers
    ex.read_registers = lambda start, count: (
        [1] if start == REG_CONTROL_AUTHORITY else original_read(start, count)
    )

    app.fire_last_timer()

    assert backend.session_state is SessionState.ARM_FAILED_AUTHORITY_HELD
    assert backend.get_diagnostics()["rollback_confirmed"] == 0


def test_failure_before_authority_does_not_enter_the_hazard_state():
    """Only a failure AFTER authority was taken is the standby hazard."""
    backend, app, ex = make_backend()
    ex.fail_on = {REG_REMOTE_DURATION}

    result = backend.send(command(ControlAction.HOLD))

    assert result is SendResult.FAILED
    assert backend.session_state is SessionState.NOT_ARMED
    assert REG_CONTROL_AUTHORITY not in ex.registers_written()
    assert "CRITICAL" not in app.levels()


# ---------------------------------------------------------------------------
# Cooldown model
# ---------------------------------------------------------------------------

def test_cooldown_defers_a_second_write_to_the_same_register():
    clock = FakeClock()
    backend, _app, ex = make_backend(clock=clock)
    backend.send(command(ControlAction.HOLD))

    clock.advance(5)
    result = backend.send(command(ControlAction.DISCHARGE_TO_LOAD))

    assert result is SendResult.RATE_LIMITED
    assert backend.get_diagnostics()["rate_limited_steps"] >= 1


def test_cooldown_expires():
    clock = FakeClock()
    backend, _app, ex = make_backend(clock=clock)
    backend.send(command(ControlAction.HOLD))

    clock.advance(31)
    result = backend.send(command(ControlAction.DISCHARGE_TO_LOAD))

    assert result is SendResult.CONFIRMED


def test_cooldown_is_per_register_so_one_command_is_not_self_blocking():
    clock = FakeClock()
    backend, _app, ex = make_backend(clock=clock)

    assert backend.send(command(ControlAction.GRID_CHARGE,
                                charge_cutoff_soc=90)) is SendResult.CONFIRMED


# ---------------------------------------------------------------------------
# 30476 capability
# ---------------------------------------------------------------------------

def test_priority_mode_is_not_written_until_confirmed_writable():
    backend, _app, ex = make_backend()
    backend.priority_mode_capability = PriorityModeCapability.WRITE_ACCEPTED

    backend.send(command(ControlAction.GRID_CHARGE))

    assert REG_PRIORITY_MODE not in ex.registers_written()


def test_priority_mode_is_written_when_confirmed_writable():
    backend, _app, ex = make_backend()
    backend.priority_mode_capability = PriorityModeCapability.CONFIRMED_WRITABLE

    backend.send(command(ControlAction.GRID_CHARGE))

    assert (REG_PRIORITY_MODE, 1) in ex.sequence()


@pytest.mark.parametrize("action", [
    ControlAction.HOLD,
    ControlAction.DISCHARGE_TO_LOAD,
    ControlAction.DISCHARGE_TO_GRID,
    ControlAction.MAX_EXPORT,
])
def test_only_grid_charge_spends_30476(action):
    """Confirmed writable is a fact about the register, not a reason to write.

    30476 is a storage register that changes the inverter's base mode. The one
    action with a hypothesis attached to it — that Battery First is what lets a
    VPP grid charge actually import — is GRID_CHARGE, so that is the only
    action that spends it until hardware evidence says otherwise.
    """
    backend, _app, ex = make_backend()
    backend.priority_mode_capability = PriorityModeCapability.CONFIRMED_WRITABLE

    backend.send(command(action))

    assert REG_PRIORITY_MODE not in ex.registers_written()


def test_priority_mode_write_can_be_disabled_by_config():
    backend, _app, ex = make_backend(priority_mode_write="never")
    backend.priority_mode_capability = PriorityModeCapability.CONFIRMED_WRITABLE

    backend.send(command(ControlAction.GRID_CHARGE))

    assert REG_PRIORITY_MODE not in ex.registers_written()


def test_priority_mode_is_restored_on_release():
    clock = FakeClock()
    backend, _app, ex = make_backend(clock=clock)
    backend.priority_mode_capability = PriorityModeCapability.CONFIRMED_WRITABLE
    backend.send(command(ControlAction.GRID_CHARGE))
    ex.executed.clear()
    clock.advance(31)

    backend.release()

    assert (REG_PRIORITY_MODE, 0) in ex.sequence()


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def test_verify_matches_when_read_back_agrees():
    backend, _app, ex = make_backend()
    cmd = command(ControlAction.DISCHARGE_TO_LOAD)
    backend.send(cmd)

    state = backend.read_state()
    result = backend.verify(cmd, state)

    assert result.verdict is VerifyVerdict.MATCH


def test_verify_mismatches_when_power_differs():
    backend, _app, ex = make_backend()
    cmd = command(ControlAction.DISCHARGE_TO_LOAD)
    backend.send(cmd)
    ex.registers[REG_REMOTE_POWER] = 50      # someone else moved it

    result = backend.verify(cmd, backend.read_state())

    assert result.verdict is VerifyVerdict.MISMATCH
    assert "power" in result.detail


def test_verify_is_unverifiable_without_state():
    backend, _app, _ex = make_backend()

    result = backend.verify(command(ControlAction.HOLD), None)

    assert result.verdict is VerifyVerdict.UNVERIFIABLE


def test_authority_without_remote_is_reported_not_diagnosed():
    """1/0 is a register pair, not a verdict about what the inverter is doing.

    Upstream calls it "VPP standby" with local logic suspended; the reference
    WIT was observed at 1/0 discharging 3.8 kW and exporting 2.6 kW. The
    property therefore says only what it can see.
    """
    assert InverterState(control_authority=1,
                         remote_enabled=0).authority_without_remote
    assert not InverterState(control_authority=1,
                             remote_enabled=1).authority_without_remote
    assert not InverterState(control_authority=0,
                             remote_enabled=0).authority_without_remote


def test_authority_held_does_not_say_who_holds_it():
    assert InverterState(control_authority=1, remote_enabled=0).authority_held
    assert InverterState(control_authority=1, remote_enabled=1).authority_held
    assert not InverterState(control_authority=0, remote_enabled=1).authority_held


# ---------------------------------------------------------------------------
# EFFECT: proving we traded, not merely that the battery moved
# ---------------------------------------------------------------------------

def effect(backend, action, battery, imported=0.0, exported=0.0,
           **state_kwargs):
    """``battery`` is the NORMALIZED value: positive = charging.

    Grid flow is given as the two always-positive directional readings, which
    is the only form evaluate_effect accepts.
    """
    state = InverterState(battery_power_w=battery,
                          grid_import_power_w=imported,
                          grid_export_power_w=exported,
                          **state_kwargs)
    return backend.evaluate_effect(command(action), state)


def test_grid_charge_passes_only_with_grid_import():
    backend, _app, _ex = make_backend()
    # Charging AND importing: genuinely buying.
    assert effect(backend, ControlAction.GRID_CHARGE, 3000,
                  imported=3000) is EffectVerdict.PASS


def test_grid_charge_from_pv_surplus_is_indeterminate_not_pass():
    """PV 4 kW, house 1 kW, battery +3 kW, no import — nothing was bought."""
    backend, _app, _ex = make_backend()
    assert effect(backend, ControlAction.GRID_CHARGE, 3000) is EffectVerdict.INDETERMINATE


def test_grid_charge_not_charging_at_all_is_a_failure():
    """The #353 signature: registers correct, charge 0 W."""
    backend, _app, _ex = make_backend()
    assert effect(backend, ControlAction.GRID_CHARGE, 0) is EffectVerdict.FAIL


def test_grid_charge_at_full_soc_is_indeterminate_not_failure():
    """A full battery must never escalate a healthy system."""
    backend, _app, _ex = make_backend()
    verdict = effect(backend, ControlAction.GRID_CHARGE, 0,
                     soc_percent=100, charge_cutoff_soc=100)
    assert verdict is EffectVerdict.INDETERMINATE


def test_discharge_to_grid_absorbed_by_house_load_is_indeterminate():
    """Battery discharging 2 kW, house eating all of it — nothing was sold."""
    backend, _app, _ex = make_backend()
    assert effect(backend, ControlAction.DISCHARGE_TO_GRID, -2000) is EffectVerdict.INDETERMINATE


def test_discharge_to_grid_passes_when_exporting():
    backend, _app, _ex = make_backend()
    assert effect(backend, ControlAction.DISCHARGE_TO_GRID, -2000,
                  exported=1800) is EffectVerdict.PASS


def test_discharge_to_load_does_not_require_grid_flow():
    backend, _app, _ex = make_backend()
    assert effect(backend, ControlAction.DISCHARGE_TO_LOAD, -2000) is EffectVerdict.PASS


def test_discharge_at_min_soc_is_indeterminate_not_failure():
    backend, _app, _ex = make_backend()
    verdict = effect(backend, ControlAction.DISCHARGE_TO_LOAD, 0,
                     soc_percent=10, discharge_cutoff_soc=10)
    assert verdict is EffectVerdict.INDETERMINATE


def test_hold_passes_when_the_battery_is_idle():
    backend, _app, _ex = make_backend()
    assert effect(backend, ControlAction.HOLD, 50) is EffectVerdict.PASS
    assert effect(backend, ControlAction.HOLD, 3000) is EffectVerdict.FAIL


def test_effect_is_indeterminate_without_a_battery_power_reading():
    backend, _app, _ex = make_backend()
    assert effect(backend, ControlAction.GRID_CHARGE, None) is EffectVerdict.INDETERMINATE


@pytest.mark.parametrize("action", [
    ControlAction.HOLD,
    ControlAction.GRID_CHARGE,
    ControlAction.DISCHARGE_TO_LOAD,
    ControlAction.DISCHARGE_TO_GRID,
    ControlAction.MAX_EXPORT,
])
def test_missing_battery_telemetry_is_indeterminate_for_every_action(action):
    """A sensor that is gone is not evidence of anything, least of all failure.

    Escalation releases the session, so letting an unavailable sensor read as
    FAIL would let a restarting integration take the battery off the schedule.
    """
    backend, _app, _ex = make_backend()
    assert effect(backend, action, None) is EffectVerdict.INDETERMINATE


def test_missing_grid_telemetry_is_indeterminate_not_failure():
    """The battery is doing the right thing; only the PROOF of trade is absent."""
    backend, _app, _ex = make_backend()

    # Charging, but the import sensor is unavailable: not proof of buying.
    assert effect(backend, ControlAction.GRID_CHARGE, 3000,
                  imported=None) is EffectVerdict.INDETERMINATE
    # Discharging, but the export sensor is unavailable: not proof of selling.
    assert effect(backend, ControlAction.DISCHARGE_TO_GRID, -2000,
                  exported=None) is EffectVerdict.INDETERMINATE
    # DISCHARGE_TO_LOAD needs no grid flow at all, so it still passes.
    assert effect(backend, ControlAction.DISCHARGE_TO_LOAD, -2000,
                  exported=None) is EffectVerdict.PASS


def test_an_unreadable_inverter_never_produces_a_failure_verdict():
    backend, _app, _ex = make_backend()

    result = backend.verify(command(ControlAction.DISCHARGE_TO_GRID), None)

    assert result.verdict is VerifyVerdict.UNVERIFIABLE
    assert result.effect is EffectVerdict.INDETERMINATE


def test_battery_power_direction_is_strict_configuration():
    """No fallback: an assumed polarity inverts every trading verdict."""
    with pytest.raises(ValueError, match="battery_power_direction"):
        BatteryOptimizerConfig(battery_power_direction="negative")

    with pytest.raises(ValueError, match="no safe default"):
        BatteryOptimizerConfig(battery_power_direction="auto")

    # Both declared conventions are accepted unchanged.
    for direction in ("negative_is_charging", "positive_is_charging"):
        assert BatteryOptimizerConfig(
            battery_power_direction=direction).battery_power_direction == direction


def test_signed_grid_power_cannot_reach_a_trading_verdict():
    """The `invert_grid_power` defence, as an executable claim.

    Signed Grid Power is whatever the integration option says it is. Here it is
    set to the most misleading value available in each direction; the verdict
    must be decided purely by the directional readings.
    """
    backend, _app, _ex = make_backend()

    # Charging with a real import, while signed grid claims a big export.
    assert effect(backend, ControlAction.GRID_CHARGE, 3000, imported=3000,
                  grid_power_w=9999) is EffectVerdict.PASS
    # Discharging with a real export, while signed grid claims a big import.
    assert effect(backend, ControlAction.DISCHARGE_TO_GRID, -2000,
                  exported=1800, grid_power_w=-9999) is EffectVerdict.PASS
    # No directional flow at all is never a trade, whatever the signed value.
    assert effect(backend, ControlAction.DISCHARGE_TO_GRID, -2000,
                  grid_power_w=-9999) is EffectVerdict.INDETERMINATE


def test_battery_polarity_is_normalized_from_configuration():
    """The reference WIT reports negative while charging; others do not."""
    app = FakeServiceApp()

    negative = read_only_backend(app, battery_power_direction="negative_is_charging")
    assert negative._normalize_battery_power(-1500) == 1500     # charging
    assert negative._normalize_battery_power(37.2) == -37.2     # discharging

    positive = read_only_backend(app, battery_power_direction="positive_is_charging")
    assert positive._normalize_battery_power(-1500) == -1500
    assert positive._normalize_battery_power(37.2) == 37.2

    assert negative._normalize_battery_power(None) is None


def test_normalization_makes_a_real_wit_discharge_read_as_discharge():
    """The bug this slice exists to kill.

    On the reference hardware a successful discharge reports +3000 W. Under the
    old "positive = charging" assumption that scored as FAIL, so every working
    discharge looked like a failed command.
    """
    app = FakeServiceApp()
    backend = read_only_backend(app, battery_power_direction="negative_is_charging")

    normalized = backend._normalize_battery_power(3000)   # raw, discharging
    state = InverterState(battery_power_w=normalized, grid_export_power_w=2500)

    verdict = backend.evaluate_effect(
        command(ControlAction.DISCHARGE_TO_GRID), state)
    assert verdict is EffectVerdict.PASS


# ---------------------------------------------------------------------------
# Dry run: builds and logs, executes nothing
# ---------------------------------------------------------------------------

def test_dry_run_executes_no_writes_but_builds_the_full_plan():
    config = BatteryOptimizerConfig(device_id="dev123")
    app = FakeApp()
    backend = UpstreamVppBackend(app, config)     # default DryRunExecutor

    result = backend.send(command(ControlAction.GRID_CHARGE,
                                  charge_cutoff_soc=90))

    assert result is SendResult.DRY_RUN
    assert backend.dry_run is True
    # The plan was fully constructed...
    planned = [s.register for s in backend.executor.executed
               if isinstance(s, RegisterWrite)]
    assert planned[-1] == REG_REMOTE_ENABLE
    assert REG_CONTROL_AUTHORITY in planned
    # ...and logged.
    assert any("grid_charge" in m for m, _lvl in app.logs)


def test_dry_run_never_changes_session_state():
    config = BatteryOptimizerConfig(device_id="dev123")
    backend = UpstreamVppBackend(FakeApp(), config)

    backend.send(command(ControlAction.HOLD))

    assert backend.session_state is SessionState.NOT_ARMED


def test_dry_run_plan_is_logged_in_order():
    config = BatteryOptimizerConfig(device_id="dev123")
    app = FakeApp()
    backend = UpstreamVppBackend(app, config)

    plan = backend.build_plan(command(ControlAction.DISCHARGE_TO_LOAD,
                                      discharge_cutoff_soc=15))
    lines = plan.describe()

    assert lines[0].startswith("1. ")
    assert str(REG_REMOTE_ENABLE) in lines[-1]
    assert plan.arms_at == len(plan.steps) - 1


def test_dry_run_describes_negative_values_with_their_encoding():
    config = BatteryOptimizerConfig(device_id="dev123")
    backend = UpstreamVppBackend(FakeApp(), config)

    plan = backend.build_plan(command(ControlAction.MAX_EXPORT))
    text = " ".join(plan.describe())

    assert "-100" in text
    assert "65436" in text      # the two's-complement encoding is visible


# ---------------------------------------------------------------------------
# Slice 2: executor capabilities — writes must be structurally impossible
# ---------------------------------------------------------------------------

class FakeServiceApp(FakeApp):
    """FakeApp that also records call_service, for the read-only executor."""

    def __init__(self, response=None):
        super().__init__()
        self.response = response
        self.raise_on_call = None
        self.service_calls = []

    def call_service(self, service, **kwargs):
        self.service_calls.append((service, kwargs))
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return self.response


def test_read_only_executor_declares_no_write_capability():
    ex = HaReadOnlyExecutor(FakeServiceApp(), device_id="dev")
    assert ex.can_write is False
    assert ex.can_read is True


def test_read_only_executor_refuses_every_write_step():
    """There is no code path from this executor to a write service."""
    app = FakeServiceApp()
    ex = HaReadOnlyExecutor(app, device_id="dev", log_func=app.log)

    result = ex.execute(RegisterWrite(REG_REMOTE_ENABLE, 1))

    assert result is StepResult.FAILED
    assert ex.refused
    assert app.service_calls == []          # nothing was called at all
    assert "ERROR" in app.levels()


def test_read_only_executor_reads_holding_registers():
    app = FakeServiceApp(response={"success": True, "values": [1, 2, 3]})
    ex = HaReadOnlyExecutor(app, device_id="dev")

    values = ex.read_registers(30404, 3)

    assert values == [1, 2, 3]
    service, kwargs = app.service_calls[0]
    assert service == "growatt_modbus/get_register_data"
    assert kwargs["register_type"] == "holding"
    assert kwargs["start_address"] == 30404
    assert kwargs["count"] == 3
    assert kwargs["device_id"] == "dev"


def test_read_only_executor_handles_nested_and_failed_responses():
    ex = HaReadOnlyExecutor(FakeServiceApp(
        response={"result": {"success": True, "values": [7, 8]}}), device_id="d")
    assert ex.read_registers(30200, 2) == [7, 8]

    ex = HaReadOnlyExecutor(FakeServiceApp(
        response={"success": False, "values": []}), device_id="d")
    assert ex.read_registers(30200, 2) is None

    ex = HaReadOnlyExecutor(FakeServiceApp(response=None), device_id="d")
    assert ex.read_registers(30200, 2) is None


def test_read_only_executor_survives_a_raising_service():
    app = FakeServiceApp()
    app.raise_on_call = RuntimeError("modbus down")
    ex = HaReadOnlyExecutor(app, device_id="dev", log_func=app.log)

    assert ex.read_registers(30404, 8) is None
    assert "WARNING" in app.levels()


def test_read_only_executor_respects_the_50_register_cap():
    app = FakeServiceApp(response={"success": True, "values": [0] * 60})
    ex = HaReadOnlyExecutor(app, device_id="dev")

    assert ex.read_registers(30000, 60) is None
    assert app.service_calls == []


def test_backend_with_read_only_executor_plans_but_never_writes():
    app = FakeServiceApp(response={"success": True, "values": [0] * 8})
    config = BatteryOptimizerConfig(device_id="dev", control_mode="read_only")
    backend = UpstreamVppBackend(app, config, executor=build_executor(app, config))

    result = backend.send(command(ControlAction.GRID_CHARGE))

    assert result is SendResult.DRY_RUN
    assert backend.dry_run is True
    assert backend.can_read is True
    assert backend.session_state is SessionState.NOT_ARMED
    # Only reads were performed.
    assert all(s == "growatt_modbus/get_register_data"
               for s, _kw in app.service_calls)


def test_build_executor_fails_safe_on_an_unknown_mode():
    config = BatteryOptimizerConfig(device_id="dev")
    config.control_mode = "write_everything"   # bypass validation
    ex = build_executor(FakeServiceApp(), config)
    assert ex.can_write is False


def test_build_executor_defaults_to_dry_run():
    config = BatteryOptimizerConfig(device_id="dev")
    assert build_executor(FakeServiceApp(), config).name == "dry_run"


def test_control_status_shouts_dry_run_over_session_state():
    backend, _app, _ex = make_backend()
    backend.executor.can_write = False
    assert backend.control_status == "DRY_RUN"
    assert "DRY RUN" in backend.describe_mode()
    assert "no inverter writes are possible" in backend.describe_mode()


def test_control_status_reports_session_state_when_live():
    backend, _app, _ex = make_backend()
    assert backend.control_status == "NOT_ARMED"
    backend.send(command(ControlAction.HOLD))
    assert backend.control_status == "ACTIVE"


def test_decision_log_shows_actual_desired_and_proposed():
    """The commissioning line: what it is, what we want, what we would write."""
    app = FakeServiceApp(response={"success": True, "values": [0] * 8})
    config = BatteryOptimizerConfig(device_id="dev", control_mode="read_only")
    backend = UpstreamVppBackend(app, config, executor=build_executor(app, config))

    backend.send(command(ControlAction.GRID_CHARGE))

    text = " ".join(m for m, _lvl in app.logs)
    assert "DRY RUN  actual:" in text
    assert "DRY RUN  desired:" in text
    assert "proposed sequence" in text
    assert str(REG_REMOTE_ENABLE) in text


def test_decision_log_warns_when_the_inverter_is_already_in_standby():
    app = FakeServiceApp()
    config = BatteryOptimizerConfig(device_id="dev", control_mode="read_only")
    backend = UpstreamVppBackend(app, config, executor=build_executor(app, config))
    # 30100=1 and 30407=0 -> the documented hazard.
    backend.read_state = lambda: InverterState(control_authority=1,
                                               remote_enabled=0)

    backend.send(command(ControlAction.HOLD))

    assert any("VPP standby" in m and lvl == "WARNING" for m, lvl in app.logs)


# ---------------------------------------------------------------------------
# Slice 2: the release lifecycle
# ---------------------------------------------------------------------------

def test_release_completes_immediately_when_the_cooldown_allows():
    clock = FakeClock()
    backend, _app, ex = make_backend(clock=clock)
    backend.send(command(ControlAction.HOLD))
    clock.advance(31)

    assert backend.release() is SendResult.CONFIRMED
    assert backend.session_state is SessionState.RELEASED
    assert backend.session_state.safe_to_stop is True


def test_release_inside_the_cooldown_is_pending_not_failed():
    """The normal-operation case: disable the optimizer right after a slot."""
    clock = FakeClock()
    backend, app, ex = make_backend(clock=clock)
    backend.send(command(ControlAction.HOLD))
    clock.advance(2)                      # user disables it 2 s later

    result = backend.release()

    assert result is SendResult.PENDING
    assert backend.session_state is SessionState.RELEASE_PENDING
    assert backend.session_state.safe_to_stop is False
    assert any("do not stop AppDaemon" in m for m, _lvl in app.logs)
    assert app.run_in_calls                # a retry is scheduled


def test_pending_release_completes_on_the_scheduled_retry():
    clock = FakeClock()
    backend, app, ex = make_backend(clock=clock)
    backend.send(command(ControlAction.HOLD))
    clock.advance(2)
    backend.release()

    clock.advance(31)
    app.fire_last_timer()

    assert backend.session_state is SessionState.RELEASED
    assert (REG_CONTROL_AUTHORITY, 0) in ex.sequence()
    assert backend.get_diagnostics()["release_deferred"] == 1


def test_release_is_not_believed_without_read_back_confirmation():
    clock = FakeClock()
    backend, app, ex = make_backend(clock=clock)
    backend.send(command(ControlAction.HOLD))
    clock.advance(31)

    # Read-back keeps insisting authority is held.
    original = ex.read_registers
    ex.read_registers = lambda start, count: (
        [1] if start == REG_CONTROL_AUTHORITY else original(start, count)
    )

    result = backend.release()

    assert result is SendResult.PENDING
    assert backend.session_state is SessionState.RELEASE_PENDING


def test_release_when_never_armed_is_a_no_op():
    backend, _app, ex = make_backend()

    assert backend.release() is SendResult.CONFIRMED
    assert ex.executed == []


def test_release_restores_base_settings_before_revoking_authority():
    clock = FakeClock()
    backend, _app, ex = make_backend(clock=clock)
    backend.priority_mode_capability = PriorityModeCapability.CONFIRMED_WRITABLE
    backend.send(command(ControlAction.GRID_CHARGE))
    ex.executed.clear()
    clock.advance(31)

    backend.release()

    order = ex.registers_written()
    assert order.index(REG_PRIORITY_MODE) < order.index(REG_CONTROL_AUTHORITY)


def test_safe_to_stop_is_false_in_both_dangerous_states():
    assert SessionState.RELEASE_PENDING.safe_to_stop is False
    assert SessionState.ARM_FAILED_AUTHORITY_HELD.safe_to_stop is False
    assert SessionState.ACTIVE.safe_to_stop is False
    assert SessionState.RELEASED.safe_to_stop is True
    assert SessionState.NOT_ARMED.safe_to_stop is True


# ---------------------------------------------------------------------------
# Slice 2: raw telemetry in diagnostics (sign-convention verification)
# ---------------------------------------------------------------------------

def test_diagnostics_publish_raw_beside_normalized_telemetry():
    """Raw next to normalized is what makes a wrong polarity visible in HA."""
    app = FakeServiceApp(response={"success": True, "values": [0] * 8})
    config = BatteryOptimizerConfig(device_id="dev", control_mode="read_only")
    app.states[config.battery_power_sensor] = "-1500"      # raw: CHARGING
    app.states[config.grid_import_power_sensor] = "1600"
    app.states[config.grid_export_power_sensor] = "0"
    app.states[config.grid_power_sensor] = "1200"          # diagnostic only
    backend = UpstreamVppBackend(app, config, executor=build_executor(app, config))

    backend.read_state()
    diag = backend.get_diagnostics()

    assert diag["battery_power_raw_w"] == -1500
    assert diag["battery_power_normalized_w"] == 1500      # positive = charging
    assert diag["battery_power_direction"] == "negative_is_charging"
    assert diag["grid_import_power_w"] == 1600
    assert diag["grid_export_power_w"] == 0
    assert diag["grid_power_signed_w_diagnostic_only"] == 1200
    assert diag["battery_power_sensor"] == config.battery_power_sensor
    assert diag["control_status"] == "DRY_RUN"
    assert diag["executor"] == "ha_read_only"


def test_diagnostics_publish_tou_state_and_that_the_fallback_is_off():
    app = FakeServiceApp()
    backend = read_only_backend(app, {30411: 15})

    backend.read_state()
    diag = backend.get_diagnostics()

    assert diag["tou_period_count"] == 15
    assert diag["tou_fallback"] == "disabled"


def test_diagnostics_expose_the_stop_safety_flag():
    backend, _app, _ex = make_backend()
    assert backend.get_diagnostics()["safe_to_stop"] is True
    backend.send(command(ControlAction.HOLD))
    assert backend.get_diagnostics()["safe_to_stop"] is False


# ---------------------------------------------------------------------------
# Slice 2: startup reconciliation
# ---------------------------------------------------------------------------

def read_only_backend(app, registers=None, **overrides):
    config = BatteryOptimizerConfig(device_id="dev", control_mode="read_only",
                                    **overrides)
    regs = registers or {}

    class Executor(HaReadOnlyExecutor):
        def read_registers(self, start, count):
            return [regs.get(start + i, 0) for i in range(count)]

    executor = Executor(app, device_id="dev", log_func=app.log)
    return UpstreamVppBackend(app, config, executor=executor)


def test_reconcile_seeds_the_write_on_change_cache():
    """Registers already holding the right value must not be rewritten."""
    app = FakeServiceApp()
    backend = read_only_backend(app, {
        30404: 90, 30405: 15, 30410: 1, 30411: 0, 30476: 0,
    })

    backend.reconcile()
    plan = backend.build_plan(command(ControlAction.GRID_CHARGE,
                                      charge_cutoff_soc=90))

    written = [s.register for s in plan.steps if isinstance(s, RegisterWrite)]
    assert REG_CHARGE_CUTOFF_SOC not in written   # already 90
    assert REG_AC_CHARGE_ENABLE not in written    # already 1
    assert REG_REMOTE_ENABLE in written           # always re-armed


def test_reconcile_does_not_adopt_a_session_it_did_not_open():
    """1/1 at startup belongs to somebody else until proven otherwise.

    reconcile() only ever runs before this process has armed anything, so
    calling an inherited session ACTIVE would claim ownership we cannot prove —
    Growatt Smart Scheduling moves 30100 on its own.
    """
    app = FakeServiceApp()
    backend = read_only_backend(app, {30100: 1, 30407: 1})

    backend.reconcile()

    assert backend.session_state is SessionState.AUTHORITY_HELD_NOT_OURS
    assert any("did not take it" in m for m, _lvl in app.logs)
    # We hold nothing, so there is no unfinished handover of ours.
    assert backend.session_state.safe_to_stop is True


def test_reconcile_warns_about_the_documented_discrepancy_at_one_zero():
    """1/0 found on the inverter is reported, and nothing more.

    Upstream documents the pair as VPP standby with local battery logic
    suspended. This hardware was observed in it while discharging and
    exporting, so the disagreement is surfaced as a warning rather than
    resolved by guessing which description is right.
    """
    app = FakeServiceApp()
    backend = read_only_backend(app, {30100: 1, 30407: 0})

    backend.reconcile()

    assert backend.session_state is SessionState.AUTHORITY_HELD_NOT_OURS
    assert any("discrepancy is unresolved" in m.lower() for m, _lvl in app.logs)
    assert "WARNING" in app.levels()
    assert "CRITICAL" not in app.levels()   # not a hazard on this hardware


def test_reconcile_never_recovers_from_one_zero_even_when_it_could_write():
    """No automatic release on the strength of the register pair alone.

    The previous rule was "roll back whenever writes are possible". Since 1/0
    is no longer evidence that anything is wrong, capability is beside the
    point: finding two registers in a combination we did not create is not a
    licence to change them.
    """
    backend, app, _ex = make_backend()      # RecordingExecutor: can_write
    backend.executor.registers.update({30100: 1, 30407: 0})

    backend.reconcile()

    assert backend.session_state is SessionState.AUTHORITY_HELD_NOT_OURS
    assert app.run_in_calls == []           # nothing scheduled
    assert backend.executor.executed == []  # nothing written


def test_reconcile_keeps_our_own_failed_arm_rather_than_calling_it_someone_elses():
    """1/0 that WE created stays ours across a re-read."""
    backend, _app, ex = make_backend()
    ex.fail_on = {REG_REMOTE_ENABLE}
    backend.send(command(ControlAction.HOLD))
    assert backend.session_state is SessionState.ARM_FAILED_AUTHORITY_HELD

    ex.registers.update({30100: 1, 30407: 0})
    backend.reconcile()

    assert backend.session_state is SessionState.ARM_FAILED_AUTHORITY_HELD


# ---------------------------------------------------------------------------
# The inverter's own TOU schedule is never written
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", [
    ControlAction.GRID_CHARGE,
    ControlAction.HOLD,
    ControlAction.DISCHARGE_TO_LOAD,
    ControlAction.DISCHARGE_TO_GRID,
    ControlAction.MAX_EXPORT,
    ControlAction.PASSTHROUGH,
])
def test_no_plan_ever_writes_the_tou_schedule(action):
    """30411 held 15 periods of unknown origin on the reference unit.

    A timed VPP override returns to the base schedule when it expires, so
    clearing it is never required — and it is not ours to destroy.
    """
    backend, _app, _ex = make_backend()
    backend.session_state = SessionState.ACTIVE   # exercise the release path too

    plan = backend.build_plan(command(action))

    written = [s.register for s in plan.steps if isinstance(s, RegisterWrite)]
    assert REG_TOU_NUM_PERIODS not in written


# ---------------------------------------------------------------------------
# The external-scheduler interlock (30411 > 0)
# ---------------------------------------------------------------------------

def with_external_schedule(periods=16, **overrides):
    backend, app, ex = make_backend(**overrides)
    ex.registers[REG_TOU_NUM_PERIODS] = periods
    return backend, app, ex


@pytest.mark.parametrize("action", [
    ControlAction.GRID_CHARGE,
    ControlAction.HOLD,
    ControlAction.DISCHARGE_TO_LOAD,
    ControlAction.DISCHARGE_TO_GRID,
    ControlAction.MAX_EXPORT,
])
def test_a_second_schedulers_schedule_blocks_every_session_holding_action(action):
    """30411 > 0 is the one register that identifies a second scheduler.

    Growatt Smart Scheduling wrote 16 periods on the reference WIT and cleared
    them when switched off (2026-09-03). Nothing here authors a TOU period, so
    a non-zero count is somebody else's schedule, and two schedulers driving
    one inverter is not a state this project commands into.
    """
    backend, _app, ex = with_external_schedule(16)

    result = backend.send(command(action))

    assert result is SendResult.FAILED
    assert ex.writes() == []
    assert backend.get_diagnostics()["external_scheduler_refusals"] == 1


def test_the_interlock_says_what_it_found_and_what_to_do():
    backend, app, _ex = with_external_schedule(16)

    backend.send(command(ControlAction.GRID_CHARGE))

    message = "\n".join(m for m, _lvl in app.logs)
    assert "30411" in message
    assert "16" in message
    assert "ERROR" in app.levels()


def test_passthrough_is_exempt_so_an_interlock_cannot_trap_a_session_open():
    """Handing the inverter back is exactly what must stay possible."""
    backend, _app, ex = with_external_schedule(16)

    result = backend.send(command(ControlAction.PASSTHROUGH))

    assert result is SendResult.CONFIRMED
    assert (REG_CONTROL_AUTHORITY, 0) in ex.sequence()


def test_release_is_not_blocked_by_a_schedule_that_appears_mid_session():
    clock = FakeClock()
    backend, _app, ex = make_backend(clock=clock)
    backend.send(command(ControlAction.HOLD))
    ex.registers[REG_TOU_NUM_PERIODS] = 16       # somebody switched it on
    ex.executed.clear()
    clock.advance(31)

    result = backend.release()

    assert result is SendResult.CONFIRMED
    assert (REG_CONTROL_AUTHORITY, 0) in ex.sequence()


def test_the_interlock_re_reads_rather_than_trusting_a_cached_count():
    """A count from the previous slot cannot see a scheduler switched on since."""
    backend, _app, ex = make_backend()
    backend.last_tou_period_count = 0            # stale: read an hour ago
    ex.registers[REG_TOU_NUM_PERIODS] = 16

    assert backend.send(command(ControlAction.HOLD)) is SendResult.FAILED


def test_a_dropped_read_falls_back_to_the_last_count_rather_than_clearing():
    """An unreadable 30411 must not be what lifts the interlock."""
    backend, _app, ex = make_backend()
    backend.last_tou_period_count = 16
    ex.read_registers = lambda start, count: None

    assert backend.send(command(ControlAction.HOLD)) is SendResult.FAILED


def test_a_cleared_schedule_lifts_the_interlock_without_a_reset():
    """It is a condition of the inverter, not a latch to be cleared."""
    backend, _app, ex = with_external_schedule(16)
    assert backend.send(command(ControlAction.HOLD)) is SendResult.FAILED

    ex.registers[REG_TOU_NUM_PERIODS] = 0
    result = backend.send(command(ControlAction.HOLD))

    assert result is SendResult.CONFIRMED
    assert backend.session_state is SessionState.ACTIVE


def test_dry_run_still_plans_a_command_the_interlock_would_refuse():
    """Nothing is transmitted in dry run, so there is nothing to interlock.

    Suppressing the plan would remove the log line that shows what WOULD be
    sent, which is the whole value of the mode.
    """
    app = FakeApp()
    backend = UpstreamVppBackend(
        app, BatteryOptimizerConfig(device_id="dev123"),
        executor=DryRunExecutor(app.log))
    backend.last_tou_period_count = 16

    result = backend.send(command(ControlAction.GRID_CHARGE))

    assert result is SendResult.DRY_RUN
    assert backend.get_diagnostics()["external_scheduler_refusals"] == 0


def test_reconcile_reports_a_schedule_it_did_not_write():
    app = FakeServiceApp()
    backend = read_only_backend(app, {30411: 16})

    backend.reconcile()

    warnings = [m for m, lvl in app.logs if lvl == "WARNING" and "30411" in m]
    assert warnings
    diag = backend.get_diagnostics()
    assert diag["external_scheduler_present"] is True
    assert diag["tou_period_count"] == 16


def test_a_cleared_schedule_is_not_reported_as_an_external_scheduler():
    app = FakeServiceApp()
    backend = read_only_backend(app, {30411: 0})

    backend.reconcile()

    assert backend.get_diagnostics()["external_scheduler_present"] is False


def test_authority_already_held_is_not_rewritten():
    """30100 is rate-limited; re-taking authority we have spends that for nothing."""
    app = FakeServiceApp()
    backend = read_only_backend(app, {30100: 1, 30407: 1})
    backend.reconcile()

    plan = backend.build_plan(command(ControlAction.HOLD))

    written = [s.register for s in plan.steps if isinstance(s, RegisterWrite)]
    assert REG_CONTROL_AUTHORITY not in written
    assert plan.authority_already_held is True
    assert plan.acquires_authority is False
    # Arming is still planned, and still last.
    assert written[-1] == REG_REMOTE_ENABLE


def test_reconcile_leaves_a_clean_inverter_not_armed():
    app = FakeServiceApp()
    backend = read_only_backend(app, {30100: 0, 30407: 0})

    backend.reconcile()

    assert backend.session_state is SessionState.NOT_ARMED
    assert backend.session_state.safe_to_stop is True


def test_reconcile_is_a_no_op_without_read_capability():
    backend, _app, _ex = make_backend()
    backend.executor.can_read = False

    assert backend.reconcile() is None
