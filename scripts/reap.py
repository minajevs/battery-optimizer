#!/usr/bin/env python3
"""Run the session reaper against real hardware, outside AppDaemon.

The reaper ships as an AppDaemon app (``appdaemon/apps/session_reaper.py``),
but its decision logic and its recovery are library code
(``control/reaper.py``), and this runs exactly that code over the same REST
shim ``commission.py`` uses. So the thing proved against the inverter here is
the thing that runs in production; the AppDaemon file is the shell around it,
untested like the rest of the orchestrator.

    uv run python scripts/reap.py --ha-url http://ha:8123 \
        --token <token> --device-id <growatt device id> --confirm

It polls, and it prints its verdict every cycle. Reaping requires ALL of:

    the heartbeat is stale        the owner is not running
    a lease exists, for THIS device
    30100 = 1 and 30407 = 1      a session really is armed
    30411 = 0                    no other scheduler appeared
    30409 == the lease setpoint  the armed session is the recorded one

None of those is sufficient alone, and two of them are refusals rather than
reasons: a TOU schedule that appeared, or a setpoint that changed, means
something touched this inverter after our session, so it is reported loudly
and left alone.

To exercise it end to end without killing AppDaemon (which would prove nothing
about the reaper), strand a session and let its heartbeat go stale:

    scripts/commission.py --operation strand --confirm --strand-i-will-recover
    scripts/reap.py --stale-after 90 --interval 15 --confirm

The first cycles will refuse while the heartbeat is fresh -- that refusal is
the most safety-critical behaviour here, and worth watching happen.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, "appdaemon/apps")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from battery_optimizer_lib.config import BatteryOptimizerConfig      # noqa: E402
from battery_optimizer_lib.control import (                          # noqa: E402
    CommissioningSession,
    Heartbeat,
    SessionLease,
    SessionReaper,
    UpstreamVppBackend,
    build_executor,
)

from commission import (                                             # noqa: E402
    DEFAULT_HEARTBEAT_PATH,
    DEFAULT_LEASE_PATH,
    RestApp,
    make_wait,
    print_state,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ha-url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--lease-path", default=DEFAULT_LEASE_PATH)
    parser.add_argument("--heartbeat-path", default=DEFAULT_HEARTBEAT_PATH)
    parser.add_argument("--stale-after", type=float, default=90.0,
                        help="seconds without a heartbeat before its owner is "
                             "presumed not to be running")
    parser.add_argument("--interval", type=float, default=60.0,
                        help="seconds between checks")
    parser.add_argument("--cycles", type=int, default=0,
                        help="stop after this many checks (0 = run until "
                             "interrupted)")
    parser.add_argument("--confirm", action="store_true",
                        help="required: this can WRITE to the inverter, though "
                             "only ever to release")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--battery-power-direction",
                        default="negative_is_charging")
    args = parser.parse_args()

    if not args.confirm:
        print("REFUSED: the reaper can write to the inverter — only ever to "
              "release a stranded session, but that is still a write. Re-run "
              "with --confirm.")
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
    backend = UpstreamVppBackend(
        app, config, executor=build_executor(app, config),
        lease=SessionLease(args.lease_path, log_func=app.log))
    session = CommissioningSession(backend, log_func=app.log)
    reaper = SessionReaper(
        backend, session, Heartbeat(args.heartbeat_path, log_func=app.log),
        device_id=args.device_id, stale_after_seconds=args.stale_after,
        log_func=app.log, clock=time.time)

    print(backend.describe_mode())
    print(f"\nlease     {args.lease_path}")
    print(f"heartbeat {args.heartbeat_path}")
    print(f"checking every {args.interval:.0f}s; the heartbeat is stale after "
          f"{args.stale_after:.0f}s\n")

    wait = make_wait(app)
    cycles = 0
    try:
        while True:
            cycles += 1
            stamp = time.strftime("%H:%M:%S")
            verdict = reaper.run_once(wait=wait)
            print(f"[{stamp}] {verdict.describe()}")

            if verdict.reap:
                print()
                print_state(backend)
                print(f"\nreap_count={reaper.reap_count} "
                      f"reap_failed={reaper.reap_failed}")

            if args.cycles and cycles >= args.cycles:
                return 0
            wait(args.interval)
    except KeyboardInterrupt:
        print("\nstopped. Nothing of this process's is outstanding unless a "
              "reap was in flight — check with commission.py --operation state")
        return 0


if __name__ == "__main__":
    sys.exit(main())
