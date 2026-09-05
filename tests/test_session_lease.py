"""A stranded session is recoverable only on durable evidence that it is ours.

The hardware watchdog does not exist: a session armed on the reference WIT was
still armed at t=90s of a 60 s window and only an explicit release ended it. So
a process that dies holding 30100=1 / 30407=1 leaves the inverter executing its
last command indefinitely, and the existing ownership rule -- a restart sees
authority it did not take and correctly refuses to touch it -- turns that into
a permanent strand.

The lease resolves exactly that and nothing more. It is not proof of ownership
and never promotes a session to ACTIVE; it says a previous instance started one
and has no record of finishing it, and it grants a single permission: release.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, "appdaemon/apps")
sys.path.insert(0, str(Path(__file__).parent))

from battery_optimizer_lib.config import BatteryOptimizerConfig
from battery_optimizer_lib.control import (
    CommissioningSession,
    SessionLease,
    SessionState,
    UpstreamVppBackend,
    build_executor,
)
from battery_optimizer_lib.control.lease import ACQUIRING, ACTIVE
from battery_optimizer_lib.control.upstream_vpp import (
    REG_CONTROL_AUTHORITY,
    REG_REMOTE_ENABLE,
    REG_REMOTE_POWER,
    REG_PRIORITY_MODE,
    REG_TOU_NUM_PERIODS,
)

from test_commissioning import FakeApp, FakeClock, clean, make_waiter


def make(tmp_path, registers=None, device_id="dev", lease_name="lease.json"):
    """A commissioning backend whose lease lives in a temp directory."""
    app = FakeApp(registers)
    clock = FakeClock()
    config = BatteryOptimizerConfig(
        device_id=device_id, control_mode="commissioning",
        session_lease_path=str(tmp_path / lease_name))
    backend = UpstreamVppBackend(app, config,
                                 executor=build_executor(app, config),
                                 clock=clock)
    backend.clock = clock
    return backend, app, CommissioningSession(backend, log_func=app.log)


def armed(power=1):
    """What a crashed process leaves behind: authority taken and armed."""
    registers = clean()
    registers[REG_CONTROL_AUTHORITY] = 1
    registers[REG_REMOTE_ENABLE] = 1
    registers[REG_REMOTE_POWER] = power
    return registers


# ---------------------------------------------------------------------------
# Writing the lease
# ---------------------------------------------------------------------------

def test_the_lease_is_written_before_the_authority_write(tmp_path):
    """The ordering IS the design. A crash between the lease and the authority
    write leaves a lease with nothing to clean up, which recovery discards; the
    other order leaves authority taken with no record of it."""
    backend, app, session = make(tmp_path, clean())
    path = Path(backend.lease.path)
    seen = {}

    original = app.call_service

    def watching(service, **kwargs):
        if (service.endswith("write_register")
                and kwargs.get("register") == REG_CONTROL_AUTHORITY
                and "authority" not in seen):
            seen["authority"] = path.exists()
        return original(service, **kwargs)

    app.call_service = watching
    session.hold(duration_minutes=5)

    assert seen["authority"] is True, "authority was taken before the lease existed"


def test_a_confirmed_session_records_what_it_asked_for(tmp_path):
    backend, _app, session = make(tmp_path, clean())

    session.hold(duration_minutes=5)

    record = backend.lease.read()
    assert record.state == ACTIVE
    assert record.device_id == "dev"
    assert record.setpoint_percent == 1
    assert record.duration_minutes == 5


def test_a_confirmed_release_removes_the_lease(tmp_path):
    backend, app, session = make(tmp_path, clean())
    session.hold(duration_minutes=5)
    backend.clock.advance(31)      # our own 30100=1 stamped the cooldown

    session.release()
    backend.clock.advance(36)
    app.fire_last_timer()          # the scheduled 30407=0

    assert backend.session_state is SessionState.RELEASED
    assert backend.lease.read() is None
    assert not Path(backend.lease.path).exists()


def test_the_lease_outlives_a_release_that_has_not_confirmed_both_halves(tmp_path):
    """RELEASE_SETTLING is not released. Removing the lease here would strand
    the outstanding 30407=0 if the process died during the settle."""
    backend, _app, session = make(tmp_path, clean())
    session.hold(duration_minutes=5)
    backend.clock.advance(31)

    session.release()             # 30100=0 confirmed; disarm still scheduled

    assert backend.session_state is SessionState.RELEASE_SETTLING
    assert backend.lease.read() is not None


# ---------------------------------------------------------------------------
# Finding one after a crash
# ---------------------------------------------------------------------------

def test_a_restart_recognises_its_own_stranded_session(tmp_path):
    """The crash this exists for: one process arms, dies, another starts."""
    crashed, _app, session = make(tmp_path, clean())
    session.hold(duration_minutes=5)          # ... and the process dies here

    restarted, _app2, _session2 = make(tmp_path, armed())
    restarted.reconcile()

    assert restarted.session_state is SessionState.RECOVERABLE_LEASE
    assert restarted.session_state.safe_to_stop is False


def test_recovery_releases_and_never_resumes(tmp_path):
    crashed, _app, session = make(tmp_path, clean())
    session.hold(duration_minutes=5)

    backend, app, restarted = make(tmp_path, armed())
    result = restarted.recover(wait=make_waiter(backend, app))

    assert result.ok is True
    assert backend.session_state is SessionState.RELEASED
    assert app.registers[REG_CONTROL_AUTHORITY] == 0
    assert app.registers[REG_REMOTE_ENABLE] == 0
    # Nothing re-armed on the way out: no write ever set either register to 1.
    assert (REG_CONTROL_AUTHORITY, 1) not in app.writes
    assert (REG_REMOTE_ENABLE, 1) not in app.writes
    assert backend.lease.read() is None


def test_a_recovered_session_still_refuses_every_other_operation(tmp_path):
    """RECOVERABLE_LEASE is a permission to clean up, not a session."""
    crashed, _app, session = make(tmp_path, clean())
    session.hold(duration_minutes=5)

    backend, app, restarted = make(tmp_path, armed())
    backend.reconcile()
    writes_before = len(app.writes)

    for result in (restarted.hold(duration_minutes=5),
                   restarted.renew(duration_minutes=5),
                   restarted.probe_priority_mode()):
        assert result.refused is True
        assert "STRANDED SESSION" in result.detail

    assert app.writes[writes_before:] == []


def test_the_backend_itself_refuses_to_arm_on_top_of_a_stranded_session(tmp_path):
    """The commissioning preflight refuses first; this is the backstop under
    it, for any caller that did not come through the session object."""
    from battery_optimizer_lib.control import ControlAction, InverterCommand, SendResult

    crashed, _app, session = make(tmp_path, clean())
    session.hold(duration_minutes=5)

    backend, app, _restarted = make(tmp_path, armed())
    backend.reconcile()
    writes_before = len(app.writes)

    result = backend.send(InverterCommand(action=ControlAction.HOLD,
                                          power_percent=1, duration_minutes=5))

    assert result is SendResult.FAILED
    assert app.writes[writes_before:] == []


# ---------------------------------------------------------------------------
# Refusing to adopt what is not ours
# ---------------------------------------------------------------------------

def test_authority_without_a_lease_is_still_not_ours(tmp_path):
    """The rule that was there before, unchanged. A lease carves out one case;
    it does not soften the default."""
    backend, _app, _session = make(tmp_path, armed())

    backend.reconcile()

    assert backend.session_state is SessionState.AUTHORITY_HELD_NOT_OURS


def test_a_lease_for_a_different_device_is_not_adopted(tmp_path):
    crashed, _app, session = make(tmp_path, clean(), device_id="dev")
    session.hold(duration_minutes=5)

    other, _app2, _session2 = make(tmp_path, armed(), device_id="a_different_inverter")
    other.reconcile()

    assert other.session_state is SessionState.AUTHORITY_HELD_NOT_OURS


def test_a_setpoint_that_no_longer_matches_the_record_is_not_adopted(tmp_path):
    """Something re-commanded the inverter after our session, so the armed
    session is not the one recorded -- and releasing it would be acting on a
    command we cannot account for."""
    crashed, _app, session = make(tmp_path, clean())
    session.hold(duration_minutes=5)          # records setpoint +1%

    backend, _app2, _restarted = make(tmp_path, armed(power=100))
    backend.reconcile()

    assert backend.session_state is SessionState.AUTHORITY_HELD_NOT_OURS


def test_an_external_schedule_blocks_recovery_rather_than_racing_it(tmp_path):
    crashed, _app, session = make(tmp_path, clean())
    session.hold(duration_minutes=5)

    registers = armed()
    registers[REG_TOU_NUM_PERIODS] = 16
    backend, app, restarted = make(tmp_path, registers)

    result = restarted.recover(wait=make_waiter(backend, app))

    assert result.refused is True
    assert backend.session_state is not SessionState.RECOVERABLE_LEASE


def test_recovery_refuses_when_there_is_no_lease(tmp_path):
    backend, app, session = make(tmp_path, armed())

    result = session.recover(wait=make_waiter(backend, app))

    assert result.refused is True
    assert "no lease for it" in result.detail
    assert app.writes == []


def test_recovery_refuses_when_nothing_is_stranded(tmp_path):
    backend, app, session = make(tmp_path, clean())

    result = session.recover(wait=make_waiter(backend, app))

    assert result.refused is True
    assert "nothing to recover" in result.detail
    assert app.writes == []


def test_a_stale_lease_over_a_released_inverter_is_discarded(tmp_path):
    """The previous run's release landed and only the unlink was lost. Keeping
    the file would have every later start believe a session is outstanding."""
    lease = SessionLease(str(tmp_path / "lease.json"))
    lease.open(device_id="dev", action="hold", setpoint_percent=1)

    backend, _app, _session = make(tmp_path, clean())
    backend.reconcile()

    assert backend.lease.read() is None
    assert backend.session_state is SessionState.NOT_ARMED


# ---------------------------------------------------------------------------
# The file itself
# ---------------------------------------------------------------------------

def test_a_corrupt_lease_is_treated_as_absent_and_says_so(tmp_path):
    """Absent is the conservative reading: it means an armed inverter is NOT
    adopted, which is refusing to act rather than acting wrongly. It must be
    loud, because the cost is a session nobody cleans up."""
    path = tmp_path / "lease.json"
    path.write_text("{ this is not json")
    logs = []
    lease = SessionLease(str(path), log_func=lambda m, level="INFO": logs.append((m, level)))

    assert lease.read() is None
    assert any(level == "ERROR" for _m, level in logs)


def test_a_lease_missing_its_fields_is_treated_as_absent(tmp_path):
    path = tmp_path / "lease.json"
    path.write_text(json.dumps({"session_id": "abc"}))

    assert SessionLease(str(path)).read() is None


def test_no_lease_path_disables_persistence_rather_than_failing(tmp_path):
    """A missing path must never be an error at write time: the alternative is
    a process that refuses to release an inverter it has already armed."""
    lease = SessionLease(None)

    assert lease.open(device_id="dev") is None
    assert lease.read() is None
    assert lease.close() is True


def test_the_lease_is_replaced_atomically(tmp_path):
    """A torn write is a lease that reads as absent, which strands a session.
    os.replace is what makes the update all-or-nothing."""
    path = tmp_path / "lease.json"
    lease = SessionLease(str(path))
    record = lease.open(device_id="dev", action="hold", setpoint_percent=1)

    lease.mark(record, ACTIVE)

    assert json.loads(path.read_text())["state"] == ACTIVE
    assert list(tmp_path.glob("*.tmp")) == [], "a temp file was left behind"


def test_an_acquiring_lease_is_recoverable_too(tmp_path):
    """The half-applied arm: authority taken, 30407 never set. That is the
    hazard state, and the lease covers it because it is written first."""
    lease = SessionLease(str(tmp_path / "lease.json"))
    lease.open(device_id="dev", action="hold", setpoint_percent=1)
    assert lease.read().state == ACQUIRING

    registers = clean()
    registers[REG_CONTROL_AUTHORITY] = 1      # 1/0, the half-applied arm
    registers[REG_REMOTE_POWER] = 1
    backend, app, session = make(tmp_path, registers)

    result = session.recover(wait=make_waiter(backend, app))

    assert result.ok is True
    assert app.registers[REG_CONTROL_AUTHORITY] == 0
    assert backend.lease.read() is None
