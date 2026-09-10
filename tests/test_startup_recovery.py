"""Startup recovery, and the ordering it depends on.

`session_reaper.py` documented that "the durable lease and startup recovery
clean it up" long before anything called recover() at startup. The lease,
RECOVERABLE_LEASE and recover() all existed; the call did not. So a crash that
left the inverter armed was cleaned up by nothing at all, and the restart made
it permanent:

    optimizer crashes while armed  -> heartbeat goes stale, lease L1 remains
    AppDaemon restarts
    optimizer stamps a FRESH heartbeat
    reaper reads OWNER_ALIVE (correctly!) and refuses forever
    the optimizer cannot arm over RECOVERABLE_LEASE, so it sits there blocked
    and nothing in the system is wrong enough to complain

The fix is ordering: the old stamp is evidence about the PREVIOUS owner, and
must be read before a new one destroys it.
"""
from __future__ import annotations

import pytest

from battery_optimizer_lib.control import InverterState
from battery_optimizer_lib.control.startup import (
    OptimizerLifecycle,
    recover_previous_session,
)
from battery_optimizer_lib.control.upstream_vpp import SessionState


class Heartbeat:
    def __init__(self, age):
        self._age = age
        self.stamped = 0

    def age(self):
        return self._age

    def stamp(self):
        self.stamped += 1
        self._age = 0.0
        return True


class Backend:
    def __init__(self, state, after_release=SessionState.RELEASED):
        self.session_state = state
        self._after = after_release
        self.reconciled = 0

    def reconcile(self):
        self.reconciled += 1
        return InverterState(control_authority=1, remote_enabled=1)

    def release_succeeds(self):
        self.session_state = self._after


class Session:
    def __init__(self, backend, ok=True, detail="released"):
        self.backend = backend
        self.ok = ok
        self.detail = detail
        self.calls = []

    def recover(self, wait, heartbeat=None, stale_after_seconds=None,
                **kwargs):
        self.calls.append({"heartbeat": heartbeat,
                           "stale_after_seconds": stale_after_seconds})
        if self.ok:
            self.backend.release_succeeds()
        return type("R", (), {"ok": self.ok, "detail": self.detail})()


def run(state, heartbeat_age, recover_ok=True):
    backend = Backend(state)
    session = Session(backend, ok=recover_ok)
    hb = Heartbeat(heartbeat_age)
    result = recover_previous_session(backend, session, hb,
                                      wait=lambda s: None,
                                      stale_after_seconds=90.0)
    return result, backend, session, hb


# --- the case that was silently broken -------------------------------------

def test_a_stranded_session_is_released_at_startup():
    result, _backend, session, hb = run(SessionState.RECOVERABLE_LEASE, 500.0)

    assert result.lifecycle is OptimizerLifecycle.READY
    assert result.recovered is True
    assert len(session.calls) == 1
    assert hb.stamped == 0, "startup recovery must not stamp the heartbeat"


def test_recovery_judges_liveness_by_the_PREVIOUS_owners_heartbeat():
    """The stamp on disk is the only evidence the old owner is gone."""
    result, _backend, session, _hb = run(SessionState.RECOVERABLE_LEASE, 500.0)

    assert result.recovered is True
    passed = session.calls[0]
    assert passed["heartbeat"] is not None, "liveness must be proved, not waived"
    assert passed["stale_after_seconds"] == 90.0


def test_a_never_stamped_heartbeat_counts_as_stale():
    result, _backend, session, _hb = run(SessionState.RECOVERABLE_LEASE, None)
    assert result.recovered is True
    assert len(session.calls) == 1


# --- when it must NOT act --------------------------------------------------

def test_a_fresh_heartbeat_at_startup_blocks_recovery():
    """At startup a fresh stamp means ANOTHER INSTANCE is running."""
    result, _backend, session, _hb = run(SessionState.RECOVERABLE_LEASE, 5.0)

    assert result.lifecycle is OptimizerLifecycle.RECOVERY_BLOCKED
    assert session.calls == [], "it must not even attempt a release"
    assert result.ready is False
    assert result.should_retry is False, "a person is needed, not a timer"


def test_foreign_authority_is_not_recovered_and_blocks_control():
    result, _backend, session, _hb = run(SessionState.AUTHORITY_HELD_NOT_OURS,
                                         500.0)

    assert result.lifecycle is OptimizerLifecycle.FOREIGN_AUTHORITY
    assert session.calls == []
    assert result.ready is False
    assert result.should_retry is False


def test_nothing_stranded_is_simply_ready():
    result, backend, session, _hb = run(SessionState.NOT_ARMED, 500.0)

    assert result.lifecycle is OptimizerLifecycle.READY
    assert result.recovered is False
    assert session.calls == []
    assert backend.reconciled == 1


# --- when the release does not land ----------------------------------------

def test_a_failed_release_holds_control_off_and_asks_to_retry():
    result, _backend, _session, _hb = run(SessionState.RECOVERABLE_LEASE,
                                          500.0, recover_ok=False)

    assert result.lifecycle is OptimizerLifecycle.RECOVERY_FAILED
    assert result.ready is False
    assert result.should_retry is True
    assert "may still be armed" in result.detail


def test_a_release_that_reports_ok_but_did_not_reach_RELEASED_is_a_failure():
    """ok is not the same as released — only the state machine says released."""
    backend = Backend(SessionState.RECOVERABLE_LEASE,
                      after_release=SessionState.RELEASE_PENDING)
    session = Session(backend, ok=True)
    result = recover_previous_session(backend, session, Heartbeat(500.0),
                                      wait=lambda s: None,
                                      stale_after_seconds=90.0)

    assert result.lifecycle is OptimizerLifecycle.RECOVERY_FAILED
    assert result.ready is False


# ---------------------------------------------------------------------------
# Unreadable is not idle. Found on the first live deployment, 2026-09-10.
# ---------------------------------------------------------------------------

class UnreadableBackend(Backend):
    """reconcile() fails, and session_state keeps its initial NOT_ARMED."""

    def reconcile(self):
        self.reconciled += 1
        return None


def test_an_unreadable_inverter_is_not_reported_as_nothing_to_recover():
    """The live failure: the register read returned nothing, session_state was
    still its initial NOT_ARMED, and startup announced "nothing to recover".
    That is a false all-clear over a possibly stranded session."""
    backend = UnreadableBackend(SessionState.NOT_ARMED)
    session = Session(backend)
    result = recover_previous_session(backend, session, Heartbeat(500.0),
                                      wait=lambda s: None,
                                      stale_after_seconds=90.0)

    assert result.lifecycle is OptimizerLifecycle.INVERTER_UNREADABLE
    assert result.ready is False
    assert result.should_retry is True
    assert session.calls == [], "it must not act on an inverter it cannot read"
    assert "Unreadable is not idle" in result.detail


def test_an_unreadable_inverter_hides_even_a_recoverable_lease():
    """session_state may say anything when the read failed; it is not evidence."""
    backend = UnreadableBackend(SessionState.RECOVERABLE_LEASE)
    session = Session(backend)
    result = recover_previous_session(backend, session, Heartbeat(500.0),
                                      wait=lambda s: None,
                                      stale_after_seconds=90.0)

    assert result.lifecycle is OptimizerLifecycle.INVERTER_UNREADABLE
    assert session.calls == []
