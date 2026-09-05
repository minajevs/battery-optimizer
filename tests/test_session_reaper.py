"""The reaper may end a session its owner abandoned, and do nothing else.

The optimizer's own cleanup covers the paths it survives. It cannot cover the
paths where it stops running -- a hung callback, a crashed app, a thread that
never returns -- and this hardware has no expiry that would end the session
left behind. So something outside the optimizer watches a heartbeat go stale.

Every condition is necessary and none is sufficient. These tests are mostly
about the NOT-reaping cases, because that is where the danger is: reaping a
running optimizer's own session, or cleaning up after an actor nobody
identified, would make the safety layer the hazard.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, "appdaemon/apps")
sys.path.insert(0, str(Path(__file__).parent))

from battery_optimizer_lib.config import BatteryOptimizerConfig
from battery_optimizer_lib.control import (
    CommissioningSession,
    Heartbeat,
    InverterState,
    SessionLease,
    SessionReaper,
    SessionState,
    UpstreamVppBackend,
    build_executor,
)
from battery_optimizer_lib.control.lease import ACTIVE, LeaseRecord
from battery_optimizer_lib.control.reaper import (
    EXTERNAL_SCHEDULER,
    HALF_ARMED,
    IDLE_NOTHING_ARMED,
    LEASE_OTHER_DEVICE,
    NO_LEASE,
    OWNER_ALIVE,
    SETPOINT_CHANGED,
    STRANDED,
    UNREADABLE,
    assess,
)
from battery_optimizer_lib.control.upstream_vpp import (
    REG_CONTROL_AUTHORITY,
    REG_REMOTE_ENABLE,
    REG_REMOTE_POWER,
    REG_TOU_NUM_PERIODS,
)

from test_commissioning import FakeApp, FakeClock, clean, make_waiter


def lease_record(device_id="dev", setpoint=1, state=ACTIVE):
    return LeaseRecord(session_id="a" * 32, device_id=device_id, state=state,
                       started_at=1000.0, action="hold",
                       setpoint_percent=setpoint, duration_minutes=5)


def armed_state(authority=1, remote=1, power=1, tou=0):
    return InverterState(control_authority=authority, remote_enabled=remote,
                         commanded_power=power, tou_period_count=tou,
                         duration_minutes=5)


def verdict(**overrides):
    kwargs = dict(heartbeat_age=300.0, stale_after_seconds=90.0,
                  lease=lease_record(), state=armed_state(), device_id="dev")
    kwargs.update(overrides)
    return assess(**kwargs)


# ---------------------------------------------------------------------------
# The one case it exists for
# ---------------------------------------------------------------------------

def test_a_stale_owner_over_an_armed_matching_session_is_reaped():
    result = verdict()

    assert result.reap is True
    assert result.code == STRANDED


def test_our_own_half_applied_arm_is_reaped_too():
    """1/0 with a matching lease: authority taken, nothing armed, nobody left
    to finish it. Releasing 30100 is strictly a reduction in control."""
    result = verdict(state=armed_state(remote=0))

    assert result.reap is True
    assert result.code == HALF_ARMED


# ---------------------------------------------------------------------------
# Each condition alone grants nothing
# ---------------------------------------------------------------------------

def test_a_stale_heartbeat_alone_grants_nothing():
    """Nothing is armed. A dead optimizer over an idle inverter is not this
    layer's business at all."""
    result = verdict(state=InverterState(control_authority=0, remote_enabled=0))

    assert result.reap is False
    assert result.code == IDLE_NOTHING_ARMED


def test_an_armed_session_alone_grants_nothing():
    """The most important refusal in the file: the owner is alive, so this is
    a running optimizer's own session."""
    result = verdict(heartbeat_age=10.0)

    assert result.reap is False
    assert result.code == OWNER_ALIVE


def test_a_lease_alone_grants_nothing():
    result = verdict(heartbeat_age=10.0, state=armed_state())

    assert result.reap is False


def test_an_armed_session_without_a_lease_is_not_ours_to_end():
    result = verdict(lease=None)

    assert result.reap is False
    assert result.code == NO_LEASE
    assert result.alarming is True


def test_a_missing_heartbeat_counts_as_stale():
    """Unknown is not healthy. Otherwise "the optimizer never started, but its
    lease and an armed inverter are both still here" is the one case nothing
    cleans up."""
    result = verdict(heartbeat_age=None)

    assert result.reap is True
    assert "never stamped" in result.detail


def test_the_boundary_is_not_stale_yet():
    assert verdict(heartbeat_age=90.0, stale_after_seconds=90.0).reap is False
    assert verdict(heartbeat_age=90.1, stale_after_seconds=90.0).reap is True


# ---------------------------------------------------------------------------
# Refusals that are about somebody else having touched the inverter
# ---------------------------------------------------------------------------

