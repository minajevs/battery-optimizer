#!/usr/bin/env python3
"""Read-only: is the battery/grid telemetry chain telling the truth?

Writes nothing, to the inverter or anywhere else. Run it before and after the
polarity change so the two snapshots can be compared directly.

**Why this exists.** On 2026-09-05 this installation had TWO sign inversions
that cancelled: the Growatt integration's `Invert Battery Power` was ON for a
WIT whose raw register is already canonical (-456.7 W read directly from
31200/31201 while SOC was falling), and battery-optimizer's
`battery_power_direction: negative_is_charging` flipped it back. The optimizer's
view of the battery was therefore correct by accident, and one toggle away from
silently inverting every trading verdict. Both must change together:

    Growatt Modbus:    Invert Battery Power   -> OFF
    battery-optimizer: battery_power_direction -> positive_is_charging

This checks the chain end to end rather than any one link, because each link
looked defensible on its own.

    uv run python scripts/polarity_check.py --ha-url http://ha:8123 \
        --token "$(cat ~/.ha_token)" --device-id <growatt device id>
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
import urllib.request

sys.path.insert(0, "appdaemon/apps")

METER_REGISTERS = {
    "power_to_load (8079/8080)": 8079,
    "power_to_user (8081/8082)": 8081,
    "power_to_grid (8083/8084)": 8083,
}
BATTERY_REGISTER = 31200


def api(ha_url, token, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{ha_url.rstrip('/')}{path}", data=data,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="POST" if data is not None else "GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode() or "{}")


def entity(ha_url, token, eid):
    try:
        d = api(ha_url, token, f"/api/states/{eid}")
        age = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.datetime.fromisoformat(
                   d["last_updated"].replace("Z", "+00:00"))).total_seconds()
        return d["state"], int(age)
    except Exception as e:  # noqa: BLE001 - a missing entity is a finding
        return f"ERR {e}", -1


def read_registers(ha_url, token, device_id, start, count, attempts=3):
    """Read, retrying: a single miss is bus contention, not a missing register.

    The coordinator polls the same bus, so an on-demand read can lose a race
    with it. Reporting that as UNREADABLE once cost a snapshot that looked like
    a dead inverter -- and this snapshot is what a polarity change is judged on.
    """
    for attempt in range(attempts):
        try:
            d = api(ha_url, token,
                    "/api/services/growatt_modbus/get_register_data?return_response",
                    {"device_id": device_id, "register_type": "input",
                     "start_address": start, "count": count})
            response = d.get("service_response", d)
            values = response.get("values") or []
            if values:
                return values
        except Exception:
            pass
        if attempt + 1 < attempts:
            time.sleep(2)
    return []


def signed32(values, index):
    combined = (values[index] << 16) | values[index + 1]
    return combined - 2 ** 32 if combined >= 2 ** 31 else combined


def number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ha-url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--device-id", required=True)
    args = parser.parse_args()

    print("=== raw registers (what the inverter itself reports) ===")
    battery_raw = None
    values = read_registers(args.ha_url, args.token, args.device_id,
                            BATTERY_REGISTER, 2)
    if len(values) >= 2:
        battery_raw = signed32(values, 0) * 0.1
        print(f"  battery_power (31200/31201) = {battery_raw:>10.1f} W")
    else:
        print("  battery_power (31200/31201) = UNREADABLE")

    for label, start in METER_REGISTERS.items():
        values = read_registers(args.ha_url, args.token, args.device_id, start, 2)
        if len(values) >= 2:
            print(f"  {label:26} = {signed32(values, 0) * 0.1:>10.1f} W")
        else:
            print(f"  {label:26} = UNREADABLE")

    print("\n=== HA entities (what the integration publishes) ===")
    published = {}
    for eid in ("sensor.growatt_battery_battery_power",
                "sensor.growatt_battery_battery_charge_power",
                "sensor.growatt_battery_battery_discharge_power",
                "sensor.growatt_grid_grid_import_power",
                "sensor.growatt_grid_grid_export_power",
                "sensor.growatt_battery_battery_soc"):
        value, age = entity(args.ha_url, args.token, eid)
        published[eid.rsplit(".", 1)[-1]] = number(value)
        print(f"  {eid:52} {value:>10}  ({age}s old)")

    print("\n=== expectations ===")
    checks = []
    battery = published.get("growatt_battery_battery_power")
    charge = published.get("growatt_battery_battery_charge_power")
    discharge = published.get("growatt_battery_battery_discharge_power")
    imported = published.get("growatt_grid_grid_import_power")
    exported = published.get("growatt_grid_grid_export_power")

    if battery_raw is not None and battery is not None:
        same_sign = (battery_raw >= 0) == (battery >= 0)
        checks.append((
            same_sign,
            f"HA battery_power agrees in SIGN with the raw register "
            f"(raw {battery_raw:+.1f}, published {battery:+.1f})",
            "the integration is inverting a register that is already canonical "
            "— Invert Battery Power should be OFF"))

    # Keyed off the RAW register, never off the published value. Comparing the
    # labels to the number they were derived from only proves the integration
    # is self-consistent, which it is even when it is inverting: the first
    # version of this check passed while the labels were demonstrably wrong.
    if battery_raw is not None and charge is not None and discharge is not None:
        if battery_raw < 0:
            ok = discharge > 0 and charge == 0
            checks.append((
                ok,
                f"raw register is negative ({battery_raw:+.1f} = DISCHARGING), "
                f"so discharge_power > 0 and charge_power = 0",
                f"the labels say the opposite (charge={charge}, "
                f"discharge={discharge})"))
        elif battery_raw > 0:
            ok = charge > 0 and discharge == 0
            checks.append((
                ok,
                f"raw register is positive ({battery_raw:+.1f} = CHARGING), "
                f"so charge_power > 0 and discharge_power = 0",
                f"the labels say the opposite (charge={charge}, "
                f"discharge={discharge})"))

    if imported is not None and battery is not None:
        fabricated = abs(abs(imported) - abs(battery)) < 0.05 and abs(battery) > 1
        checks.append((
            not fabricated,
            "grid import is not a mirror of battery power",
            f"grid_import ({imported}) equals battery power ({battery}) to the "
            f"decimal — the energy-balance fallback is fabricating flow from a "
            f"meter that read a valid zero"))

    if imported is not None and exported is not None:
        checks.append((
            not (imported > 1 and exported > 1),
            "grid is not importing and exporting at once",
            f"import={imported} and export={exported} simultaneously"))

    for ok, expectation, failure in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {expectation}")
        if not ok:
            print(f"         -> {failure}")

    failures = sum(1 for ok, _e, _f in checks if not ok)
    print(f"\n{len(checks) - failures}/{len(checks)} expectations met")
    if battery_raw is not None:
        print("\nGround truth is SOC over time: a rising SOC is charging and a "
              "falling one is discharging, whatever any label says. That is how "
              "the raw register's convention was established, and it is the "
              "only check here that cannot itself be inverted.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
