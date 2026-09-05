#!/usr/bin/env python3
"""Decode the WIT's complete VPP TOU schedule. READ-ONLY — writes nothing.

The inverter carries a base time-of-use schedule this project never authored
and never writes. The reference unit was found holding 15 periods, then 10 a
day later, with 30410 flipping 0 -> 1 in between — so something was editing it.
This tool is how that gets watched, and how the "something" was identified:

    2026-09-03 22:13  Smart Scheduling ON   30411 = 16, 30100 = 1, 30410 = 1
    2026-09-03 22:36  (23 min later)        no register changed
    2026-09-03 22:39  Smart Scheduling OFF  30411 = 0, 30100 = 0, 30410 = 0,
                                            all 60 period registers zeroed
    2026-09-05 18:44  (44 h later)          still zero — nothing pushes it back

So the base schedule is **Growatt Smart Scheduling's**: it writes it, and
clearing it in the Growatt dashboard clears the registers. That is what makes
30411 usable as an interlock elsewhere in this project (nothing here writes a
TOU period, so a non-zero count is somebody else's) — see fact 4 in
``battery_optimizer_lib/control/upstream_vpp.py``.

    30100        control authority
    30407-30410  remote enable / duration / power / AC charge
    30411        number of active TOU periods
    30412-30471  up to 20 periods, 3 registers each:
                     +0 start  (minutes since midnight)
                     +1 end    (minutes since midnight)
                     +2 power  (signed %, + charge / - discharge)
    30476        priority mode

Times are plain minutes since midnight on WIT VPP — NOT the hex-packed
hours*256+minutes form some other Growatt families use. Any value that cannot
be minutes (> 1440) is flagged and shown under both readings, so a wrong
assumption shows up as a complaint rather than as a plausible-looking time.

Snapshots and diffs:

    --save   before.json        take a snapshot and write it out
    --save   after.json         ... and again later
    --diff   before.json after.json     compare the two, no inverter needed

There is no code path here that can write a register: the only service it
calls is growatt_modbus/get_register_data.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import urllib.error
import urllib.request

TOU_BASE = 30412
TOU_MAX_PERIODS = 20
TOU_REGISTERS_PER_PERIOD = 3
TOU_LAST = TOU_BASE + TOU_MAX_PERIODS * TOU_REGISTERS_PER_PERIOD - 1   # 30471

MINUTES_IN_DAY = 1440

# Read as one block wherever possible; get_register_data caps a read at 50.
CONTEXT_REGISTERS = {
    30100: "control_authority",
    30407: "remote_power_enable",
    30408: "remote_duration_min",
    30409: "remote_power_pct",
    30410: "ac_charge_enable",
    30411: "tou_num_periods",
    30476: "priority_mode",
}
SIGNED_REGISTERS = {30409}


def decode_signed(raw: int) -> int:
    return raw - 65536 if raw > 32767 else raw


def as_hhmm(raw: int):
    """Minutes since midnight -> "HH:MM", or None when it cannot be that.

    1440 is the legal end-of-day value for a period end and renders as 24:00.
    """
    if raw is None or raw < 0 or raw > MINUTES_IN_DAY:
        return None
    return f"{raw // 60:02d}:{raw % 60:02d}"


def as_packed_hhmm(raw: int) -> str:
    """The OTHER convention (hours*256 + minutes), shown only when needed."""
    hours, minutes = raw >> 8, raw & 0xFF
    if hours > 23 or minutes > 59:
        return "not a packed time either"
    return f"{hours:02d}:{minutes:02d} if hex-packed"


def _post(url: str, token: str, payload: dict, timeout: int):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode() or "{}")


def read_block(base: str, token: str, device_id: str, start: int, count: int,
               timeout: int):
    data = _post(
        f"{base}/api/services/growatt_modbus/get_register_data?return_response",
        token,
        {"device_id": device_id, "register_type": "holding",
         "start_address": start, "count": count},
        timeout)
    payload = data.get("service_response", data)
    if isinstance(payload, dict) and payload.get("success") is False:
        return None
    values = payload.get("values") if isinstance(payload, dict) else None
    if not isinstance(values, list) or len(values) < count:
        return None
    return [int(v) for v in values[:count]]


def snapshot(base: str, token: str, device_id: str, timeout: int) -> dict:
    """Every register this tool reports, as {register: raw}."""
    registers = {}

    for register in sorted(CONTEXT_REGISTERS):
        values = read_block(base, token, device_id, register, 1, timeout)
        registers[register] = None if values is None else values[0]

    # 60 TOU registers, in two reads to stay under the 50-register cap.
    for start in (TOU_BASE, TOU_BASE + 30):
        values = read_block(base, token, device_id, start, 30, timeout)
        for offset in range(30):
            registers[start + offset] = (
                None if values is None else values[offset])

    return {
        "taken_at": datetime.datetime.now().astimezone().isoformat(
            timespec="seconds"),
        "device_id": device_id,
        "registers": {str(k): v for k, v in registers.items()},
    }


def periods(registers: dict) -> list:
    """Decode all 20 period triplets, declared-active or not."""
    out = []
    declared = registers.get(30411)
    for index in range(TOU_MAX_PERIODS):
        base = TOU_BASE + index * TOU_REGISTERS_PER_PERIOD
        start, end, power = (registers.get(base), registers.get(base + 1),
                             registers.get(base + 2))
        out.append({
            "period": index + 1,
            "base_register": base,
            "active": declared is not None and index < declared,
            "start_raw": start,
            "end_raw": end,
            "power_raw": power,
            "start": as_hhmm(start),
            "end": as_hhmm(end),
            "power_pct": None if power is None else decode_signed(power),
        })
    return out


def describe_power(power) -> str:
    if power is None:
        return "?"
    if power > 0:
        return f"+{power}% charge"
    if power < 0:
        return f"{power}% discharge"
    return "0% (suspend forced cycle)"


def render(snap: dict) -> None:
    registers = {int(k): v for k, v in snap["registers"].items()}

    print(f"snapshot taken {snap['taken_at']}")
    print()
    print("--- control context ---")
    for register in sorted(CONTEXT_REGISTERS):
        raw = registers.get(register)
        shown = raw
        if raw is not None and register in SIGNED_REGISTERS:
            shown = f"{decode_signed(raw)}  (raw {raw})"
        print(f"    {register}  {CONTEXT_REGISTERS[register]:<22} = {shown}")

    declared = registers.get(30411)
    print()
    print(f"--- TOU schedule (30411 declares {declared} active "
          f"of {TOU_MAX_PERIODS} slots) ---")
    print(f"    {'#':>2}  {'reg':>5}  {'start':>5}  {'end':>5}  "
          f"{'power':>18}   raw (start,end,power)")

    suspect = []
    for period in periods(registers):
        if period["start_raw"] is None:
            print(f"    {period['period']:>2}  {period['base_register']:>5}  "
                  f"  unreadable")
            continue

        empty = (period["start_raw"] == 0 and period["end_raw"] == 0
                 and period["power_raw"] == 0)
        if empty and not period["active"]:
            continue

        marker = "*" if period["active"] else " "
        start = period["start"] or "??:??"
        end = period["end"] or "??:??"
        print(f"  {marker} {period['period']:>2}  {period['base_register']:>5}  "
              f"{start:>5}  {end:>5}  {describe_power(period['power_pct']):>18}"
              f"   ({period['start_raw']}, {period['end_raw']}, "
              f"{period['power_raw']})")

        for label, raw, decoded in (("start", period["start_raw"], period["start"]),
                                    ("end", period["end_raw"], period["end"])):
            if decoded is None:
                suspect.append(
                    f"period {period['period']} {label} raw={raw} is not "
                    f"minutes-since-midnight ({as_packed_hhmm(raw)})")

    print()
    print("    * = within the count declared by 30411; "
          "all-zero inactive slots are omitted")
    if suspect:
        print()
        print("    TIME DECODE WARNINGS (the assumed convention does not fit):")
        for line in suspect:
            print(f"      - {line}")


def diff(before: dict, after: dict) -> int:
    """Register-level and period-level differences between two snapshots."""
    before_registers = {int(k): v for k, v in before["registers"].items()}
    after_registers = {int(k): v for k, v in after["registers"].items()}

    print(f"before: {before['taken_at']}")
    print(f"after:  {after['taken_at']}")
    print()

    changed = [r for r in sorted(set(before_registers) | set(after_registers))
               if before_registers.get(r) != after_registers.get(r)]

    print("--- register diff ---")
    if not changed:
        print("    no register changed")
    for register in changed:
        name = CONTEXT_REGISTERS.get(register, "")
        if TOU_BASE <= register <= TOU_LAST:
            index = (register - TOU_BASE) // TOU_REGISTERS_PER_PERIOD
            field = ("start", "end", "power")[
                (register - TOU_BASE) % TOU_REGISTERS_PER_PERIOD]
            name = f"period {index + 1} {field}"
        print(f"    {register}  {name:<22} "
              f"{before_registers.get(register)} -> {after_registers.get(register)}")

    print()
    print("--- period diff ---")
    before_periods = {p["period"]: p for p in periods(before_registers)}
    after_periods = {p["period"]: p for p in periods(after_registers)}

    def summarize(period):
        if period is None or period["start_raw"] is None:
            return "unreadable"
        if (period["start_raw"] == 0 and period["end_raw"] == 0
                and period["power_raw"] == 0):
            return "empty"
        flag = "active" if period["active"] else "inactive"
        return (f"{period['start'] or '??:??'}-{period['end'] or '??:??'} "
                f"{describe_power(period['power_pct'])} [{flag}]")

    period_changes = 0
    for number in range(1, TOU_MAX_PERIODS + 1):
        was = summarize(before_periods.get(number))
        now = summarize(after_periods.get(number))
        if was == now:
            continue
        period_changes += 1
        print(f"    period {number:>2}:  {was}")
        print(f"                {now}")

    if not period_changes:
        print("    no period changed")

    return 1 if (changed or period_changes) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ha-url")
    parser.add_argument("--token")
    parser.add_argument("--device-id")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--save", metavar="PATH",
                        help="write this snapshot to a JSON file")
    parser.add_argument("--diff", nargs=2, metavar=("BEFORE", "AFTER"),
                        help="compare two saved snapshots; reads no inverter")
    args = parser.parse_args()

    if args.diff:
        with open(args.diff[0]) as handle:
            before = json.load(handle)
        with open(args.diff[1]) as handle:
            after = json.load(handle)
        return diff(before, after)

    missing = [name for name, value in (("--ha-url", args.ha_url),
                                        ("--token", args.token),
                                        ("--device-id", args.device_id))
               if not value]
    if missing:
        parser.error(f"{', '.join(missing)} required unless --diff is used")

    print("READ-ONLY TOU schedule dump — nothing will be written.\n")
    try:
        snap = snapshot(args.ha_url.rstrip("/"), args.token, args.device_id,
                        args.timeout)
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.reason}")
        return 1
    except Exception as e:  # noqa: BLE001 - a commissioning script
        print(f"failed: {e}")
        return 1

    render(snap)

    if args.save:
        with open(args.save, "w") as handle:
            json.dump(snap, handle, indent=2)
        print(f"\nsnapshot written to {args.save}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
