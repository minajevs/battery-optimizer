#!/usr/bin/env python3
"""Watch the Modbus gateway's link and HA's polling at the same time, passively.

Three explanations survive for the stalls where HA stops producing Growatt
updates for ~10 minutes and then resumes on its own (2026-09-07):

  1. the dongle leaves the network (reboot, WiFi drop) -- ICMP dies with it;
  2. the dongle's IP stack is alive but its Modbus server stops answering --
     ICMP keeps replying straight through the stall;
  3. something between them (the router, one hop away) drops the flow --
     ICMP dies only if the same path is affected, which is a different shape
     again from (1) because it recovers on our next packet, not on a reboot.

ICMP separates those, and it is the ONLY probe this script sends to the
gateway. It never opens a TCP connection and never speaks Modbus: this
hardware has repeatedly been shown to wedge when a second Modbus client
appears, so a diagnostic that could itself cause the fault is worthless.
Everything about HA's polling is read from HA's own REST API instead.

    uv run --no-project python scripts/link_tracer.py \
        --gateway 192.168.2.127 --ha-url http://192.168.1.130:8123 \
        --token "$(cat ~/.ha_token)" --hours 6

Two files are written next to --out (default ./link-trace):
    trace-<start>.csv     one row per second: rtt, ttl, sensor age
    events-<start>.log    stall starts/ends, with the ICMP verdict for each

The verdict that matters is printed at each stall's end: the loss and RTT
measured DURING the stall, against the same numbers from the ten minutes
before it. Read it as evidence about which of the three above is happening,
not as a diagnosis -- three earlier theories about this gateway were each
consistent with the data until the next measurement.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

RTT_RE = re.compile(r"time[=<]([0-9.]+)\s*ms")
TTL_RE = re.compile(r"ttl[=|](\d+)", re.IGNORECASE)

# HA marks a state's last_reported on every write, changed or not, so it is
# the only field that distinguishes "the value is steady" from "polling
# stopped". last_updated would call a flat PV curve a stall.
FRESH_FIELD = "last_reported"


def ping_once(host: str, timeout_ms: int = 2000):
    """One ICMP echo. Returns (rtt_ms|None, ttl|None). Never raises."""
    try:
        proc = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout_ms), host],
            capture_output=True, text=True,
            timeout=(timeout_ms / 1000.0) + 3.0)
    except (subprocess.TimeoutExpired, OSError):
        return None, None
    if proc.returncode != 0:
        return None, None
    rtt = RTT_RE.search(proc.stdout)
    ttl = TTL_RE.search(proc.stdout)
    return (float(rtt.group(1)) if rtt else None,
            int(ttl.group(1)) if ttl else None)


class SensorFreshness:
    """Age of HA's most recent write to one Growatt entity. HA only."""

    def __init__(self, url: str, token: str, entity: str):
        self.url = url.rstrip("/")
        self.token = token
        self.entity = entity
        self.last_error = None

    def last_reported(self):
        """When HA last wrote the state, as an aware datetime, or None.

        The TIMESTAMP is returned rather than an age so the caller can age it
        every second against its own clock. Returning an age would make the
        stall detector only as sharp as the HA polling interval, and would
        report a fresh sensor for up to one interval after polling died.
        """
        req = urllib.request.Request(
            f"{self.url}/api/states/{self.entity}",
            headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as handle:
                data = json.load(handle)
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.last_error = str(e)
            return None, None
        stamp = data.get(FRESH_FIELD) or data.get("last_updated")
        if not stamp:
            return None, data.get("state")
        return (datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")),
                data.get("state"))


def summarise(samples) -> str:
    """RTT/loss over a window of (rtt|None) samples, in words."""
    if not samples:
        return "no ICMP samples"
    lost = sum(1 for r in samples if r is None)
    got = [r for r in samples if r is not None]
    loss = 100.0 * lost / len(samples)
    if not got:
        return f"{len(samples)} pings, 100% LOSS — the gateway was off the network"
    return (f"{len(samples)} pings, {loss:.0f}% loss, "
            f"rtt min/med/max {min(got):.0f}/{statistics.median(got):.0f}/"
            f"{max(got):.0f} ms")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", default="192.168.2.127",
                        help="Modbus gateway IP — pinged, never connected to")
    parser.add_argument("--ha-url", default="http://192.168.1.130:8123")
    parser.add_argument("--token", required=True)
    # The coordinator's own "last update" timestamp: its STATE changes on every
    # successful poll, so it is written whenever polling happens and cannot go
    # quiet just because a measurement is steady. A power sensor cannot do this
    # job -- pv1_power sat at 0.0 all evening and produced hours of phantom
    # stalls, and HA's recorder stores only CHANGED states, so its history looks
    # identical to polling having stopped.
    parser.add_argument("--entity", default="sensor.growatt_last_update")
    parser.add_argument("--stall-seconds", type=float, default=180.0,
                        help="sensor age above which polling counts as stalled")
    parser.add_argument("--ha-interval", type=float, default=15.0)
    parser.add_argument("--hours", type=float, default=6.0)
    parser.add_argument("--out", default="link-trace")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    started = datetime.datetime.now()
    tag = started.strftime("%Y%m%d-%H%M%S")
    csv_path = os.path.join(args.out, f"trace-{tag}.csv")
    log_path = os.path.join(args.out, f"events-{tag}.log")

    fresh = SensorFreshness(args.ha_url, args.token, args.entity)

    # One second of ICMP per row; the HA read is slower and is carried
    # forward between polls so every row has both columns.
    state = {"seen": None, "value": None}
    stop = threading.Event()

    def poll_ha():
        while not stop.is_set():
            try:
                seen, value = fresh.last_reported()
                state["seen"], state["value"] = seen, value
            except Exception as e:  # noqa: BLE001
                # A dead poller freezes `seen`, and a frozen `seen` reports a
                # stall that never ends -- the exact false positive this trace
                # exists to avoid. Keep the thread alive and say so.
                print(f"  HA poll error ({type(e).__name__}: {e}); "
                      f"keeping last reading", flush=True)
            stop.wait(args.ha_interval)

    thread = threading.Thread(target=poll_ha, daemon=True)
    thread.start()

    def emit(line: str):
        stamped = f"{datetime.datetime.now():%H:%M:%S}  {line}"
        print(stamped, flush=True)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(stamped + "\n")

    emit(f"tracing {args.gateway} (ICMP only) against {args.entity}")
    emit(f"a stall is {args.stall_seconds:.0f}s without HA writing the state")
    emit(f"csv {csv_path}")

    deadline = time.time() + args.hours * 3600
    rtts = []              # rolling ICMP history, one per second
    stalled = False
    stall_started_at = None
    stall_rtts = []
    baseline_before = []

    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "rtt_ms", "ttl", "sensor_age_s",
                         "sensor_state", "stalled"])
        try:
            while time.time() < deadline:
                tick = time.time()
                rtt, ttl = ping_once(args.gateway)
                rtts.append(rtt)
                if len(rtts) > 600:          # keep ten minutes
                    rtts.pop(0)

                seen = state["seen"]
                age = None if seen is None else max(
                    0.0, (datetime.datetime.now(datetime.timezone.utc)
                          - seen).total_seconds())
                now_stalled = age is not None and age > args.stall_seconds
                if now_stalled:
                    stall_rtts.append(rtt)

                if now_stalled and not stalled:
                    stall_started_at = tick
                    baseline_before = list(rtts[:-1])
                    stall_rtts = [rtt]
                    emit(f"STALL BEGINS — HA has not written {args.entity} "
                         f"for {age:.0f}s")
                    emit(f"    ten minutes before: {summarise(baseline_before)}")
                elif stalled and not now_stalled:
                    length = (tick - stall_started_at) / 60.0
                    emit(f"STALL ENDS — {length:.1f} min")
                    emit(f"    during the stall: {summarise(stall_rtts)}")
                    emit(f"    before it:        {summarise(baseline_before)}")
                stalled = now_stalled

                writer.writerow([
                    datetime.datetime.now().isoformat(timespec="seconds"),
                    "" if rtt is None else f"{rtt:.1f}",
                    "" if ttl is None else ttl,
                    "" if age is None else f"{age:.0f}",
                    state["value"] or "",
                    int(now_stalled)])
                handle.flush()

                slept = time.time() - tick
                if slept < 1.0:
                    time.sleep(1.0 - slept)
        except KeyboardInterrupt:
            emit("stopped by operator")
        finally:
            stop.set()

    emit(f"done. csv {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
