"""Growatt WIT VPP control over the upstream 0xAHA/Growatt_ModbusTCP services.

This is the ONLY module that knows a register number.

Upstream has no ``set_wit_mode``, so one fork service call becomes a sequence of
VPP register writes.  Three documented hardware facts shape every sequence here,
and each one has already cost a design revision:

1. **Pair discipline.** 30100/30407 are written as a pair; 0/0 and 1/1 are the
   two states this project ever aims for. Upstream documents 1/0 as "VPP
   standby" — local logic suspended, load drawn from the grid — but the
   reference WIT was observed at 1/0 discharging 3.8 kW and exporting 2.6 kW
   (2026-09-03), so that description is treated as an unresolved DISCREPANCY
   to warn about, not as a hazard to recover from. Nothing acts on 1/0 alone.
2. **Write order.** 30408 (duration) -> 30409 (power) -> 30407 (enable) LAST.
   Arming first applies whatever stale setpoint 30409 still held.
3. **EEPROM.** Only 30407/30408/30409 are marked "Not storage" and safe to
   write every slot.  Everything else is written only when it changes.
4. **The base TOU schedule is not ours.** A timed VPP override is documented as
   an override: when it expires the inverter returns to its own base schedule.
   Nothing here needs 30411 cleared to run a session, and the reference
   installation was found holding a schedule of its own.  So no plan writes
   30411 at all.

   The origin is no longer a guess.  On 2026-09-03 the reference WIT held 16
   periods with Growatt Smart Scheduling enabled; turning Smart Scheduling off
   in the Growatt dashboard zeroed 30411 and all 60 period registers, and they
   were still zero 44 h later.  Growatt Smart Scheduling writes that schedule
   and clears it.

   That makes 30411 the one register that identifies a second scheduler
   POSITIVELY.  30100=1 says only that authority is held — this inverter has
   been seen holding it while running its own logic — but a non-zero period
   count cannot be ours, because no code path here writes one.  So
   ``30411 > 0`` is an INTERLOCK: no session-holding write is transmitted
   while another scheduler's schedule is loaded.  Releasing is never blocked
   by it; giving the inverter back is always allowed.

STATUS: this slice builds and logs sequences.  Execution is delegated to an
``executor``; the default one is dry-run and performs no I/O whatsoever.
"""

from __future__ import annotations

import datetime
import enum
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .actions import ControlAction
from .lease import ACTIVE as LEASE_ACTIVE, SessionLease
from .backend import (
    EffectVerdict,
    InverterCommand,
    InverterState,
    SendResult,
    VerifyResult,
    VerifyVerdict,
)

# --- VPP register map (WIT, profiles/wit.py:435-582) -----------------------
REG_CONTROL_AUTHORITY = 30100
REG_EXPORT_LIMIT_ENABLE = 30200
REG_EXPORT_LIMIT_RATE = 30201
REG_CHARGE_CUTOFF_SOC = 30404
REG_DISCHARGE_CUTOFF_SOC = 30405
REG_REMOTE_ENABLE = 30407
REG_REMOTE_DURATION = 30408
REG_REMOTE_POWER = 30409
REG_AC_CHARGE_ENABLE = 30410
REG_TOU_NUM_PERIODS = 30411
REG_TOU_PERIOD1_BASE = 30412
REG_SETPOINT_MIRROR = 30474
REG_PRIORITY_MODE = 30476

# Registers the integration rate-limits to one FC06 write per 30 s, each
# (growatt_modbus.py:1018-1033). The timestamp is stamped on SUCCESS, which is
# why a successful 30100=1 blocks the 30100=0 that must roll it back.
RATE_LIMITED_REGISTERS = frozenset({
    201, 202, 203,
    REG_CONTROL_AUTHORITY,
    REG_EXPORT_LIMIT_ENABLE,
    REG_EXPORT_LIMIT_RATE,
    REG_REMOTE_ENABLE,
    REG_REMOTE_DURATION,
    REG_REMOTE_POWER,
})

WIT_COOLDOWN_SECONDS = 30

# The TOU fallback for grid charging is NOT implemented, deliberately. It would
# have to overwrite the inverter's existing TOU schedule, which we neither
# authored nor can restore. Until VPP grid charging is proven or disproven on
# real hardware, an unambiguous EFFECT failure releases control and escalates
# instead. If it is ever built it must first snapshot the whole schedule and
# have a tested restore path.
TOU_FALLBACK_ENABLED = False

# The only actions commissioning mode will transmit. Everything that moves
# energy for money -- grid charge, either discharge, max export -- is excluded:
# commissioning proves the SESSION machinery (take authority, arm, rewrite the
# duration, release) and nothing else. HOLD is the least energetic way to do
# that: +1 % keeps a session alive while moving ~100-150 W into the battery and
# putting the house load on the grid -- small, but not nothing.
COMMISSIONING_ACTIONS = frozenset({
    ControlAction.HOLD,
    ControlAction.PASSTHROUGH,
})

# 30409 value that means "hold". NOT 0 — that is documented as "suspend forced
# cycle (passthrough)", and 0 was observed clipping PV.
#
# It is a LITERAL SMALL CHARGE REQUEST, not a neutral sentinel. Measured on the
# reference WIT (2026-09-05): battery discharge stopped and turned to roughly
# 100-150 W of charge, with the house load transferred to the grid (~390 W
# imported), reverting within seconds of the release. That is the wanted
# behaviour for a reserve/HOLD — the battery stops serving the house — but it
# is bought by importing, and anything reasoning about cost must treat it as a
# small purchase rather than as nothing happening.
HOLD_POWER_PERCENT = 1


def encode_unsigned(value: int) -> int:
    """Two's-complement encode for FC10, whose schema rejects negatives."""
    return value & 0xFFFF


def decode_signed(raw: int) -> int:
    """Decode a 16-bit register that carries a signed value."""
    return raw - 65536 if raw > 32767 else raw


class PriorityModeCapability(enum.Enum):
    """How much we actually know about register 30476 on THIS inverter.

    Write-same proves only that FC06 to the address was accepted; it does not
    prove the value can be changed.  Only CONFIRMED_WRITABLE licenses using
    30476=1 in normal operation, and reaching it requires a supervised
    value-changing probe (it mutates the inverter's base mode).
    """

    UNKNOWN = "unknown"
    WRITE_ACCEPTED = "write_accepted"
    CONFIRMED_WRITABLE = "confirmed_writable"
    REJECTED = "rejected"


class SessionState(enum.Enum):
    """Where the VPP session is.

    Two of these mean "do NOT stop AppDaemon right now":

    * ``RELEASE_PENDING`` — a release was requested but authority is not yet
      confirmed revoked, usually because our own successful 30100=1 stamped the
      30 s cooldown that now blocks 30100=0. The retry only exists while this
      process does.
    * ``ARM_FAILED_AUTHORITY_HELD`` — THIS process took authority and then
      failed to arm, leaving its own command half-applied. The trigger is our
      own failed write, never an inference from register values.

    * ``RELEASE_SETTLING`` — 30100=0 is confirmed, but the delayed 30407=0 has
      not been written and read back yet. The session is not finished, and the
      timer that finishes it only exists while this process does.

    ``RELEASED`` is reserved for a session whose BOTH halves were confirmed by
    read-back: 30100 == 0 and 30407 == 0. An unreadable inverter is never
    confirmation -- ``read_state() is None`` means "not known to be released",
    which is the opposite of success.

    Timer expiry of the override was treated as a third thing that must not be
    conflated with either -- the watchdog returning the inverter to its base
    mode without cleaning up authority. On the reference WIT it does not happen
    at all: a session left un-renewed held 30407=1 through t=90s of a 60 s
    window (2026-09-05). Nothing but an explicit release has been observed to
    end a session, so none of these states may be assumed to resolve on their
    own.
    """

    NOT_ARMED = "not_armed"      # never taken authority in this process
    ACTIVE = "active"
    RELEASE_PENDING = "release_pending"
    # Authority revoked and confirmed; the 30407=0 cleanup is still outstanding.
    RELEASE_SETTLING = "release_settling"
    RELEASED = "released"
    ARM_FAILED_AUTHORITY_HELD = "arm_failed_authority_held"
    # 30100=1 found without this process having taken it.
    #
    # This is NOT proof that somebody else is actively controlling the
    # inverter. 30100 is a register; the reference WIT has been observed with
    # it set while running its own schedule, and the name says only what is
    # actually known: the authority is not ours. That is enough to make it a
    # hard interlock — we never arm on top of authority we cannot account for —
    # without pretending to know who set it or why.
    AUTHORITY_HELD_NOT_OURS = "authority_held_not_ours"
    # 30100=1 found WITH a durable lease from a previous instance of this
    # process, so the evidence that it is ours is our own record rather than an
    # inference from the register. It grants ONE permission: release. Never
    # resume, never re-arm, never promote to ACTIVE -- a file cannot say what a
    # register means, only that we started something and have no record of
    # finishing it. See control/lease.py.
    RECOVERABLE_LEASE = "recoverable_lease"

    @property
    def safe_to_stop(self) -> bool:
        """True when stopping AppDaemon leaves no unfinished handover.

        AUTHORITY_HELD_NOT_OURS counts as safe: we hold nothing, so there is
        nothing of ours left dangling. It is still a loud state, because what
        the optimizer plans next is not what the inverter is currently doing.

        RELEASE_SETTLING is NOT safe. Authority is already back with the
        inverter, but the scheduled 30407=0 has not run, and stopping the
        process kills the only thing that will ever run it -- leaving the
        inverter armed for a session nobody owns.

        RECOVERABLE_LEASE is NOT safe either, and it is the one state where
        that verdict is about a session THIS process never opened: a previous
        instance left one armed, this one has the evidence, and there is no
        hardware expiry coming to end it. Stopping here abandons it again.
        """
        return self in (SessionState.NOT_ARMED, SessionState.RELEASED,
                        SessionState.AUTHORITY_HELD_NOT_OURS)


