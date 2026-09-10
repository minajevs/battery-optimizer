"""Contract for the baseline cleanup: 30200 and 30409 back to zero, safely.

A released session leaves its command fields behind on this hardware — 30409
keeps the last setpoint and 30200 keeps the last discharge's export limit — and
nothing in a release clears them. They are inert while 30100/30407 are 0, but a
test that starts from a stale setpoint cannot tell its own effect from the
residue of the previous one, which is what this exists to prevent.

The risk is that a "cleanup" is a write path, and write paths on this project
are gated by evidence rather than intent. So the refusals matter more than the
writes: anything armed, any foreign scheduler, or any unreadable register means
the registers are not ours to tidy.
"""
from __future__ import annotations

from battery_optimizer_lib.config import BatteryOptimizerConfig
from battery_optimizer_lib.control import InverterState, UpstreamVppBackend
from battery_optimizer_lib.control.upstream_vpp import (
    REG_CONTROL_AUTHORITY,
    REG_EXPORT_LIMIT_ENABLE,
    REG_REMOTE_ENABLE,
    REG_REMOTE_POWER,
    REG_TOU_NUM_PERIODS,
    RegisterWrite,
    StepResult,
)


class FakeApp:
    def __init__(self):
        self.logs = []

    def log(self, message, level="INFO"):
        self.logs.append((message, level))

    def get_state(self, entity):
        return None


class RecordingExecutor:
    """Records planned steps; performs no I/O."""

    name = "recording"
    can_write = True
    can_read = True

    def __init__(self):
        self.executed = []
        self.fail_on = set()

    def execute(self, step):
        self.executed.append(step)
        if isinstance(step, RegisterWrite) and step.register in self.fail_on:
            return StepResult.FAILED
        return StepResult.OK

    def read_registers(self, start, count):
        return [0] * count

    def sequence(self):
        return [(s.register, s.value) for s in self.executed
                if isinstance(s, RegisterWrite)]


def make_backend(**overrides):
    config = BatteryOptimizerConfig(device_id="dev123", **overrides)
    backend = UpstreamVppBackend(FakeApp(), config, executor=RecordingExecutor())
    return backend, backend.executor


def idle_state(**kwargs):
    base = dict(control_authority=0, remote_enabled=0, tou_period_count=0,
                export_limit_enabled=1, commanded_power=-3)
    base.update(kwargs)
    return InverterState(**base)


def _after(backend, **kwargs):
    """Make the verification read return a specific state."""
    backend.read_state = lambda: idle_state(**kwargs)


# --- the refusals ----------------------------------------------------------

def test_refuses_while_a_session_is_armed():
    backend, ex = make_backend()
    ok, detail = backend.clear_residual_setpoints(
        idle_state(control_authority=1, remote_enabled=1))
    assert ok is False
    assert "a session is armed" in detail
    assert ex.sequence() == []


def test_refuses_on_half_armed_authority():
    """30100=1/30407=0 is the documented hazard pair, not a tidy-up target."""
    backend, ex = make_backend()
    ok, _ = backend.clear_residual_setpoints(
        idle_state(control_authority=1, remote_enabled=0))
    assert ok is False
    assert ex.sequence() == []


def test_refuses_when_a_foreign_scheduler_is_loaded():
    backend, ex = make_backend()
    ok, detail = backend.clear_residual_setpoints(
        idle_state(tou_period_count=4))
    assert ok is False
    assert "30411" in detail
    assert ex.sequence() == []


def test_refuses_when_the_control_pair_is_unreadable():
    """Unknown is never treated as idle."""
    backend, ex = make_backend()
    ok, detail = backend.clear_residual_setpoints(
        idle_state(control_authority=None))
    assert ok is False
    assert "not known" in detail
    assert ex.sequence() == []


# --- the writes ------------------------------------------------------------

def test_writes_only_the_two_residual_registers():
    backend, ex = make_backend()
    _after(backend, export_limit_enabled=0, commanded_power=0)

    ok, _ = backend.clear_residual_setpoints(idle_state())

    assert ok is True
    assert ex.sequence() == [(REG_EXPORT_LIMIT_ENABLE, 0), (REG_REMOTE_POWER, 0)]
    written = {register for register, _ in ex.sequence()}
    assert REG_CONTROL_AUTHORITY not in written
    assert REG_REMOTE_ENABLE not in written
    assert REG_TOU_NUM_PERIODS not in written


def test_skips_registers_already_at_zero():
    """30200 is a STORAGE register; writing it unchanged spends EEPROM."""
    backend, ex = make_backend()
    _after(backend, export_limit_enabled=0, commanded_power=0)

    ok, detail = backend.clear_residual_setpoints(
        idle_state(export_limit_enabled=0))

    assert ok is True
    assert ex.sequence() == [(REG_REMOTE_POWER, 0)]
    assert "30200=0 already" in detail


def test_nothing_to_do_is_success_and_writes_nothing():
    backend, ex = make_backend()
    ok, detail = backend.clear_residual_setpoints(
        idle_state(export_limit_enabled=0, commanded_power=0))
    assert ok is True
    assert ex.sequence() == []
    assert "no writes needed" in detail


# --- verification ----------------------------------------------------------

def test_a_write_that_does_not_read_back_is_not_success():
    """Accepted is not applied — the 30476 probe exists because of that."""
    backend, _ex = make_backend()
    _after(backend, export_limit_enabled=1, commanded_power=0)

    ok, detail = backend.clear_residual_setpoints(idle_state())

    assert ok is False
    assert "NOT CONFIRMED" in detail
    assert "30200 still reads 1" in detail


def test_an_unreadable_inverter_after_writing_is_unconfirmed():
    backend, _ex = make_backend()
    backend.read_state = lambda: None

    ok, detail = backend.clear_residual_setpoints(idle_state())

    assert ok is False
    assert "UNCONFIRMED" in detail


def test_a_refused_write_fails_even_if_the_other_lands():
    backend, ex = make_backend()
    ex.fail_on = {REG_EXPORT_LIMIT_ENABLE}
    _after(backend, export_limit_enabled=1, commanded_power=0)

    ok, detail = backend.clear_residual_setpoints(idle_state())

    assert ok is False
    assert "FAILED" in detail
    assert ex.sequence() == [(REG_EXPORT_LIMIT_ENABLE, 0), (REG_REMOTE_POWER, 0)]
