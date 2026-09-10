"""Contract for the 30408 timing discriminator.

The 3 % discharge on 2026-09-05 saw the inverter's output collapse at t~60s
with 30408=1 while 30100/30407/30409 still read 1/1/-3. Either the register
state does not expire but the POWER COMMAND does, or something unrelated moved
at the same moment. This operation asks that directly, and the verdict logic is
where the whole experiment lives — so it is tested far harder than the plumbing.

The rule the verdict must never break: an expiry cannot be inferred from a
command whose effect was never distinguishable in the first place. At 96 % SOC
with PV running, "the battery did not change" is the expected reading whether
or not 30408 does anything, and calling that NO_EXPIRY would be a false
negative dressed as a result.
"""
from __future__ import annotations

import pytest

from battery_optimizer_lib.control import ControlAction, InverterState
from battery_optimizer_lib.control.commissioning import (
    DEFAULT_DURATION_EFFECT_W,
    CommissioningSession,
)


class Recorder:
    def __init__(self):
        self.messages = []

    def __call__(self, message, level="INFO"):
        self.messages.append((message, level))


def session():
    return CommissioningSession.__new__(CommissioningSession)


def sample(t, battery_w, authority=1, remote=1, export_w=None):
    return (t, InverterState(control_authority=authority, remote_enabled=remote,
                             battery_power_w=battery_w, commanded_power=-3,
                             duration_minutes=2, grid_export_power_w=export_w))


def verdict(baseline_w, samples, expiry=120,
            threshold=DEFAULT_DURATION_EFFECT_W, commanded=-8):
    return CommissioningSession._duration_verdict(
        session(), InverterState(battery_power_w=baseline_w), samples,
        expiry, threshold, commanded_percent=commanded)


# --- stage 1: was an effect established at all? ---------------------------

def test_no_early_effect_is_inconclusive_not_no_expiry():
    """The false negative this operation exists to avoid."""
    flat = [sample(t, 100.0) for t in range(0, 200, 20)]
    out = verdict(100.0, flat)
    assert out.startswith("INCONCLUSIVE")
    assert "never produced a distinguishable effect" in out
    assert "NO_EXPIRY" not in out


def test_effect_just_under_the_threshold_is_inconclusive():
    samples = [sample(t, 100.0 - 200.0) for t in range(0, 200, 20)]
    assert verdict(100.0, samples).startswith("INCONCLUSIVE")


def test_unreadable_battery_power_is_inconclusive():
    samples = [(t, InverterState(control_authority=1, remote_enabled=1))
               for t in range(0, 200, 20)]
    assert verdict(100.0, samples).startswith("INCONCLUSIVE")


# --- stage 2: did it collapse near the expiry? -----------------------------

def test_collapse_while_armed_supports_expiry():
    samples = ([sample(t, -900.0) for t in (0, 20, 40, 60, 80, 100)]
               + [sample(t, 90.0) for t in (140, 160, 180)])
    out = verdict(100.0, samples)
    assert out.startswith("SUPPORTS_COMMAND_EXPIRY")
    assert "30100/30407 stayed 1/1" in out
    # The careful half of the claim: the command stops, the SESSION does not.
    assert "authority and the session do NOT" in out


def test_effect_persisting_past_expiry_is_no_expiry():
    samples = [sample(t, -900.0) for t in range(0, 200, 20)]
    out = verdict(100.0, samples)
    assert out.startswith("NO_EXPIRY_OBSERVED")


def test_collapse_with_registers_that_also_changed_says_nothing():
    """If the arm dropped too, the collapse is not evidence about 30408."""
    samples = ([sample(t, -900.0) for t in (0, 20, 40, 60, 80, 100)]
               + [sample(t, 90.0, authority=0, remote=0) for t in (140, 160, 180)])
    out = verdict(100.0, samples)
    assert out.startswith("INCONCLUSIVE")
    assert "did NOT stay armed" in out


def test_run_that_ends_before_the_expiry_window_is_inconclusive():
    samples = [sample(t, -900.0) for t in (0, 20, 40, 60, 80, 100)]
    out = verdict(100.0, samples)
    assert out.startswith("INCONCLUSIVE")
    assert "expiry window was never observed" in out


def test_partial_decay_is_not_a_collapse():
    """A 30% fade is the house changing, not a command being withdrawn."""
    samples = ([sample(t, -900.0) for t in (0, 20, 40, 60, 80, 100)]
               + [sample(t, -650.0) for t in (140, 160, 180)])
    assert verdict(100.0, samples).startswith("NO_EXPIRY_OBSERVED")