# Session states that assert something about THIS process's own authority.
# reconcile() must never overwrite one from a register read: the registers say
# what the inverter is doing, not who asked it to.
OWN_AUTHORITY_STATES = frozenset({
    SessionState.ACTIVE,
    SessionState.RELEASE_PENDING,
    # We took the authority and could not arm it. Ours, and known to be ours
    # from our own write result rather than from what the registers now read.
    SessionState.ARM_FAILED_AUTHORITY_HELD,
    # A stranded session this process has durable evidence for. In this set so
    # that a re-read cannot quietly downgrade it to AUTHORITY_HELD_NOT_OURS and
    # strand it again -- NOT because it may act like the others. Every write
    # path checks it separately and refuses everything except the release.
    SessionState.RECOVERABLE_LEASE,
})

# States in which this process still has unfinished business with the inverter
# even though authority itself reads 0. reconcile() must not demote these to
# NOT_ARMED: that would flip safe_to_stop to True while a scheduled 30407=0 is
# the only thing standing between the inverter and a stranded arm.
UNFINISHED_STATES = frozenset({
    SessionState.RELEASE_PENDING,
    SessionState.RELEASE_SETTLING,
})


class StepResult(enum.Enum):
    OK = "ok"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"


@dataclass(frozen=True)
class RegisterWrite:
    """One planned register write."""

    register: int
    value: int          # signed / human-readable
    fc: str = "0x06"
    note: str = ""

    def describe(self) -> str:
        enc = ""
        if self.value < 0:
            enc = f" (encoded {encode_unsigned(self.value)})"
        return f"{self.register}={self.value}{enc} FC{self.fc}  {self.note}".rstrip()


@dataclass(frozen=True)
class ServiceStep:
    """One planned non-register service call (currently only TOU sync)."""

    service: str
    data: Dict[str, Any]
    note: str = ""

    def describe(self) -> str:
        return f"{self.service} {self.data}  {self.note}".rstrip()


@dataclass
class CommandPlan:
    """The full ordered sequence for one command, before anything is executed."""

    action: ControlAction
    steps: List[Any] = field(default_factory=list)
    arms_at: Optional[int] = None   # index of the 30407=1 write, if any
    acquires_authority: bool = False
    # True when the inverter already reported 30100=1, so no acquire step was
    # planned. The arming write is still the hazard boundary: failing it with
    # authority held is VPP standby whether or not WE took that authority.
    authority_already_held: bool = False

    def add(self, step: Any) -> None:
        self.steps.append(step)

    def describe(self) -> List[str]:
        return [f"{i + 1}. {s.describe()}" for i, s in enumerate(self.steps)]


class DryRunExecutor:
    """Plans everything, performs no I/O at all.

    ``can_write`` is False, so the backend never treats a plan as applied.
    """

    name = "dry_run"
    can_write = False
    can_read = False

    def __init__(self, log_func: Optional[Callable] = None):
        self._log = log_func or (lambda *a, **k: None)
        self.executed: List[Any] = []

    def execute(self, step: Any) -> StepResult:
        self.executed.append(step)
        return StepResult.OK

    def read_registers(self, start: int, count: int) -> Optional[List[int]]:
        return None


class _HaRegisterReader:
    """Shared real-read plumbing. Owns no write capability of any kind.

    ``get_register_data`` is one of only two upstream services that returns a
    response, which is what makes real verification possible. Subclasses decide
    what — if anything — they will write; nothing here can.
    """

    # get_register_data caps a single read at 50 registers.
    MAX_READ_COUNT = 50

    def __init__(self, app, device_id: str, timeout_seconds: int = 15,
                 log_func: Optional[Callable] = None):
        self.app = app
        self.device_id = device_id
        self.timeout_seconds = timeout_seconds
        self._log = log_func or (lambda *a, **k: None)
        self.read_calls: List[Any] = []
        self.refused: List[Any] = []
        self.writes: List[Any] = []

    def read_registers(self, start: int, count: int) -> Optional[List[int]]:
        """Read holding registers via growatt_modbus/get_register_data."""
        if not self.device_id or count <= 0:
            return None
        if count > self.MAX_READ_COUNT:
            return None

        self.read_calls.append((start, count))
        try:
            result = self.app.call_service(
                "growatt_modbus/get_register_data",
                hass_timeout=self.timeout_seconds,
                device_id=self.device_id,
                register_type="holding",
                start_address=start,
                count=count,
            )
        except Exception as e:
            self._log(
                f"[{self.name}] get_register_data({start}, {count}) failed: {e}",
                level="WARNING",
            )
            return None

        return self._extract_values(result, count)

    def _refuse(self, step: Any, reason: str) -> StepResult:
        self.refused.append(step)
        self._log(f"[{self.name}] REFUSING {step.describe()} — {reason}",
                  level="ERROR")
        return StepResult.FAILED

    @staticmethod
    def _extract_values(result: Any, count: int) -> Optional[List[int]]:
        """Pull the values list out of the service response.

        The handler returns {"success": bool, "values": [...]}; some AppDaemon
        versions nest that under "result".
        """
        if not isinstance(result, dict):
            return None
        payload = result
        if "values" not in payload and isinstance(payload.get("result"), dict):
            payload = payload["result"]
        if payload.get("success") is False:
            return None
        values = payload.get("values")
        if not isinstance(values, list) or len(values) < count:
            return None
        try:
            return [int(v) for v in values[:count]]
        except (TypeError, ValueError):
            return None


class HaReadOnlyExecutor(_HaRegisterReader):
    """Real reads. Structurally incapable of writing.

    There is deliberately no code path here to ``write_register``,
    ``write_registers`` or ``sync_tou_schedule``: ``execute()`` refuses every
    step rather than performing it, so an injection mistake cannot silently
    arm an inverter.
    """

    name = "ha_read_only"
    can_write = False
    can_read = True
    commissioning = False

    def execute(self, step: Any) -> StepResult:
        """Refuse. This executor exists precisely so writes cannot happen."""
        return self._refuse(step, "this executor is read-only")


class HaCommissioningExecutor(_HaRegisterReader):
    """Supervised writes, restricted to a fixed register allowlist.

    This is the first executor that can change an inverter, so the limit on
    WHAT it will change lives here — in the executor — rather than being
    entrusted to whichever plan happens to be passed in. A plan that somehow
    contained an export limit, an AC-charge mode or a TOU write is refused at
    this boundary instead of being obeyed.

    Writes go through ``growatt_modbus/write_register`` (FC06), which returns
    no response and signals failure by raising — including the WIT cooldown
    refusal, which is mapped back to RATE_LIMITED so the retry machinery above
    behaves exactly as it does everywhere else.
    """

    name = "ha_commissioning"
    can_write = True
    can_read = True
    commissioning = True

    # Exactly what the four supervised operations need, and nothing else.
    # Notably absent: 30200/30201 (export limit), 30410 (AC charge),
    # 30404/30405 (SOC cutoffs) and 30411 (the inverter's own TOU schedule).
    WRITABLE_REGISTERS = frozenset({
        REG_CONTROL_AUTHORITY,   # 30100 — take/release authority
        REG_REMOTE_ENABLE,       # 30407 — arm/disarm
        REG_REMOTE_DURATION,     # 30408 — duration field (NOT enforced; see below)
        REG_REMOTE_POWER,        # 30409 — signed setpoint (+1 % HOLD only)
        REG_PRIORITY_MODE,       # 30476 — capability probe, always restored
    })

    def execute(self, step: Any) -> StepResult:
        if not isinstance(step, RegisterWrite):
            return self._refuse(step, "only register writes are permitted in "
                                      "commissioning mode")
        if step.register not in self.WRITABLE_REGISTERS:
            return self._refuse(
                step,
                f"register {step.register} is not in the commissioning "
                f"allowlist {sorted(self.WRITABLE_REGISTERS)}",
            )

        self.writes.append(step)
        try:
            self.app.call_service(
                "growatt_modbus/write_register",
                hass_timeout=self.timeout_seconds,
                device_id=self.device_id,
                register=step.register,
                value=step.value,
            )
        except Exception as e:  # noqa: BLE001 - the service raises on failure
            message = str(e)
            if "rate-limited" in message.lower() or "cooldown" in message.lower():
                self._log(
                    f"[{self.name}] {step.register} refused by the inverter's "
                    f"own write cooldown: {e}",
                    level="WARNING",
                )
                return StepResult.RATE_LIMITED
            self._log(
                f"[{self.name}] write {step.register}={step.value} failed: {e}",
                level="ERROR",
            )
            return StepResult.FAILED

        return StepResult.OK


