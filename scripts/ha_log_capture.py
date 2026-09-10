#!/usr/bin/env python3
"""Keep a local copy of Home Assistant's Growatt/Modbus log lines.

Current HA no longer writes ``home-assistant.log`` and ``/api/error_log`` is
gone; the Core log lives in the Supervisor's journal, which is a RING BUFFER.
On this installation a stall lasts ~10 minutes and happens roughly hourly, so
by the time anyone looks, the interesting lines can already have rolled out --
especially with pymodbus at DEBUG, which is what makes them interesting.

So this pulls the tail of the Core log on a timer and appends whatever is new
to a local file. Read-only against HA, and it never touches the gateway.

    uv run --no-project python scripts/ha_log_capture.py \
        --token "$(cat ~/.ha_token)" --out link-trace --hours 14

Enable the detail it is meant to capture first (this does NOT survive an HA
restart, which is the intended safety valve):

    logger.set_level {"custom_components.growatt_modbus": "debug",
                      "pymodbus": "debug"}
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
import time
import urllib.error
import urllib.request

ANSI = re.compile(r"\x1b\[[0-9;]*m")
KEEP = re.compile(r"growatt|pymodbus|modbus", re.IGNORECASE)
LOG_PATH = "/api/hassio/core/logs"


def fetch(url: str, token: str, lines: int) -> list[str]:
    """The last ``lines`` journal entries, ANSI stripped. [] on any failure."""
    req = urllib.request.Request(
        url.rstrip("/") + LOG_PATH,
        headers={"Authorization": f"Bearer {token}",
                 "Range": f"entries=:-{lines}:{lines}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as handle:
            body = handle.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        print(f"  fetch failed: {e}", file=sys.stderr, flush=True)
        return []
    return [ANSI.sub("", ln) for ln in body.splitlines() if ln.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ha-url", default="http://192.168.1.130:8123")
    parser.add_argument("--token", required=True)
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--lines", type=int, default=3000,
                        help="journal tail per fetch; must comfortably exceed "
                             "what HA logs in one interval or lines are lost")
    parser.add_argument("--hours", type=float, default=14.0)
    parser.add_argument("--out", default="link-trace")
    parser.add_argument("--all", action="store_true",
                        help="keep every line, not only Modbus-related ones")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    tag = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(args.out, f"halog-{tag}.log")

    # Dedupe by remembering the last line written and resuming after it. A
    # timestamp comparison would be wrong: journal lines are not guaranteed
    # unique per second, and identical lines DO repeat.
    last_written = None
    deadline = time.time() + args.hours * 3600
    gaps = 0

    print(f"capturing {args.ha_url}{LOG_PATH} -> {path}", flush=True)
    while time.time() < deadline:
        tick = time.time()
        lines = fetch(args.ha_url, args.token, args.lines)
        if lines:
            if last_written is None:
                fresh = lines
            else:
                try:
                    index = len(lines) - 1 - lines[::-1].index(last_written)
                    fresh = lines[index + 1:]
                except ValueError:
                    # Our anchor rolled out of the buffer: everything between
                    # it and this fetch is gone. Say so in the file rather
                    # than silently stitching a discontinuity.
                    gaps += 1
                    fresh = lines
                    with open(path, "a", encoding="utf-8") as handle:
                        handle.write(
                            f"\n=== GAP: anchor line rolled out of the journal "
                            f"buffer at {datetime.datetime.now():%H:%M:%S}; "
                            f"lines were lost. Raise --lines or lower "
                            f"--interval. ===\n\n")
            if fresh:
                last_written = fresh[-1]
                kept = fresh if args.all else [l for l in fresh if KEEP.search(l)]
                if kept:
                    with open(path, "a", encoding="utf-8") as handle:
                        handle.write("\n".join(kept) + "\n")

        slept = time.time() - tick
        if slept < args.interval:
            time.sleep(args.interval - slept)

    print(f"done: {path} ({gaps} gap(s))", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
