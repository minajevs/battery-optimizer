"""The recovery fence: a reaper may only release the session it decided on.

Found 2026-09-10 by reading the ownership signal rather than trusting it.
`session_id` was written into every lease and read NOWHERE — not by assess(),
not by recover(), not anywhere outside lease.py — so the ownership evidence was
"a lease exists, names this device, and its setpoint matches the registers".

That is not the same statement as "this is the session I decided to reap", and
the difference is a real race with a wide window:

    check(): heartbeat stale, lease L1, inverter 1/1  -> REAP
    recover() waits out the inverter's 30s cooldown on 30100
    ...AppDaemon restarts, the optimizer stamps a heartbeat, opens L2, arms...
    reconcile() sees a lease + 1/1 + a matching setpoint -> RECOVERABLE_LEASE
    the reaper releases a LIVE session

The setpoint check cannot catch it: L2's setpoint matches the inverter by
construction, because L2 wrote it. Liveness was checked once, in assess(), and
never again. And the window coincides with the most likely recovery event —
the reaper fires BECAUSE the optimizer died, and a supervisor brings it back
within seconds.

The fix is a fencing token, not another precondition. claim_for_recovery() is a
compare-and-swap on session_id that puts the lease into RECOVERING, and open()
REFUSES while that claim is live — so the racing party cannot act at all,
rather than being detected after it already has.
"""
from __future__ import annotations

import os

import pytest

from battery_optimizer_lib.control import InverterState
from battery_optimizer_lib.control.lease import (
    ACTIVE,
    CLAIM_TTL_SECONDS,
    RECOVERING,
    SessionLease,
)


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Heartbeat:
    """Just enough of the real one: an age the test can move."""

    def __init__(self, age):
        self._age = age

    def age(self):
        return self._age


def lease_at(tmp_path, clock=None):
    logs = []
    lease = SessionLease(str(tmp_path / "lease.json"),
                         log_func=lambda m, level="INFO": logs.append((level, m)),
                         clock=clock or Clock())
    return lease, logs


# --- the fence itself ------------------------------------------------------

def test_a_fenced_lease_refuses_to_open_a_new_session(tmp_path):
    """The core guarantee: while L1 is claimed, L2 cannot be created."""
    lease, logs = lease_at(tmp_path)
    l1 = lease.open(device_id="dev", action="hold", setpoint_percent=1)

    assert lease.claim_for_recovery(l1.session_id) is not None
    assert lease.open(device_id="dev", action="hold", setpoint_percent=1) is None

    assert any(lvl == "CRITICAL" and "fenced for recovery" in m
               for lvl, m in logs)
    # The lease on disk is still L1, untouched by the refused open.
    assert lease.read().session_id == l1.session_id
    assert lease.read().state == RECOVERING


def test_claiming_is_a_compare_and_swap_on_session_id(tmp_path):
    lease, _logs = lease_at(tmp_path)
    lease.open(device_id="dev", action="hold", setpoint_percent=1)

    assert lease.claim_for_recovery("a-different-session") is None
    # A refused claim must not fence the lease against its real owner.
    assert lease.read().state != RECOVERING


def test_a_second_reaper_cannot_steal_a_live_claim(tmp_path):
    lease, _logs = lease_at(tmp_path)
    l1 = lease.open(device_id="dev", action="hold", setpoint_percent=1)

    assert lease.claim_for_recovery(l1.session_id) is not None
    assert lease.claim_for_recovery(l1.session_id) is None


def test_an_expired_claim_does_not_lock_the_optimizer_out_forever(tmp_path):
    """A reaper that dies mid-recovery must not deadlock the optimizer."""
    clock = Clock()
    lease, logs = lease_at(tmp_path, clock)
    l1 = lease.open(device_id="dev", action="hold", setpoint_percent=1)
    lease.claim_for_recovery(l1.session_id)

    clock.advance(CLAIM_TTL_SECONDS + 1)

    fresh = lease.open(device_id="dev", action="hold", setpoint_percent=1)
    assert fresh is not None
    assert fresh.session_id != l1.session_id
    assert any(lvl == "ERROR" and "expired" in m for lvl, m in logs)


