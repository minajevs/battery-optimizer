"""Growatt WIT VPP control over the upstream 0xAHA/Growatt_ModbusTCP services.

This is the ONLY module that knows a register number.

Upstream has no ``set_wit_mode``, so one fork service call becomes a sequence of
VPP register writes.  Three documented hardware facts shape every sequence here,
and each one has already cost a design revision:

1. **Safe-state pair.** Only 30100/30407 = 0/0 and 1/1 are safe.  1/0 is
   "VPP standby": local battery logic suspended, load drawn from the grid.
2. **Write order.** 30408 (duration) -> 30409 (power) -> 30407 (enable) LAST.
   Arming first applies whatever stale setpoint 30409 still held.
3. **EEPROM.** Only 30407/30408/30409 are marked "Not storage" and safe to
   write every slot.  Everything else is written only when it changes.

STATUS: this slice builds and logs sequences.  Execution is delegated to an
``executor``; the default one is dry-run and performs no I/O whatsoever.
"""

from __future__ import annotations

import datetime
import enum
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .actions import ControlAction
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

# 30409 value that means "hold". NOT 0 — that is documented as "suspend forced
# cycle (passthrough)", and 0 was observed clipping PV. +1% keeps the VPP
# session active while charging nothing meaningful.
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
    * ``ARM_FAILED_AUTHORITY_HELD`` — the documented VPP standby hazard.

    ``RELEASED`` is reserved for a read-back-confirmed 30100=0. Timer expiry of
    the override is a different thing and must not be conflated with it: the
    watchdog returns the inverter to its base mode, it does not clean up
    authority.
    """

    NOT_ARMED = "not_armed"      # never taken authority in this process
    ACTIVE = "active"
    RELEASE_PENDING = "release_pending"
    RELEASED = "released"
    ARM_FAILED_AUTHORITY_HELD = "arm_failed_authority_held"

    @property
    def safe_to_stop(self) -> bool:
        """True when stopping AppDaemon leaves no unfinished handover."""
        return self in (SessionState.NOT_ARMED, SessionState.RELEASED)


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


class HaReadOnlyExecutor:
    """Real Home Assistant reads. Structurally incapable of writing.

    ``get_register_data`` is one of only two upstream services that returns a
    response, which is what makes real verification possible without any
    write capability existing on this object at all.

    There is deliberately no code path here to ``write_register``,
    ``write_registers`` or ``sync_tou_schedule``: ``execute()`` refuses every
    step rather than performing it, so an injection mistake cannot silently
    arm an inverter.
    """

    name = "ha_read_only"
    can_write = False
    can_read = True

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

    def execute(self, step: Any) -> StepResult:
        """Refuse. This executor exists precisely so writes cannot happen."""
        self.refused.append(step)
        self._log(
            f"[{self.name}] refusing write step ({step.describe()}) — this "
            f"executor is read-only",
            level="ERROR",
        )
        return StepResult.FAILED

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


def build_executor(app, config):
    """Pick an executor from config.control_mode. Fails SAFE, never open.

    Anything unrecognised yields the dry-run executor: a misconfiguration must
    never be the thing that grants write access to an inverter.
    """
    mode = getattr(config, "control_mode", "dry_run")
    log = getattr(app, "log", None)

    if mode == "read_only":
        return HaReadOnlyExecutor(
            app,
            device_id=getattr(config, "device_id", ""),
            timeout_seconds=int(getattr(config, "command_timeout_seconds", 15)),
            log_func=log,
        )
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

    def __init__(self, app, config, executor=None, clock: Optional[Callable] = None):
        self.app = app
        self.config = config
        self.executor = executor or DryRunExecutor(getattr(app, "log", None))
        self.cooldown = CooldownTracker(
            int(getattr(config, "wit_cooldown_seconds", WIT_COOLDOWN_SECONDS)),
            clock=clock,
        )

        self.session_state = SessionState.NOT_ARMED
        self.priority_mode_capability = PriorityModeCapability.UNKNOWN
        self.grid_charge_path = "remote_power"
        # Raw last-read power values, republished in diagnostics so the two
        # OPPOSITE sign conventions can be verified on real hardware.
        self.last_battery_power_w: Optional[float] = None
        self.last_grid_power_w: Optional[float] = None
        self._release_timer = None

        # Last value we successfully wrote per register — drives "write only on
        # change" for the non-EEPROM-safe registers.
        self._applied: Dict[int, int] = {}
        self._wrote_priority_mode = False
        self._used_tou = False

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
    def control_status(self) -> str:
        """One unmistakable string for the health sensor.

        DRY_RUN is surfaced ABOVE the session state on purpose: an accidental
        dependency-injection fallback is electrically safe but operationally
        awful, because the optimizer would look healthy while controlling
        nothing.
        """
        if self.dry_run:
            return "DRY_RUN"
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
        return (
            f"BATTERY OPTIMIZER CONTROL: LIVE ({self.executor.name}) — this "
            "app can write inverter control registers."
        )

    def _log(self, message: str, level: str = "INFO") -> None:
        log = getattr(self.app, "log", None)
        if log is not None:
            log(f"[{self.name}] {message}", level=level)

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

        plan = CommandPlan(action=action)

        # 1. Policy registers, only when changed. Applied BEFORE the power
        #    command so the inverter is never briefly driven under stale policy.
        if self._may_write_priority_mode():
            self._staged(plan, REG_PRIORITY_MODE, 1, "priority mode = Battery First")

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

        # Clear any TOU periods — the VPP session governs, not a schedule.
        self._staged(plan, REG_TOU_NUM_PERIODS, 0, "clear TOU periods")

        # 2. Timed override. Both EEPROM-safe, rewritten every slot.
        plan.add(RegisterWrite(REG_REMOTE_DURATION, int(command.duration_minutes),
                               note="duration (watchdog)"))
        plan.add(RegisterWrite(REG_REMOTE_POWER, self._power_target(command),
                               note="signed power target"))

        # 3. Authority acquired LATE, so a partial failure leaves it un-armed.
        if self.session_state is not SessionState.ACTIVE:
            plan.acquires_authority = True
            plan.add(RegisterWrite(REG_CONTROL_AUTHORITY, 1,
                                   note="acquire authority (late)"))

        # 4. Arm LAST, every slot — re-arming because nothing documents that
        #    rewriting 30408 renews a timed session.
        plan.arms_at = len(plan.steps)
        plan.add(RegisterWrite(REG_REMOTE_ENABLE, 1, note="ARM (always last)"))

        return plan

    def _build_release_plan(self) -> CommandPlan:
        """PASSTHROUGH: give the inverter fully back to local control (0/0)."""
        plan = CommandPlan(action=ControlAction.PASSTHROUGH)

        if self._wrote_priority_mode and self._may_write_priority_mode():
            plan.add(RegisterWrite(REG_PRIORITY_MODE, 0,
                                   note="restore priority mode = Load First"))
        if self._used_tou:
            plan.add(RegisterWrite(REG_TOU_NUM_PERIODS, 0, note="clear TOU periods"))

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

        return self._execute_plan(plan)

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

        if state is not None and state.in_vpp_standby:
            self._log(
                "inverter is currently in VPP standby (30100=1, 30407=0): local "
                "battery logic is suspended and load is drawn from the grid",
                level="WARNING",
            )

    def _execute_plan(self, plan: CommandPlan) -> SendResult:
        authority_taken = False

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
            self.session_state = SessionState.RELEASED
        else:
            self.session_state = SessionState.ACTIVE
        return SendResult.CONFIRMED

    def _enter_arm_failed(self) -> None:
        """Authority held but arming failed — the documented VPP standby hazard.

        The obvious rollback (30100=0) CANNOT land: our own successful 30100=1
        stamped the cooldown, so the revoke is refused for up to 30 s. So it is
        scheduled rather than attempted, and retried until a read-back confirms
        30100 == 0.
        """
        self.session_state = SessionState.ARM_FAILED_AUTHORITY_HELD
        self._counters["arm_failures"] += 1
        remaining = self.cooldown.seconds_remaining(REG_CONTROL_AUTHORITY)
        self._log(
            "ARMING FAILED with control authority held — the inverter is in VPP "
            "standby (30100=1, 30407=0): local battery logic is suspended and "
            f"load is drawn from the grid. Authority revoke is rate-limited for "
            f"{remaining:.0f}s; scheduling rollback.",
            level="CRITICAL",
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
            self.session_state = SessionState.RELEASED
            self._log("authority rollback confirmed by read-back (30100=0)")
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

        state = self.read_state()
        if state is not None and state.control_authority not in (0, None):
            self._log(
                "authority revoke not yet confirmed by read-back; retrying",
                level="WARNING",
            )
            self._schedule_release_retry(
                self.cooldown.seconds_remaining(REG_CONTROL_AUTHORITY)
            )
            return SendResult.PENDING

        # Confirmed (or unreadable, in which case the write itself stood).
        self._log("authority released (30100=0); disarming after settle")
        self._schedule_disarm()
        self.session_state = SessionState.RELEASED
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

    def _schedule_disarm(self) -> None:
        run_in = getattr(self.app, "run_in", None)
        if run_in is None:
            return
        try:
            run_in(self._disarm, self._settle_seconds())
        except Exception as e:  # pragma: no cover
            self._log(f"could not schedule disarm: {e}", level="ERROR")

    def _disarm(self, kwargs=None) -> None:
        self._execute_step(
            RegisterWrite(REG_REMOTE_ENABLE, 0, note="disarm after settle")
        )

    # --- startup reconciliation -------------------------------------------

    def reconcile(self) -> Optional[InverterState]:
        """Seed our model of the inverter from what it actually reports.

        Two things this fixes, both only possible once reads exist:

        * **Redundant writes.** The write-on-change cache starts empty, so a
          fresh start would rewrite every policy register even when the
          inverter already holds the right value — against a 30 s per-register
          cooldown.
        * **Inherited state.** AppDaemon may restart into an inverter already
          under VPP control, possibly one this app never armed. Growatt Smart
          Scheduling has been observed moving 30100 on its own, so finding
          authority held is not proof it is ours.
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

        if state.in_vpp_standby:
            self.session_state = SessionState.ARM_FAILED_AUTHORITY_HELD
            self._log(
                "found the inverter in VPP STANDBY at startup (30100=1, "
                "30407=0): local battery logic is suspended and load is drawn "
                "from the grid. This app did not necessarily cause it — "
                "Growatt Smart Scheduling can move 30100 independently. "
                "Scheduling a release.",
                level="CRITICAL",
            )
            self._schedule_rollback(
                self.cooldown.seconds_remaining(REG_CONTROL_AUTHORITY))
        elif state.control_authority == 1 and state.remote_enabled == 1:
            self.session_state = SessionState.ACTIVE
            self._log(
                f"found an ACTIVE VPP session at startup ({state.describe()}); "
                f"adopting it")
        else:
            self.session_state = SessionState.NOT_ARMED

        self._log(f"reconciled with inverter: {state.describe()}")
        return state

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

        battery_power = self._read_power(
            getattr(self.config, "battery_power_sensor", ""))
        grid_power = self._read_power(
            getattr(self.config, "grid_power_sensor", ""))
        # Remembered for diagnostics: these two carry OPPOSITE sign conventions
        # and must be verifiable against the real installation.
        self.last_battery_power_w = battery_power
        self.last_grid_power_w = grid_power

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
            grid_power_w=grid_power,
            soc_percent=self._read_power(
                getattr(self.config, "soc_sensor", "")),
        )

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

        The two sign conventions are opposite and mixing them would read an
        import as an export:
            battery_power_w > 0  ->  CHARGING
            grid_power_w    > 0  ->  EXPORTING
        """
        threshold = float(getattr(self.config, "effect_threshold_w", 200))
        battery = state.battery_power_w
        grid = state.grid_power_w

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
            if grid is None:
                return EffectVerdict.INDETERMINATE
            if grid < -threshold:
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
            if grid is None:
                return EffectVerdict.INDETERMINATE
            if grid > threshold:
                return EffectVerdict.PASS     # exporting — genuinely selling
            # Discharging, but house load absorbs it all. Not selling.
            return EffectVerdict.INDETERMINATE

        return EffectVerdict.INDETERMINATE

    @staticmethod
    def _at_cutoff(state: InverterState, charging: bool) -> bool:
        """Has the inverter a legitimate reason to ignore the command?

        Without this, a battery that is simply full would look like a failed
        grid-charge and latch the TOU fallback permanently.
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
            "can_read": self.can_read,
            "session_state": self.session_state.value,
            "safe_to_stop": self.session_state.safe_to_stop,
            "grid_charge_path": self.grid_charge_path,
            "priority_mode_capability": self.priority_mode_capability.value,
            "arm_failures": self._counters["arm_failures"],
            "rate_limited_steps": self._counters["rate_limited"],
            "rollback_attempts": self._counters["rollback_attempts"],
            "rollback_confirmed": self._counters["rollback_confirmed"],
            "release_attempts": self._counters["release_attempts"],
            "release_deferred": self._counters["release_deferred"],
            # Raw signed telemetry, republished so the OPPOSITE sign
            # conventions can be confirmed on the real installation:
            #   battery_power_w > 0 = CHARGING
            #   grid_power_w    > 0 = EXPORTING
            "battery_power_sensor": getattr(
                self.config, "battery_power_sensor", ""),
            "battery_power_w": self.last_battery_power_w,
            "grid_power_sensor": getattr(self.config, "grid_power_sensor", ""),
            "grid_power_w": self.last_grid_power_w,
        }
