#!/usr/bin/env python3
"""Run ONE supervised commissioning operation against the inverter.

This is the only entry point in the project that can write a control register,
and it is deliberately a manual command-line tool: one operation per
invocation, each requiring --confirm, with no loop, no timer and no scheduler
anywhere near it.

    uv run python scripts/commission.py --ha-url http://ha:8123 \
        --token <long-lived token> --device-id <growatt device id> \
        --operation state

`state` is read-only and needs no --confirm. The others write:

    session-test  HOLD -> timed renewal experiment -> RELEASE, start to
                  finish in this one process. THE ONLY WAY TO OPEN A SESSION.
    watchdog-test HOLD for one minute, then DO NOTHING and watch: does the
                  inverter end the session by itself? Polls 30100/30407/30408/
                  30409 and the measured battery and grid power every few
                  seconds, past the expiry, then releases.
    recover       release a session a PREVIOUS run left armed, using the
                  durable lease as evidence that it is ours to clean up
    strand        deliberately abandon an armed +1% session, for testing
                  recovery. Requires --strand-i-will-recover as well
    release       give the inverter back to its own local logic
    probe         write 30476 to a different value, read it back, restore it

**There is deliberately no standalone `hold` or `renew`.** Ownership of a VPP
session is process-local: it comes from this process's own successful writes
and is never reconstructed from register values, because a register cannot
tell you who wrote it. A `hold` in one invocation therefore CANNOT be renewed
or released by the next one — that invocation would find 30100=1 it did not
set and refuse, correctly, leaving an armed session that only a watchdog
expiry could end. An operation that can only ever open a session it cannot
close has no safe use, so it is not offered.

`watchdog-test` is the experiment `session-test` cannot be: it never renews.
Everything else here assumed a session left alone expires on its own, and
nothing had observed that happen. It does not: on 2026-09-05 the reference WIT
held 30407=1 through t=90s of a 60 s window, and only the release ended the
session. 30408 does not count down either -- it echoes the last value written
-- so neither register describes a timeout, and re-running this is how any
firmware change to that would be noticed.

Its telemetry answered the other open question the same run. At +1 % the
battery stopped serving the house: discharge fell from ~460 W to ~110 W and
~390 W came from the grid instead, reverting within seconds of the release. So
HOLD does hold, and it holds by importing.

`session-test` opens and closes the session in one process. It stays alive
through the whole release: RELEASED requires BOTH halves confirmed by
read-back — 30100=0, then the delayed 30407=0 — and the timers that finish it
exist only while this process does.

**Interrupting it:** one Ctrl-C aborts the experiment and hands the inverter
back; the process stays alive through that cleanup. A second Ctrl-C during
cleanup force-aborts and may leave the inverter armed — it says so, loudly,
and `--operation state` is how you check. A reporting timeout marks the test
failed but never ends the cleanup on its own.

**If the process is killed outright, nothing in the hardware rescues the
inverter.** The timed override was expected to expire by itself; `watchdog-test`
established on 2026-09-05 that it does not (30407 still 1 at t=90s after a 60 s
window). The bounded 1-10 minute duration is therefore a bound on the
operator's attention, not a safety net.

What does rescue it is the durable lease at `--lease-path`, written BEFORE
authority is taken and removed only once both halves of a release are
confirmed. A later run that finds a lease AND a matching armed inverter is
allowed exactly one thing: `--operation recover`, which releases. It never
resumes the command, never re-arms, and never adopts the session as its own.
Without a lease, 30100=1 stays AUTHORITY_HELD_NOT_OURS and nothing touches it.

`--operation strand` exists to test that path honestly: it opens a +1% HOLD,
confirms it, and then terminates the process outright, leaving the inverter
armed exactly as a crash would. Run `--operation recover` afterwards. Nothing
else in this file will ever leave a session behind on purpose.

`release` remains for the aftermath of exactly that: it refuses to revoke
authority this process did not take, so it is a safe thing to try and a
useless thing to rely on.

Run hold -> renew -> release first, in that order. Those three are the minimal
VPP path (30408, 30409, 30100, 30407) and none of them touches 30476. `probe`
is NOT a warm-up: proving 30476 writable is not a reason to write it, and it
changes the inverter's base mode to find out. Save it for the grid-charge
experiment, which is the only place a hypothesis needs it.

**30411 must read 0.** Every writing operation refuses while another
scheduler's TOU schedule is loaded — on the reference installation that is
Growatt Smart Scheduling, which wrote 16 periods and cleared them when it was
switched off. Nothing in this project ever writes a TOU period, so a non-zero
count is not ours. Check with `--operation state`, and switch the external
scheduler off before commissioning. `release` is exempt: handing the inverter
back is never blocked.

Revoking authority is rate-limited for 30 s by the very write that took it, so
`release` reports "in progress" and schedules its own retry; because a one-shot
CLI exits, --wait-for-release holds the process open until both halves read
back. It defaults to 120 s for exactly that reason.

Nothing here reads a schedule or a price. It proves the session machinery
works, and nothing else.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "appdaemon/apps")

from battery_optimizer_lib.config import BatteryOptimizerConfig          # noqa: E402
from battery_optimizer_lib.control import (                             # noqa: E402
    CommissioningSession,
    Heartbeat,
    SessionLease,
    SessionState,
    UpstreamVppBackend,
    build_executor,
)
from battery_optimizer_lib.control.commissioning import (               # noqa: E402
    DEFAULT_COMMISSIONING_MINUTES,
    DEFAULT_WATCHDOG_MINUTES,
    DEFAULT_WATCHDOG_OBSERVE_SECONDS,
    MAX_COMMISSIONING_MINUTES,
    MIN_COMMISSIONING_MINUTES,
)

# No "hold" and no "renew": see the module docstring. An operation that can
# only open a session this process cannot close is not offered at all.
WRITING_OPERATIONS = ("session-test", "watchdog-test", "recover", "strand",
                      "probe", "release")

DEFAULT_LEASE_PATH = os.path.expanduser("~/.battery_optimizer_commission_lease.json")
DEFAULT_HEARTBEAT_PATH = os.path.expanduser(
    "~/.battery_optimizer_commission_heartbeat.json")


class RestApp:
    """The slice of the AppDaemon API the backend uses, over HA's REST API."""

    # Services that HA registers with SupportsResponse.NONE reject a call
    # carrying ?return_response with a 400 BEFORE the handler runs, so asking
    # for a response indiscriminately makes every write fail without ever
    # reaching the inverter. Which services return one is discovered from HA
    # itself; this set is only the fallback if that discovery fails, and it
    # holds the one read the backend cannot work without.
    FALLBACK_RESPONSE_SERVICES = frozenset({"growatt_modbus/get_register_data"})

    def __init__(self, base: str, token: str, verbose: bool = False):
        self.base = base.rstrip("/")
        self.token = token
        self.verbose = verbose
        self.pending = []          # (callback, due_timestamp, handle)
        self._handle = 0
        self._response_services = None   # discovered lazily, once

    def _request(self, path: str, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{self.base}{path}", data=data,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"},
            method="POST" if data is not None else "GET")
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode()
        return json.loads(body or "{}")

    def log(self, message, level="INFO"):
        if level == "DEBUG" and not self.verbose:
            return
        print(f"  [{level:<8}] {message}")

    def get_state(self, entity):
        try:
            return self._request(f"/api/states/{entity}").get("state")
        except Exception:
            return None

    def _returns_response(self, service: str) -> bool:
        """Does this service declare a response? Asked of HA, once."""
        if self._response_services is None:
            try:
                domains = self._request("/api/services")
                self._response_services = {
                    f"{domain['domain']}/{name}"
                    for domain in domains
                    for name, spec in domain.get("services", {}).items()
                    if spec.get("response")
                }
            except Exception as e:  # noqa: BLE001 - discovery is best-effort
                self.log(f"service discovery failed ({e}); falling back to "
                         f"{sorted(self.FALLBACK_RESPONSE_SERVICES)}",
                         level="WARNING")
                self._response_services = set(self.FALLBACK_RESPONSE_SERVICES)
        return service in self._response_services

    def call_service(self, service, hass_timeout=None, **kwargs):
        path = f"/api/services/{service}"
        if self._returns_response(service):
            path += "?return_response"
        try:
            data = self._request(path, kwargs)
        except urllib.error.HTTPError as e:
            # A ServiceValidationError reaches us as a 400 whose body carries
            # the message. A plain ValueError -- which is what
            # growatt_modbus.write_register raises, including for the WIT write
            # cooldown -- becomes a 500 with a generic body, so that text does
            # NOT survive the REST hop and the executor cannot map it to
            # RATE_LIMITED. The backend's own cooldown tracker is what keeps
            # that case from being reported as a hard failure; a 500 here means
            # the real reason is in the HA log, not in this message.
            detail = e.read().decode(errors="replace")
            if e.code >= 500:
                detail += (" (HA hides service exception messages on 5xx -- "
                           "check the Home Assistant log for the real cause)")
            raise RuntimeError(f"HTTP {e.code}: {detail}") from None
        if not isinstance(data, dict):
            # Without ?return_response HA answers with the LIST of states the
            # call changed -- often empty. There is no service response to
            # unwrap, and the write path ignores the value anyway; what matters
            # is that a successful write is not turned into an exception here.
            return data
        return data.get("service_response", data)

    def run_in(self, callback, delay, **kwargs):
        """Record a scheduled retry. --wait-for-release is what fires these."""
        self._handle += 1
        handle = f"timer_{self._handle}"
        self.pending.append([callback, time.time() + delay, handle])
        return handle

    def cancel_timer(self, handle):
        self.pending = [p for p in self.pending if p[2] != handle]

    def run_due_timers(self):
        now = time.time()
        due = [p for p in self.pending if p[1] <= now]
        for entry in due:
            self.pending.remove(entry)
            entry[0]()
        return bool(due)


