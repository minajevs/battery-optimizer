"""The renewal callback as it actually runs: timers, sends, releases.

tests/test_renewal_guards.py proves assess_renewal() SAYS "do not write".
These prove the thing driving the timers OBEYS it — the assertions here are
`backend.sent == []`, not `decision.action is DROP`, because the claim worth
making is that nothing reached the inverter.
"""
from __future__ import annotations

import pytest

from battery_optimizer_lib.control.renewal import (
    CommandRenewal,
    RenewalAction,
    RenewalRunner,
    RenewalState,
)


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self


class Backend:
    """Records every send and release. Nothing here is allowed to be implicit."""

    def __init__(self, confirm=True):
        self.sent = []
        self.released = 0
        self.confirm = confirm

    def send(self, command):
        self.sent.append(command)
        return self.confirm

    def release(self):
        self.released += 1


class Timers:
    """A fake AppDaemon scheduler: ONE-SHOT handles, explicit firing.

    A one-shot that has fired is spent, so `pending` excludes it — modelling
    that correctly matters, because "no timer may survive a release" is about
    timers that could still fire, not about ones already consumed.
    """

    def __init__(self):
        self.scheduled = []          # (handle, delay, generation)
        self.cancelled = []
        self.fired = []
        self._next = 0

    def schedule(self, delay, generation):
        self._next += 1
        handle = f"t{self._next}"
        self.scheduled.append((handle, delay, generation))
        return handle

    def cancel(self, handle):
        self.cancelled.append(handle)

    @property
    def pending(self):
        return [s for s in self.scheduled
                if s[0] not in self.cancelled and s[0] not in self.fired]

    def fire_latest(self, runner):
        """Fire the newest outstanding one-shot, as AppDaemon would."""
        handle, _delay, generation = self.pending[-1]
        self.fired.append(handle)
        return runner.on_timer(generation)


def make(ttl_minutes=2, confirm=True, ready=True, automatic=True, active=True):
    clock = Clock()
    renewal = CommandRenewal(ttl_minutes=ttl_minutes, clock=clock)
    backend = Backend(confirm=confirm)
    timers = Timers()
    state = {"ready": ready, "automatic": automatic, "active": active}
    runner = RenewalRunner(
        renewal,
        schedule=timers.schedule,
        cancel_timer=timers.cancel,
        send=backend.send,
        release=backend.release,
        lifecycle_ready=lambda: state["ready"],
        automatic_writes_allowed=lambda: state["automatic"],
        session_active=lambda: state["active"],
    )
    return runner, renewal, backend, timers, clock, state


# --- the two that must be airtight, now one layer higher -------------------

def test_a_stale_generation_callback_sends_nothing_to_the_backend():
    runner, _renewal, backend, _timers, clock, _state = make()
    old = runner.command_armed("HOLD")
    clock.advance(61)
    runner.command_armed("HOLD")               # a new slot supersedes it

    backend.sent.clear()
    decision = runner.on_timer(generation=old)

    assert backend.sent == [], "a superseded command must not be re-armed"
    assert backend.released == 0
    assert decision.action is RenewalAction.DROP


def test_a_callback_after_release_has_begun_sends_nothing():
    runner, renewal, backend, _timers, clock, _state = make()
    generation = runner.command_armed("HOLD")
    clock.advance(121)                          # past the TTL
    runner.on_timer(generation)                 # -> RELEASE, release() called
    assert backend.released == 1
    backend.sent.clear()

    late = runner.on_timer(generation)          # a queued callback outruns cancel

    assert backend.sent == [], "nothing may be re-armed behind a release"
    assert backend.released == 1, "and release must not be repeated"
    assert late.action is RenewalAction.DROP


# --- scheduling discipline --------------------------------------------------

def test_arming_cancels_any_previous_timer_before_scheduling_the_new_one():
    runner, _renewal, _backend, timers, clock, _state = make()
    runner.command_armed("HOLD")
    first = timers.scheduled[0][0]

    clock.advance(10)
    runner.command_armed("HOLD")

    assert first in timers.cancelled, "the old timer must be cancelled first"
    assert len(timers.pending) == 1, "exactly one timer may be outstanding"


def test_drop_never_reschedules():
    runner, _renewal, backend, timers, clock, state = make()
    generation = runner.command_armed("HOLD")
    clock.advance(61)
    before = len(timers.scheduled)

    state["ready"] = False
    runner.on_timer(generation)

    assert len(timers.scheduled) == before, "a dropped callback must not rearm"
    assert backend.sent == []


def test_wait_reschedules_without_writing():
    runner, _renewal, backend, timers, clock, _state = make()
    generation = runner.command_armed("HOLD")
    clock.advance(10)                           # well before 50% of 120s
    before = len(timers.scheduled)

    decision = runner.on_timer(generation)

    assert decision.action is RenewalAction.WAIT
    assert backend.sent == []
    assert len(timers.scheduled) == before + 1


def test_renew_sends_exactly_the_stored_command_through_the_normal_path():
    runner, _renewal, backend, _timers, clock, _state = make()
    generation = runner.command_armed("THE-COMMAND")
    clock.advance(61)

    decision = runner.on_timer(generation)

    assert decision.action is RenewalAction.RENEW
    assert backend.sent == ["THE-COMMAND"]


def test_a_confirmed_renewal_schedules_the_next_check_from_the_renewed_ttl():
    runner, renewal, _backend, timers, clock, _state = make(ttl_minutes=2)
    generation = runner.command_armed("HOLD")
    clock.advance(61)

    runner.on_timer(generation)

    assert renewal.renewals == 1
    handle, delay, gen = timers.pending[-1]
    assert gen == renewal.generation, "the next callback carries the NEW token"
    assert delay == pytest.approx(60.0, abs=2.0), (
        "the next check is half of the freshly restarted TTL")


def test_release_cancels_first_then_releases_exactly_once():
    runner, renewal, backend, timers, clock, _state = make(ttl_minutes=2)
    runner.command_armed("HOLD")
    clock.advance(121)

    decision = timers.fire_latest(runner)

    assert decision.action is RenewalAction.RELEASE
    assert backend.released == 1
    assert renewal.state is RenewalState.FAULT or renewal.state is RenewalState.IDLE
    assert timers.pending == [], "no timer may survive a release"


def test_an_unconfirmed_renewal_inside_the_ttl_retries_without_releasing():
    runner, _renewal, backend, timers, clock, _state = make(
        ttl_minutes=2, confirm=False)
    generation = runner.command_armed("HOLD")
    clock.advance(61)

    runner.on_timer(generation)

    assert backend.sent == ["HOLD"]
    assert backend.released == 0, "half the TTL is left; this is retryable"
    assert timers.pending, "a retry must be scheduled"


@pytest.mark.parametrize("flag", ["ready", "automatic", "active"])
def test_a_queued_callback_is_inert_once_the_world_changed(flag):
    runner, _renewal, backend, _timers, clock, state = make()
    generation = runner.command_armed("HOLD")
    clock.advance(61)

    state[flag] = False
    decision = runner.on_timer(generation)

    assert decision.action is RenewalAction.DROP
    assert backend.sent == []
    assert backend.released == 0


def test_cancel_is_idempotent_and_leaves_nothing_pending():
    runner, _renewal, backend, timers, _clock, _state = make()
    runner.command_armed("HOLD")

    runner.cancel("first")
    runner.cancel("second")

    assert timers.pending == []
    assert backend.sent == []