# --- the action's own invariants ------------------------------------------

def test_export_is_never_part_of_the_verdict():
    """This operation writes no export limit, so where the energy went is not
    a question it is entitled to answer."""
    samples = ([sample(t, -900.0, export_w=3000.0) for t in (0, 20, 40, 60, 80, 100)]
               + [sample(t, 90.0, export_w=3000.0) for t in (140, 160, 180)])
    out = verdict(100.0, samples)
    assert out.startswith("SUPPORTS_COMMAND_EXPIRY")
    assert "export" not in out.lower()


def test_duration_probe_writes_exactly_four_registers_and_no_export_limit():
    """The whole point of a separate action: the bare arm sequence."""
    from battery_optimizer_lib.config import BatteryOptimizerConfig
    from battery_optimizer_lib.control import InverterCommand, UpstreamVppBackend
    from battery_optimizer_lib.control.upstream_vpp import (
        REG_CONTROL_AUTHORITY, REG_EXPORT_LIMIT_ENABLE, REG_EXPORT_LIMIT_RATE,
        REG_REMOTE_DURATION, REG_REMOTE_ENABLE, REG_REMOTE_POWER, StepResult,
    )

    class Executor:
        name, can_write, can_read, commissioning = "fake", True, True, True

        def execute(self, step):
            return StepResult.OK

        def read_registers(self, start, count):
            return [0] * count

    class App:
        def log(self, message, level="INFO"):
            pass

        def get_state(self, entity):
            return None

    backend = UpstreamVppBackend(
        App(), BatteryOptimizerConfig(device_id="dev",
                                      control_mode="commissioning"),
        executor=Executor())
    assert backend.commissioning is True

    plan = backend.build_plan(InverterCommand(
        action=ControlAction.DURATION_PROBE, power_percent=3,
        duration_minutes=2))
    written = [(s.register, s.value) for s in plan.steps]

    assert written == [
        (REG_REMOTE_DURATION, 2),
        (REG_REMOTE_POWER, -3),
        (REG_CONTROL_AUTHORITY, 1),
        (REG_REMOTE_ENABLE, 1),
    ]
    registers = {r for r, _ in written}
    assert REG_EXPORT_LIMIT_ENABLE not in registers
    assert REG_EXPORT_LIMIT_RATE not in registers


# ---------------------------------------------------------------------------
# Regression: the real 2026-09-08 run, which the first verdict got backwards
# ---------------------------------------------------------------------------

# Commanded -8%. Baseline -664 W. The battery discharged HARDER (-1060 W), then
# collapsed to -142 W while the house moved onto the grid, with 30100/30407
# still 1/1. The original metric scored that collapse as 522 W of "effect" --
# larger than the 396 W of real effect -- because it measured absolute distance
# from the baseline instead of movement toward the command, and so reported
# NO_EXPIRY_OBSERVED for a run that is the clearest expiry evidence we have.
REAL_RUN = (
    [(0, -763.0)]
    + [(t, -1060.0) for t in range(5, 50, 5)]
    + [(t, -1018.0) for t in range(50, 95, 5)]
    + [(t, -100.0) for t in range(95, 140, 5)]
    + [(t, -142.0) for t in range(140, 185, 5)]
)


def test_the_real_run_is_read_as_a_collapse_not_a_stronger_effect():
    samples = [sample(t, w) for t, w in REAL_RUN]
    out = verdict(-664.0, samples, expiry=120, commanded=-8)

    assert out.startswith("SUPPORTS_COMMAND_EXPIRY"), out
    assert "30100/30407 stayed 1/1" in out
    # It must also name the measurement limit rather than claim 30408 timing.
    assert "does NOT by itself establish" in out


def test_the_real_run_reports_the_grid_serving_overshoot():
    """Collapsing PAST the baseline is a different fact from merely stopping."""
    samples = [sample(t, w) for t, w in REAL_RUN]
    out = verdict(-664.0, samples, expiry=120, commanded=-8)
    assert "PAST the baseline" in out
    assert "grid-serving standby" in out


def test_a_positive_command_is_measured_in_its_own_direction():
    """The sign of the setpoint decides which way 'toward the command' is."""
    charging = ([sample(t, 900.0) for t in (0, 20, 40, 60, 80, 100)]
                + [sample(t, -50.0) for t in (140, 160, 180)])
    out = verdict(100.0, charging, expiry=120, commanded=+8)
    assert out.startswith("SUPPORTS_COMMAND_EXPIRY"), out