def test_verify_claim_detects_every_way_a_claim_can_stop_holding(tmp_path):
    lease, _logs = lease_at(tmp_path)
    l1 = lease.open(device_id="dev", action="hold", setpoint_percent=1)
    lease.claim_for_recovery(l1.session_id)
    assert lease.verify_claim(l1.session_id) is True

    # a different session took the lease
    lease.close()
    l2 = lease.open(device_id="dev", action="hold", setpoint_percent=1)
    assert lease.verify_claim(l1.session_id) is False

    # the lease is gone entirely
    lease.close()
    assert lease.verify_claim(l1.session_id) is False
    assert lease.verify_claim(l2.session_id) is False


def test_a_lease_not_in_recovering_state_is_not_a_claim(tmp_path):
    lease, _logs = lease_at(tmp_path)
    l1 = lease.open(device_id="dev", action="hold", setpoint_percent=1)
    lease.mark(l1, ACTIVE)
    assert lease.verify_claim(l1.session_id) is False


# ---------------------------------------------------------------------------
# The race, end to end: reaper -> recover() -> release, with a real lease file.
# ---------------------------------------------------------------------------

from battery_optimizer_lib.config import BatteryOptimizerConfig
from battery_optimizer_lib.control import (
    CommissioningSession, SessionReaper, UpstreamVppBackend, build_executor)
from battery_optimizer_lib.control.upstream_vpp import (
    REG_CONTROL_AUTHORITY, REG_PRIORITY_MODE, REG_REMOTE_ENABLE,
    REG_REMOTE_POWER, REG_TOU_NUM_PERIODS)

from test_commissioning import FakeApp, FakeClock, make_waiter


def stranded_registers():
    """An armed session: 30100/30407 = 1/1, setpoint -8, no foreign scheduler."""
    return {REG_CONTROL_AUTHORITY: 1, REG_REMOTE_ENABLE: 1,
            REG_REMOTE_POWER: -8, REG_PRIORITY_MODE: 0,
            REG_TOU_NUM_PERIODS: 0}


def make_reaper(tmp_path, heartbeat_age, registers=None):
    app = FakeApp(registers if registers is not None else stranded_registers())
    config = BatteryOptimizerConfig(device_id="dev", control_mode="commissioning")
    lease = SessionLease(str(tmp_path / "lease.json"), log_func=app.log)
    clock = FakeClock()
    backend = UpstreamVppBackend(app, config,
                                 executor=build_executor(app, config),
                                 lease=lease, clock=clock)
    backend.clock = clock
    session = CommissioningSession(backend, log_func=app.log)
    reaper = SessionReaper(backend, session, Heartbeat(heartbeat_age),
                           device_id="dev", stale_after_seconds=90.0,
                           log_func=app.log)
    return reaper, backend, app, lease


def strand_a_session(lease, setpoint=-8):
    """A lease left behind by a process that died with the inverter armed."""
    record = lease.open(device_id="dev", action="discharge_to_load",
                        setpoint_percent=setpoint)
    return lease.mark(record, ACTIVE, authority_written_at=0.0)


def control_writes(app):
    return [(r, v) for r, v in app.writes
            if r in (REG_CONTROL_AUTHORITY, REG_REMOTE_ENABLE)]


def test_the_owner_coming_back_during_recovery_aborts_with_zero_writes(tmp_path):
    """The race that started this. Stale at assessment, alive by the release."""
    reaper, _backend, app, lease = make_reaper(tmp_path, heartbeat_age=200.0)
    strand_a_session(lease)

    verdict = reaper.check()
    assert verdict.reap is True

    # The optimizer comes back between the decision and the write.
    reaper.heartbeat = Heartbeat(5.0)
    result = reaper.session.recover(
        wait=lambda s: None,
        expected_session_id=reaper.assessed_session_id,
        heartbeat=reaper.heartbeat, stale_after_seconds=90.0)

    assert result.refused is True
    assert "the owner came back" in result.detail
    assert control_writes(app) == [], "it must not touch 30100/30407"


