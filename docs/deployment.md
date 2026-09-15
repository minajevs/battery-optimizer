# Deployment & operations runbook — reference installation

Everything a future session needs to deploy, verify and operate the battery
optimizer on the one real installation it runs on. Written 2026-09-15 from the
live system, not from memory; re-verify anything load-bearing before acting on
it (section 9 says how).

> **Secrets never appear in this repo or in a transcript.** Commands below read
> them from files with `$(cat …)`. Do not `cat` the token or password files to
> look at them.

---

## 0. Before you do anything — current state (2026-09-15)

| Layer | State |
|---|---|
| Optimizer (`battery_optimizer`) | Deployed under AppDaemon, **`control_mode: read_only`** — real reads, writes structurally impossible |
| Session reaper (`session_reaper`) | Deployed, `idle_nothing_armed`. **Can write — release only** |
| HA watchdog | Installed, proven (Stage 5, 2026-09-13) |
| Deployed code | As of commit **`0a0cfef`** (Stage 4). Everything after it is **undeployed** |
| Repo HEAD `375b6ee` | Adds command-TTL renewal, `live` mode, HOLD-only gate — library only |
| Working tree | **Uncommitted Stage 7 orchestrator wiring from another session** (`install_renewal`, `_set_lifecycle`, `tests/test_renewal_shell.py`, a CLAUDE.md hunk). 1310 tests pass. Not reviewed, not committed, not deployed — **do not `git add -A` over it** |

**Not safe to go `live` yet.** Blockers, in order:

1. **There is no working kill switch.** `_is_enabled()` returns `True` when
   `input_boolean.battery_optimizer_enabled` does not exist — and it does not,
   because `homeassistant/packages/battery_optimizer.yaml` (the entities
   package) is **not installed**. Same for `input_number.battery_min_soc`,
   `battery_max_soc`, `input_select.battery_manual_mode`. Install that package
   and confirm the switch actually stops the optimizer before any write-capable
   mode.
2. Stage 7 wiring must be finished, committed, and deployed in `read_only` with
   `live_test_hold_only: true` and `command_ttl_minutes: 2`, proving timer
   scheduling/cancellation with zero writes.
3. Battery parameters in `apps.yaml` (`battery_capacity_kwh`, rates, SOC
   limits) are placeholders marked `TODO` — confirm against the real pack.
4. Then the supervised sequence: 7A (live, HOLD-only) → 7B (small energetic
   renewal) → Stage 8 (crash during an owned session). See CLAUDE.md.

---

## 1. Topology & identities

