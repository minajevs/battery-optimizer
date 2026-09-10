"""Control actions — the one place a ScheduleEntry becomes an inverter action.

Before this module the mapping lived twice: once in ``DirectControl`` (which
sent it) and once in ``schedule_formatter.resolve_wit_mode`` (which displayed
it), with a comment in the latter promising to mirror the former.  Two copies of
a rule that decides whether the battery exports to the grid is one copy too
many, so both now delegate here.
"""

from __future__ import annotations

import enum
from typing import Optional

from ..models import BatteryMode, ScheduleEntry


class ControlAction(enum.Enum):
    """What the inverter should be doing for one slot.

    The values are the wire-level mode strings the previous ``set_wit_mode``
    backend used.  They are kept identical so logs, the schedule display and
    any stored diagnostics read the same across the backend change.
    """

    GRID_CHARGE = "grid_charge"
    HOLD = "hold"
    DISCHARGE_TO_LOAD = "discharge_to_load"
    DISCHARGE_TO_GRID = "discharge_to_grid"
    MAX_EXPORT = "max_export"
    PASSTHROUGH = "passthrough"

    # Commissioning only, and never produced by resolve_action(): a bare timed
    # override used to ask whether 30408 bounds the ENERGETIC effect of 30409
    # while the control registers stay armed. It is deliberately NOT a
    # discharge: is_discharge pulls in the cutoff-SOC write and groups the
    # action into the discharge effect family, and this operation must write
    # nothing but 30408/30409/30100/30407. build_plan() refuses it outside
    # commissioning mode.
    DURATION_PROBE = "duration_probe"

    @property
    def holds_session(self) -> bool:
        """True when the action needs an active VPP session (30100/30407 = 1/1).

        PASSTHROUGH is the only action that gives the inverter back to its own
        local logic.
        """
        return self is not ControlAction.PASSTHROUGH

    @property
    def is_discharge(self) -> bool:
        return self in (
            ControlAction.DISCHARGE_TO_LOAD,
            ControlAction.DISCHARGE_TO_GRID,
            ControlAction.MAX_EXPORT,
        )

    @property
    def exports_to_grid(self) -> bool:
        """True when the action is expected to push power out to the grid."""
        return self in (ControlAction.DISCHARGE_TO_GRID, ControlAction.MAX_EXPORT)


def resolve_action(
    entry: ScheduleEntry, default_power_percent: int = 100
) -> ControlAction:
    """Map a ScheduleEntry to the action the inverter should be told to perform.

    DISCHARGE splits three ways on ``export_rate``, and the split is deliberate:
    the default is DISCHARGE_TO_LOAD so that a missing export_rate can never
    become an accidental export.
    """
    mode = entry.mode

    if mode == BatteryMode.CHARGE:
        return ControlAction.GRID_CHARGE

    if mode == BatteryMode.DISCHARGE:
        export_rate = entry.export_rate
        if export_rate is not None and export_rate > 0:
            if export_rate >= 100 and default_power_percent >= 100:
                return ControlAction.MAX_EXPORT
            return ControlAction.DISCHARGE_TO_GRID
        # Default: no accidental export.
        return ControlAction.DISCHARGE_TO_LOAD

    return ControlAction.HOLD


def resolve_ac_charge_mode(
    entry: ScheduleEntry,
    pv_power: Optional[float] = None,
    pv_threshold: float = 0.0,
) -> str:
    """Intent for AC charging: "disabled" / "pv_priority" / "ac_priority".

    An explicit ``entry.ac_charge_mode`` always wins; otherwise a CHARGE slot
    picks pv_priority when PV is already producing above the threshold.

    NOTE: on the reference WIT firmware register 30410 accepts only 0 and 1 —
    value 2 ("AC priority") is rejected with Illegal Function.  This function
    therefore expresses *intent*; the backend decides what that intent can
    actually be written as.
    """
    if entry.ac_charge_mode is not None:
        return entry.ac_charge_mode

    if entry.mode == BatteryMode.CHARGE:
        if pv_power is not None and pv_power > pv_threshold:
            return "pv_priority"
        return "ac_priority"

    return "disabled"