def test_a_different_session_before_recovery_aborts_with_zero_writes(tmp_path):
    reaper, _backend, app, lease = make_reaper(tmp_path, heartbeat_age=200.0)
    strand_a_session(lease)
    verdict = reaper.check()
    assert verdict.reap is True

    # A new optimizer replaced the lease outright.
    lease.close()
    strand_a_session(lease)

    result = reaper.session.recover(
        wait=lambda s: None,
        expected_session_id=reaper.assessed_session_id,
        heartbeat=Heartbeat(200.0), stale_after_seconds=90.0)

    assert result.refused is True
    assert "could not be fenced" in result.detail
    assert control_writes(app) == []


def test_a_new_session_cannot_arm_while_the_old_one_is_fenced(tmp_path):
    """The fence must PREVENT the racing party, not merely notice it."""
    reaper, backend, app, lease = make_reaper(tmp_path, heartbeat_age=200.0)
    stranded = strand_a_session(lease)

    assert lease.claim_for_recovery(stranded.session_id) is not None

    # A restarted optimizer tries to open its own session on the same lease.
    backend._lease_record = None
    from battery_optimizer_lib.control import ControlAction, InverterCommand
    from battery_optimizer_lib.control.backend import SendResult
    result = backend.send(InverterCommand(action=ControlAction.HOLD,
                                          power_percent=1, duration_minutes=5))

    assert result is SendResult.FAILED
    assert app.writes == [], "a fenced lease must stop the arm before any write"
    assert any("REFUSING to arm" in m for m, _lvl in app.logs)


def test_the_lease_vanishing_before_the_release_aborts(tmp_path):
    reaper, backend, app, lease = make_reaper(tmp_path, heartbeat_age=200.0)
    stranded = strand_a_session(lease)
    reaper.check()

    # Fence it, then have the lease disappear underneath the final check.
    lease.claim_for_recovery(stranded.session_id)
    os.unlink(lease.path)

    ok = reaper.session._recovery_still_warranted(
        "recover", stranded.session_id, Heartbeat(200.0), 90.0)

    assert ok is False
    assert control_writes(app) == []


def test_registers_no_longer_armed_aborts(tmp_path):
    reaper, backend, app, lease = make_reaper(tmp_path, heartbeat_age=200.0)
    stranded = strand_a_session(lease)
    reaper.check()
    lease.claim_for_recovery(stranded.session_id)

    # The session ended without us between the fence and the write.
    app.registers[REG_CONTROL_AUTHORITY] = 0
    app.registers[REG_REMOTE_ENABLE] = 0

    ok = reaper.session._recovery_still_warranted(
        "recover", stranded.session_id, Heartbeat(200.0), 90.0)

    assert ok is False
    assert control_writes(app) == []


def test_a_genuinely_stranded_session_still_gets_released(tmp_path):
    """The fence must not break the case it exists to protect."""
    reaper, backend, app, lease = make_reaper(tmp_path, heartbeat_age=200.0)
    stranded = strand_a_session(lease)
    reaper.check()
    lease.claim_for_recovery(stranded.session_id)

    ok = reaper.session._recovery_still_warranted(
        "recover", stranded.session_id, Heartbeat(200.0), 90.0)

    assert ok is True


def test_race_aborts_are_loud_but_do_not_latch(tmp_path):
    """A race that resolved in the optimizer's favour is information, not a
    fault: the next cycle must be able to reassess from scratch."""
    reaper, _backend, _app, lease = make_reaper(tmp_path, heartbeat_age=200.0)
    strand_a_session(lease)
    reaper.check()

    reaper.session.recover(
        wait=lambda s: None,
        expected_session_id=reaper.assessed_session_id,
        heartbeat=Heartbeat(1.0), stale_after_seconds=90.0)

    assert reaper.session.degraded is False, "an abort must not latch degraded"
    assert reaper.session.history[-1].refused is True