def test_a_changed_setpoint_is_reported_loudly_and_left_alone():
    result = verdict(state=armed_state(power=100))

    assert result.reap is False
    assert result.code == SETPOINT_CHANGED
    assert result.alarming is True
    assert "re-commanded" in result.detail


def test_an_external_schedule_stops_the_reaper():
    result = verdict(state=armed_state(tou=16))

    assert result.reap is False
    assert result.code == EXTERNAL_SCHEDULER
    assert result.alarming is True


def test_a_lease_for_another_device_is_not_acted_on():
    result = verdict(lease=lease_record(device_id="another_inverter"))

    assert result.reap is False
    assert result.code == LEASE_OTHER_DEVICE
    assert result.alarming is True


def test_armed_without_authority_is_reported_not_acted_on():
    """0/1 is a pair this project never creates, and revoking authority we do
    not hold would not end it."""
    result = verdict(state=armed_state(authority=0, remote=1))

    assert result.reap is False
    assert result.code == HALF_ARMED
    assert result.alarming is True


def test_an_unreadable_inverter_is_never_acted_on():
    result = verdict(state=None)

    assert result.reap is False
    assert result.code == UNREADABLE


# ---------------------------------------------------------------------------
# Driving the real backend
# ---------------------------------------------------------------------------

def make_reaper(tmp_path, registers, stale_after=90.0, heartbeat_age=None,
                device_id="dev"):
    app = FakeApp(registers)
    clock = FakeClock()
    lease_path = str(tmp_path / "lease.json")
    heartbeat_path = str(tmp_path / "heartbeat.json")
    config = BatteryOptimizerConfig(device_id=device_id,
                                    control_mode="commissioning",
                                    session_lease_path=lease_path)
    backend = UpstreamVppBackend(app, config,
                                 executor=build_executor(app, config),
                                 clock=clock)
    backend.clock = clock
    session = CommissioningSession(backend, log_func=app.log)

    now = [10_000.0]
    heartbeat = Heartbeat(heartbeat_path, log_func=app.log,
                          clock=lambda: now[0])
    if heartbeat_age is not None:
        heartbeat.stamp()
        now[0] += heartbeat_age

    reaper = SessionReaper(backend, session, heartbeat, device_id=device_id,
                           stale_after_seconds=stale_after, log_func=app.log,
                           clock=lambda: now[0])
    return reaper, backend, app, session


def strand_a_session(tmp_path):
    """Arm a session, then hand the inverter to a SEPARATE reaper.

    The two backends are distinct on purpose: the reaper is a different app
    from the optimizer, so it has no memory of the session and must decide
    from the lease on disk and the registers alone. Sharing one backend would
    quietly test the in-memory path instead -- and that path correctly refuses
    to reap, because a process that still remembers its own session is not one
    that stopped running.
    """
    _reaper, crashed, crashed_app, crashed_session = make_reaper(
        tmp_path, clean(), heartbeat_age=0.0)
    crashed_session.hold(duration_minutes=5)

    # Same inverter, seen by another process.
    reaper, backend, app, _session = make_reaper(
        tmp_path, dict(crashed_app.registers), heartbeat_age=0.0)
    return reaper, backend, app


def test_the_reaper_releases_a_stranded_session_through_recover(tmp_path):
    reaper, backend, app = strand_a_session(tmp_path)
    reaper.heartbeat._clock = lambda: 10_000.0 + 300      # owner went quiet
    reaper._clock = lambda: 10_000.0 + 300

    result = reaper.run_once(wait=make_waiter(backend, app))

    assert result.reap is True
    assert backend.session_state is SessionState.RELEASED
    assert app.registers[REG_CONTROL_AUTHORITY] == 0
    assert app.registers[REG_REMOTE_ENABLE] == 0
    assert backend.lease.read() is None
    assert reaper.reap_count == 1
    assert reaper.reap_failed == 0


def test_the_reaper_never_arms_anything(tmp_path):
    """It has no path to HOLD, charge, discharge or export -- the only writes
    it can produce are the two that end a session."""
    reaper, backend, app = strand_a_session(tmp_path)
    reaper.heartbeat._clock = lambda: 10_000.0 + 300
    writes_before = len(app.writes)

    reaper.run_once(wait=make_waiter(backend, app))

    written = app.writes[writes_before:]
    assert written, "the reaper wrote nothing at all"
    assert {register for register, _v in written} <= {REG_CONTROL_AUTHORITY,
                                                      REG_REMOTE_ENABLE}
    assert all(value == 0 for _r, value in written)


