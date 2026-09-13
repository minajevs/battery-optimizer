"""30408 renewal: re-arm before the energetic command expires.

The hardware fact this rests on (matched runs, 2026-09-08, 31200/31201 at 5 s):
30408 bounds the COMMAND — 1 min collapsed at 60 s, 2 min at 122 s — and does
NOT end the session. So a slot longer than the TTL must re-arm, and a renewal
that does not land is not a cosmetic miss: the battery stops doing what was
asked while the inverter stays armed and the house draws from the grid.
"""
from __future__ import annotations

import pytest

from battery_optimizer_lib.control.renewal import (
    CommandRenewal,
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


def renewal(ttl_minutes=5, fraction=0.5):
    clock = Clock()
    return CommandRenewal(ttl_minutes=ttl_minutes, renew_fraction=fraction,
                          clock=clock), clock


# --- the policy is the TTL's, not the poll interval's -----------------------

def test_renewal_is_due_at_the_configured_fraction_of_the_ttl():
    r, clock = renewal(ttl_minutes=5, fraction=0.5)
    r.record_armed()

    clock.advance(149)
    assert r.due() is False
    clock.advance(2)                      # 151s of a 300s TTL
    assert r.due() is True


def test_the_margin_scales_with_the_ttl_not_with_any_other_clock():
    short, c1 = renewal(ttl_minutes=1)
    long, c2 = renewal(ttl_minutes=10)
    short.record_armed(); long.record_armed()

    c1.advance(31); c2.advance(31)
    assert short.due() is True            # 31s of 60s
    assert long.due() is False            # 31s of 600s


def test_nothing_is_due_when_nothing_is_armed():
    r, clock = renewal()
    assert r.due() is False
    clock.advance(10_000)
    assert r.due() is False


@pytest.mark.parametrize("ttl", [0, 11, -1])
def test_a_ttl_outside_what_30408_accepts_is_refused(ttl):
    with pytest.raises(ValueError, match="1..10 minute"):
        CommandRenewal(ttl_minutes=ttl)


@pytest.mark.parametrize("fraction", [0.0, 0.95, 1.5])
def test_a_fraction_leaving_no_margin_is_refused(fraction):
    with pytest.raises(ValueError, match="no usable margin"):
        CommandRenewal(renew_fraction=fraction)


# --- renewing ---------------------------------------------------------------

def test_a_confirmed_renewal_restarts_the_ttl():
    r, clock = renewal(ttl_minutes=5)
    r.record_armed()
    clock.advance(151)

    verdict = r.renew(send=lambda: True)

    assert verdict.is_fault is False
    assert r.state is RenewalState.ARMED
    assert r.renewals == 1
    assert r.due() is False               # the clock restarted
    assert r.seconds_until_expiry() == pytest.approx(300.0)


def test_a_failed_renewal_inside_the_ttl_asks_to_be_retried():
    """Half the TTL is left on purpose: a miss at 50% can still be recovered."""
    r, clock = renewal(ttl_minutes=5)
    r.record_armed()
    clock.advance(151)

    verdict = r.renew(send=lambda: False)

    assert verdict.is_fault is False
    assert r.state is RenewalState.ARMED
    assert r.due() is True, "it must still be due so the caller retries"
    assert r.failures == 1
    assert "will retry" in verdict.detail


def test_a_retry_that_lands_before_expiry_recovers():
    r, clock = renewal(ttl_minutes=5)
    r.record_armed()
    clock.advance(151)
    r.renew(send=lambda: False)
    clock.advance(30)

    verdict = r.renew(send=lambda: True)

    assert r.state is RenewalState.ARMED
    assert verdict.is_fault is False
    assert r.renewals == 1 and r.failures == 1


def test_a_renewal_that_misses_the_ttl_is_a_control_fault():
    r, clock = renewal(ttl_minutes=5)
    r.record_armed()
    clock.advance(301)                    # past expiry

    verdict = r.renew(send=lambda: False)

    assert verdict.is_fault is True
    assert r.state is RenewalState.FAULT
    assert "STILL ARMED" in verdict.detail
    assert "Release" in verdict.detail


def test_a_raising_send_is_a_fault_not_a_crash():
    r, clock = renewal()
    r.record_armed()
    clock.advance(151)

    verdict = r.renew(send=lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    assert verdict.is_fault is True
    assert "RuntimeError" in verdict.detail


def test_two_renewals_cannot_race_the_same_registers():
    r, clock = renewal()
    r.record_armed()
    clock.advance(151)

    def reentrant():
        inner = r.renew(send=lambda: True)
        assert inner.state is RenewalState.RENEWING
        assert "already in flight" in inner.detail
        return True

    r.renew(send=reentrant)
    assert r.renewals == 1, "the inner attempt must not have counted"


# --- the fact that makes all this necessary ---------------------------------

def test_expiry_is_reported_even_though_the_session_would_stay_armed():
    """The hazard is not a runaway battery: it is energy motion stopping while
    control ownership does not revert."""
    r, clock = renewal(ttl_minutes=2)
    r.record_armed()

    clock.advance(121)                    # the measured 30408=2 collapse point
    assert r.expired() is True
    assert r.seconds_until_expiry() < 0


def test_release_clears_the_renewal_so_nothing_is_due_afterwards():
    r, clock = renewal()
    r.record_armed()
    clock.advance(200)
    r.released()

    assert r.state is RenewalState.IDLE
    assert r.due() is False
    assert r.age() is None