def test_the_half_applied_arm_survives_the_final_check(tmp_path):
    """30100=1/30407=0 is the documented hazard pair and the case with the
    STRONGEST claim on being cleaned up. A final check demanding exactly 1/1
    would refuse it — which it did, until tests/test_session_lease.py caught it."""
    registers = stranded_registers()
    registers[REG_REMOTE_ENABLE] = 0
    reaper, _backend, app, lease = make_reaper(tmp_path, heartbeat_age=200.0,
                                               registers=registers)
    stranded = strand_a_session(lease, setpoint=-8)
    lease.claim_for_recovery(stranded.session_id)

    ok = reaper.session._recovery_still_warranted(
        "recover", stranded.session_id, Heartbeat(200.0), 90.0)

    assert ok is True


def test_authority_already_gone_aborts(tmp_path):
    """0/0 means the session ended without us: nothing left to revoke."""
    registers = stranded_registers()
    registers[REG_CONTROL_AUTHORITY] = 0
    registers[REG_REMOTE_ENABLE] = 0
    reaper, _backend, app, lease = make_reaper(tmp_path, heartbeat_age=200.0,
                                               registers=registers)
    stranded = strand_a_session(lease)
    lease.claim_for_recovery(stranded.session_id)

    ok = reaper.session._recovery_still_warranted(
        "recover", stranded.session_id, Heartbeat(200.0), 90.0)

    assert ok is False
    assert control_writes(app) == []


def test_a_manual_recover_is_fenced_too(tmp_path):
    """One primitive makes the ownership statement — the operator path included,
    because the optimizer can restart under a person's recovery as easily as
    under a reaper's."""
    reaper, backend, app, lease = make_reaper(tmp_path, heartbeat_age=200.0)
    strand_a_session(lease)

    # No expected_session_id: the manual path. It must still fence.
    # A real waiter, because this one runs the whole release lifecycle: the
    # settle finishes on a timer this process scheduled, so a wait that only
    # passed time would hang there.
    result = reaper.session.recover(wait=make_waiter(backend, app),
                                operator_override=True)

    assert result.ok is True
    # Fenced on the way through, and the lease is closed once released.
    assert lease.read() is None
    assert any("FENCED for recovery" in m for m, _lvl in app.logs)


# ---------------------------------------------------------------------------
# unclaim: a claim token per ATTEMPT, not per session
# ---------------------------------------------------------------------------

def test_unclaim_restores_the_state_the_fence_replaced(tmp_path):
    lease, _logs = lease_at(tmp_path)
    l1 = lease.open(device_id="dev", action="hold", setpoint_percent=1)
    lease.mark(l1, ACTIVE)

    claimed = lease.claim_for_recovery(l1.session_id)
    assert lease.read().state == RECOVERING

    assert lease.unclaim(l1.session_id, claimed.recovery_claim_id) is True
    restored = lease.read()
    assert restored.state == ACTIVE          # not a guess: what was there before
    assert restored.recovery_claim_id is None
    assert restored.claimed_at is None
    # And the optimizer can arm again immediately.
    assert lease.open(device_id="dev", action="hold", setpoint_percent=1) is not None


def test_unclaim_restores_acquiring_not_a_hardcoded_active(tmp_path):
    """The half-applied arm is fenced from ACQUIRING and must go back to it."""
    lease, _logs = lease_at(tmp_path)
    l1 = lease.open(device_id="dev", action="hold", setpoint_percent=1)
    claimed = lease.claim_for_recovery(l1.session_id)

    lease.unclaim(l1.session_id, claimed.recovery_claim_id)
    assert lease.read().state == "acquiring"


