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
        inverter is being asked to do*, not about the watchdog window.
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
    the missing half of the 30100/30407 safe-state pair, without which a
    passthrough (0/0) cannot be told from the hazardous VPP-standby state (1/0).
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

    # Measured power, for EFFECT. Signs are OPPOSITE by upstream convention:
    #   battery_power_w: positive = charging
    #   grid_power_w:    positive = EXPORTING
    battery_power_w: Optional[float] = None
    grid_power_w: Optional[float] = None
    soc_percent: Optional[float] = None

    @property
    def in_vpp_standby(self) -> bool:
        """The documented hazard state: authority granted, remote control off.

        Local battery logic is suspended and load is drawn from the grid.
        """
        return self.control_authority == 1 and self.remote_enabled == 0

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