def print_lease(backend) -> None:
    record = backend.lease.read()
    if record is None:
        print(f"  lease                     = none ({backend.lease.path})")
        return
    print(f"  lease                     = {record.describe()}")
    print( "                              ^ a session may still be armed from "
           "an earlier run;")
    print( "                                --operation recover releases it "
           "(never resumes it)")


def print_state(backend) -> None:
    state = backend.read_state()
    if state is None:
        print("  inverter state: UNREADABLE")
        return
    print(f"  30100 control_authority   = {state.control_authority}")
    print(f"  30407 remote_power_enable = {state.remote_enabled}")
    print(f"  30408 duration            = {state.duration_minutes}")
    print(f"  30409 commanded_power     = {state.commanded_power}")
    scheduler = ("  <-- EXTERNAL SCHEDULER: writes are interlocked"
                 if state.external_scheduler_present else "  (never written)")
    print(f"  30411 tou_period_count    = {state.tou_period_count}{scheduler}")
    print(f"  30476 priority_mode       = {state.priority_mode}")
    print(f"  battery (normalized)      = {state.battery_power_w} W  (+ = charging)")
    print(f"  grid import / export      = {state.grid_import_power_w} / "
          f"{state.grid_export_power_w} W")
    print(f"  SOC                       = {state.soc_percent} %")


