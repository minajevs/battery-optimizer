"""What a fired renewal timer is allowed to do.

A renewal callback runs minutes after it was scheduled. In between, the slot
may have changed, the app may have been disabled, the lifecycle may have left
READY, the mode may have been reduced, or the session may have been released —
by a person, by the reaper, or by this app's own fault handling. Every one of
those is a reason to DROP, never a reason to try harder: a callback that tries
to be helpful re-arms an inverter nobody expects to be armed.

Stale-timer resurrection is THE failure mode here, which is why every armed
command carries a generation and every callback carries the one it was
scheduled for.
"""
from __future__ import annotations

import pytest

from battery_optimizer_lib.control.renewal import (
    CommandRenewal,
    RenewalAction,
    RenewalState,
    assess_renewal,
)


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self


def armed(ttl_minutes=2):
    clock = Clock()
    r = CommandRenewal(ttl_minutes=ttl_minutes, clock=clock)
    generation = r.record_armed()
    return r, clock, generation


def decide(r, generation, **overrides):
    kwargs = dict(renewal=r, generation=generation, lifecycle_ready=True,
                  automatic_writes_allowed=True, session_active=True)
    kwargs.update(overrides)
    return assess_renewal(**kwargs)


# --- the two that must be airtight -----------------------------------------

def test_an_old_generation_callback_after_a_new_command_writes_nothing():
    """The named case: a superseded command's timer fires late."""
    r, clock, old_generation = armed()
    clock.advance(61)

    new_generation = r.record_armed()           # a new slot armed its own command
    assert new_generation != old_generation

    decision = decide(r, old_generation)

    assert decision.action is RenewalAction.DROP
    assert decision.writes is False
    assert "stale callback" in decision.reason


def test_a_faulted_renewal_callback_after_release_started_writes_nothing():
    """The other named case: the fault handler is already releasing."""
    r, clock, generation = armed(ttl_minutes=2)
    clock.advance(121)                          # past the TTL
    verdict = r.renew(send=lambda: False)       # -> FAULT, caller releases
    assert verdict.is_fault is True

    decision = decide(r, generation)

    assert decision.action is RenewalAction.DROP
    assert decision.writes is False
    assert "must not be re-armed" in decision.reason


# --- every other way the world can move on ---------------------------------

def test_leaving_READY_drops_the_renewal():
    r, clock, g = armed()
    clock.advance(61)
    d = decide(r, g, lifecycle_ready=False)
    assert d.action is RenewalAction.DROP and d.writes is False


def test_losing_automatic_write_permission_drops_the_renewal():
    """A renewal is a write like any other — a mode change must stop it."""
    r, clock, g = armed()
    clock.advance(61)
    d = decide(r, g, automatic_writes_allowed=False)
    assert d.action is RenewalAction.DROP and d.writes is False


def test_a_session_that_is_no_longer_active_drops_the_renewal():
    """Released by a person, by the reaper, or by our own fault handling."""
    r, clock, g = armed()
    clock.advance(61)
    d = decide(r, g, session_active=False)
    assert d.action is RenewalAction.DROP and d.writes is False


def test_a_changed_active_command_drops_the_renewal():
    r, clock, g = armed()
    clock.advance(61)
    d = decide(r, g, scheduled_command="hold", active_command="grid_charge")
    assert d.action is RenewalAction.DROP
    assert "changed since this renewal was scheduled" in d.reason


def test_cancel_invalidates_every_callback_already_scheduled():
    r, clock, g = armed()
    clock.advance(61)

    r.cancel("new slot")

    d = decide(r, g)
    assert d.action is RenewalAction.DROP and d.writes is False
    assert r.state is RenewalState.IDLE


def test_release_invalidates_callbacks_too():
    r, clock, g = armed()
    clock.advance(61)
    r.released()
    assert decide(r, g).action is RenewalAction.DROP


# --- and the cases where it SHOULD act -------------------------------------

def test_a_due_renewal_with_everything_intact_renews():
    r, clock, g = armed(ttl_minutes=2)
    clock.advance(61)                           # past 50% of 120s

    d = decide(r, g)

    assert d.action is RenewalAction.RENEW
    assert d.writes is True


def test_not_yet_due_waits_without_writing():
    r, clock, g = armed(ttl_minutes=2)
    clock.advance(30)
    d = decide(r, g)
    assert d.action is RenewalAction.WAIT
    assert d.writes is False


def test_an_expired_ttl_releases_rather_than_re_arming():
    """The command has stopped; the session has not. Give it back."""
    r, clock, g = armed(ttl_minutes=2)
    clock.advance(121)

    d = decide(r, g)

    assert d.action is RenewalAction.RELEASE
    assert "still" in d.reason and "armed" in d.reason


def test_a_callback_with_no_generation_is_still_subject_to_every_other_check():
    """Belt and braces: omitting the token must not skip the guards."""
    r, clock, _g = armed()
    clock.advance(61)
    assert decide(r, None, session_active=False).action is RenewalAction.DROP
    assert decide(r, None, lifecycle_ready=False).action is RenewalAction.DROP


def test_generation_advances_on_every_arm_so_tokens_are_never_reused():
    r, _clock, first = armed()
    second = r.record_armed()
    third = r.record_armed()
    assert first < second < third
