"""The `live` executor: the only one that may write on the OPTIMIZER's say-so.

Everything else in control/ exists so that this can be switched on without
leaving the inverter in a state nobody owns. The tests here are about
PERMISSION, not about plans: who may initiate a write, which registers are
reachable, and — most importantly — that nothing becomes live by accident.
"""
from __future__ import annotations

import pytest

from battery_optimizer_lib.config import BatteryOptimizerConfig
from battery_optimizer_lib.control import UpstreamVppBackend, build_executor
from battery_optimizer_lib.control.upstream_vpp import (
    DryRunExecutor,
    HaCommissioningExecutor,
    HaLiveExecutor,
    HaReadOnlyExecutor,
    REG_TOU_NUM_PERIODS,
    RegisterWrite,
    StepResult,
)


class App:
    def __init__(self):
        self.logs = []
        self.calls = []

    def log(self, message, level="INFO"):
        self.logs.append((message, level))

    def get_state(self, entity):
        return None

    def call_service(self, service, **kwargs):
        self.calls.append((service, kwargs))
        return {"result": {"response": {"success": True, "values": [0]}}}


def backend_for(mode):
    app = App()
    config = BatteryOptimizerConfig(device_id="dev", control_mode=mode)
    return UpstreamVppBackend(app, config,
                              executor=build_executor(app, config)), app


# --- mode selection must be explicit ---------------------------------------

@pytest.mark.parametrize("mode,cls", [
    ("live", HaLiveExecutor),
    ("commissioning", HaCommissioningExecutor),
    ("read_only", HaReadOnlyExecutor),
])
def test_each_named_mode_selects_its_own_executor(mode, cls):
    app = App()
    config = BatteryOptimizerConfig(device_id="dev", control_mode=mode)
    assert isinstance(build_executor(app, config), cls)


@pytest.mark.parametrize("mode", [
    "", "Live", "LIVE", "automatic", "livee", "true", "yes", "dry_run", None])
def test_anything_unrecognised_falls_back_to_dry_run(mode):
    """A misconfiguration must never be what grants write authority."""
    app = App()
    config = BatteryOptimizerConfig(device_id="dev", control_mode=mode)
    assert isinstance(build_executor(app, config), DryRunExecutor)


# --- permission is declared positively, never inferred ---------------------

@pytest.mark.parametrize("mode,expected", [
    ("live", True),
    ("commissioning", False),
    ("read_only", False),
    ("dry_run", False),
    ("nonsense", False),
])
def test_only_live_allows_the_optimizer_to_drive_the_inverter(mode, expected):
    backend, _app = backend_for(mode)
    assert backend.automatic_writes_allowed is expected


def test_a_new_executor_is_not_live_by_saying_nothing():
    """The old definition was `not dry_run and not commissioning`, so any
    executor that was neither inherited unattended write authority by
    omission. Permission must be opt-in."""
    class Newcomer:
        name, can_write, can_read, commissioning = "newcomer", True, True, False
        # deliberately declares no `automatic`

    app = App()
    backend = UpstreamVppBackend(
        app, BatteryOptimizerConfig(device_id="dev", control_mode="live"),
        executor=Newcomer())

    assert backend.automatic_writes_allowed is False


# --- the register surface ---------------------------------------------------

def test_live_can_reach_the_registers_a_normal_plan_needs():
    """Commissioning refuses these; a real schedule slot needs them."""
    from battery_optimizer_lib.control.upstream_vpp import (
        REG_AC_CHARGE_ENABLE, REG_CHARGE_CUTOFF_SOC, REG_DISCHARGE_CUTOFF_SOC)

    live = HaLiveExecutor(App(), device_id="dev")
    for register in (REG_AC_CHARGE_ENABLE, REG_CHARGE_CUTOFF_SOC,
                     REG_DISCHARGE_CUTOFF_SOC):
        assert register in live.WRITABLE_REGISTERS
        assert register not in HaCommissioningExecutor.WRITABLE_REGISTERS


def test_live_still_never_writes_the_inverters_own_tou_schedule():
    """30411 is not ours. No plan in this project writes it, in any mode."""
    app = App()
    live = HaLiveExecutor(app, device_id="dev", log_func=app.log)

    assert REG_TOU_NUM_PERIODS not in live.WRITABLE_REGISTERS
    result = live.execute(RegisterWrite(REG_TOU_NUM_PERIODS, 4))

    assert result is StepResult.FAILED
    assert app.calls == [], "nothing may reach the inverter"
    assert any("not ours" in m for m, _lvl in app.logs)


def test_live_refuses_anything_that_is_not_a_register_write():
    app = App()
    live = HaLiveExecutor(app, device_id="dev", log_func=app.log)
    assert live.execute(object()) is StepResult.FAILED
    assert app.calls == []


def test_live_and_commissioning_share_one_write_implementation():
    """A renewal must take the same cooldown accounting and failure mapping as
    the original arm; a separate fast path would be a second write path."""
    assert HaLiveExecutor._write_register is HaCommissioningExecutor._write_register


def test_live_must_be_named_in_BOTH_gates_before_it_can_write():
    """config.CONTROL_MODES and build_executor() are separate on purpose.

    A mode has to appear in both before anything can write, so neither can
    grant authority by omission. `live` was added to build_executor() first
    and the config layer quietly kept coercing it to dry_run — which is the
    fail-safe working, and worth a test so it stays that way.
    """
    from battery_optimizer_lib.config import CONTROL_MODES

    assert "live" in CONTROL_MODES
    app = App()
    config = BatteryOptimizerConfig(device_id="dev", control_mode="live")
    assert config.control_mode == "live", "the config gate must not coerce it"
    assert isinstance(build_executor(app, config), HaLiveExecutor)


def test_a_mode_missing_from_the_config_gate_cannot_write_however_it_is_spelled():
    app = App()
    config = BatteryOptimizerConfig(device_id="dev", control_mode="live")
    config.control_mode = "not_a_mode"          # as a typo would leave it
    assert isinstance(build_executor(app, config), DryRunExecutor)
