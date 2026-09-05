#!/usr/bin/env python3
"""Run ONE supervised commissioning operation against the inverter.

This is the only entry point in the project that can write a control register,
and it is deliberately a manual command-line tool: one operation per
invocation, each requiring --confirm, with no loop, no timer and no scheduler
anywhere near it.

    uv run python scripts/commission.py --ha-url http://ha:8123 \
        --token <long-lived token> --device-id <growatt device id> \
        --operation state

`state` is read-only and needs no --confirm. The other four write:

    hold     open a timed VPP session at +1 % (the least energetic command)
    renew    re-arm the watchdog on the session THIS process opened
    release  give the inverter back to its own local logic
    probe    write 30476 to a different value, read it back, restore it

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

`hold` and `renew` open a session that this process must also close. A single
invocation cannot do both — that is the point — so run `release` afterwards,
and note that revoking authority is rate-limited for 30 s by the very write
that took it. `release` reports "in progress" and schedules its own retry;
because a one-shot CLI exits, use --wait-for-release to hold the process open
until read-back confirms 30100=0.

Nothing here reads a schedule or a price. It proves the session machinery
works, and nothing else.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "appdaemon/apps")

from battery_optimizer_lib.config import BatteryOptimizerConfig          # noqa: E402
from battery_optimizer_lib.control import (                             # noqa: E402
    CommissioningSession,
    SessionState,
    UpstreamVppBackend,
    build_executor,
)

WRITING_OPERATIONS = ("probe", "hold", "renew", "release")


class RestApp:
    """The slice of the AppDaemon API the backend uses, over HA's REST API."""

    def __init__(self, base: str, token: str, verbose: bool = False):
        self.base = base.rstrip("/")
        self.token = token
        self.verbose = verbose
        self.pending = []          # (callback, due_timestamp, handle)
        self._handle = 0

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

    def call_service(self, service, hass_timeout=None, **kwargs):
        try:
            data = self._request(f"/api/services/{service}?return_response", kwargs)
        except urllib.error.HTTPError as e:
            # HA turns a service exception into a 400 whose body carries the
            # message -- including the WIT cooldown refusal, which the executor
            # maps to RATE_LIMITED by matching on that text.
            detail = e.read().decode(errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {detail}") from None
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


def wait_for_release(app, backend, timeout_seconds: int) -> bool:
    """Hold the process open so the scheduled release retry can complete."""
    print(f"\nwaiting up to {timeout_seconds}s for the release to confirm "
          f"(do NOT interrupt)...")
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if backend.session_state is SessionState.RELEASED:
            print("  release CONFIRMED by read-back (30100=0)")
            return True
        if not app.pending:
            break
        time.sleep(2)
        app.run_due_timers()
    confirmed = backend.session_state is SessionState.RELEASED
    print(f"  session_state = {backend.session_state.value}")
    return confirmed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ha-url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--operation", required=True,
                        choices=("state",) + WRITING_OPERATIONS)
    parser.add_argument("--duration-minutes", type=int, default=5)
    parser.add_argument("--confirm", action="store_true",
                        help="required for any operation that writes")
    parser.add_argument("--wait-for-release", type=int, default=0,
                        metavar="SECONDS",
                        help="after release, wait for read-back confirmation")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--battery-power-direction",
                        default="negative_is_charging")
    args = parser.parse_args()

    if args.operation in WRITING_OPERATIONS and not args.confirm:
        print(f"REFUSED: '{args.operation}' writes to the inverter. "
              f"Re-run with --confirm if you are supervising it right now.")
        return 2

    app = RestApp(args.ha_url, args.token, verbose=args.verbose)
    config = BatteryOptimizerConfig(
        device_id=args.device_id,
        control_mode="commissioning",
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
        return 0

    print(f"--- {args.operation} ---")
    if args.operation == "probe":
        result = session.probe_priority_mode()
    elif args.operation == "hold":
        result = session.hold(duration_minutes=args.duration_minutes)
    elif args.operation == "renew":
        result = session.renew(duration_minutes=args.duration_minutes)
    else:
        result = session.release()

    print()
    print(result.describe())
    print(f"session_state = {backend.session_state.value}   "
          f"safe_to_stop = {backend.session_state.safe_to_stop}")
    print()
    print_state(backend)

    if args.operation == "release" and args.wait_for_release:
        wait_for_release(app, backend, args.wait_for_release)
        print()
        print_state(backend)

    if not backend.session_state.safe_to_stop:
        print("\n*** THIS PROCESS HOLDS INVERTER AUTHORITY. Run "
              "--operation release before walking away. ***")

    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
