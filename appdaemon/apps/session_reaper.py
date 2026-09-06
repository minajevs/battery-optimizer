"""AppDaemon app: end a VPP session whose owner stopped running.

A SEPARATE app from the optimizer, deliberately. A hung callback, a crashed
app or a thread that never returns takes the optimizer's own cleanup with it,
and this hardware has no expiry that would end the session it leaves behind
(see ``control/lease.py``). Nothing inside the optimizer can be the answer to
the optimizer not running.

Its independence has a boundary worth stating plainly: this is independent of
the optimizer *app*, not of AppDaemon. If the add-on itself dies, both die,
and the inverter stays armed until AppDaemon comes back — at which point the
durable lease and startup recovery clean it up. Covering the add-on's own death
needs a watcher outside AppDaemon, and that is a prerequisite for unattended
energetic control, not for this.

All the decision logic lives in ``control/reaper.py``, which is unit-tested;
this file is the thin AppDaemon shell around it, in the same way the optimizer
orchestrator is a shell around tested libraries.

apps.yaml:

    session_reaper:
      module: session_reaper
      class: SessionReaper
      device_id: "<growatt device id>"
      session_lease_path: /config/battery_optimizer/session_lease.json
      heartbeat_path: /config/battery_optimizer/optimizer_heartbeat.json
      heartbeat_stale_seconds: 90
      check_interval_seconds: 60
"""
import datetime
import threading
import time

import appdaemon.plugins.hass.hassapi as hass

from battery_optimizer_lib.config import BatteryOptimizerConfig
from battery_optimizer_lib.control import (
    CommissioningSession,
    Heartbeat,
    SessionLease,
    SessionReaper as ReaperCore,
    UpstreamVppBackend,
    build_executor,
)

STATUS_SENSOR = "sensor.battery_session_reaper"


class SessionReaper(hass.Hass):

    def initialize(self):
        self.device_id = self.args.get("device_id", "")
        if not self.device_id:
            self.log("no device_id configured — the reaper will not run. "
                     "Nothing will end a session whose owner stops running.",
                     level="ERROR")
            return

        lease_path = self.args.get("session_lease_path", "")
        heartbeat_path = self.args.get("heartbeat_path", "")
        if not lease_path or not heartbeat_path:
            self.log("session_lease_path and heartbeat_path are both required "
                     "— without them there is no evidence to act on, and the "
                     "reaper will not run.", level="ERROR")
            return

        # "commissioning" is the executor that can write, restricted to the
        # session register allowlist. The reaper needs 30100 and 30407 and
        # nothing else; grid charge, the discharges and max export are
        # unreachable from this executor whatever it is asked for.
        config = BatteryOptimizerConfig(
            device_id=self.device_id,
            control_mode="commissioning",
            session_lease_path=lease_path,
            soc_sensor=self.args.get("soc_sensor",
                                     "sensor.growatt_battery_battery_soc"),
            battery_power_sensor=self.args.get(
                "battery_power_sensor", "sensor.growatt_battery_battery_power"),
            battery_power_direction=self.args.get("battery_power_direction",
                                                  "positive_is_charging"),
            grid_import_power_sensor=self.args.get(
                "grid_import_power_sensor",
                "sensor.growatt_grid_grid_import_power"),
            grid_export_power_sensor=self.args.get(
                "grid_export_power_sensor",
                "sensor.growatt_grid_grid_export_power"),
        )

        backend = UpstreamVppBackend(
            self, config, executor=build_executor(self, config),
            lease=SessionLease(lease_path, log_func=self.log))
        session = CommissioningSession(backend, log_func=self.log)

        self.core = ReaperCore(
            backend, session,
            Heartbeat(heartbeat_path, log_func=self.log),
            device_id=self.device_id,
            stale_after_seconds=float(
                self.args.get("heartbeat_stale_seconds", 90)),
            log_func=self.log,
            clock=time.time,
        )

        interval = int(self.args.get("check_interval_seconds", 60))
        self.run_every(self._check,
                       self.datetime() + datetime.timedelta(seconds=15),
                       interval)
        self.log(f"session reaper armed: checking every {interval}s, "
                 f"heartbeat stale after {self.core.stale_after_seconds:.0f}s. "
                 f"It can RELEASE a stranded session and nothing else.")

    def _check(self, kwargs=None):
        """One cycle, off the callback thread.

        A recovery outlives many AppDaemon callback budgets: it waits out the
        30 s cooldown on 30100 and then the settle before 30407=0 reads back.
        Doing that on the callback thread would stall every other app's
        callbacks, which is the failure mode this project already documents
        for synchronous inverter writes. So the decision is cheap and
        inline-able, and the recovery runs on a thread of its own; the timers
        that finish the release are AppDaemon's own and fire normally.
        """
        if self.core.reaping:
            return
        thread = threading.Thread(target=self._cycle, name="session-reaper",
                                  daemon=True)
        thread.start()

    def _cycle(self):
        try:
            self.core.run_once(wait=time.sleep)
        finally:
            self._publish()

    def _publish(self):
        status = self.core.status()
        verdict = status["verdict"]
        try:
            self.set_state(STATUS_SENSOR, state=verdict, attributes=status)
        except Exception as e:  # noqa: BLE001 - publishing must not break it
            self.log(f"could not publish {STATUS_SENSOR}: {e}", level="WARNING")