def test_a_stale_attempt_cannot_unclaim_a_later_attempts_fence(tmp_path):
    """The reason the claim id exists. Claim A expires, claim B fences the same
    SESSION, then a resumed process A must not drop B's valid fence."""
    clock = Clock()
    lease, logs = lease_at(tmp_path, clock)
    l1 = lease.open(device_id="dev", action="hold", setpoint_percent=1)

    attempt_a = lease.claim_for_recovery(l1.session_id)
    clock.advance(CLAIM_TTL_SECONDS + 1)          # A's claim expires
    attempt_b = lease.claim_for_recovery(l1.session_id)
    assert attempt_b is not None
    assert attempt_b.recovery_claim_id != attempt_a.recovery_claim_id

    # Process A wakes up and tries to tidy up after itself.
    assert lease.unclaim(l1.session_id, attempt_a.recovery_claim_id) is False
    assert lease.read().state == RECOVERING, "B's fence must survive"
    assert lease.read().recovery_claim_id == attempt_b.recovery_claim_id
    assert any(lvl == "ERROR" and "belongs to recovery attempt" in m
               for lvl, m in logs)


def test_unclaim_refuses_when_a_different_session_holds_the_lease(tmp_path):
    lease, _logs = lease_at(tmp_path)
    l1 = lease.open(device_id="dev", action="hold", setpoint_percent=1)
    claimed = lease.claim_for_recovery(l1.session_id)

    lease.close()
    lease.open(device_id="dev", action="hold", setpoint_percent=1)

    assert lease.unclaim(l1.session_id, claimed.recovery_claim_id) is False


def test_an_aborted_recovery_hands_the_fence_back(tmp_path):
    """The whole point: a loud abort must not latch for the claim TTL."""
    reaper, _backend, app, lease = make_reaper(tmp_path, heartbeat_age=200.0)
    strand_a_session(lease)
    reaper.check()

    result = reaper.session.recover(
        wait=lambda s: None,
        expected_session_id=reaper.assessed_session_id,
        heartbeat=Heartbeat(1.0),          # the owner came back
        stale_after_seconds=90.0)

    assert result.refused is True
    record = lease.read()
    assert record.state != RECOVERING, "the fence must be handed back"
    assert any("fence released" in m for m, _lvl in app.logs)


def test_the_optimizer_can_arm_again_immediately_after_an_abort(tmp_path):
    """The abort reason is usually 'the owner came back' — so the thing being
    unblocked is precisely the process that just proved it is alive."""
    reaper, backend, app, lease = make_reaper(tmp_path, heartbeat_age=200.0)
    strand_a_session(lease)
    reaper.check()
    reaper.session.recover(
        wait=lambda s: None,
        expected_session_id=reaper.assessed_session_id,
        heartbeat=Heartbeat(1.0), stale_after_seconds=90.0)

    assert lease.open(device_id="dev", action="hold",
                      setpoint_percent=1) is not None


# ---------------------------------------------------------------------------
# operator_override: liveness proved, or waived on purpose — never absent
# ---------------------------------------------------------------------------

def test_recover_refuses_without_liveness_or_an_explicit_override(tmp_path):
    reaper, _backend, _app, lease = make_reaper(tmp_path, heartbeat_age=200.0)
    strand_a_session(lease)

    result = reaper.session.recover(wait=lambda s: None)

    assert result.refused is True
    assert "operator_override" in result.detail
    assert lease.read().state != RECOVERING, "it must refuse before fencing"


def test_the_operator_override_says_so_loudly(tmp_path):
    reaper, backend, app, lease = make_reaper(tmp_path, heartbeat_age=200.0)
    strand_a_session(lease)

    reaper.session.recover(wait=make_waiter(backend, app),
                           operator_override=True)

    assert any("OPERATOR OVERRIDE" in m and lvl == "CRITICAL"
               for m, lvl in app.logs)