def test_a_late_average_not_a_late_maximum():
    """One lingering sample must not hide a collapse."""
    samples = ([sample(t, -900.0) for t in (0, 20, 40, 60, 80, 100)]
               + [sample(140, -900.0), sample(160, 90.0), sample(180, 90.0)])
    out = verdict(100.0, samples, expiry=120, commanded=-8)
    assert out.startswith("SUPPORTS_COMMAND_EXPIRY"), out


# ---------------------------------------------------------------------------
# High-resolution verdict (31200/31201 at ~5s)
# ---------------------------------------------------------------------------

def fast_verdict(baseline_w, samples, armed=True, expiry=120, commanded=-8,
                 threshold=DEFAULT_DURATION_EFFECT_W):
    checks = [(t, InverterState(control_authority=1 if armed else 0,
                                remote_enabled=1 if armed else 0))
              for t in (0, 30, 60, 90, 120, 150)]
    return CommissioningSession._duration_verdict_fast(
        session(), baseline_w, samples, checks, expiry, threshold,
        commanded_percent=commanded)


def ramp(collapse_at, baseline=-664.0, driven=-1060.0, after=-120.0,
         end=180, step=5):
    """A run that works, then collapses at a known time."""
    return [(float(t), driven if t < collapse_at else after)
            for t in range(0, end + step, step)]


def test_collapse_time_is_reported_at_sample_resolution():
    out = fast_verdict(-664.0, ramp(120), expiry=120)
    assert "COLLAPSE_AT=120s" in out
    assert out.startswith("COLLAPSE_OBSERVED")


def test_one_run_refuses_to_claim_30408_governs_the_timing():
    """The matched pair is the discriminator; a single run is not."""
    out = fast_verdict(-664.0, ramp(120), expiry=120)
    assert "does NOT say whether" in out
    assert "matched run" in out


def test_a_single_low_sample_is_not_a_collapse():
    """One dip is a measurement; two in a row is a transition."""
    samples = ramp(999)                       # never collapses
    samples[8] = (samples[8][0], -120.0)      # one isolated dip
    out = fast_verdict(-664.0, samples, expiry=120)
    assert out.startswith("NO_EXPIRY_OBSERVED")
    assert "COLLAPSE_AT=none" in out


def test_dropped_samples_are_tolerated_not_fatal():
    """Bus contention with the coordinator's poll loses samples; that must
    degrade resolution, not invalidate the run."""
    samples = [(t, None) if t in (25, 30, 95) else (t, w)
               for t, w in ramp(120)]
    out = fast_verdict(-664.0, samples, expiry=120)
    assert out.startswith("COLLAPSE_OBSERVED")


def test_too_few_readable_samples_is_inconclusive():
    samples = [(float(t), None) for t in range(0, 185, 5)]
    samples[0] = (0.0, -1060.0)
    assert fast_verdict(-664.0, samples).startswith("INCONCLUSIVE")


def test_unarmed_registers_make_the_timing_meaningless():
    out = fast_verdict(-664.0, ramp(120), armed=False, expiry=120)
    assert out.startswith("INCONCLUSIVE")
    assert "did NOT stay armed" in out


def test_the_matched_pair_is_what_discriminates():
    """The comparison this whole experiment exists to make."""
    at_60 = fast_verdict(-664.0, ramp(60, end=180), expiry=60)
    at_120 = fast_verdict(-664.0, ramp(120, end=180), expiry=120)
    assert "COLLAPSE_AT=60s" in at_60 and "DURATION=60s" in at_60
    assert "COLLAPSE_AT=120s" in at_120 and "DURATION=120s" in at_120

    # A fixed timeout would look like this instead: both collapse together.
    fixed_a = fast_verdict(-664.0, ramp(75, end=180), expiry=60)
    fixed_b = fast_verdict(-664.0, ramp(75, end=180), expiry=120)
    assert "COLLAPSE_AT=75s" in fixed_a and "COLLAPSE_AT=75s" in fixed_b


# ---------------------------------------------------------------------------
# End to end. The verdict functions were unit-tested while duration_test itself
# was not, so an AttributeError on the backend reached real hardware on
# 2026-09-08 (it failed before arming, but only by luck of ordering). These
# drive the whole operation.
# ---------------------------------------------------------------------------

from battery_optimizer_lib.control import (
    SendResult, VerifyResult, VerifyVerdict)
from battery_optimizer_lib.control.upstream_vpp import SessionState


