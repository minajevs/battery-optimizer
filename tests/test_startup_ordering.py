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
        position("self._heartbeat.stamp()")


def initialize_body() -> str:
    """The text of initialize(), which is where the ordering must hold.

    _stamp_heartbeat() also stamps — that is the periodic timer and is fine.
    Only the startup path is constrained.
    """
    start = SOURCE.index("    def initialize(self")
    end = SOURCE.index("\n    def ", start + 1)
    return SOURCE[start:end]


def test_the_heartbeat_is_stamped_exactly_once_during_initialize():
    """A second stamp inside initialize() could precede recovery again."""
    assert initialize_body().count("self._heartbeat.stamp()") == 1


def test_scheduled_execution_is_gated_on_startup_recovery():
    execute = SOURCE.index("def execute_scheduled_mode(")
    override = SOURCE.index("_is_override_active()", execute)
    gate = SOURCE.index("_startup_ready()", execute)
    assert gate < override, (
        "the startup gate must come before the slot logic, not after it")


def test_only_a_failed_recovery_retries():
    """RECOVERY_BLOCKED and FOREIGN_AUTHORITY need a person, not a timer."""
    assert "self._startup.should_retry" in SOURCE
    assert "OptimizerLifecycle.RECOVERY_FAILED" in SOURCE