def build_executor(app, config):
    """Pick an executor from config.control_mode. Fails SAFE, never open.

    Anything unrecognised yields the dry-run executor: a misconfiguration must
    never be the thing that grants write access to an inverter.
    """
    mode = getattr(config, "control_mode", "dry_run")
    log = getattr(app, "log", None)
    kwargs = dict(
        device_id=getattr(config, "device_id", ""),
        timeout_seconds=int(getattr(config, "command_timeout_seconds", 15)),
        log_func=log,
    )

    if mode == "commissioning":
        return HaCommissioningExecutor(app, **kwargs)
    if mode == "read_only":
        return HaReadOnlyExecutor(app, **kwargs)
    return DryRunExecutor(log)


class CooldownTracker:
    """Mirrors the integration's per-register 30 s cooldown.

    Stamped on SUCCESS only, exactly as upstream does, so our model of when a
    write will be refused matches theirs.
    """

    def __init__(self, cooldown_seconds: int = WIT_COOLDOWN_SECONDS,
                 clock: Optional[Callable[[], float]] = None):
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock or (lambda: datetime.datetime.now().timestamp())
        self._last_write: Dict[int, float] = {}

    def record(self, register: int) -> None:
        if register in RATE_LIMITED_REGISTERS:
            self._last_write[register] = self._clock()

    def seconds_remaining(self, register: int) -> float:
        if register not in RATE_LIMITED_REGISTERS:
            return 0.0
        last = self._last_write.get(register)
        if last is None:
            return 0.0
        elapsed = self._clock() - last
        return max(0.0, self.cooldown_seconds - elapsed)

    def blocked(self, register: int) -> bool:
        return self.seconds_remaining(register) > 0