def test_a_live_owner_is_never_reaped(tmp_path):
    """The session is armed and matching, and the heartbeat is fresh. Nothing
    may be written."""
    reaper, backend, app = strand_a_session(tmp_path)
    writes_before = len(app.writes)

    result = reaper.run_once(wait=make_waiter(backend, app))

    assert result.reap is False
    assert result.code == OWNER_ALIVE
    assert app.writes[writes_before:] == []
    assert backend.lease.read() is not None


def test_a_second_cycle_does_not_race_a_recovery_in_flight(tmp_path):
    """A recovery outlives several check intervals, and two of them over the
    same registers is exactly the collision this must not create."""
    reaper, backend, app = strand_a_session(tmp_path)
    reaper.heartbeat._clock = lambda: 10_000.0 + 300
    reaper.reaping = True
    writes_before = len(app.writes)

    reaper.run_once(wait=make_waiter(backend, app))

    assert app.writes[writes_before:] == []


def test_a_failed_reap_is_counted_and_says_the_inverter_may_be_armed(tmp_path):
    reaper, backend, app = strand_a_session(tmp_path)
    reaper.heartbeat._clock = lambda: 10_000.0 + 300

    def exploding_recover(**kwargs):
        raise RuntimeError("the inverter went away")

    reaper.session.recover = exploding_recover
    reaper.run_once(wait=make_waiter(backend, app))

    assert reaper.reap_failed == 1
    assert reaper.reap_count == 0
    assert any("may still be armed" in message
               for message, _level in app.logs)


def test_the_status_is_publishable_and_says_what_happened(tmp_path):
    reaper, backend, app = strand_a_session(tmp_path)
    reaper.heartbeat._clock = lambda: 10_000.0 + 300
    reaper._clock = lambda: 10_000.0 + 300

    reaper.run_once(wait=make_waiter(backend, app))
    status = reaper.status()

    assert status["verdict"] == STRANDED
    assert status["reap_count"] == 1
    assert status["reap_failed"] == 0
    assert status["last_reap"] is not None
    assert status["heartbeat_age_seconds"] == pytest.approx(300, abs=1)
    assert status["reaping_now"] is False


def test_check_never_writes(tmp_path):
    """`check()` is the read-only half, and the AppDaemon app publishes from
    it every cycle."""
    reaper, backend, app = strand_a_session(tmp_path)
    reaper.heartbeat._clock = lambda: 10_000.0 + 300
    writes_before = len(app.writes)

    result = reaper.check()

    assert result.reap is True
    assert app.writes[writes_before:] == []


def test_an_external_scheduler_appearing_mid_session_blocks_the_reap(tmp_path):
    reaper, backend, app = strand_a_session(tmp_path)
    reaper.heartbeat._clock = lambda: 10_000.0 + 300
    app.registers[REG_TOU_NUM_PERIODS] = 16
    writes_before = len(app.writes)

    result = reaper.run_once(wait=make_waiter(backend, app))

    assert result.reap is False
    assert result.code == EXTERNAL_SCHEDULER
    assert app.writes[writes_before:] == []


def test_a_setpoint_changed_under_us_blocks_the_reap(tmp_path):
    reaper, backend, app = strand_a_session(tmp_path)
    reaper.heartbeat._clock = lambda: 10_000.0 + 300
    app.registers[REG_REMOTE_POWER] = 100
    writes_before = len(app.writes)

    result = reaper.run_once(wait=make_waiter(backend, app))

    assert result.reap is False
    assert result.code == SETPOINT_CHANGED
    assert reaper.alarm_count == 1
    assert app.writes[writes_before:] == []


# ---------------------------------------------------------------------------
# The heartbeat file
# ---------------------------------------------------------------------------

def test_a_stamp_can_be_read_back_as_an_age(tmp_path):
    now = [500.0]
    heartbeat = Heartbeat(str(tmp_path / "hb.json"), clock=lambda: now[0])
    heartbeat.stamp()
    now[0] += 45

    assert heartbeat.age() == pytest.approx(45)


def test_a_missing_heartbeat_has_an_unknown_age(tmp_path):
    assert Heartbeat(str(tmp_path / "nothing.json")).age() is None


def test_an_unreadable_heartbeat_is_unknown_rather_than_fresh(tmp_path):
    path = tmp_path / "hb.json"
    path.write_text("not json at all")

    assert Heartbeat(str(path)).age() is None


def test_a_stamp_from_the_future_is_not_treated_as_staleness(tmp_path):
    """A clock change must not be able to make a healthy optimizer look dead."""
    now = [500.0]
    heartbeat = Heartbeat(str(tmp_path / "hb.json"), clock=lambda: now[0])
    heartbeat.stamp()
    now[0] -= 120

    assert heartbeat.age() == 0


def test_no_heartbeat_path_disables_stamping_without_failing(tmp_path):
    heartbeat = Heartbeat(None)

    assert heartbeat.stamp() is False
    assert heartbeat.age() is None
    assert heartbeat.enabled is False