def make_wait(app):
    """A `wait(seconds)` that lets real time pass AND runs due callbacks.

    The release lifecycle finishes on timers this process scheduled, so a wait
    that only slept would hang forever at the settle.
    """
    def wait(seconds):
        deadline = time.time() + seconds
        app.run_due_timers()
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(2.0, remaining))
            app.run_due_timers()
    return wait


def wait_for_release(app, backend, timeout_seconds: int) -> bool:
    """Hold the process open so the scheduled release retry can complete."""
    print(f"\nwaiting up to {timeout_seconds}s for the release to confirm "
          f"(do NOT interrupt)...")
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if backend.session_state is SessionState.RELEASED:
            print("  release CONFIRMED by read-back (30100=0 and 30407=0)")
            return True
        if not app.pending:
            break
        time.sleep(2)
        app.run_due_timers()
    confirmed = backend.session_state is SessionState.RELEASED
    print(f"  session_state = {backend.session_state.value}")
    return confirmed


def strand(app, session, backend, duration_minutes, acknowledged: bool,
           heartbeat_path: str = "") -> int:
    """Open a session and abandon it, the way a killed process would.

    The only operation here that deliberately leaves an inverter armed, and it
    exists for one reason: the recovery path must be proved against the real
    failure rather than against a simulation of it. So the exit is os._exit --
    no cleanup, no finally blocks, no scheduled disarm surviving in a timer --
    which is exactly what SIGKILL would leave behind, and exactly the state
    `--operation recover` has to be able to find.

    Harmless by construction: the session it strands is the +1% HOLD, measured
    on the reference WIT at roughly 100-150 W of charge with the house load on
    the grid. Nothing about it is energetic; what is dangerous is leaving it
    there, which is the point.
    """
    if not acknowledged:
        print("REFUSED: 'strand' LEAVES THE INVERTER ARMED and no hardware "
              "expiry will end it. Re-run with --strand-i-will-recover if you "
              "are about to run --operation recover.")
        return 2

    # Stamp the heartbeat the way a living owner would, once. Abandoning it
    # here is what makes this a faithful test of the reaper: the stamp goes
    # stale on its own, exactly as it would for an optimizer that stopped
    # running, rather than being deleted to force the verdict.
    heartbeat = Heartbeat(heartbeat_path, log_func=app.log)
    heartbeat.stamp()

    result = session.hold(duration_minutes=duration_minutes)
    print()
    print(result.describe())

    if not result.ok:
        print("\nthe hold did not arm, so there is nothing stranded. "
              "Releasing normally.")
        session.release()
        print_state(backend)
        print_lease(backend)
        return 1

    print_state(backend)
    print_lease(backend)
    print(f"\nheartbeat stamped at {heartbeat_path} and now abandoned; it "
          f"goes stale on its own")
    print("ABANDONING THIS PROCESS NOW — the inverter stays armed, exactly "
          "as it would after a crash.")
    print("Recover it with:  --operation recover --confirm")
    print("or let the reaper find it:  scripts/reap.py --confirm")
    sys.stdout.flush()
    # Not sys.exit: that unwinds, and an orderly unwind is the one thing this
    # operation must not do.
    os._exit(0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ha-url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--operation", required=True,
                        choices=("state",) + WRITING_OPERATIONS)
    parser.add_argument("--duration-minutes", type=int, default=None,
                        help=f"watchdog window for the session "
                             f"({MIN_COMMISSIONING_MINUTES}-"
                             f"{MAX_COMMISSIONING_MINUTES} min; 0 is refused — "
                             f"it is a session with no watchdog). Defaults to "
                             f"{DEFAULT_COMMISSIONING_MINUTES} min, and to "
                             f"{DEFAULT_WATCHDOG_MINUTES} for watchdog-test, "
                             f"which wants the shortest window it can get")
    parser.add_argument("--observe-seconds", type=int,
                        default=DEFAULT_WATCHDOG_OBSERVE_SECONDS,
                        help="watchdog-test: how long to keep polling after "
                             "the hold. Must outlast the window, or the test "
                             "ends while the session is still legitimately "
                             "armed and proves nothing")
    parser.add_argument("--poll-seconds", type=int, default=5,
                        help="watchdog-test: seconds between observations")
    parser.add_argument("--confirm", action="store_true",
                        help="required for any operation that writes")
    parser.add_argument("--wait-for-release", type=int, default=120,
                        metavar="SECONDS",
                        help="after release, wait for BOTH halves to read back "
                             "(30100=0, then the delayed 30407=0). 0 disables, "
                             "which can leave the session settling")
    parser.add_argument("--renew-after-seconds", type=int, default=35,
                        help="session-test: seconds to hold the session open "
                             "before re-arming (must clear the 30 s cooldown)")
    parser.add_argument("--release-timeout", type=int, default=240,
                        help="session-test: how long to wait for RELEASED")
    parser.add_argument("--lease-path", default=DEFAULT_LEASE_PATH,
                        help="durable record of an unfinished session. It is "
                             "the only thing that lets a later run recognise "
                             "a stranded session as ours to release")
    parser.add_argument("--heartbeat-path", default=DEFAULT_HEARTBEAT_PATH,
                        help="strand: where to stamp the heartbeat that the "
                             "reaper watches go stale. Stamped once, then "
                             "abandoned with the session")
    parser.add_argument("--strand-i-will-recover", action="store_true",
                        help="strand: acknowledge that this LEAVES THE "
                             "INVERTER ARMED and that you will run "
                             "--operation recover next")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--battery-power-direction",
                        default="negative_is_charging")
    args = parser.parse_args()

    duration_minutes = args.duration_minutes
    if duration_minutes is None:
        duration_minutes = (DEFAULT_WATCHDOG_MINUTES
                            if args.operation == "watchdog-test"
                            else DEFAULT_COMMISSIONING_MINUTES)

    if args.operation in WRITING_OPERATIONS and not args.confirm:
        print(f"REFUSED: '{args.operation}' writes to the inverter. "
              f"Re-run with --confirm if you are supervising it right now.")
        return 2

    app = RestApp(args.ha_url, args.token, verbose=args.verbose)
    config = BatteryOptimizerConfig(
        device_id=args.device_id,
        control_mode="commissioning",
        session_lease_path=args.lease_path,
        soc_sensor="sensor.growatt_battery_battery_soc",
        battery_power_sensor="sensor.growatt_battery_battery_power",
        battery_power_direction=args.battery_power_direction,
        grid_import_power_sensor="sensor.growatt_grid_grid_import_power",
        grid_export_power_sensor="sensor.growatt_grid_grid_export_power",
        grid_power_sensor="sensor.growatt_grid_grid_power",
    )
    backend = UpstreamVppBackend(app, config,
                                 executor=build_executor(app, config))
    session = CommissioningSession(backend, log_func=app.log)

    print(backend.describe_mode())
    print()

    if args.operation == "state":
        backend.reconcile()
        print(f"\nsession_state = {backend.session_state.value}\n")
        print_state(backend)
        print_lease(backend)
        return 0

    print(f"--- {args.operation} ---")
    if args.operation == "session-test":
        result = session.session_test(
            wait=make_wait(app),
            duration_minutes=duration_minutes,
            renew_after_seconds=args.renew_after_seconds,
            release_timeout_seconds=args.release_timeout,
        )
    elif args.operation == "watchdog-test":
        result = session.watchdog_test(
            wait=make_wait(app),
            duration_minutes=duration_minutes,
            observe_seconds=args.observe_seconds,
            poll_seconds=args.poll_seconds,
            release_timeout_seconds=args.release_timeout,
        )
    elif args.operation == "recover":
        result = session.recover(wait=make_wait(app),
                                 release_timeout_seconds=args.release_timeout)
    elif args.operation == "strand":
        return strand(app, session, backend, duration_minutes,
                      acknowledged=args.strand_i_will_recover,
                      heartbeat_path=args.heartbeat_path)
    elif args.operation == "probe":
        result = session.probe_priority_mode()
    else:
        result = session.release()

    print()
    if session.observations:
        print("observations:")
        for line in session.observations:
            print(f"    {line}")
        print()
    print(result.describe())
    print(f"session_state = {backend.session_state.value}   "
          f"safe_to_stop = {backend.session_state.safe_to_stop}")
    print()
    print_state(backend)

    if (args.operation in ("release", "session-test", "watchdog-test")
            and args.wait_for_release):
        wait_for_release(app, backend, args.wait_for_release)
        print()
        print_state(backend)

    if not backend.session_state.safe_to_stop:
        print("\n*** THIS PROCESS HOLDS INVERTER AUTHORITY. Run "
              "--operation release before walking away. ***")

    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
