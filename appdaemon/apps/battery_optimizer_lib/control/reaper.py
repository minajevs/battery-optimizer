"""The second safety layer: something outside the optimizer ends what it left.

The optimizer's own cleanup covers the paths it survives — a failure, an
exception, an interrupt. It cannot cover the paths where it stops running: a
hung callback, a crashed app, a thread that never returns. This hardware has no
expiry (``lease.py``), so those leave a session running forever.

**Every condition is necessary and none is sufficient.** A stale heartbeat
grants nothing. A lease grants nothing. 30100/30407 = 1/1 grants nothing.
Reaping requires all of them to agree, about the same device, at the same time:

    heartbeat stale       the owner is not running
    lease present         a session was started, and by us
    lease device matches  it was started against THIS inverter
    30100 = 1, 30407 = 1  a session really is armed right now
    30411 = 0             no other scheduler has appeared meanwhile
    30409 = lease setpoint  the armed session is the one recorded

The last two are refusals to act rather than reasons to act. A schedule that
appeared, or a setpoint that changed, means something touched this inverter
after our session — so the armed session is not the one the lease accounts for,
and the honest response is to report loudly and leave it alone. Cleaning up
after an actor we cannot identify is how a "safety" layer becomes the hazard.

**What the reaper is allowed to do is exactly one thing: release.** It has no
path to HOLD, charge, discharge or export — it calls ``CommissioningSession
.recover()``, the same path proved against real hardware on 2026-09-05, which
refuses unless the backend independently reached RECOVERABLE_LEASE from its own
read and its own reading of the lease. The checks below are therefore a
pre-filter in front of the backend's, never a substitute for them: the reaper
adds the one fact the backend cannot know (is the owner alive?) and duplicates
nothing else.

**Scope.** A separate AppDaemon app is independent of the optimizer app, not of
AppDaemon. If the whole add-on dies, the reaper dies with it, and only the lease
plus recovery at the next start will clean up. Covering that needs a watcher
outside AppDaemon entirely, which is a later slice and a prerequisite for
unattended energetic control.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .backend import InverterState
from .lease import LeaseRecord
from .upstream_vpp import SessionState

# Why the reaper did or did not act. Stable strings: they are published as the
# status sensor's verdict and are what an operator greps for.
IDLE_NOTHING_ARMED = "idle_nothing_armed"
OWNER_ALIVE = "owner_alive"
NO_LEASE = "no_lease"
LEASE_OTHER_DEVICE = "lease_other_device"
EXTERNAL_SCHEDULER = "external_scheduler"
SETPOINT_CHANGED = "setpoint_changed"
UNREADABLE = "unreadable"
HALF_ARMED = "half_armed"
STRANDED = "stranded"


@dataclass(frozen=True)
class ReapVerdict:
    """One decision, and the reason for it in words an operator can act on."""

    reap: bool
    code: str
    detail: str
    # True when the situation is one a person should look at, whether or not
    # the reaper acted: something touched this inverter that we cannot account
    # for, and nothing here will clean that up.
    alarming: bool = False

    def describe(self) -> str:
        return f"{'REAP' if self.reap else 'hold'} [{self.code}] {self.detail}"


def assess(
    *,
    heartbeat_age: Optional[float],
    stale_after_seconds: float,
    lease: Optional[LeaseRecord],
    state: Optional[InverterState],
    device_id: str,
) -> ReapVerdict:
    """Decide whether a stranded session is ours to end. Writes nothing.

    ``heartbeat_age`` of None means unknown, which counts as stale: an owner
    that cannot be shown to be alive is not alive for this purpose. That is
    safe precisely because staleness alone decides nothing.
    """
    if state is None:
        return ReapVerdict(
            False, UNREADABLE,
            "the inverter could not be read, so nothing is known about it. "
            "Not acting blind; will look again next cycle")

    authority = state.control_authority
    armed = state.remote_enabled

    if authority == 0 and armed == 0:
        return ReapVerdict(False, IDLE_NOTHING_ARMED,
                           "inverter is at 0/0 — nothing is armed")

    fresh = (heartbeat_age is not None
             and heartbeat_age <= stale_after_seconds)
    if fresh:
        return ReapVerdict(
            False, OWNER_ALIVE,
            f"a session is armed and its owner is alive "
            f"(heartbeat {heartbeat_age:.0f}s old, stale after "
            f"{stale_after_seconds:.0f}s). Reaping a running optimizer's own "
            f"session is the one thing this must never do")

    age = "never stamped" if heartbeat_age is None else f"{heartbeat_age:.0f}s old"

    if lease is None:
        return ReapVerdict(
            False, NO_LEASE,
            f"30100={authority}/30407={armed} with the heartbeat {age}, but "
            f"there is NO lease — so this session was not started by us and is "
            f"not ours to end. Establish what set it",
            alarming=True)

    if lease.device_id != str(device_id):
        return ReapVerdict(
            False, LEASE_OTHER_DEVICE,
            f"the lease names device {lease.device_id}, not {device_id}. A "
            f"reaper acts only on the inverter its own record names",
            alarming=True)

    if state.external_scheduler_present:
        return ReapVerdict(
            False, EXTERNAL_SCHEDULER,
            f"30411 reports {state.tou_period_count} TOU period(s): another "
            f"scheduler appeared after our session. Releasing would hand the "
            f"inverter to a schedule nobody here chose. NOT adopting",
            alarming=True)

    if (lease.setpoint_percent is not None
            and state.commanded_power is not None
            and state.commanded_power != lease.setpoint_percent):
        return ReapVerdict(
            False, SETPOINT_CHANGED,
            f"30409 reads {state.commanded_power} where the lease records "
            f"{lease.setpoint_percent}: something re-commanded this inverter "
            f"after our session, so the armed session is not the one recorded. "
            f"NOT adopting — establish what changed it",
            alarming=True)

    if authority == 1 and armed == 0:
        # Our own half-applied arm: authority taken, nothing armed. Releasing
        # 30100 is strictly a reduction in control, and the lease says the
        # authority is ours, so this is reapable -- and named separately
        # because it is the hazard pair, not a running session.
        return ReapVerdict(
            True, HALF_ARMED,
            f"30100=1/30407=0 with the heartbeat {age} and a matching lease "
            f"({lease.describe()}): authority held with nothing armed, and "
            f"nobody left to finish it. Releasing")

    if authority == 0 and armed == 1:
        # Armed with no authority. Not a session we can end by revoking
        # authority we do not hold, and not a state this project creates.
        return ReapVerdict(
            False, HALF_ARMED,
            f"30100=0/30407=1: armed without authority. This project never "
            f"creates that pair and revoking authority would not end it. "
            f"Reported, not acted on",
            alarming=True)

    return ReapVerdict(
        True, STRANDED,
        f"30100=1/30407=1 with the heartbeat {age} and a matching lease "
        f"({lease.describe()}). Nothing in this hardware will end it. "
        f"Releasing — never resuming")


class SessionReaper:
    """Runs ``assess`` on a schedule and, when it says so, recovers.

    Deliberately free of AppDaemon: it takes a backend, a session, a heartbeat
    and a ``wait``, so the same object is driven by the AppDaemon app, by a
    command-line runner against real hardware, and by tests.
    """

    def __init__(self, backend, session, heartbeat, device_id: str,
                 stale_after_seconds: float = 90.0, log_func=None,
                 clock=None):
        self.backend = backend
        self.session = session
        self.heartbeat = heartbeat
        self.device_id = str(device_id)
        self.stale_after_seconds = float(stale_after_seconds)
        self._log_func = log_func
        self._clock = clock

        self.last_verdict: Optional[ReapVerdict] = None
        self.last_check_at: Optional[float] = None
        self.last_reap_at: Optional[float] = None
        self.reap_count = 0
        self.reap_failed = 0
        self.alarm_count = 0
        self.reaping = False
        self.assessed_session_id: Optional[str] = None

    def _log(self, message: str, level: str = "INFO") -> None:
        if self._log_func is not None:
            self._log_func(f"[reaper] {message}", level=level)
        else:
            self.backend._log(f"[reaper] {message}", level=level)

    def _now(self) -> Optional[float]:
        return self._clock() if self._clock is not None else None

    def check(self) -> ReapVerdict:
        """One cycle: read, decide, remember. Never writes to the inverter."""
        lease = self.backend.lease.read()
        # The id of the session THIS decision was made about. Everything after
        # this point acts on that id, never on "whatever lease is there now" --
        # the two stop being the same thing the moment a restarted optimizer
        # opens a new one, which is precisely what a recovery's cooldown wait
        # gives it time to do.
        self.assessed_session_id = None if lease is None else lease.session_id
        verdict = assess(
            heartbeat_age=self.heartbeat.age(),
            stale_after_seconds=self.stale_after_seconds,
            lease=lease,
            state=self.backend.read_state(),
            device_id=self.device_id,
        )
        self.last_verdict = verdict
        self.last_check_at = self._now()

        if verdict.alarming:
            self.alarm_count += 1
            self._log(verdict.describe(), level="ERROR")
        elif verdict.reap:
            self._log(verdict.describe(), level="CRITICAL")
        else:
            self._log(verdict.describe(), level="DEBUG")
        return verdict

    def run_once(self, wait) -> ReapVerdict:
        """Check, and recover if the verdict says to.

        Re-entrancy matters here in a way it does not for the CLI: this is
        driven by a timer, and a recovery outlives several of its intervals.
        A second one started on top would race the first over the same
        registers.
        """
        if self.reaping:
            self._log("a recovery is already running; skipping this cycle",
                      level="DEBUG")
            return self.last_verdict or ReapVerdict(False, IDLE_NOTHING_ARMED,
                                                    "recovery in progress")

        verdict = self.check()
        if not verdict.reap:
            return verdict

        self.reaping = True
        try:
            # recover() FENCES the assessed session first: the lease is
            # claimed by id, which stops a restarted optimizer opening a new
            # one while the release waits out the inverter's cooldown. It then
            # re-reads the inverter, re-consults the lease, and re-checks the
            # heartbeat before writing. Nothing here can talk it past any of
            # that, and it refuses unless the backend independently reached
            # RECOVERABLE_LEASE.
            result = self.session.recover(
                wait=wait,
                expected_session_id=self.assessed_session_id,
                heartbeat=self.heartbeat,
                stale_after_seconds=self.stale_after_seconds)
            if result.ok and self.backend.session_state is SessionState.RELEASED:
                self.reap_count += 1
                self.last_reap_at = self._now()
                self._log(f"REAPED: {result.detail}")
            else:
                self.reap_failed += 1
                self._log(
                    f"reap did NOT complete: {result.detail}. The inverter may "
                    f"still be armed — check 30100/30407 by hand",
                    level="CRITICAL")
        except BaseException as e:  # noqa: BLE001 - a timer must not die here
            self.reap_failed += 1
            self._log(f"reap raised {type(e).__name__}: {e}. The inverter may "
                      f"still be armed", level="CRITICAL")
        finally:
            self.reaping = False

        return verdict

    def status(self) -> dict:
        """What to publish. Flat and boring on purpose — it is read in a UI."""
        verdict = self.last_verdict
        return {
            "verdict": verdict.code if verdict else "not_checked_yet",
            "verdict_detail": verdict.detail if verdict else "",
            "would_reap": bool(verdict.reap) if verdict else False,
            "alarming": bool(verdict.alarming) if verdict else False,
            "last_check": self.last_check_at,
            "last_reap": self.last_reap_at,
            "reap_count": self.reap_count,
            "reap_failed": self.reap_failed,
            "alarm_count": self.alarm_count,
            "heartbeat_age_seconds": self.heartbeat.age(),
            "stale_after_seconds": self.stale_after_seconds,
            "reaping_now": self.reaping,
        }