class FakeBackend:
    """A backend that arms, samples a scripted power curve, and releases."""

    commissioning = True
    dry_run = False

    def __init__(self, curve, baseline=-664.0):
        self.curve = list(curve)
        self.baseline = baseline
        self.calls = 0
        self.sent = []
        self.session_state = SessionState.NOT_ARMED
        self.released = False
        self.lease = None
        self.armed = False

    def _log(self, message, level="INFO"):
        pass

    def reconcile(self):
        return self.read_state()

    def read_state(self):
        return InverterState(
            control_authority=1 if self.armed else 0,
            remote_enabled=1 if self.armed else 0,
            duration_minutes=1, commanded_power=-8 if self.armed else 0,
            tou_period_count=0, export_limit_enabled=0, export_limit_rate=0,
            priority_mode=0, ac_charge_mode=0,
            soc_percent=60.0, battery_power_w=self.baseline)

    def read_battery_power_w(self):
        if not self.armed:
            return self.baseline
        value = self.curve[min(self.calls, len(self.curve) - 1)]
        self.calls += 1
        return value

    def send(self, command):
        self.sent.append(command)
        self.armed = True
        self.session_state = SessionState.ACTIVE
        return SendResult.CONFIRMED

    def verify(self, command, state):
        return VerifyResult(VerifyVerdict.MATCH,
                            actual="30100=1 30407=1 30409=-8")

    def release(self):
        self.armed = False
        self.released = True
        self.session_state = SessionState.RELEASED
        return True

    @property
    def safe_to_stop(self):
        return True

    def describe_mode(self):
        return "fake"


def run_duration_test(curve, read_cost=0.0, **kwargs):
    """Drive the operation with a clock that only WAITING advances.

    A clock that ticked on every call would make the loop's own bookkeeping
    reads look like elapsed time; here a register read costs `read_cost`
    seconds, which is how bus contention is modelled.
    """
    from battery_optimizer_lib.control.commissioning import CommissioningSession
    backend = FakeBackend(curve)
    now = [0.0]

    def clock():
        return now[0]

    def wait(seconds):
        now[0] += seconds

    original = backend.read_battery_power_w

    def timed_read():
        now[0] += read_cost
        return original()

    backend.read_battery_power_w = timed_read
    sess = CommissioningSession(backend, log_func=lambda *a, **k: None,
                                clock=clock)
    sess.release = backend.release
    sess._release_and_report = lambda op, wait, t, p, summary, steps_ok=True: (
        backend.release() and None) or __import__(
        "battery_optimizer_lib.control.commissioning", fromlist=["x"]
    ).CommissioningResult(operation=op, ok=steps_ok, detail=summary)
    result = sess.duration_test(wait=wait, power_percent=8,
                                duration_minutes=1, observe_seconds=180,
                                **kwargs)
    return sess, backend, result


def test_duration_test_runs_end_to_end_and_releases():
    """The smoke test whose absence let an AttributeError reach the inverter."""
    curve = [-1060.0] * 13 + [-120.0] * 40
    sess, backend, result = run_duration_test(curve)

    assert backend.sent, "nothing was ever commanded"
    assert backend.sent[0].action is ControlAction.DURATION_PROBE
    assert backend.released is True
    assert len(sess.fast_samples) > 20
    assert "COLLAPSE" in result.detail or "EXPIRY" in result.detail


def test_duration_test_refuses_without_a_high_resolution_baseline():
    """A 60s sensor baseline against 5s samples would bake in coordinator lag."""
    from battery_optimizer_lib.control.commissioning import CommissioningSession
    backend = FakeBackend([-1060.0] * 40)
    backend.read_battery_power_w = lambda: None
    sess = CommissioningSession(backend, log_func=lambda *a, **k: None)

    result = sess.duration_test(wait=lambda s: None, power_percent=8,
                                duration_minutes=1, observe_seconds=180)

    assert result.refused is True
    assert "31200/31201" in result.detail
    assert backend.sent == [], "it must refuse BEFORE arming"


def test_a_slow_read_does_not_stretch_the_timeline():
    """When a read blocks behind the coordinator's bus lock, elapsed time must
    stay honest and the loop must not wait an extra interval on top."""
    curve = [-1060.0] * 13 + [-120.0] * 60
    sess, _backend, _result = run_duration_test(curve, read_cost=3.0)

    stamps = [t for t, _w in sess.fast_samples]
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    # Each pass costs 3s of reading, so the loop should wait only the
    # remaining 2s -- never 5s on top of the read.
    assert max(gaps) <= 5.5, gaps
    # The run covers the window to within one interval; the loop stops as soon
    # as elapsed reaches it rather than overshooting.
    assert stamps[-1] >= 180 - 5.5, stamps[-1]
