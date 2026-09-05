#!/usr/bin/env python3
"""Read the WIT's live VPP control registers. READ-ONLY — writes nothing.

Slice 2 commissioning aid: confirms that the register block this project
depends on decodes correctly on YOUR inverter, before anything is ever
written to it.

It calls only ``growatt_modbus/get_register_data`` (one of the two upstream
services that returns a response) over the Home Assistant REST API, plus plain
entity-state reads. There is no code path here that can write a register.

Usage:
    uv run python scripts/read_inverter_state.py \
        --ha-url http://homeassistant.local:8123 \
        --token  <long-lived access token> \
        --device-id <growatt_modbus device id>

The device id is the one from apps.yaml (Settings -> Devices & Services ->
Growatt Modbus -> the device -> its URL ends with the id).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

# Mirrors battery_optimizer_lib/control/upstream_vpp.py. Kept as a literal map
# so this script stays runnable standalone, outside AppDaemon.
BLOCKS = [
    (30100, 1, ["30100 control_authority"]),
    (30200, 2, ["30200 export_limit_enable", "30201 export_limit_rate*"]),
    (30404, 8, [
        "30404 charge_cutoff_soc",
        "30405 discharge_cutoff_soc",
        "30406 (unused on WIT)",
        "30407 remote_power_enable",
        "30408 remote_duration_min",
        "30409 remote_power_pct*",
        "30410 ac_charge_enable",
        "30411 tou_num_periods",
    ]),
    (30474, 3, [
        "30474 vpp_setpoint_mirror*  (LAST COMMANDED, not applied power)",
        "30475 offgrid_discharge_soc",
        "30476 priority_mode",
    ]),
]

SIGNED = {30201, 30409, 30474}   # marked with * above

# Confirmed on the reference WIT (2026-09-02) rather than assumed:
#   * battery: SOC climbed 19->54 % while this sensor read -200..-2400 W, and
#     the only SOC decreases coincided with positive values.
#   * grid: a simultaneous read gave grid_power=-793.2 with
#     grid_export_power=+793.2 and grid_import_power=0.
# The signed grid sensor is subject to the integration's `invert_grid_power`
# option; the two directional sensors are not, which is why the optimizer
# verifies trades against those instead.
POWER_SENSORS = [
    ("battery_power", "sensor.growatt_battery_battery_power",
     "negative = CHARGING on the reference WIT (declared, see "
     "battery_power_direction)"),
    ("grid_import_power", "sensor.growatt_grid_grid_import_power",
     "always positive; > 0 means BUYING"),
    ("grid_export_power", "sensor.growatt_grid_grid_export_power",
     "always positive; > 0 means SELLING"),
    ("grid_power", "sensor.growatt_grid_grid_power",
     "signed, DIAGNOSTIC ONLY — sign depends on invert_grid_power"),
]


def decode_signed(raw: int) -> int:
    return raw - 65536 if raw > 32767 else raw


def _post(url: str, token: str, payload: dict, timeout: int):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode() or "{}")


def _get(url: str, token: str, timeout: int):
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}, method="GET"
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode() or "{}")


def read_block(base: str, token: str, device_id: str, start: int, count: int,
               timeout: int):
    url = (f"{base}/api/services/growatt_modbus/get_register_data"
           f"?return_response")
    data = _post(url, token, {
        "device_id": device_id,
        "register_type": "holding",
        "start_address": start,
        "count": count,
    }, timeout)

    payload = data.get("service_response", data)
    if isinstance(payload, dict) and payload.get("success") is False:
        return None
    values = payload.get("values") if isinstance(payload, dict) else None
    return values if isinstance(values, list) else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ha-url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--battery-power-entity",
                        default=POWER_SENSORS[0][1])
    parser.add_argument("--grid-import-power-entity",
                        default=POWER_SENSORS[1][1])
    parser.add_argument("--grid-export-power-entity",
                        default=POWER_SENSORS[2][1])
    parser.add_argument("--grid-power-entity", default=POWER_SENSORS[3][1])
    args = parser.parse_args()

    base = args.ha_url.rstrip("/")

    print("READ-ONLY inverter state check — nothing will be written.\n")

    ok = True
    for start, count, labels in BLOCKS:
        print(f"--- holding {start}..{start + count - 1} ---")
        try:
            values = read_block(base, args.token, args.device_id,
                                start, count, args.timeout)
        except urllib.error.HTTPError as e:
            print(f"    HTTP {e.code}: {e.reason}")
            ok = False
            continue
        except Exception as e:  # noqa: BLE001 - a commissioning script
            print(f"    failed: {e}")
            ok = False
            continue

        if values is None:
            print("    no values returned (register block unsupported?)")
            ok = False
            continue

        for offset, raw in enumerate(values):
            register = start + offset
            label = labels[offset] if offset < len(labels) else str(register)
            shown = raw
            if register in SIGNED:
                shown = f"{decode_signed(raw)}  (raw {raw})"
            print(f"    {label:<52} = {shown}")
        print()

    # Read live so the conventions can be re-confirmed on any installation.
    print("--- power telemetry (verify the sign conventions) ---")
    for entity, meaning in (
        (args.battery_power_entity, POWER_SENSORS[0][2]),
        (args.grid_import_power_entity, POWER_SENSORS[1][2]),
        (args.grid_export_power_entity, POWER_SENSORS[2][2]),
        (args.grid_power_entity, POWER_SENSORS[3][2]),
    ):
        try:
            state = _get(f"{base}/api/states/{entity}", args.token,
                         args.timeout)
            print(f"    {entity:<52} = {state.get('state')}   [{meaning}]")
        except Exception as e:  # noqa: BLE001
            print(f"    {entity:<52} : {e}")
            ok = False

    print()
    print("Interpretation notes:")
    print("  30100=1 with 30407=0 is the VPP STANDBY HAZARD "
          "(local battery logic suspended).")
    print("  30474 mirrors the LAST COMMANDED setpoint; it is not proof of "
          "what the inverter is doing.")
    print("  30476 is priority_mode: 0=Load First, 1=Battery First, "
          "2=Grid First.")
    print("  30411 is the inverter's OWN base TOU schedule. Nothing in this "
          "project writes it.")
    print("  30405 is the VPP discharge cutoff, NOT a proven local-mode floor "
          "(observed discharging to 18 % with 30405=20).")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
