"""The startup ORDER in battery_optimizer.py, which no unit test can reach.

The orchestrator is not unit-tested (see CLAUDE.md), and the invariant here is
purely about the order of three statements, so this pins it by reading the
source. A behavioural test would need a live AppDaemon.

The invariant: the heartbeat on disk belongs to the PREVIOUS instance and is
the only evidence that that instance is gone. It must be READ by startup
recovery before a fresh stamp destroys it.

    construct Heartbeat        (no stamp)
        -> recover_previous_session(...)
            -> heartbeat.stamp()

Get this backwards and the failure is silent and permanent: the app comes up,
stamps, and the reaper reads OWNER_ALIVE — correctly — and refuses forever
while the inverter stays armed.
"""
from __future__ import annotations

import pathlib

import pytest

SOURCE = (pathlib.Path(__file__).parent.parent
          / "appdaemon" / "apps" / "battery_optimizer.py").read_text()


def position(needle: str) -> int:
    index = SOURCE.find(needle)
    assert index != -1, f"{needle!r} is not in battery_optimizer.py"
    return index


def test_the_heartbeat_is_constructed_before_startup_recovery_reads_it():
    assert position("self._heartbeat = Heartbeat(") < \
        position("recover_previous_session(")


def test_startup_recovery_runs_before_the_heartbeat_is_stamped():
    """The one that matters. Stamping first erases the evidence."""
    assert position("recover_previous_session(") < \
        position("self._stamp_heartbeat()")


def initialize_body() -> str:
    """The text of initialize(), which is where the ordering must hold.

    _stamp_heartbeat() also stamps — that is the periodic timer and is fine.
    Only the startup path is constrained.
    """
    start = SOURCE.index("    def initialize(self")
    end = SOURCE.index("\n    def ", start + 1)
    return SOURCE[start:end]


def test_initialize_triggers_exactly_one_stamp():
    """A second stamp inside initialize() could precede recovery again."""
    assert initialize_body().count("self._stamp_heartbeat()") == 1


def test_scheduled_execution_is_gated_on_startup_recovery():
    execute = SOURCE.index("def execute_scheduled_mode(")
    override = SOURCE.index("_is_override_active()", execute)
    gate = SOURCE.index("_startup_ready()", execute)
    assert gate < override, (
        "the startup gate must come before the slot logic, not after it")


def test_the_retry_timer_and_the_retry_guard_agree():
    """They must both defer to should_retry, or they drift apart.

    They did: should_retry gained INVERTER_UNREADABLE, the guard kept naming
    RECOVERY_FAILED, and the app sat forever firing a timer that refused to
    act and logged nothing (live, 2026-09-10).
    """
    start = SOURCE.index("    def _retry_startup_recovery(")
    end = SOURCE.index("\n    def ", start + 1)
    guard = SOURCE[start:end]

    assert "self._startup.should_retry" in guard, (
        "the retry guard must ask should_retry, not name a lifecycle itself")
    assert "OptimizerLifecycle." not in guard.split('"""')[-1], (
        "no lifecycle may be named in the guard body — that is how it drifted")
    # And the scheduling side asks the same question.
    assert SOURCE.count("self._startup.should_retry") >= 2


def test_the_heartbeat_is_only_stamped_once_the_lifecycle_is_ready():
    """The invariant the native watcher will ultimately trust.

    The heartbeat is not "the process exists" — it is the claim the reaper
    reads as OWNER_ALIVE and stands down on. An app in RECOVERY_FAILED is an
    owner that has NOT finished its session, so stamping there would tell the
    one thing that could clean up to do nothing.
    """
    start = SOURCE.index("    def _stamp_heartbeat(")
    end = SOURCE.index("\n    def ", start + 1)
    body = SOURCE[start:end]

    assert "OptimizerLifecycle.READY" in body, (
        "_stamp_heartbeat must gate on the lifecycle")
    guard = body.index("OptimizerLifecycle.READY")
    stamp = body.index("self._heartbeat.stamp()")
    assert guard < stamp, "the guard must precede the stamp"


def test_initialize_stamps_through_the_guarded_helper_not_directly():
    """A direct stamp in initialize() would bypass the READY gate."""
    body = initialize_body()
    assert "self._heartbeat.stamp()" not in body, (
        "initialize() must stamp via _stamp_heartbeat(), which enforces READY")
    assert "self._stamp_heartbeat()" in body


def test_liveness_is_published_only_from_the_ready_gated_stamp():
    """The sensor is the watcher's only view of this app, so it must carry the
    same meaning as the heartbeat file: alive AND owning its session properly.
    Publishing it from anywhere ungated would make a stuck app look healthy."""
    start = SOURCE.index("    def _stamp_heartbeat(")
    end = SOURCE.index("\n    def _publish_liveness(")
    body = SOURCE[start:end]

    guard = body.index("OptimizerLifecycle.READY")
    publish = body.index("self._publish_liveness()")
    assert guard < publish, "the READY guard must precede the publish"
    assert SOURCE.count("self._publish_liveness()") == 1, (
        "one call site only — a second could bypass the gate")


def test_the_liveness_state_changes_every_beat():
    """A constant state ('ready') is indistinguishable from a stopped app in
    HA's recorder, which stores state CHANGES. That trap already produced a
    phantom stall once while diagnosing the Modbus link."""
    start = SOURCE.index("    def _publish_liveness(")
    end = SOURCE.index("\n    def ", start + 1)
    body = SOURCE[start:end]

    assert "state=self.datetime().isoformat()" in body, (
        "the state must be the timestamp, not a constant")
    assert "lifecycle" in body and "pid" in body and "control_mode" in body


def test_publishing_cannot_break_the_heartbeat():
    """The file is what the reaper reads and is the more important of the two."""
    start = SOURCE.index("    def _publish_liveness(")
    end = SOURCE.index("\n    def ", start + 1)
    body = SOURCE[start:end]
    assert "except Exception" in body

    stamp_start = SOURCE.index("    def _stamp_heartbeat(")
    stamp_body = SOURCE[stamp_start:SOURCE.index("\n    def _publish_liveness(")]
    assert stamp_body.index("self._heartbeat.stamp()") < \
        stamp_body.index("self._publish_liveness()"), (
        "stamp the file first; the sensor is the derived signal")