| Thing | Value | Notes |
|---|---|---|
| Home Assistant | `http://192.168.1.130:8123` | HA OS with Supervisor. **Not** `192.168.33.167` (upstream fork's host, does not exist here) |
| Growatt WIT gateway | `192.168.2.127:502` (RS485→TCP) | **Never open a TCP connection to it.** A second client wedges it for ~10 min |
| Growatt integration | `growatt_modbus` via HACS, **2.0.3** (2026-09-15) | Our PRs #421/#422 are merged upstream. Do not `deploy.py --target integration` — see `scripts/README.md` |
| Integration `scan_interval` | **60 s — hardware constraint, not tuning** | At 200 s the gateway drops the idle session and wedges (see §8) |
| Inverter device id | `c309d26919de729fc20af4734070a56f` | The **parent** "Growatt" device. `device_id()` on a child entity (e.g. `sensor.growatt_solar_pv1_power`) returns `b652fe65…` — **wrong one** |
| Nord Pool | built-in integration, config entry `01KERKE7V76XGPXAP6XEA44ECZ`, area `LV` | Get it with `{{ config_entry_id("sensor.nord_pool_lv_current_price") }}` |
| AppDaemon add-on | slug `a0d7b954_appdaemon`, AppDaemon **4.5.13**, Python 3.13 | |
| Dev machine | macOS, repo venv Python 3.10 via `uv` | |

## 2. Credentials and what they can do

| File (on the Mac) | Use |
|---|---|
| `~/.ha_token` (0600) | HA long-lived token. Pass as `--token "$(cat ~/.ha_token)"` |
| `~/.ha_samba_password` | Samba password, user `homeassistant` |

What the token can reach through the Supervisor proxy (measured):

| Endpoint | Result |
|---|---|
| `GET /api/hassio/core/logs` (+ header `Range: entries=:-3000:3000`) | ✅ 200 — Core log |
| `GET /api/hassio/addons/a0d7b954_appdaemon/logs` | ✅ 200 — AppDaemon log |
| `GET /api/hassio/addons`, `/core/check`, add-on start/stop/info | ❌ 401 |
| `/api/error_log` | ❌ 404 (removed in current HA) |

**So stopping/starting the AppDaemon add-on and restarting HA are done BY HAND**
in the UI (Settings → Add-ons → AppDaemon). Ask the user.

The Advanced SSH & Web Terminal add-on is installed, but key auth never worked
(`Permission denied (publickey,password)`). Don't plan around SSH; the Web
Terminal in the HA sidebar works if a command must run on the host.

## 3. Mounting the shares

They drop after sleep or a few days. Check, then remount:

```bash
mount | grep -c 192.168.1.130            # expect 2

mkdir -p ~/mnt/ha-config ~/mnt/ha-addons
P=$(cat ~/.ha_samba_password)
mount_smbfs "//homeassistant:${P}@192.168.1.130/config"        ~/mnt/ha-config
mount_smbfs "//homeassistant:${P}@192.168.1.130/addon_configs" ~/mnt/ha-addons
unset P
```

| Mac path | Is | Seen inside AppDaemon as |
|---|---|---|
| `~/mnt/ha-config/` | HA's `/config` | not visible |
| `~/mnt/ha-addons/a0d7b954_appdaemon/` | AppDaemon's own config dir | `/config` |

**HA cannot see the AppDaemon directory.** That is why the heartbeat is mirrored
into an HA sensor for the watchdog (§6).

## 4. The live files

### `~/mnt/ha-addons/a0d7b954_appdaemon/apps/apps.yaml`

**Contains the HA token. Never overwrite it from the repo, never commit it,
never print it unredacted.** `deploy.py` deliberately never copies it. Edit it
in place, back it up first, keep it `chmod 600`. To look at it:

```bash
sed -E 's/(ha_token: ").*(")/\1<redacted>\2/' \
  ~/mnt/ha-addons/a0d7b954_appdaemon/apps/apps.yaml
```

It defines two apps. Settings that matter:

| Key | Value | Why |
|---|---|---|
| `ha_url` | `http://192.168.1.130:8123` | |
| `nordpool_config_entry` / `nordpool_area` | `01KERKE7V76XGPXAP6XEA44ECZ` / `LV` | |
| `device_id` (both apps) | `c309d26919de729fc20af4734070a56f` | parent device, see §1 |
| `control_mode` | **`read_only`** | `dry_run` / `read_only` / `commissioning` / `live`. A mode must be in BOTH `config.CONTROL_MODES` and `build_executor()`; anything else → `dry_run` |
| `battery_power_sensor` / `battery_power_direction` | `sensor.growatt_battery_battery_power` / `positive_is_charging` | Integration's "Invert Battery Power" is OFF. **Change both together or neither** |
| `grid_import/export/power_sensor` | `sensor.growatt_grid_grid_*` | Verdicts use import/export, never signed grid power |
| `battery_capacity_kwh`, rates, SOC | 14.3 / 4.5 / 5.9 / 10–100 | **Placeholders (`TODO`)** |
| `session_lease_path` | `/config/battery_optimizer/session_lease.json` | Same path in both apps |
| `heartbeat_path` | `/config/battery_optimizer/optimizer_heartbeat.json` | Same path in both apps |
| `heartbeat_seconds` / `heartbeat_stale_seconds` | 30 / 90 | Reaper must use the same staleness bar |
| `session_reaper.check_interval_seconds` | 60 | |

Keys added in HEAD but **not yet set live** (defaults apply): `command_ttl_minutes`
(5), `command_renew_fraction` (0.5), `live_test_hold_only` (false).

Leftovers in `apps/`: `hello.py` (default sample, unreferenced — harmless).
In the add-on dir: `net_probe_result.json` (written 2026-09-10 17:30 by a probe
nobody has accounted for; three connect timeouts from container IP
`172.30.33.2`). Ask before deleting either.

### `~/mnt/ha-addons/a0d7b954_appdaemon/appdaemon.yaml`

- `time_zone: Europe/Riga`, Riga coordinates. It shipped as Amsterdam — wrong
  for Nord Pool LV and for the Riga 23/25-hour DST handling in the code.
- File logging to `/config/appdaemon.log` and `/config/appdaemon_error.log`.
- **No `total_threads`, no `pin_apps`.** Deliberate: see §8.
- Changes here need an **add-on restart**.

### `~/mnt/ha-config/configuration.yaml` + `packages/`

`homeassistant: packages: !include_dir_named packages` was added 2026-09-13.
`packages/battery_optimizer_watchdog.yaml` is a copy of
`homeassistant/packages/battery_optimizer_watchdog.yaml` from the repo. New
top-level keys need a **full HA restart**, not a reload.

### Runtime files (AppDaemon dir)

`battery_optimizer/optimizer_heartbeat.json` (`{"at": epoch, "pid": n}`, every
30 s, **only while lifecycle is READY**) and `battery_optimizer/session_lease.json`
(present only while a session is armed or unrecovered). Plus
`session_lease.json.lock` once a recovery has ever run — that is the `flock`
target and must persist.

### Backups

- `~/ha-config-backups/` on the Mac — hand-edited HA/AppDaemon config files, timestamped.
- `~/mnt/ha-addons/a0d7b954_appdaemon/deploy-backups/appdaemon-<ts>/` — made by every `deploy.py --confirm`.

## 5. Deploying

### Optimizer / reaper code → AppDaemon

```bash
cd battery-optimizer

# 1. tests (and see §0: don't sweep another session's uncommitted work in)
uv run --no-project python -m pytest tests/ -q

# 2. dry run — shows exactly which files differ
uv run --no-project python scripts/deploy.py --target appdaemon --dry-run \
  --addons-mount ~/mnt/ha-addons --config-mount ~/mnt/ha-config

# 3. ASK THE USER TO STOP the AppDaemon add-on, and wait for "stopped"

# 4. deploy (backs up, copies, SHA256-verifies every file)
uv run --no-project python scripts/deploy.py --target appdaemon --confirm --appdaemon-stopped \
  --addons-mount ~/mnt/ha-addons --config-mount ~/mnt/ha-config

# 5. ASK THE USER TO START it, then verify (§6)
```

- The `--addons-mount/--config-mount` flags are required: the script defaults to
  `/Volumes/ha-*`, which do not exist on this machine.
- **Never add `--prune` for the AppDaemon target**: it deletes every `.py` in the
  apps directory that the repo doesn't have (other apps, `hello.py`, probe apps).
- **`--appdaemon-stopped` is an assertion, not a formality.** AppDaemon
  hot-reloads on every `.py` change, so a multi-file copy into a running
  instance imports new modules against old peers. It was once passed while the
  add-on was running; a single-file copy happened to be safe. Don't repeat it.
- Rotating the log first makes the next startup unambiguous:
  `mv …/appdaemon.log …/appdaemon.log.prev`.

### Validate a config before it goes live

```bash
uv run --no-project python scripts/smoke_config.py ~/mnt/ha-addons/a0d7b954_appdaemon/apps/apps.yaml
# or against a staged copy you have not written to the share yet
```

Look for `control_mode=… (NO WRITES POSSIBLE)` and `SMOKE TEST PASSED`.
Editing `apps.yaml` alone hot-reloads the apps (no restart needed);
`appdaemon.yaml` needs an add-on restart.

### HA package

```bash
cp homeassistant/packages/battery_optimizer_watchdog.yaml ~/mnt/ha-config/packages/
python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" ~/mnt/ha-config/packages/battery_optimizer_watchdog.yaml
```

Then ask the user to **restart HA**. `homeassistant.check_config` over REST did
not return a usable result here; validate the YAML yourself. Expect AppDaemon
to lose its websocket for ~2 min during the restart (§8) — that is normal.

## 6. Verifying

### AppDaemon logs

```bash
A=~/mnt/ha-addons/a0d7b954_appdaemon
wc -l < $A/appdaemon_error.log                         # expect 0 new lines
grep -E "reconciled with inverter|\[startup\]|CONTROL:|session reaper armed" $A/appdaemon.log | tail
grep -c "Invalid thread ID" $A/appdaemon.log           # expect 0
grep -cE "write_register|REFUSING to arm" $A/appdaemon.log   # expect 0 in read_only
```

A healthy start looks like:

```
BATTERY OPTIMIZER CONTROL: … no inverter writes are possible (ha_read_only, live reads)
[upstream_vpp] reconciled with inverter: auth=0 remote=0 power=0 duration=5 ac=0 tou=0 priority=0
[startup] nothing to recover (session_state=not_armed)
session reaper armed: checking every 60s, heartbeat stale after 90s …
```

`reconciled with inverter: auth=… remote=…` carrying **real numbers** is the
proof the read works. `could not reconcile: inverter state unreadable` followed
by `scheduled control is held off: startup recovery is inverter_unreadable` is
the safe failure (no READY, no heartbeat) — see §8 for the causes we hit.

Also readable without the share: `GET /api/hassio/addons/a0d7b954_appdaemon/logs`.

### Sensors

```bash
T=$(cat ~/.ha_token)
for e in sensor.battery_optimizer_liveness sensor.battery_optimizer_liveness_age \
         sensor.battery_session_reaper sensor.battery_session_reaper_age \
         sensor.growatt_last_update; do
  curl -s -H "Authorization: Bearer $T" http://192.168.1.130:8123/api/states/$e \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('entity_id'), '=', str(d.get('state'))[:30], d.get('attributes',{}).get('lifecycle',''))"
done
```

Healthy: both ages near 0, liveness `lifecycle` = `ready`, reaper
`idle_nothing_armed`, `growatt_last_update` within ~70 s.

### Gateway link health

Measure liveness with `sensor.growatt_last_update` — its state is the poll
timestamp, so it changes every poll. **Never** a power sensor (see §8).

```bash
T=$(cat ~/.ha_token); START=$(python3 -c "import datetime;print((datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(hours=6)).isoformat())")
curl -s -H "Authorization: Bearer $T" \
  "http://192.168.1.130:8123/api/history/period/$START?filter_entity_id=sensor.growatt_last_update&minimal_response" \
| python3 -c "
import json,sys,datetime
rows=json.load(sys.stdin)[0]; ts=[datetime.datetime.fromisoformat(r['last_changed']) for r in rows]
g=[(b-a).total_seconds() for a,b in zip(ts,ts[1:])]
print(f'{len(rows)} polls, median {sorted(g)[len(g)//2]:.0f}s, max {max(g):.0f}s, gaps>180s: {sum(x>180 for x in g)}')"
```

Healthy: median ~68–70 s, no gaps over 180 s.

### Inverter state (safe read, through the HA service path)

```bash
uv run --no-project python scripts/commission.py --operation state \
  --ha-url http://192.168.1.130:8123 --token "$(cat ~/.ha_token)" \
  --device-id c309d26919de729fc20af4734070a56f
```

Resting baseline: `30100=0 30407=0 30409=0 30411=0 30476=0`, lease none.
(`30408` stays at the last duration written; that is expected.)

## 7. Monitoring & self-healing, as deployed

| Entity | Source | Meaning |
|---|---|---|
| `sensor.battery_optimizer_liveness` | optimizer, every 30 s **only while READY** | State = ISO timestamp (changes every beat on purpose). Attrs `lifecycle`, `pid`, `control_mode` |
| `sensor.battery_session_reaper` | reaper, every 60 s | State = verdict code (`idle_nothing_armed`, `owner_alive`, `stranded`, …) |
| `sensor.battery_optimizer_liveness_age`, `sensor.battery_session_reaper_age` | HA template | Seconds since `last_reported`; missing sensor → 99999 |
| `input_datetime.ha_last_started` | automation on HA start | Guard against acting while HA is settling |
| `automation.battery_restart_appdaemon_when_the_optimizer_stops_reporting` | watchdog | Fires when optimizer age > 300 s **for 120 s**, reaper age also > 300 s, and HA up > 600 s → `hassio.addon_restart` |

`set_state` entities are **not** restored after an HA Core restart; they
reappear when AppDaemon reconnects and republishes. `hassio.addon_restart` **does
start a stopped add-on** (proven). The automation contains no inverter logic;
AppDaemon's startup recovery handles anything left armed.

Failure layering: optimizer app wedged → reaper releases after 90 s. AppDaemon
dead → watchdog restarts it → startup recovery. HA dead → `30408` stops the
energetic command, ownership stays stranded until HA returns, then startup
recovery cleans up.

## 8. Hard-won gotchas

**Transport**
- Never connect to `192.168.2.127:502` directly (port probes, `pymodbus`, `nc`
  repeatedly). All inverter I/O goes through `growatt_modbus/get_register_data`
  / `write_register`, which share the integration's one socket and lock.
- `scan_interval` must stay 60 s. One TCP connection then stays ESTAB for days
  (14 h / 50,758 `ss` samples, zero reconnects). At 200 s it wedged ~hourly.
- `31200/31201` (battery power) are **INPUT** registers. Reading them as
  holding doesn't error — it times out three retries deep, `SharedConn` resets,
  and telemetry is gone for ~10 min. Don't probe unmapped ranges.
- HA integration reloads, option saves and HA restarts cost ~10–20 min of
  Growatt telemetry. Batch them.

**AppDaemon**
- `call_service` in 4.5.13 returns the **whole websocket envelope**; the
  handler payload is at `['result']['response']`. `_extract_values` descends
  repeatedly for this reason. **Do not** pass `return_result=True` — it lands in
  `service_data` and HA rejects the call.
- Leave threading to AppDaemon. Apps are pinned; `total_threads` can't
  parallelise a pinned app, and with `pin_apps: false` it logged
  `Invalid thread ID … assigning to thread 0` on every dispatch. Measured: zero
  slow-callback warnings, so there is nothing to fix.
- An HA Core restart disconnects AppDaemon for 120–172 s (three measurements)
  while it stays healthy. That is why the watchdog threshold is 300 s + 120 s.

**Measurement**
- HA's recorder stores state *changes*. A steady power sensor has no history
  rows and looks exactly like stopped polling; that produced a phantom stall.
  Use `sensor.growatt_last_update`, and publish liveness as a changing timestamp.
- Time sub-minute inverter events from `31200/31201` directly, never from a
  60 s coordinator-backed sensor (it misplaced a 122 s transition at ~95 s).
- `grid_import/export` can be fabricated when the meter reads a true 0 (the
  unfixed valid-zero bug): export equals battery power byte-for-byte.

**Hardware semantics** (details in CLAUDE.md and memory)
- `30408` bounds the energetic **command** (1 min → 60 s, 2 min → 122 s); it
  does **not** release `30100/30407` or restore local logic. Expiry parks the
  house on the grid. Long slots must re-arm.
- A release does not clear `30409`/`30200`; run `commission.py --operation baseline`
  after supervised tests.
- HOLD (+1 %) imports ~100–150 W; it is not neutral.

**Process**
- Probe apps are the fastest way to learn what AppDaemon really returns: drop a
  one-file app + an `apps.yaml` entry (hot-loads), read `appdaemon.log`, then
  remove both. That found the envelope shape after four restart cycles of guessing.
- Temporary debug: `logger.set_level` `{"custom_components.growatt_modbus":"debug","pymodbus":"debug"}`
  via REST. Reverts on HA restart. Set `pymodbus` back to `warning` after.

## 9. Supervised hardware tools (from the Mac)

All go through HA's REST API and the integration's service path. Writing
operations refuse without `--confirm`. Common args:
`--ha-url http://192.168.1.130:8123 --token "$(cat ~/.ha_token)" --device-id c309d26919de729fc20af4734070a56f`.

| Command | Writes? | Use |
|---|---|---|
| `commission.py --operation state` | no | Read control block, power, SOC, lease |
| `commission.py --operation baseline --confirm` | `30200`, `30409` → 0 | Clear residue after a test; refuses if anything is armed |
| `commission.py --operation duration-test --confirm --power-percent 8 --duration-minutes 2 --observe-seconds 180` | arm/release | `30408` timing discriminator, samples `31200/31201` at 5 s. Run after `baseline`; pick SOC 40–80 %, no PV, battery not already doing more than asked |
| `commission.py --operation strand --confirm --strand-i-will-recover` | arms +1 %, then `os._exit` | Deliberately strand a session to test recovery |
| `commission.py --operation recover --confirm` | release | Operator recovery (passes `operator_override=True` — asserts no live optimizer owns the session) |
| `commission.py --operation release --confirm` | release | |
| `reap.py --stale-after 10 --interval 5 --cycles 15 --confirm` | release only | Run the reaper logic from the Mac. Short staleness puts the fence inside the live 30 s cooldown |
| `link_tracer.py` | ICMP only | Passive gateway reachability + HA poll freshness |
| `ha_log_capture.py` | no | Tail the Core-log ring buffer to a local file |

The Mac-side tools use **their own** lease/heartbeat files
(`~/.battery_optimizer_commission_lease.json`, `…_heartbeat.json`), separate
from AppDaemon's.

Re-verifying this document: §3 mounts, §6 sensors + logs + `state`, and a
`deploy.py --dry-run` to see how far the deployed code is behind HEAD.