class UpstreamVppBackend:
    """ControlBackend for the upstream integration's generic register services."""

    name = "upstream_vpp"

    def __init__(self, app, config, executor=None, clock: Optional[Callable] = None,
                 lease=None):
        self.app = app
        self.config = config
        # Durable record of an unfinished session. None disables persistence,
        # which is what every dry-run and read-only caller gets: a backend that
        # cannot write cannot strand anything.
        self.lease = lease if lease is not None else SessionLease(
            getattr(config, "session_lease_path", None),
            log_func=getattr(app, "log", None))
        self._lease_record = None
        self.executor = executor or DryRunExecutor(getattr(app, "log", None))
        self.cooldown = CooldownTracker(
            int(getattr(config, "wit_cooldown_seconds", WIT_COOLDOWN_SECONDS)),
            clock=clock,
        )

        self.session_state = SessionState.NOT_ARMED
        self.priority_mode_capability = PriorityModeCapability.UNKNOWN
        self.grid_charge_path = "remote_power"
        # Last-read telemetry, republished in diagnostics so normalization can
        # be checked against the raw reading on the real installation.
        self.last_battery_power_raw_w: Optional[float] = None
        self.last_battery_power_w: Optional[float] = None
        self.last_grid_import_power_w: Optional[float] = None
        self.last_grid_export_power_w: Optional[float] = None
        self.last_grid_power_w: Optional[float] = None
        self.last_tou_period_count: Optional[int] = None
        self._release_timer = None
        self._disarm_timer = None

        # Last value we successfully wrote per register — drives "write only on
        # change" for the non-EEPROM-safe registers.
        self._applied: Dict[int, int] = {}
        self._wrote_priority_mode = False

        self._rollback_timer = None
        self._counters: Dict[str, int] = {
            "plans_built": 0,
            "steps_executed": 0,
            "rate_limited": 0,
            "arm_failures": 0,
            "rollback_attempts": 0,
            "rollback_confirmed": 0,
            "release_attempts": 0,
            "release_deferred": 0,
            "disarm_attempts": 0,
            "disarm_confirmed": 0,
            "external_scheduler_refusals": 0,
            "authority_not_ours_refusals": 0,
            "stranded_session_refusals": 0,
            "leases_recovered": 0,
        }

    # --- configuration helpers -------------------------------------------

    @property
    def dry_run(self) -> bool:
        """True whenever the executor cannot write — planning only."""
        return not getattr(self.executor, "can_write", False)

    @property
    def can_read(self) -> bool:
        return bool(getattr(self.executor, "can_read", False))

    @property
    def commissioning(self) -> bool:
        """True when writes are possible but only as supervised operations."""
        return bool(getattr(self.executor, "commissioning", False))

    @property
    def automatic_writes_allowed(self) -> bool:
        """True only when the OPTIMIZER may drive the inverter unattended.

        Commissioning can write, so ``dry_run`` is False for it — which is
        exactly why this is a separate question. Answering "can this process
        write?" would let the scheduler start trading the moment commissioning
        was enabled.
        """
        return not self.dry_run and not self.commissioning

    @property
    def control_status(self) -> str:
        """One unmistakable string for the health sensor.

        DRY_RUN is surfaced ABOVE the session state on purpose: an accidental
        dependency-injection fallback is electrically safe but operationally
        awful, because the optimizer would look healthy while controlling
        nothing.
        """
        if self.dry_run:
            return "DRY_RUN"
        if self.commissioning:
            return f"COMMISSIONING/{self.session_state.value.upper()}"
        return self.session_state.value.upper()

    def describe_mode(self) -> str:
        """Startup banner text. Deliberately loud."""
        if self.dry_run:
            reads = "live reads" if self.can_read else "no I/O"
            return (
                "BATTERY OPTIMIZER CONTROL: DRY RUN — no inverter writes are "
                f"possible ({self.executor.name}, {reads}). Register sequences "
                "are planned and logged only."
            )
        if self.commissioning:
            return (
                f"BATTERY OPTIMIZER CONTROL: COMMISSIONING ({self.executor.name}) "
                "— supervised writes only. The optimizer does NOT drive the "
                "inverter in this mode; only deliberately invoked operations "
                f"({', '.join(sorted(a.value for a in COMMISSIONING_ACTIONS))}, "
                "priority-mode probe) can write, to a fixed register allowlist."
            )
        return (
            f"BATTERY OPTIMIZER CONTROL: LIVE ({self.executor.name}) — this "
            "app can write inverter control registers."
        )

    def _log(self, message: str, level: str = "INFO") -> None:
        log = getattr(self.app, "log", None)
        if log is not None:
            log(f"[{self.name}] {message}", level=level)

    def _read_tou_period_count(self) -> Optional[int]:
        """Read 30411 on its own. One register, cheap enough to re-read."""
        read = getattr(self.executor, "read_registers", None)
        if read is None:
            return None
        values = read(REG_TOU_NUM_PERIODS, 1)
        if not values:
            return None
        return values[0]

    def external_scheduler_count(self) -> Optional[int]:
        """The TOU period count when another scheduler is present, else None.

        Re-read rather than remembered: the whole point of the interlock is
        that somebody else can load a schedule between two of our slots, and a
        value cached an hour ago cannot see that. Falls back to the last read
        when 30411 is momentarily unreadable, so a dropped read cannot quietly
        clear the interlock.
        """
        count = self._read_tou_period_count()
        if count is None:
            count = self.last_tou_period_count
        if count is not None and count > 0:
            return count
        return None

    def _may_write_priority_mode(self) -> bool:
        """30476 is used only when a supervised probe proved it truly writable."""
        if getattr(self.config, "priority_mode_write", "auto") == "never":
            return False
        return self.priority_mode_capability is PriorityModeCapability.CONFIRMED_WRITABLE

    # --- plan construction ------------------------------------------------

    def _power_target(self, command: InverterCommand) -> int:
        """Signed 30409 value. One number is the whole difference between modes."""
        action = command.action
        if action is ControlAction.GRID_CHARGE:
            return abs(command.power_percent)
        if action is ControlAction.HOLD:
            return HOLD_POWER_PERCENT
        if action is ControlAction.MAX_EXPORT:
            return -100
        if action.is_discharge:
            return -abs(command.power_percent)
        return 0

    def _export_policy(self, command: InverterCommand):
        """(30200, 30201) for the action, or (None, None) to leave alone."""
        action = command.action
        if action is ControlAction.DISCHARGE_TO_LOAD:
            return 1, 0
        if action is ControlAction.DISCHARGE_TO_GRID:
            rate = command.export_rate if command.export_rate is not None else 100
            # 30201 is clamped to 0..100: negatives trigger WIT warning 401.
            return 1, max(0, min(100, int(rate)))
        # GRID_CHARGE, HOLD, MAX_EXPORT: no export limit.
        return 0, None

    def _staged(self, plan: CommandPlan, register: int, value: Optional[int],
                note: str) -> None:
        """Write a policy register only when its value actually changes."""
        if value is None:
            return
        if self._applied.get(register) == value:
            return
        plan.add(RegisterWrite(register, value, note=note))

    def build_plan(self, command: InverterCommand) -> CommandPlan:
        """Construct the full ordered sequence. Pure — executes nothing."""
        self._counters["plans_built"] += 1
        action = command.action

        if action is ControlAction.PASSTHROUGH:
            return self._build_release_plan()

        if self.commissioning:
            if action not in COMMISSIONING_ACTIONS:
                # send() refuses these before ever reaching here; this is the
                # structural backstop, so a future caller that bypasses send()
                # cannot get a forbidden sequence built for it either.
                raise ValueError(
                    f"{action.value} is not a commissioning operation "
                    f"(allowed: "
                    f"{sorted(a.value for a in COMMISSIONING_ACTIONS)})"
                )
            return self._build_commissioning_plan(command)

        plan = CommandPlan(action=action)

        # 1. Policy registers, only when changed. Applied BEFORE the power
        #    command so the inverter is never briefly driven under stale policy.
        #
        #    30476=1 is GRID_CHARGE only. Proving the register writable does
        #    not establish that anything needs it written: it is a storage
        #    register that changes the inverter's base mode, and the one
        #    action with a hypothesis attached — that Battery First is what
        #    lets a VPP grid charge actually import — is the grid-charge
        #    experiment. Until hardware evidence shows another action needs
        #    it, HOLD and the discharges leave the operator's base mode alone.
        if action is ControlAction.GRID_CHARGE and self._may_write_priority_mode():
            self._staged(plan, REG_PRIORITY_MODE, 1,
                         "priority mode = Battery First (grid charge only)")

        ac_enable = 1 if action is ControlAction.GRID_CHARGE else 0
        self._staged(plan, REG_AC_CHARGE_ENABLE, ac_enable,
                     "AC charge enable (never 2 — Illegal Function on this firmware)")

        if action is ControlAction.GRID_CHARGE and command.charge_cutoff_soc is not None:
            self._staged(plan, REG_CHARGE_CUTOFF_SOC, int(command.charge_cutoff_soc),
                         "charge cutoff SOC")
        if action.is_discharge and command.discharge_cutoff_soc is not None:
            self._staged(plan, REG_DISCHARGE_CUTOFF_SOC,
                         int(command.discharge_cutoff_soc), "discharge cutoff SOC")

        exp_enable, exp_rate = self._export_policy(command)
        self._staged(plan, REG_EXPORT_LIMIT_ENABLE, exp_enable, "export limit enable")
        self._staged(plan, REG_EXPORT_LIMIT_RATE, exp_rate, "export limit rate %")

        # NOTE: 30411 is deliberately NOT written. See fact 4 in the module
        # docstring — a timed override does not require the base TOU schedule
        # to be cleared, and that schedule is not ours to destroy.

        # 2. Timed override. Both EEPROM-safe, rewritten every slot.
        plan.add(RegisterWrite(REG_REMOTE_DURATION, int(command.duration_minutes),
                               note="duration (not enforced)"))
        plan.add(RegisterWrite(REG_REMOTE_POWER, self._power_target(command),
                               note="signed power target"))

        # 3. Authority acquired LATE, so a partial failure leaves it un-armed.
        #    Skipped ONLY for authority this process owns -- an ACTIVE session
        #    it armed itself and has not released. It used to be skipped
        #    whenever _applied said 30100 was 1, but reconcile() seeds
        #    _applied from a register READ, so any inherited 30100=1 looked
        #    like a session of ours to build on. A register value is not
        #    ownership; our own successful write is.
        if self.session_state is SessionState.ACTIVE:
            plan.authority_already_held = True
        else:
            plan.acquires_authority = True
            plan.add(RegisterWrite(REG_CONTROL_AUTHORITY, 1,
                                   note="acquire authority (late)"))

        # 4. Arm LAST, every slot — re-arming because nothing documents that
        #    rewriting 30408 renews a timed session.
        plan.arms_at = len(plan.steps)
        plan.add(RegisterWrite(REG_REMOTE_ENABLE, 1, note="ARM (always last)"))

        return plan

    def _build_commissioning_plan(self, command: InverterCommand) -> CommandPlan:
        """The timed override, and nothing else.

        No export policy (30200/30201), no AC charge mode (30410), no SOC
        cutoffs (30404/30405), no priority mode, no TOU (30411). Commissioning
        proves that a VPP session can be opened, renewed and released; it does
        not configure the inverter, and every register it does not need is a
        register it cannot leave changed.
        """
        plan = CommandPlan(action=command.action)

        plan.add(RegisterWrite(REG_REMOTE_DURATION, int(command.duration_minutes),
                               note="duration (not enforced)"))
        plan.add(RegisterWrite(REG_REMOTE_POWER, self._power_target(command),
                               note="signed power target"))

        # Authority is only ever taken FRESH here. Inherited authority is
        # refused by the commissioning preflight long before this point (see
        # CommissioningSession), so unlike the normal path there is no
        # "already held, skip the write" branch to re-use somebody else's.
        if self.session_state is not SessionState.ACTIVE:
            plan.acquires_authority = True
            plan.add(RegisterWrite(REG_CONTROL_AUTHORITY, 1,
                                   note="acquire authority (late)"))

        # Arm LAST, exactly as the normal path does — this is the ordering the
        # whole commissioning exercise exists to prove.
        plan.arms_at = len(plan.steps)
        plan.add(RegisterWrite(REG_REMOTE_ENABLE, 1, note="ARM (always last)"))
        return plan

    def _build_release_plan(self) -> CommandPlan:
        """PASSTHROUGH: give the inverter fully back to local control (0/0)."""
        plan = CommandPlan(action=ControlAction.PASSTHROUGH)

        if self._wrote_priority_mode and self._may_write_priority_mode():
            plan.add(RegisterWrite(REG_PRIORITY_MODE, 0,
                                   note="restore priority mode = Load First"))
        # No 30411 write here either: releasing gives the inverter back to its
        # own base schedule, which is exactly the schedule we never touched.

        # Authority first: local logic resumes on revocation. 30407=0 follows
        # after release_settle_seconds, SCHEDULED (never slept on).
        plan.add(RegisterWrite(REG_CONTROL_AUTHORITY, 0,
                               note="release authority (local control resumes)"))
        return plan

    # --- execution --------------------------------------------------------

    def _execute_step(self, step: Any) -> StepResult:
        if isinstance(step, RegisterWrite):
            remaining = self.cooldown.seconds_remaining(step.register)
            if remaining > 0:
                self._counters["rate_limited"] += 1
                self._log(
                    f"register {step.register} is rate-limited for another "
                    f"{remaining:.0f}s — command deferred, not applied",
                    level="WARNING",
                )
                return StepResult.RATE_LIMITED

        result = self.executor.execute(step)
        self._counters["steps_executed"] += 1

        if result is StepResult.OK and isinstance(step, RegisterWrite):
            self.cooldown.record(step.register)
            self._applied[step.register] = step.value
            if step.register == REG_PRIORITY_MODE and step.value == 1:
                self._wrote_priority_mode = True
        return result

    def send(self, command: InverterCommand) -> SendResult:
        if self.commissioning and command.action not in COMMISSIONING_ACTIONS:
            self._log(
                f"REFUSED: {command.action.value} is not a commissioning "
                f"operation. This mode transmits only "
                f"{sorted(a.value for a in COMMISSIONING_ACTIONS)} — nothing "
                f"was sent to the inverter.",
                level="ERROR",
            )
            return SendResult.FAILED

        # HARD INTERLOCK: authority we cannot account for. Independent of the
        # TOU interlock below -- either one alone refuses. The commissioning
        # preflight has always refused on this; putting it in the backend too
        # means automatic control cannot arm on top of an inherited 30100=1
        # merely because nothing supervised was in the way.
        #
        # Only reconcile() sets this state, and only from a read, so it never
        # fires on authority this process took.
        if (command.action.holds_session and not self.dry_run
                and self.session_state is SessionState.RECOVERABLE_LEASE):
            self._counters["stranded_session_refusals"] += 1
            self._log(
                f"REFUSED {command.action.value}: a stranded session from a "
                f"previous instance is still armed on this inverter. The only "
                f"permitted action is releasing it — recovery never resumes a "
                f"session and never arms on top of one. Nothing was sent.",
                level="ERROR",
            )
            return SendResult.FAILED

        if (command.action.holds_session and not self.dry_run
                and self.session_state is SessionState.AUTHORITY_HELD_NOT_OURS):
            self._counters["authority_not_ours_refusals"] += 1
            self._log(
                f"REFUSED {command.action.value}: 30100=1 without this process "
                f"having taken it. Authority we cannot account for is never "
                f"armed on top of — establish what set it, or release it from "
                f"wherever it came from. Nothing was sent.",
                level="ERROR",
            )
            return SendResult.FAILED

        # The external-scheduler interlock. Checked here because send() is the
        # one door every command goes through, commissioning and automatic
        # alike -- the commissioning preflight refuses earlier and on a fresher
        # read, and this is the backstop under it.
        #
        # PASSTHROUGH is exempt on purpose: it gives the inverter back, which
        # is exactly what should stay possible when a second scheduler turns
        # up. An interlock that could trap a session open would be a worse
        # hazard than the one it guards against.
        if command.action.holds_session and not self.dry_run:
            periods = self.external_scheduler_count()
            if periods is not None:
                self._counters["external_scheduler_refusals"] += 1
                self._log(
                    f"REFUSED {command.action.value}: 30411 reports {periods} "
                    f"TOU period(s). This project never writes one, so the "
                    f"schedule is another scheduler's (Growatt Smart "
                    f"Scheduling on the reference unit). Nothing was sent -- "
                    f"turn the external scheduler off, or clear its schedule, "
                    f"before commanding this inverter.",
                    level="ERROR",
                )
                return SendResult.FAILED

        plan = self.build_plan(command)

        if self.dry_run:
            self._log_decision(command, plan)
            # Nothing is transmitted. The plan is fully constructed and logged,
            # which is the entire point of this mode.
            if not self.can_read:
                for step in plan.steps:
                    self.executor.execute(step)
            return SendResult.DRY_RUN

        self._log(
            f"{command.action.value}: {len(plan.steps)} step(s) — "
            f"{command.describe()}"
        )
        for line in plan.describe():
            self._log(f"    {line}", level="DEBUG")

        # BEFORE the first write, never after: a crash between the lease and
        # the authority write leaves a lease with nothing to clean up, which
        # recovery detects from the registers and discards. The other order
        # would leave authority taken with no record of it -- the exact failure
        # the lease exists to prevent.
        if command.action.holds_session and self._lease_record is None:
            self._lease_record = self.lease.open(
                device_id=str(getattr(self.config, "device_id", "")),
                action=command.action.value,
                setpoint_percent=command.power_percent,
                duration_minutes=command.duration_minutes)

        result = self._execute_plan(plan)

        if result is SendResult.CONFIRMED and command.action.holds_session:
            self._lease_record = self.lease.mark(self._lease_record, LEASE_ACTIVE)

        return result

    def _log_decision(self, command: InverterCommand, plan: CommandPlan) -> None:
        """Log actual inverter state -> desired action -> proposed sequence.

        With live reads available this is the single most useful line in the
        log during commissioning: it shows what the inverter really is, what
        the optimizer wants, and exactly what would be written to get there.
        """
        state = self.read_state() if self.can_read else None

        self._log(f"DRY RUN  actual: {state.describe() if state else 'unreadable'}")
        self._log(f"DRY RUN  desired: {command.describe()}")
        self._log(f"DRY RUN  proposed sequence ({len(plan.steps)} step(s)):")
        for line in plan.describe():
            self._log(f"DRY RUN      {line}")

        if state is not None and state.authority_without_remote:
            self._log(
                "inverter is currently at 30100=1 / 30407=0. Upstream calls "
                "this VPP standby; this hardware has been observed discharging "
                "and exporting in it. Reported, not acted on.",
                level="WARNING",
            )

    def _execute_plan(self, plan: CommandPlan) -> SendResult:
        # Authority we inherited counts the same as authority we just took:
        # either way, failing the arming write leaves 30100=1 / 30407=0.
        authority_taken = plan.authority_already_held

        for index, step in enumerate(plan.steps):
            result = self._execute_step(step)

            if result is StepResult.OK:
                if (isinstance(step, RegisterWrite)
                        and step.register == REG_CONTROL_AUTHORITY
                        and step.value == 1):
                    authority_taken = True
                continue

            # A step did not land. If authority is already held and this was
            # the arming write, the inverter is now in the hazard state.
            if authority_taken and index == plan.arms_at:
                self._enter_arm_failed()
                return SendResult.FAILED

            if result is StepResult.RATE_LIMITED:
                return SendResult.RATE_LIMITED
            return SendResult.FAILED

        if plan.action is ControlAction.PASSTHROUGH:
            # The authority write landed. Landing is not confirmation, and
            # confirmation of 30100 alone is not the end of the lifecycle, so
            # this goes through exactly the same gate release() does.
            return self._confirm_authority_revoked()

        self.session_state = SessionState.ACTIVE
        return SendResult.CONFIRMED

    def _enter_arm_failed(self) -> None:
        """WE took authority and then failed to arm. Undo what we did.

        This is the one place that still recovers automatically, and the
        distinction matters: the trigger is our own write result — we know we
        took 30100 and we know 30407 did not land — not an inference drawn
        from finding two registers in a particular combination. Leaving a
        command of ours half-applied is a bug we caused; 1/0 discovered on an
        inverter we have not written to is not.

        The obvious rollback (30100=0) CANNOT land immediately: our own
        successful 30100=1 stamped the cooldown, so the revoke is refused for
        up to 30 s. It is scheduled rather than attempted, and retried until a
        read-back confirms 30100 == 0.
        """
        self.session_state = SessionState.ARM_FAILED_AUTHORITY_HELD
        self._counters["arm_failures"] += 1
        remaining = self.cooldown.seconds_remaining(REG_CONTROL_AUTHORITY)
        self._log(
            "ARMING FAILED after this process took control authority: 30100=1 "
            "landed and 30407=1 did not, so OUR command is half-applied. "
            f"Authority revoke is rate-limited for {remaining:.0f}s; scheduling "
            f"the rollback of the authority we took.",
            level="ERROR",
        )
        self._schedule_rollback(remaining)

    def _schedule_rollback(self, remaining: float) -> None:
        delay = max(1.0, remaining + 2.0)
        run_in = getattr(self.app, "run_in", None)
        if run_in is None:
            return
        try:
            self._rollback_timer = run_in(self._attempt_rollback, delay)
        except Exception as e:  # pragma: no cover - scheduler failure
            self._log(f"could not schedule authority rollback: {e}", level="ERROR")

    def _attempt_rollback(self, kwargs=None) -> None:
        """Revoke authority, and only believe it when a read-back agrees."""
        self._rollback_timer = None
        if self.session_state is not SessionState.ARM_FAILED_AUTHORITY_HELD:
            return

        self._counters["rollback_attempts"] += 1
        result = self._execute_step(
            RegisterWrite(REG_CONTROL_AUTHORITY, 0, note="rollback: revoke authority")
        )

        if result is not StepResult.OK:
            remaining = self.cooldown.seconds_remaining(REG_CONTROL_AUTHORITY)
            self._log(
                f"authority rollback did not land ({result.value}); retrying",
                level="ERROR",
            )
            self._schedule_rollback(remaining)
            return

        state = self.read_state()
        if state is not None and state.control_authority == 0:
            self._counters["rollback_confirmed"] += 1
            self._log("authority rollback confirmed by read-back (30100=0)")
            # A failed arm means 30407=1 never landed, so this normally ends
            # the lifecycle outright -- but that is checked, not assumed.
            self._enter_settling(state)
            return

        # A write that merely did not raise is not proof. Keep trying.
        self._log(
            "authority rollback not yet confirmed by read-back; retrying",
            level="WARNING",
        )
        self._schedule_rollback(self.cooldown.seconds_remaining(REG_CONTROL_AUTHORITY))

    def release(self) -> SendResult:
        """Begin the release lifecycle; it may complete asynchronously.

            ACTIVE -> RELEASE_PENDING -> (cooldown wait) -> 30100=0
                   -> read-back confirms -> settle -> 30407=0 -> RELEASED

        Revoking authority is frequently blocked by the cooldown OUR OWN
        successful 30100=1 stamped, so this cannot be a single synchronous
        step. RELEASED is only ever reached via a read-back showing 30100 == 0.
        """
        plan = self._build_release_plan()

        if self.dry_run:
            self._log(f"DRY RUN  release: {len(plan.steps)} step(s)")
            for line in plan.describe():
                self._log(f"DRY RUN      {line}")
            self._log(
                f"DRY RUN      (then after {self._settle_seconds()}s: "
                f"{REG_REMOTE_ENABLE}=0 — scheduled, never slept on)"
            )
            if not self.can_read:
                for step in plan.steps:
                    self.executor.execute(step)
            return SendResult.DRY_RUN

        if self.session_state is SessionState.NOT_ARMED:
            self._log("release requested but authority was never taken")
            return SendResult.CONFIRMED

        if self.session_state is SessionState.RELEASE_SETTLING:
            # Authority is already confirmed revoked; only the scheduled
            # 30407=0 is outstanding. Re-revoking would spend the 30100
            # cooldown for nothing.
            self._log("release already settling: 30100=0 is confirmed and the "
                      "30407 disarm is scheduled")
            return SendResult.PENDING

        self.session_state = SessionState.RELEASE_PENDING
        self._log(f"release requested: {len(plan.steps)} step(s)")
        for line in plan.describe():
            self._log(f"    {line}", level="DEBUG")

        # Restore base settings first (everything except the authority revoke).
        for step in plan.steps[:-1]:
            self._execute_step(step)

        return self._attempt_release()

    def _attempt_release(self, kwargs=None) -> SendResult:
        """Revoke authority, retrying past the cooldown until read-back agrees."""
        self._release_timer = None
        self._counters["release_attempts"] += 1

        result = self._execute_step(
            RegisterWrite(REG_CONTROL_AUTHORITY, 0,
                          note="release: revoke authority")
        )

        if result is not StepResult.OK:
            self._counters["release_deferred"] += 1
            remaining = self.cooldown.seconds_remaining(REG_CONTROL_AUTHORITY)
            self._log(
                f"release deferred — revoking authority is rate-limited for "
                f"another {remaining:.0f}s. The inverter is NOT released yet; "
                f"do not stop AppDaemon until the health sensor reads RELEASED.",
                level="WARNING",
            )
            self._schedule_release_retry(remaining)
            return SendResult.PENDING

        return self._confirm_authority_revoked()

    def _confirm_authority_revoked(self) -> SendResult:
        """Believe 30100=0 only when a read-back says so, then start settling.

        An unreadable inverter is explicitly NOT success. The previous version
        treated ``read_state() is None`` as "the write stood", which turned
        every lost read into a RELEASED that nobody had confirmed -- and
        RELEASED is what tells the operator it is safe to stop the process.
        """
        state = self.read_state()

        if state is None:
            self._log(
                "authority revoke could NOT be confirmed: the inverter is "
                "unreadable, which is not evidence that it was released. "
                "Staying RELEASE_PENDING and retrying.",
                level="WARNING",
            )
            self.session_state = SessionState.RELEASE_PENDING
            self._schedule_release_retry(
                self.cooldown.seconds_remaining(REG_CONTROL_AUTHORITY))
            return SendResult.PENDING

        if state.control_authority != 0:
            self._log(
                "authority revoke not yet confirmed by read-back; retrying",
                level="WARNING",
            )
            self.session_state = SessionState.RELEASE_PENDING
            self._schedule_release_retry(
                self.cooldown.seconds_remaining(REG_CONTROL_AUTHORITY)
            )
            return SendResult.PENDING

        return self._enter_settling(state)

    def _enter_settling(self, state: Optional[InverterState]) -> SendResult:
        """30100=0 is confirmed. The session ends when 30407=0 is confirmed too.

        Authority is back with the inverter, so nothing is being commanded --
        but an armed 30407 belonging to a session nobody owns is exactly the
        litter this lifecycle exists to prevent, and the timer that clears it
        dies with this process. Hence a state of its own, and
        ``safe_to_stop`` stays False throughout it.
        """
        if state is not None and state.remote_enabled == 0:
            # Already disarmed (a watchdog expiry, or our own earlier disarm).
            self.session_state = SessionState.RELEASED
            self._log("authority released and 30407 already 0 — RELEASED "
                      "(both halves confirmed by read-back)")
            self._close_lease()
            return SendResult.CONFIRMED

        self.session_state = SessionState.RELEASE_SETTLING
        self._log(
            f"authority released (30100=0, confirmed). Disarming 30407 after "
            f"{self._settle_seconds()}s — the session is NOT finished and "
            f"this process must stay alive until it reads RELEASED."
        )
        self._schedule_disarm()
        return SendResult.CONFIRMED

    def _schedule_release_retry(self, remaining: float) -> None:
        delay = max(1.0, remaining + 2.0)
        run_in = getattr(self.app, "run_in", None)
        if run_in is None:
            return
        try:
            self._release_timer = run_in(self._attempt_release, delay)
        except Exception as e:  # pragma: no cover - scheduler failure
            self._log(f"could not schedule release retry: {e}", level="ERROR")

    def _settle_seconds(self) -> int:
        return int(getattr(self.config, "release_settle_seconds", 35))

    def _schedule_disarm(self, delay: Optional[float] = None) -> None:
        run_in = getattr(self.app, "run_in", None)
        if run_in is None:
            self._log(
                "no scheduler available to disarm 30407; the session cannot "
                "reach RELEASED on its own",
                level="ERROR",
            )
            return
        if delay is None:
            delay = self._settle_seconds()
        try:
            self._disarm_timer = run_in(self._disarm, max(1.0, float(delay)))
        except Exception as e:  # pragma: no cover
            self._log(f"could not schedule disarm: {e}", level="ERROR")

    def _disarm(self, kwargs=None) -> None:
        """Write 30407=0 and read it back. Only that finishes the lifecycle."""
        self._disarm_timer = None
        if self.session_state is not SessionState.RELEASE_SETTLING:
            return

        self._counters["disarm_attempts"] += 1
        result = self._execute_step(
            RegisterWrite(REG_REMOTE_ENABLE, 0, note="disarm after settle")
        )

        if result is not StepResult.OK:
            remaining = self.cooldown.seconds_remaining(REG_REMOTE_ENABLE)
            self._log(
                f"disarm did not land ({result.value}); the session stays "
                f"RELEASE_SETTLING and the write is retried",
                level="WARNING",
            )
            self._schedule_disarm(remaining)
            return

        state = self.read_state()
        if state is None or state.remote_enabled != 0:
            self._log(
                "disarm not confirmed by read-back "
                f"(30407={None if state is None else state.remote_enabled}); "
                f"retrying. Not RELEASED until it reads 0.",
                level="WARNING",
            )
            self._schedule_disarm(
                self.cooldown.seconds_remaining(REG_REMOTE_ENABLE))
            return

        self._counters["disarm_confirmed"] += 1
        self.session_state = SessionState.RELEASED
        self._log("disarm confirmed by read-back (30407=0) — RELEASED; "
                  "both halves of the release are now confirmed")
        self._close_lease()

    # --- startup reconciliation -------------------------------------------

    def reconcile(self) -> Optional[InverterState]:
        """Seed our model of the inverter from what it actually reports.

        Two things this fixes, both only possible once reads exist:

        * **Redundant writes.** The write-on-change cache starts empty, so a
          fresh start would rewrite every policy register even when the
          inverter already holds the right value — against a 30 s per-register
          cooldown.
        * **Inherited state.** AppDaemon may restart into an inverter that
          already has 30100 set, which this app never wrote. Finding authority
          held is not proof it is ours — and equally, it is not proof that
          anything else is actively controlling the inverter. It is treated as
          exactly what it is: authority we cannot account for, and therefore
          must not arm on top of.
        """
        if not self.can_read:
            return None

        state = self.read_state()
        if state is None:
            self._log("could not reconcile: inverter state unreadable",
                      level="WARNING")
            return None

        for register, value in (
            (REG_CONTROL_AUTHORITY, state.control_authority),
            (REG_EXPORT_LIMIT_ENABLE, state.export_limit_enabled),
            (REG_EXPORT_LIMIT_RATE, state.export_limit_rate),
            (REG_CHARGE_CUTOFF_SOC, state.charge_cutoff_soc),
            (REG_DISCHARGE_CUTOFF_SOC, state.discharge_cutoff_soc),
            (REG_AC_CHARGE_ENABLE, state.ac_charge_mode),
            (REG_TOU_NUM_PERIODS, state.tou_period_count),
            (REG_PRIORITY_MODE, state.priority_mode),
        ):
            if value is not None:
                self._applied[register] = value

        if state.authority_held:
            if self.session_state in OWN_AUTHORITY_STATES:
                # Ours, re-observed. Commissioning reconciles before EVERY
                # operation, so this branch is reached mid-session, mid-release
                # and after a failed arm, and must not demote authority this
                # process actually holds. Those states are set from our own
                # write results -- never inferred from registers -- which is
                # what makes them the proof of ownership.
                self._log(
                    f"re-confirmed the authority this process holds "
                    f"(session_state={self.session_state.value})",
                    level="DEBUG")
            else:
                # 30100=1 without this process having taken it. Deliberately
                # NOT called external control: 30100 is a register, and this
                # inverter has been seen holding it while running its own
                # schedule. All that is actually known is that the authority
                # is not ours, which is reason enough never to arm on top of
                # it, and not reason to claim who set it.
                self.session_state = SessionState.AUTHORITY_HELD_NOT_OURS
                self._log(
                    f"30100=1 but this process did not take it "
                    f"({state.describe()}). Reported as "
                    f"AUTHORITY_HELD_NOT_OURS: not adopted, not attributed to "
                    f"anyone in particular, and never armed on top of.",
                    level="WARNING",
                )

            if state.authority_without_remote:
                # Warned about, acted on by nobody. Upstream calls 1/0 "VPP
                # standby" with local logic suspended; this hardware was
                # observed at 1/0 discharging and exporting. Until that is
                # resolved, the pair is a discrepancy to report, not a fault
                # to recover from -- so there is no rollback scheduled here in
                # ANY mode, however capable of writing it is.
                self._log(
                    "30100=1 with 30407=0. Upstream documents this pair as VPP "
                    "standby (local battery logic suspended, load drawn from "
                    "the grid), but this installation has been observed in it "
                    "while discharging and exporting. The discrepancy is "
                    "unresolved: nothing is being recovered or released on the "
                    "strength of these two registers alone.",
                    level="WARNING",
                )
        elif (self.session_state is not SessionState.RELEASED
                and self.session_state not in UNFINISHED_STATES):
            # Authority is not held. RELEASED is kept as-is ("we released it"
            # is stronger and still true than "never armed"), and an
            # unfinished release -- pending or settling -- is left for its own
            # scheduled callback to resolve rather than being raced by a read.
            # Demoting a settling session would report safe_to_stop=True while
            # our own 30407=0 was still outstanding.
            self.session_state = SessionState.NOT_ARMED

        if state.external_scheduler_present:
            self._log(
                f"30411 reports {state.tou_period_count} TOU period(s). No "
                f"plan in this project writes a TOU period, so this schedule "
                f"is not ours -- on the reference unit it is Growatt Smart "
                f"Scheduling's. Session-holding writes are INTERLOCKED while "
                f"it is loaded; releasing stays available.",
                level="WARNING",
            )

        self._reconcile_lease(state)

        self._log(f"reconciled with inverter: {state.describe()}")
        return state

    # --- durable recovery of a session this process did not open -----------

    def _close_lease(self) -> None:
        """Both halves of the release are confirmed: nothing is outstanding."""
        self.lease.close()
        self._lease_record = None

    def _reconcile_lease(self, state: InverterState) -> None:
        """Consult the durable lease about authority we did not just take.

        This runs AFTER the ownership branch above, so the register-only
        verdict is reached first and this can only ever move it in one
        direction: from "not ours, do not touch" to "ours to CLEAN UP". It
        never promotes anything to ACTIVE, and without a lease nothing changes
        at all.
        """
        if self._lease_record is not None:
            # This process's own session, already accounted for in memory.
            return

        record = self.lease.read()
        if record is None:
            return

        if not state.authority_held:
            # A lease with nothing stranded behind it: the previous instance
            # got as far as writing the lease and no further, or its release
            # landed and only the unlink was lost. Either way the inverter is
            # already back to its own logic, and keeping the file would make
            # the next start believe there is something to recover.
            if state.remote_enabled in (0, None):
                self._log(
                    f"discarding a stale lease ({record.describe()}): the "
                    f"inverter reads 30100=0/30407={state.remote_enabled}, so "
                    f"nothing of that session is still applied")
                self.lease.close()
            return

        expected_device = str(getattr(self.config, "device_id", ""))
        if record.device_id != expected_device:
            self._log(
                f"a lease exists but it names device {record.device_id}, not "
                f"{expected_device}. NOT adopted — recovery acts only on the "
                f"inverter the record names",
                level="ERROR")
            return

        if state.external_scheduler_present:
            self._log(
                f"a lease exists for this device, but 30411 reports "
                f"{state.tou_period_count} TOU period(s) — another scheduler "
                f"is loaded. NOT adopted: releasing here would hand the "
                f"inverter to a schedule nobody in this process chose. Clear "
                f"the external scheduler, then re-run recovery",
                level="ERROR")
            return

        if (record.setpoint_percent is not None
                and state.commanded_power is not None
                and state.commanded_power != record.setpoint_percent):
            self._log(
                f"a lease exists for this device, but 30409 reads "
                f"{state.commanded_power} where the record says "
                f"{record.setpoint_percent}. Something re-commanded this "
                f"inverter after our session, so the armed session is NOT the "
                f"one recorded. NOT adopted — establish what changed it",
                level="ERROR")
            return

        self.session_state = SessionState.RECOVERABLE_LEASE
        self._lease_record = record
        self._counters["leases_recovered"] += 1
        self._log(
            f"STRANDED SESSION RECOVERED: {record.describe()}. The inverter "
            f"reads 30100={state.control_authority}/"
            f"30407={state.remote_enabled} and there is no hardware expiry "
            f"coming for it. This process may now do exactly one thing with "
            f"it — RELEASE. It will not resume the command, re-arm, or treat "
            f"the session as its own.",
            level="CRITICAL")

    # --- supervised capability probe --------------------------------------

    def probe_priority_mode(self, state: InverterState):
        """Value-CHANGING probe of 30476, always followed by a restore.

        Write-same proves only that the address accepts FC06. Only a probe that
        actually changes the value and reads that change back can license using
        30476=1 in normal operation. It therefore mutates the inverter's base
        mode, which is why it exists solely as a supervised commissioning
        operation and never runs inside a slot.

        30476 is a storage register, so this spends two EEPROM writes. If the
        restore does not land the inverter is left in a base mode the operator
        did not choose, which is reported at CRITICAL rather than swallowed.

        Returns ``(capability, detail)``; also updates
        ``self.priority_mode_capability``.
        """
        original = state.priority_mode
        if original is None:
            return (PriorityModeCapability.UNKNOWN,
                    "30476 could not be read; not probing")

        target = 0 if original != 0 else 1

        result = self._execute_step(RegisterWrite(
            REG_PRIORITY_MODE, target,
            note=f"probe: change priority mode {original} -> {target}"))
        if result is not StepResult.OK:
            self.priority_mode_capability = PriorityModeCapability.REJECTED
            detail = (f"30476 refused the write ({result.value}); the inverter "
                      f"remains at {original}")
            self._log(f"priority mode probe: {detail}", level="WARNING")
            return self.priority_mode_capability, detail

        observed = self._read_priority_mode()

        if observed == target:
            self.priority_mode_capability = PriorityModeCapability.CONFIRMED_WRITABLE
            detail = f"30476 changed {original} -> {observed} and read back"
        elif observed is None:
            self.priority_mode_capability = PriorityModeCapability.WRITE_ACCEPTED
            detail = ("30476 accepted the write but could not be read back; "
                      "the change is unproven")
        else:
            self.priority_mode_capability = PriorityModeCapability.WRITE_ACCEPTED
            detail = (f"30476 accepted the write but still reads {observed}: "
                      f"accepted is not the same as writable")

        restored = self._restore_priority_mode(original, observed)
        detail = f"{detail}; {restored}"
        self._log(f"priority mode probe: {detail}")
        return self.priority_mode_capability, detail

    def _restore_priority_mode(self, original: int, observed) -> str:
        """Put 30476 back exactly as it was found."""
        if observed == original:
            return "nothing to restore (value never changed)"

        result = self._execute_step(RegisterWrite(
            REG_PRIORITY_MODE, original, note="probe: restore priority mode"))
        if result is not StepResult.OK:
            self._log(
                f"PRIORITY MODE NOT RESTORED: 30476 could not be written back "
                f"to {original} ({result.value}). The inverter's base mode is "
                f"NOT what it was before the probe — restore it by hand.",
                level="CRITICAL",
            )
            return f"RESTORE FAILED ({result.value})"

        back = self._read_priority_mode()
        if back != original:
            self._log(
                f"PRIORITY MODE RESTORE UNCONFIRMED: 30476 reads {back}, "
                f"expected {original}. Verify the inverter's base mode by hand.",
                level="CRITICAL",
            )
            return f"restore unconfirmed (reads {back}, expected {original})"
        return f"restored to {original} and confirmed"

    def _read_priority_mode(self):
        read = getattr(self.executor, "read_registers", None)
        if read is None:
            return None
        values = read(REG_PRIORITY_MODE, 1)
        if not values:
            return None
        return values[0]

    # --- verification -----------------------------------------------------

    def read_state(self) -> Optional[InverterState]:
        """Four cheap reads covering every input a decision depends on."""
        read = getattr(self.executor, "read_registers", None)
        if read is None:
            return None

        authority = read(REG_CONTROL_AUTHORITY, 1)
        export = read(REG_EXPORT_LIMIT_ENABLE, 2)
        block = read(REG_CHARGE_CUTOFF_SOC, 8)   # 30404..30411
        tail = read(REG_SETPOINT_MIRROR, 3)      # 30474..30476

        if block is None and authority is None:
            return None

        def at(seq, index):
            if seq is None or len(seq) <= index:
                return None
            return seq[index]

        commanded = at(block, 5)
        mirror = at(tail, 0)
        rate = at(export, 1)

        battery_raw = self._read_power(
            getattr(self.config, "battery_power_sensor", ""))
        battery_power = self._normalize_battery_power(battery_raw)
        grid_import = self._read_power(
            getattr(self.config, "grid_import_power_sensor", ""))
        grid_export = self._read_power(
            getattr(self.config, "grid_export_power_sensor", ""))
        grid_signed = self._read_power(
            getattr(self.config, "grid_power_sensor", ""))
        # Remembered for diagnostics: raw beside normalized is what makes a
        # wrong battery_power_direction visible at a glance on real hardware.
        self.last_battery_power_raw_w = battery_raw
        self.last_battery_power_w = battery_power
        self.last_grid_import_power_w = grid_import
        self.last_grid_export_power_w = grid_export
        self.last_grid_power_w = grid_signed
        self.last_tou_period_count = at(block, 7)

        return InverterState(
            control_authority=at(authority, 0),
            charge_cutoff_soc=at(block, 0),
            discharge_cutoff_soc=at(block, 1),
            remote_enabled=at(block, 3),
            duration_minutes=at(block, 4),
            commanded_power=None if commanded is None else decode_signed(commanded),
            ac_charge_mode=at(block, 6),
            tou_period_count=at(block, 7),
            export_limit_enabled=at(export, 0),
            export_limit_rate=None if rate is None else decode_signed(rate),
            vpp_setpoint_mirror=None if mirror is None else decode_signed(mirror),
            priority_mode=at(tail, 2),
            battery_power_w=battery_power,
            battery_power_raw_w=battery_raw,
            grid_import_power_w=grid_import,
            grid_export_power_w=grid_export,
            grid_power_w=grid_signed,
            soc_percent=self._read_power(
                getattr(self.config, "soc_sensor", "")),
        )

    def _normalize_battery_power(
        self, raw: Optional[float]
    ) -> Optional[float]:
        """Convert the sensor's polarity to the internal one: + = charging.

        Declared in config, never detected. The reference WIT reports negative
        while charging; other Growatt families have been reported the other
        way round, and guessing per-reading would silently invert every
        trading verdict. See BATTERY_POWER_DIRECTIONS in config.py.
        """
        if raw is None:
            return None
        direction = getattr(self.config, "battery_power_direction",
                            "negative_is_charging")
        return -raw if direction == "negative_is_charging" else raw

    def _read_power(self, entity: str) -> Optional[float]:
        if not entity:
            return None
        get_state = getattr(self.app, "get_state", None)
        if get_state is None:
            return None
        try:
            value = get_state(entity)
            if value in (None, "unknown", "unavailable"):
                return None
            return float(value)
        except (ValueError, TypeError):
            return None

    def verify(
        self, command: InverterCommand, state: Optional[InverterState]
    ) -> VerifyResult:
        if state is None:
            return VerifyResult(VerifyVerdict.UNVERIFIABLE, actual="unreadable")

        if command.action is ControlAction.PASSTHROUGH:
            if state.control_authority == 0:
                return VerifyResult(VerifyVerdict.MATCH, actual=state.describe())
            return VerifyResult(VerifyVerdict.MISMATCH, actual=state.describe(),
                                detail="authority still held")

        expected_power = self._power_target(command)
        mismatches = []
        if state.control_authority != 1:
            mismatches.append("authority not held")
        if state.remote_enabled != 1:
            mismatches.append("remote control not enabled")
        if state.commanded_power != expected_power:
            mismatches.append(
                f"power {state.commanded_power} != {expected_power}")

        if mismatches:
            return VerifyResult(
                VerifyVerdict.MISMATCH,
                actual=state.describe(),
                detail="; ".join(mismatches),
            )

        return VerifyResult(
            VerifyVerdict.MATCH,
            actual=state.describe(),
            effect=self.evaluate_effect(command, state),
        )

    def evaluate_effect(
        self, command: InverterCommand, state: InverterState
    ) -> EffectVerdict:
        """Is the inverter actually TRADING, not merely moving the battery?

        Two properties keep this honest on hardware whose conventions we do
        not control:

        * ``battery_power_w`` arrives already NORMALIZED (positive = charging),
          so no per-model polarity reaches this logic;
        * grid flow comes from the two ALWAYS-POSITIVE directional readings,
          never the signed one, so the integration's ``invert_grid_power``
          option cannot turn a purchase into a sale here.
        """
        threshold = float(getattr(self.config, "effect_threshold_w", 200))
        battery = state.battery_power_w
        imported = state.grid_import_power_w
        exported = state.grid_export_power_w

        if battery is None:
            return EffectVerdict.INDETERMINATE

        action = command.action

        if action is ControlAction.HOLD:
            return (EffectVerdict.PASS if abs(battery) < threshold
                    else EffectVerdict.FAIL)

        if action is ControlAction.GRID_CHARGE:
            if battery <= threshold:
                if self._at_cutoff(state, charging=True):
                    # A full battery is not a failed command.
                    return EffectVerdict.INDETERMINATE
                return EffectVerdict.FAIL
            if imported is None:
                return EffectVerdict.INDETERMINATE
            if imported > threshold:
                return EffectVerdict.PASS     # importing — genuinely buying
            # Charging, but PV surplus can explain it. Not proof either way.
            return EffectVerdict.INDETERMINATE

        if action.is_discharge:
            if battery >= -threshold:
                if self._at_cutoff(state, charging=False):
                    # An empty battery is not a failed command.
                    return EffectVerdict.INDETERMINATE
                return EffectVerdict.FAIL
            if not action.exports_to_grid:
                return EffectVerdict.PASS
            if exported is None:
                return EffectVerdict.INDETERMINATE
            if exported > threshold:
                return EffectVerdict.PASS     # exporting — genuinely selling
            # Discharging, but house load absorbs it all. Not selling.
            return EffectVerdict.INDETERMINATE

        return EffectVerdict.INDETERMINATE

    @staticmethod
    def _at_cutoff(state: InverterState, charging: bool) -> bool:
        """Has the inverter a legitimate reason to ignore the command?

        Without this, a battery that is simply full would look like a failed
        grid-charge and escalate a healthy system.

        The discharge limb reads 30405, which is the VPP-cluster cutoff. It is
        used here ONLY to downgrade a FAIL to INDETERMINATE. It is emphatically
        not the optimizer's reserve: the reference inverter was observed
        discharging to 18 % under ordinary Load First operation with 30405=20,
        so 30405 does not describe local-mode behaviour. The authoritative
        "do not sell below here" rule is the optimizer's own configured
        min_soc (DirectControl._get_min_soc).
        """
        soc = state.soc_percent
        if soc is None:
            return False
        if charging:
            limit = state.charge_cutoff_soc
            return limit is not None and soc >= limit
        limit = state.discharge_cutoff_soc
        return limit is not None and soc <= limit

    # --- diagnostics ------------------------------------------------------

    def get_diagnostics(self) -> Dict[str, Any]:
        return {
            "backend": self.name,
            "executor": getattr(self.executor, "name", "unknown"),
            "control_status": self.control_status,
            "dry_run": self.dry_run,
            "commissioning": self.commissioning,
            "automatic_writes_allowed": self.automatic_writes_allowed,
            "can_read": self.can_read,
            "session_state": self.session_state.value,
            "safe_to_stop": self.session_state.safe_to_stop,
            "grid_charge_path": self.grid_charge_path,
            "tou_fallback": "enabled" if TOU_FALLBACK_ENABLED else "disabled",
            "tou_period_count": self.last_tou_period_count,
            "external_scheduler_present": (
                self.last_tou_period_count is not None
                and self.last_tou_period_count > 0),
            "external_scheduler_refusals": (
                self._counters["external_scheduler_refusals"]),
            "priority_mode_capability": self.priority_mode_capability.value,
            "arm_failures": self._counters["arm_failures"],
            "rate_limited_steps": self._counters["rate_limited"],
            "rollback_attempts": self._counters["rollback_attempts"],
            "rollback_confirmed": self._counters["rollback_confirmed"],
            "release_attempts": self._counters["release_attempts"],
            "release_deferred": self._counters["release_deferred"],
            "disarm_attempts": self._counters["disarm_attempts"],
            "disarm_confirmed": self._counters["disarm_confirmed"],
            "authority_not_ours_refusals": (
                self._counters["authority_not_ours_refusals"]),
            # Telemetry, republished so normalization is verifiable on the
            # real installation: raw beside normalized makes a wrong
            # battery_power_direction obvious at a glance.
            "battery_power_sensor": getattr(
                self.config, "battery_power_sensor", ""),
            "battery_power_direction": getattr(
                self.config, "battery_power_direction", ""),
            "battery_power_raw_w": self.last_battery_power_raw_w,
            "battery_power_normalized_w": self.last_battery_power_w,
            "grid_import_power_w": self.last_grid_import_power_w,
            "grid_export_power_w": self.last_grid_export_power_w,
            # Signed grid power is published for humans only. Nothing in the
            # EFFECT path reads it, because its sign is an integration option.
            "grid_power_signed_w_diagnostic_only": self.last_grid_power_w,
        }
