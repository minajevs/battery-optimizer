"""The seam between optimizer policy and one particular inverter integration.

``DirectControl`` owns policy — deduplication, the bounded verify ladder,
outcome accounting, diagnostics.  A ``ControlBackend`` owns everything that
knows what a Growatt register is.  Nothing above this file may mention a
register number or a Home Assistant service name.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

try:  # pragma: no cover - typing.Protocol exists on every supported runtime
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore[assignment]

from .actions import ControlAction


class SendResult(enum.Enum):
    """What happened to one command the backend was asked to transmit.

    ``RATE_LIMITED`` is separate from ``FAILED`` on purpose.  The integration
    enforces a 30 s per-register cooldown on the VPP control registers; hitting
    it means the command was *not applied*, but it is not evidence that the
    inverter or the connection is unhealthy.  Counting it as a failure would
    escalate on a healthy system; counting it as a success would silently drop
    a safety command.  It means "deferred — retry after the cooldown".
    """

    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"
    RATE_LIMITED = "rate_limited"
    FAILED = "failed"
    DRY_RUN = "dry_run"
    # Accepted and in progress, completing on a scheduled retry. Used by the
    # release lifecycle, where revoking authority can be blocked by the very
    # cooldown our own acquisition stamped.
    PENDING = "pending"


class VerifyVerdict(enum.Enum):
    """Level 1 (ACK): did the inverter accept the configuration we sent?"""

    MATCH = "match"
    MISMATCH = "mismatch"
    UNVERIFIABLE = "unverifiable"


class EffectVerdict(enum.Enum):
    """Level 3 (EFFECT): is the inverter actually trading?

    ``INDETERMINATE`` is what keeps the grid-charge fallback honest.  A battery
    charging from PV surplus, or a discharge fully absorbed by house load, is
    not evidence of failure — and must never latch a fallback.
    """

    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class InverterCommand:
    """One normalized, backend-agnostic instruction for a slot."""

    action: ControlAction
    power_percent: int = 100
    duration_minutes: int = 20
    export_rate: Optional[int] = None
    ac_charge_mode: Optional[str] = None
    charge_cutoff_soc: Optional[int] = None
    discharge_cutoff_soc: Optional[int] = None
    reason: str = ""

    def dedup_key(self) -> Tuple[Any, ...]:
        """Fields that make two commands meaningfully identical.

        ``power_percent`` and ``duration_minutes`` are deliberately excluded,
        preserving the original behaviour: a duplicate is about *what the
        inverter is being asked to do*, not about the duration field — which
        this hardware does not enforce anyway.
        """
        return (
            self.action,
            self.export_rate,
            self.ac_charge_mode,
            self.charge_cutoff_soc,
            self.discharge_cutoff_soc,
        )

    def describe(self) -> str:
        return (
            f"{self.action.value} power={self.power_percent}% "
            f"duration={self.duration_minutes}min "
            f"export={self.export_rate if self.export_rate is not None else '-'} "
            f"ac={self.ac_charge_mode or '-'} "
            f"soc=[{self.discharge_cutoff_soc if self.discharge_cutoff_soc is not None else '-'}"
            f"-{self.charge_cutoff_soc if self.charge_cutoff_soc is not None else '-'}]"
        )


@dataclass(frozen=True)
class InverterState:
    """Everything read back from the inverter that a decision depends on.

    Deliberately wider than "what did we just send": ``control_authority`` is
    the missing half of the 30100/30407 pair, without which a passthrough (0/0)
    cannot be told from authority held without remote control (1/0).
    """

    control_authority: Optional[int] = None
    remote_enabled: Optional[int] = None
    duration_minutes: Optional[int] = None
    commanded_power: Optional[int] = None
    ac_charge_mode: Optional[int] = None
    charge_cutoff_soc: Optional[int] = None
    discharge_cutoff_soc: Optional[int] = None
    tou_period_count: Optional[int] = None
    export_limit_enabled: Optional[int] = None
    export_limit_rate: Optional[int] = None
    priority_mode: Optional[int] = None
    vpp_setpoint_mirror: Optional[int] = None

    # Measured power, for EFFECT.
    #
    # ``battery_power_w`` is NORMALIZED by the backend — positive = charging,
    # negative = discharging — whatever polarity the sensor itself reports.
    # Policy above this line never has to know the hardware convention.
    battery_power_w: Optional[float] = None
    battery_power_raw_w: Optional[float] = None
    # Grid flow as two ALWAYS-POSITIVE directional readings. At most one is
    # meaningfully non-zero at a time, and no integration option can invert
    # them, which is why these and not the signed value decide whether energy
    # was bought or sold.
    grid_import_power_w: Optional[float] = None
    grid_export_power_w: Optional[float] = None
    # Signed grid power as the integration reports it. DIAGNOSTIC ONLY: its
    # sign flips with the integration's `invert_grid_power` option, so it must
    # never be what decides a trade.
    grid_power_w: Optional[float] = None
    soc_percent: Optional[float] = None

    @property
    def authority_without_remote(self) -> bool:
        """30100=1 with 30407=0: authority granted, remote control not enabled.

        Upstream documents this pair as "VPP standby" — local battery logic
        suspended, load drawn from the grid. **That description does not match
        this hardware.** On 2026-09-03 the reference WIT was observed at 1/0
        while discharging 3.8 kW and exporting 2.6 kW, with SOC falling 31 % ->
        28 %: manifestly not a suspended battery, and not load drawn from the
        grid either.

        So this is reported as a documented DISCREPANCY worth a warning, not as
        a hazard, and nothing recovers, releases or escalates on the strength
        of the register pair alone. What DOES still act is a failed arm of our
        own (see ``UpstreamVppBackend._enter_arm_failed``) — there the trigger
        is direct knowledge that this process left a command half-applied, not
        an inference drawn from two register values.
        """
        return self.control_authority == 1 and self.remote_enabled == 0

    @property
    def external_scheduler_present(self) -> bool:
        """30411 > 0: some other scheduler has written a TOU schedule here.

        This project never writes a TOU period — not in any plan, in any mode
        (see fact 4 in ``upstream_vpp``'s module docstring, and
        ``test_no_plan_ever_writes_the_tou_schedule``). So a non-zero period
        count cannot be ours, and is positive evidence of a second scheduler,
        which is stronger than what 30100 alone supports.

        Confirmed on the reference WIT on 2026-09-03: with Growatt Smart
        Scheduling enabled the inverter held 16 periods; switching it off in
        the Growatt dashboard zeroed 30411 and all 60 period registers, and
        they stayed zero over the following 44 h. The schedule is Smart
        Scheduling's, it is written and cleared by it, and nothing else here
        authors one.

        None means 30411 was not read, which is not evidence of absence — the
        commissioning preflight refuses on the unread register rather than
        reading this property as False.
        """
        return self.tou_period_count is not None and self.tou_period_count > 0

    @property
    def authority_held(self) -> bool:
        """30100=1, whoever set it.

        Not evidence of who is controlling the inverter: 30100 is a register,
        and it has been observed set on a WIT that was running its own
        schedule. It only means the authority is not ours to build on.
        """
        return self.control_authority == 1

    def describe(self) -> str:
        return (
            f"auth={self.control_authority} remote={self.remote_enabled} "
            f"power={self.commanded_power} duration={self.duration_minutes} "
            f"ac={self.ac_charge_mode} tou={self.tou_period_count} "
            f"priority={self.priority_mode}"
        )


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of comparing intent against the inverter's reported state."""

    verdict: VerifyVerdict
    actual: str = ""
    effect: EffectVerdict = EffectVerdict.INDETERMINATE
    detail: str = ""

    @property
    def matched(self) -> bool:
        return self.verdict is VerifyVerdict.MATCH

    @property
    def unverifiable(self) -> bool:
        return self.verdict is VerifyVerdict.UNVERIFIABLE


class ControlBackend(Protocol):
    """What DirectControl needs from any inverter integration."""

    name: str

    def send(self, command: InverterCommand) -> SendResult:
        """Apply one command. Must not raise for ordinary inverter failures."""

    def release(self) -> SendResult:
        """Return the inverter to local control."""

    def read_state(self) -> Optional[InverterState]:
        """Read back actual inverter state, or None when unreadable."""

    def verify(
        self, command: InverterCommand, state: Optional[InverterState]
    ) -> VerifyResult:
        """Compare a command against read-back state."""

    def get_diagnostics(self) -> Dict[str, Any]:
        """Backend-specific counters merged into the health sensor."""
