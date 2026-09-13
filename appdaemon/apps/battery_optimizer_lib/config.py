"""
Configuration dataclass for BatteryOptimizer.

Centralizes all configuration with type hints, defaults, and validation.
"""

from dataclasses import dataclass, field
from typing import Optional


# Declared battery-power polarities. There is no "auto": inferring polarity
# from a live reading needs a known charge/discharge event to calibrate
# against, and getting it wrong silently inverts every trading verdict. The
# reference WIT publishes positive_is_charging, because the raw register is already
# canonical (31200/31201 read -456.7 W while SOC was falling) and the integration's
# "Invert Battery Power" option is now OFF, as it should always have been.
#
# It was negative_is_charging until 2026-09-06, and that was right only by accident:
# the integration inverted a sign that needed no inverting, and this setting inverted
# it back. Two wrongs producing a right-looking number, one toggle away from silently
# inverting every trading verdict. Both halves were corrected together.
BATTERY_POWER_DIRECTIONS = ("negative_is_charging", "positive_is_charging")

# How the control backend may talk to the inverter. Ordered by capability.
#   dry_run       - plan and log register sequences, no I/O at all
#   read_only     - real register/entity READS; writes remain impossible
#   commissioning - supervised writes, restricted to the commissioning
#                   allowlist. The optimizer still does NOT drive the inverter
#                   in this mode; only deliberately invoked operations write.
#   live          - the OPTIMIZER itself may write, unattended. The only mode
#                   where automatic_writes_allowed is true, and the one every
#                   other mechanism here (lease, fenced recovery, reaper,
#                   command TTL renewal, effect verification) exists to make
#                   safe to switch on.
# Anything unrecognised falls back to dry_run: a typo must never be what
# grants write access to an inverter. This list and build_executor()'s are
# deliberately SEPARATE gates -- a mode has to be named in both before it can
# write, so neither one alone can grant authority by omission.
CONTROL_MODES = ("dry_run", "read_only", "commissioning", "live")


# Emitted at startup (config load) and by the DP whenever the deployed
# configuration pins the end-of-horizon value to zero. Kept as one constant so
# the config warning, the DP warning and the tests all assert the same text.
TERMINAL_VALUE_ZERO_NOTICE = (
    "terminal_energy_value_eur_kwh=0 is no-salvage mode: energy still in the "
    "battery at the end of the price horizon is valued at zero, so the last "
    "slots spend it (EXPORT/DISCHARGE until depleted). This is harmless as long "
    "as the daily re-optimization extends the horizon before those slots "
    "execute. The alternative, \"auto\", derives a salvage value from the median "
    "forecast import price and instead risks stranding charge at the horizon "
    "edge and skipping evening slots priced below that median. Neither is "
    "universally correct — pick per installation."
)


@dataclass
class BatteryOptimizerConfig:
    """
    Configuration for the Battery Optimizer AppDaemon app.

    All fields have sensible defaults matching the original apps.yaml defaults.
    Use `from_args()` to load from AppDaemon's args dict.
    """

    # =========================================================================
    # Nord Pool Configuration
    # =========================================================================
    nordpool_config_entry: str = ""  # For built-in HA integration (from diagnostics)
    nordpool_area: str = "LV"
    nordpool_sensor: str = "sensor.nord_pool_lv_current_price"  # For HACS component
    tomorrow_prices_hour: int = 14  # Hour when tomorrow's prices become available (local time)

    # =========================================================================
    # Home Assistant Connection
    # =========================================================================
    ha_url: str = ""
    ha_token: str = ""

    # =========================================================================
    # Sensor Entities
    # =========================================================================
    soc_sensor: str = "sensor.growatt_battery_soc"
    pv_power_sensor: str = "sensor.growatt_pv_power"
    battery_temp_sensor: str = ""
    battery_charge_sensor: str = "sensor.growatt_battery_charge_today"
    battery_discharge_sensor: str = "sensor.growatt_battery_discharge_today"
    # INSTANTANEOUS power, for EFFECT verification. The *_today sensors above
    # are kWh energy counters and cannot resolve a 60-90 s window.
    #
    # Battery polarity is DECLARED, never assumed. The reference WIT reports
    # negative while charging (confirmed against SOC movement over a full
    # charge), but the convention varies by model and by integration option,
    # so it is configuration. The backend normalizes it exactly once and
    # everything above the backend sees positive = charging.
    battery_power_sensor: str = "sensor.growatt_battery_battery_power"
    battery_power_direction: str = "positive_is_charging"
    # Grid flow for EFFECT uses the two ALWAYS-POSITIVE directional sensors,
    # never the signed one. Upstream applies its `invert_grid_power` option to
    # signed Grid Power but explicitly NOT to these two, so no integration
    # setting can turn an import into an export in trading verification.
    grid_import_power_sensor: str = "sensor.growatt_grid_grid_import_power"
    grid_export_power_sensor: str = "sensor.growatt_grid_grid_export_power"
    # Signed grid power. DIAGNOSTIC ONLY — never an EFFECT input.
    grid_power_sensor: str = "sensor.growatt_grid_grid_power"
    # Minimum |W| that counts as the inverter genuinely acting on a command.
    effect_threshold_w: float = 200.0
    # Consecutive unambiguous EFFECT failures before control latches degraded:
    # released to local inverter logic and no longer commanded until cleared.
    effect_failure_limit: int = 2
    use_inverter_energy_sensors: bool = True
    load_power_sensor: str = ""

    # =========================================================================
    # Device Control
    # =========================================================================
    device_id: str = ""  # Empty = dry-run mode (no inverter control)

    # =========================================================================
    # Direct Control Settings
    # =========================================================================
    direct_control_buffer_minutes: int = 5
    # Buffer added to slot_minutes for override duration.
    # slot=15 + buffer=5 = 20 min override. If optimizer misses a refresh,
    # inverter reverts to safe base mode after 20 min.

    default_power_percent: int = 100
    # Default charge/discharge power when not specified per-slot.

    # --- Verify-after-set timing -------------------------------------------
    # The integration recomputes its Inverter Mode sensor on each coordinator
    # poll (~30-60s), so the sensor LAGS a write. At a fixed delay a lagging
    # sensor is indistinguishable from a lost command, which is why both the
    # first check and the post-resend re-check are configurable.
    verify_delay_seconds: int = 90       # first check after a mode was sent
    verify_recheck_seconds: int = 60     # single re-check after a resend
    # Per-call websocket timeout for set_wit_mode. This call is SYNCHRONOUS on
    # the AppDaemon callback thread: every second here blocks every other
    # callback of this app. Keep it just above the handler's normal duration.
    command_timeout_seconds: int = 15
    # Per-register write cooldown enforced by the integration (30 s on the VPP
    # control registers). A collision defers a command; it does not fail it.
    wit_cooldown_seconds: int = 30
    # Seconds between revoking control authority and disarming remote control
    # on release. SCHEDULED, never slept on.
    release_settle_seconds: int = 35
    # Where the durable session lease is written. It is the ONLY thing that can
    # tell a restarted process that a session left armed on the inverter is its
    # own to clean up -- no expiry ends a SESSION on this hardware (30408
    # bounds the energetic command only, see
    # control/lease.py). Empty disables persistence, which means a crash mid-
    # session strands the inverter until someone notices by hand.
    session_lease_path: str = ""
    # Where this app stamps proof that it is still running. The reaper
    # (appdaemon/apps/session_reaper.py) watches it go stale; a session whose
    # owner has stopped running is one nothing else will ever end. Empty
    # disables the stamp, which disables the reaper with it.
    heartbeat_path: str = ""
    heartbeat_seconds: int = 30
    # How old the PREVIOUS instance's heartbeat must be before startup
    # recovery will release the session it left armed. Should match the
    # reaper's heartbeat_stale_seconds: the two are answering the same
    # question about the same file, and a startup that is more eager than the
    # reaper would release sessions the reaper still considers owned.
    heartbeat_stale_seconds: float = 90.0
    # How long a single armed command is good for — 30408, in minutes, which
    # this firmware validates as 1..10. This is the ENERGETIC TTL: matched runs
    # on 2026-09-08 showed the battery effect collapsing at 60 s for 1 min and
    # 122 s for 2 min, with 30100/30407/30409 unchanged. A slot longer than
    # this is covered by re-arming (control/renewal.py), never by asking for a
    # longer duration, and expiry is a hazard rather than a fallback: the
    # command stops while the session stays armed and the house draws grid.
    command_ttl_minutes: int = 5
    # Re-arm this far through the TTL. 0.5 leaves the whole second half for
    # retries, so a renewal that misses once can still land before the effect
    # stops.
    command_renew_fraction: float = 0.5
    # "auto" = use register 30476 only if a supervised probe confirmed it is
    # genuinely writable; "never" = never write it.
    priority_mode_write: str = "auto"
    # How the control backend is allowed to talk to the inverter:
    #   "dry_run"   - plan and log only, no I/O whatsoever (default)
    #   "read_only" - real register/entity READS, writes still impossible
    # Live writes are a later, explicitly opted-in slice.
    control_mode: str = "dry_run"

    # =========================================================================
    # Battery Parameters
    # =========================================================================
    battery_capacity: float = 14.3  # kWh
    charge_rate: float = 4.5  # kW
    discharge_rate: float = 4.5  # kW (from_args defaults to charge_rate if not specified)
    export_discharge_rate: float = 0.0  # kW — discharge rate during grid export (0 = use discharge_rate)
    efficiency: float = 0.85
    base_consumption: float = 500.0  # W (fallback when no load profile)

    # =========================================================================
    # Scheduling Resolution
    # =========================================================================
    slot_minutes: int = 15
    adaptive_recalc_minutes: int = 15
    load_observation_minutes: int = 15
    soc_step_percent: float = 0.25  # DP resolution for SOC (must be < load/slot in kWh)

    # =========================================================================
    # Load Profile
    # =========================================================================
    load_quantile: float = 0.75
    load_profile_entity: str = "input_text.battery_load_profile"
    load_profile_max_samples: int = 60
    load_profile_min_samples: int = 6
    load_zero_floor_w: float = 450.0
    load_profile_file: str = "/config/load_profile.json"
    prediction_tracker_file: str = "/config/prediction_tracker.json"
    load_profile_last_obs_entity: str = "sensor.load_profile_last_observation"
    load_profile_count_entity: str = "sensor.load_profile_observation_count"

    # =========================================================================
    # PV Profile
    # =========================================================================
    pv_profile_file: str = "/config/pv_profile.json"
    pv_profile_max_samples: int = 60
    pv_profile_min_samples: int = 6
    pv_quantile: float = 0.5
    pv_forecast_sensor: str = ""  # Optional external PV forecast sensor (e.g., Solcast)
    pv_forecast_unit: str = "W"  # Unit of pv_forecast_sensor: "W" or "kW"
    pv_reactive_threshold: float = 0.5  # Recalc if actual PV < this fraction of forecast
    pv_reactive_min_forecast_w: float = 200.0  # Only check PV shortfall when forecast > this (W)
    # A shortfall is measured on COMPLETED slots from the mean of many samples
    # (i.e. slot energy), never from a single instantaneous reading.
    pv_reactive_consecutive_slots: int = 2  # Consecutive shortfall slots before a full recalc
    pv_reactive_min_samples: int = 3  # Min samples in a slot before its mean is trusted
    pv_sample_seconds: int = 60  # PV power sampling interval (s)
    inverter_mode_sensor: str = ""  # OPTIONAL monitoring only.
    # The upstream integration has no "Inverter Mode" status sensor — that was a
    # fork-only entity — so verification NO LONGER reads it: DirectControl now
    # verifies against register read-back through the control backend. This key
    # survives solely for _get_inverter_mode() reporting, and is inert when empty.

    # =========================================================================
    # PV Forecast Service (Solcast / Forecast.Solar)
    # =========================================================================
    solcast_today_entity: str = ""  # e.g. sensor.solcast_pv_forecast_forecast_today
    solcast_tomorrow_entity: str = ""  # e.g. sensor.solcast_pv_forecast_forecast_tomorrow
    solcast_estimate_field: str = "pv_estimate"  # pv_estimate, pv_estimate10, pv_estimate90

    forecast_solar_lat: float = 0.0
    forecast_solar_lon: float = 0.0
    forecast_solar_declination: int = 0  # panel tilt degrees
    forecast_solar_azimuth: int = 0  # 0=north, 90=east, 180=south, 270=west
    forecast_solar_kwp: float = 0.0  # peak kW (0 = disabled)
    forecast_solar_api_key: str = ""  # optional paid API key

    pv_forecast_cache_minutes: int = 60  # how often to refresh forecast
    # Retry interval after a FAILED provider fetch. Much shorter than the cache
    # TTL: the cache-age guard keys off the last SUCCESS, so a provider that is
    # down would otherwise be re-tried by every optimize / adaptive /
    # PV-shortfall pass (each a blocking HTTP call on a callback thread), while
    # a transient failure must still recover within minutes.
    pv_forecast_failure_retry_minutes: int = 10

    # =========================================================================
    # PV Forecast Bias
    # =========================================================================
    # Sliding median of measured/forecast PV over the last window, clamped and
    # applied to the CURRENT AND REMAINING horizon (not just one slot).
    pv_bias_enabled: bool = True
    pv_bias_window_minutes: int = 120
    pv_bias_min_slots: int = 2
    pv_bias_min_factor: float = 0.2
    pv_bias_max_factor: float = 1.5
    pv_bias_decay_slots: int = 8  # slots without fresh data to relax back to 1.0
    # Across a local-day boundary the bias is attenuated: today's cloud cover is
    # weather, not a calibration error of the provider's model for tomorrow.
    # Each further day keeps `weight ** days` of the deviation from 1.0, and a
    # separate (looser) clamp floors the result for those slots.
    pv_bias_next_day_weight: float = 0.5
    pv_bias_next_day_min_factor: float = 0.7

    # =========================================================================
    # Ambient Temperature / Thermal Model
    # =========================================================================
    # Ambient must be a function of TIME across the horizon. Without an external
    # source it was estimated as min(recent battery temps) — in summer that is
    # ~the current battery temperature, so the projected trajectory was flat.
    ambient_weather_entity: str = ""   # e.g. weather.forecast_home (hourly forecast)
    outdoor_temp_sensor: str = ""      # preferred if the battery is indoors
    ambient_diurnal_amplitude_c: float = 4.0   # half the peak-to-peak daily swing
    ambient_diurnal_peak_hour: float = 15.0    # local hour of the daily maximum
    ambient_forecast_cache_minutes: int = 60
    # Retry interval after a FAILED weather-forecast fetch (same semantics as
    # pv_forecast_failure_retry_minutes). Backing a failure off for the full
    # cache interval left T_ambient(t) on the diurnal fallback for an hour after
    # a restart that raced the HA weather integration.
    ambient_forecast_failure_retry_minutes: int = 10
    # Thermal model fallbacks (used until enough samples are collected)
    thermal_default_cooling_rate_per_min: float = 0.012
    thermal_default_heating_c_per_kwh: float = 0.35

    # =========================================================================
    # SOC Limits (defaults - can be overridden by HA entities at runtime)
    # =========================================================================
    default_min_soc: float = 10.0
    default_max_soc: float = 100.0
    default_pv_threshold: float = 500.0  # W
    soc_deviation_threshold: float = 10.0  # % deviation to trigger recalc
    soc_shortfall_recalc_threshold: float = 3.0  # % SOC shortfall to trigger pre-execution recalc

    # =========================================================================
    # Pricing
    # =========================================================================
    grid_fee: float = 0.052  # EUR/kWh — trading margin + distribution fee on purchases
    grid_export_fee: float = 0.02  # EUR/kWh — fixed deduction from spot price when selling
    battery_wear_cost: float = 0.0  # EUR/kWh
    export_rate_multiplier: float = 1.0  # 1.0 = no percentage reduction (deduction is fixed)
    inverter_efficiency: float = 1.0  # AC↔DC conversion efficiency (e.g., 0.97 for 97%)
    import_price_multiplier: float = 1.0  # e.g. 1.21 when spot + variable fees exclude Latvian VAT
    # None derives a terminal value from the median forecast import price. A
    # numeric value is an explicit EUR/kWh value for energy left in the battery.
    terminal_energy_value_eur_kwh: Optional[float] = None

    # =========================================================================
    # HA Entities for Dynamic Config
    # =========================================================================
    min_soc_entity: str = "input_number.battery_min_soc"
    max_soc_entity: str = "input_number.battery_max_soc"
    pv_threshold_entity: str = "input_number.battery_pv_threshold"
    battery_cost_entity: str = "input_number.battery_avg_cost"
    battery_cost_basis_version_entity: str = "input_number.battery_cost_basis_version"

    # =========================================================================
    # Control Entities
    # =========================================================================
    enabled_entity: str = "input_boolean.battery_optimizer_enabled"
    override_entity: str = "input_boolean.battery_optimizer_override"
    manual_mode_entity: str = "input_select.battery_manual_mode"

    # =========================================================================
    # Persistence
    # =========================================================================
    learning_data_file: str = ""

    # =========================================================================
    # Logging
    # =========================================================================
    decision_log_level: int = 1  # 0=minimal, 1=summary, 2=verbose
    # AppDaemon serializes an app's callbacks on its worker thread(s) and warns
    # at 10s by default ("Excessive time spent in callback"). We measure the
    # same thing locally so the log names the offending callback and can point
    # at total_threads.
    callback_warn_seconds: float = 10.0

    # =========================================================================
    # Derived Properties (computed after initialization)
    # =========================================================================
    slot_hours: float = field(init=False)

    def __post_init__(self):
        """Validate and compute derived values."""
        # Validate slot_minutes
        if self.slot_minutes <= 0 or 1440 % self.slot_minutes != 0:
            self.slot_minutes = 15

        # Validate adaptive_recalc_minutes
        if self.adaptive_recalc_minutes <= 0 or 1440 % self.adaptive_recalc_minutes != 0:
            self.adaptive_recalc_minutes = 15

        # Validate load_observation_minutes
        if self.load_observation_minutes <= 0 or 1440 % self.load_observation_minutes != 0:
            self.load_observation_minutes = 15

        # Validate soc_step_percent
        if self.soc_step_percent <= 0:
            self.soc_step_percent = 1.0

        # Clamp load_quantile to valid range
        self.load_quantile = min(1.0, max(0.0, self.load_quantile))

        # Reactive PV shortfall detection
        self.pv_reactive_consecutive_slots = max(1, int(self.pv_reactive_consecutive_slots))
        self.pv_reactive_min_samples = max(1, int(self.pv_reactive_min_samples))
        # A sample interval longer than a slot could never produce a usable mean
        self.pv_sample_seconds = max(
            10, min(self.slot_minutes * 60, int(self.pv_sample_seconds))
        )

        # PV forecast bias
        self.pv_bias_window_minutes = max(self.slot_minutes, int(self.pv_bias_window_minutes))
        self.pv_bias_min_slots = max(1, int(self.pv_bias_min_slots))
        self.pv_bias_min_factor = max(0.0, min(1.0, float(self.pv_bias_min_factor)))
        self.pv_bias_max_factor = max(
            self.pv_bias_min_factor + 0.01, float(self.pv_bias_max_factor)
        )
        self.pv_bias_decay_slots = max(1, int(self.pv_bias_decay_slots))
        self.pv_bias_next_day_weight = max(
            0.0, min(1.0, float(self.pv_bias_next_day_weight))
        )
        # Never tighter than the same-day clamp — attenuation must move the
        # factor TOWARD 1.0, never further away from it.
        self.pv_bias_next_day_min_factor = max(
            self.pv_bias_min_factor,
            min(1.0, float(self.pv_bias_next_day_min_factor)),
        )

        # Ambient / thermal model
        self.ambient_diurnal_amplitude_c = max(0.0, float(self.ambient_diurnal_amplitude_c))
        self.ambient_diurnal_peak_hour = float(self.ambient_diurnal_peak_hour) % 24.0
        self.ambient_forecast_cache_minutes = max(1, int(self.ambient_forecast_cache_minutes))
        # A retry interval above the cache TTL would be a no-op back-off.
        self.pv_forecast_cache_minutes = max(1, int(self.pv_forecast_cache_minutes))
        self.pv_forecast_failure_retry_minutes = max(
            1,
            min(
                int(self.pv_forecast_failure_retry_minutes),
                self.pv_forecast_cache_minutes,
            ),
        )
        self.ambient_forecast_failure_retry_minutes = max(
            1,
            min(
                int(self.ambient_forecast_failure_retry_minutes),
                self.ambient_forecast_cache_minutes,
            ),
        )
        self.thermal_default_cooling_rate_per_min = min(
            0.1, max(0.001, float(self.thermal_default_cooling_rate_per_min))
        )
        self.thermal_default_heating_c_per_kwh = min(
            2.0, max(0.0, float(self.thermal_default_heating_c_per_kwh))
        )

        # Inverter control timing / blocking
        self.verify_delay_seconds = max(5, min(600, int(self.verify_delay_seconds)))
        self.verify_recheck_seconds = max(5, min(600, int(self.verify_recheck_seconds)))
        if self.control_mode not in CONTROL_MODES:
            self.control_mode = "dry_run"
        # Polarity is STRICT: an unrecognised value raises rather than falling
        # back. A silently assumed polarity inverts every trading verdict, and
        # a default that happens to match one inverter is exactly how that bug
        # reaches the next one. There is deliberately no "auto" either.
        if self.battery_power_direction not in BATTERY_POWER_DIRECTIONS:
            raise ValueError(
                f"battery_power_direction must be one of "
                f"{list(BATTERY_POWER_DIRECTIONS)}, got "
                f"{self.battery_power_direction!r}. This setting has no safe "
                f"default: confirm the sign on YOUR inverter (watch the "
                f"battery power sensor while the battery is charging) and "
                f"declare it in apps.yaml."
            )
        self.effect_failure_limit = max(1, min(10, int(self.effect_failure_limit)))
        self.command_timeout_seconds = max(
            5, min(120, int(self.command_timeout_seconds))
        )
        self.callback_warn_seconds = max(1.0, min(60.0, float(self.callback_warn_seconds)))

        # Compute derived values
        self.slot_hours = self.slot_minutes / 60.0

    @property
    def effective_export_discharge_rate(self) -> float:
        """Discharge rate during grid export (kW). Falls back to discharge_rate if not set."""
        return self.export_discharge_rate if self.export_discharge_rate > 0 else self.discharge_rate

    @classmethod
    def from_args(cls, args: dict, log_func=None) -> "BatteryOptimizerConfig":
        """
        Load configuration from AppDaemon args dictionary.

        Args:
            args: The args dict from AppDaemon's apps.yaml
            log_func: Optional logging function for warnings

        Returns:
            Configured BatteryOptimizerConfig instance
        """
        def log_warn(msg):
            if log_func:
                log_func(msg, level="WARNING")

        def log_info(msg):
            # log_func is documented as optional, so every call site must be
            # guarded — not just the warnings.
            if log_func:
                log_func(msg)

        # Extract discharge_rate with fallback to charge_rate
        charge_rate = float(args.get("charge_rate_kw", 4.5))
        discharge_rate = float(args.get("discharge_rate_kw", charge_rate))
        export_discharge_rate = float(args.get("export_discharge_rate_kw", 0))

        # Extract slot_minutes with validation warning
        slot_minutes = int(args.get("slot_minutes", 15))
        if slot_minutes <= 0 or 1440 % slot_minutes != 0:
            log_warn(f"Invalid slot_minutes={slot_minutes}, falling back to 15")

        # Extract adaptive_recalc_minutes with validation warning
        adaptive_recalc_minutes = int(args.get("adaptive_recalc_minutes", 15))
        if adaptive_recalc_minutes <= 0 or 1440 % adaptive_recalc_minutes != 0:
            log_warn(f"Invalid adaptive_recalc_minutes={adaptive_recalc_minutes}, falling back to 15")

        # Extract load_observation_minutes with validation warning
        load_observation_minutes = int(args.get("load_observation_minutes", adaptive_recalc_minutes))
        if load_observation_minutes <= 0 or 1440 % load_observation_minutes != 0:
            log_warn(f"Invalid load_observation_minutes={load_observation_minutes}, falling back to 15")

        terminal_value_raw = args.get("terminal_energy_value_eur_kwh", "auto")
        if terminal_value_raw is None or str(terminal_value_raw).strip().lower() == "auto":
            terminal_energy_value = None
        else:
            terminal_energy_value = max(0.0, float(terminal_value_raw))
            if terminal_energy_value == 0.0:
                log_info(TERMINAL_VALUE_ZERO_NOTICE)

        return cls(
            # Nord Pool
            nordpool_config_entry=args.get("nordpool_config_entry", ""),
            nordpool_area=args.get("nordpool_area", "LV"),
            nordpool_sensor=args.get("nordpool_sensor", "sensor.nord_pool_lv_current_price"),
            tomorrow_prices_hour=int(args.get("tomorrow_prices_hour", 14)),

            # HA Connection
            ha_url=args.get("ha_url", ""),
            ha_token=args.get("ha_token", ""),

            # Sensors
            soc_sensor=args.get("soc_sensor", "sensor.growatt_battery_soc"),
            pv_power_sensor=args.get("pv_power_sensor", "sensor.growatt_pv_power"),
            battery_temp_sensor=args.get("battery_temp_sensor", ""),
            battery_charge_sensor=args.get("battery_charge_sensor", "sensor.growatt_battery_charge_today"),
            battery_discharge_sensor=args.get("battery_discharge_sensor", "sensor.growatt_battery_discharge_today"),
            battery_power_sensor=args.get("battery_power_sensor", "sensor.growatt_battery_battery_power"),
            battery_power_direction=args.get("battery_power_direction", "positive_is_charging"),
            grid_import_power_sensor=args.get("grid_import_power_sensor", "sensor.growatt_grid_grid_import_power"),
            grid_export_power_sensor=args.get("grid_export_power_sensor", "sensor.growatt_grid_grid_export_power"),
            grid_power_sensor=args.get("grid_power_sensor", "sensor.growatt_grid_grid_power"),
            effect_threshold_w=float(args.get("effect_threshold_w", 200.0)),
            effect_failure_limit=int(args.get("effect_failure_limit", 2)),
            wit_cooldown_seconds=int(args.get("wit_cooldown_seconds", 30)),
            release_settle_seconds=int(args.get("release_settle_seconds", 35)),
            session_lease_path=args.get("session_lease_path", ""),
            heartbeat_path=args.get("heartbeat_path", ""),
            heartbeat_seconds=int(args.get("heartbeat_seconds", 30)),
            heartbeat_stale_seconds=float(
                args.get("heartbeat_stale_seconds", 90)),
            command_ttl_minutes=int(args.get("command_ttl_minutes", 5)),
            command_renew_fraction=float(
                args.get("command_renew_fraction", 0.5)),
            priority_mode_write=args.get("priority_mode_write", "auto"),
            control_mode=args.get("control_mode", "dry_run"),
            use_inverter_energy_sensors=args.get("use_inverter_energy_sensors", True),
            load_power_sensor=args.get("load_power_sensor", ""),

            # Device Control
            device_id=args.get("device_id", ""),

            # Direct Control
            direct_control_buffer_minutes=int(args.get("direct_control_buffer_minutes", 5)),
            default_power_percent=int(args.get("default_power_percent", 100)),
            verify_delay_seconds=int(args.get("verify_delay_seconds", 90)),
            verify_recheck_seconds=int(args.get("verify_recheck_seconds", 60)),
            command_timeout_seconds=int(
                args.get("command_timeout_seconds",
                         args.get("set_wit_mode_timeout_seconds", 15))
            ),

            # Battery Parameters
            battery_capacity=float(args.get("battery_capacity_kwh", 14.3)),
            charge_rate=charge_rate,
            discharge_rate=discharge_rate,
            export_discharge_rate=export_discharge_rate,
            efficiency=float(args.get("efficiency", 0.85)),
            base_consumption=float(args.get("base_consumption_w", 500)),

            # Scheduling
            slot_minutes=slot_minutes,
            adaptive_recalc_minutes=adaptive_recalc_minutes,
            load_observation_minutes=load_observation_minutes,
            soc_step_percent=float(args.get("soc_step_percent", 1.0)),

            # Load Profile
            load_quantile=float(args.get("load_quantile", 0.75)),
            load_profile_entity=args.get("load_profile_entity", "input_text.battery_load_profile"),
            load_profile_max_samples=int(args.get("load_profile_max_samples", 60)),
            load_profile_min_samples=int(args.get("load_profile_min_samples", 6)),
            load_zero_floor_w=float(args.get("load_zero_floor_w", 450)),
            load_profile_file=args.get("load_profile_file", "/config/load_profile.json"),
            prediction_tracker_file=args.get("prediction_tracker_file", "/config/prediction_tracker.json"),
            load_profile_last_obs_entity=args.get(
                "load_profile_last_observation_entity",
                "sensor.load_profile_last_observation"
            ),
            load_profile_count_entity=args.get(
                "load_profile_observation_count_entity",
                "sensor.load_profile_observation_count"
            ),

            # PV Profile
            pv_profile_file=args.get("pv_profile_file", "/config/pv_profile.json"),
            pv_profile_max_samples=int(args.get("pv_profile_max_samples", 60)),
            pv_profile_min_samples=int(args.get("pv_profile_min_samples", 6)),
            pv_quantile=float(args.get("pv_quantile", 0.5)),
            pv_forecast_sensor=args.get("pv_forecast_sensor", ""),
            pv_forecast_unit=args.get("pv_forecast_unit", "W"),
            pv_reactive_threshold=float(args.get("pv_reactive_threshold", 0.5)),
            pv_reactive_min_forecast_w=float(args.get("pv_reactive_min_forecast_w", 200.0)),
            pv_reactive_consecutive_slots=max(
                1, int(args.get("pv_reactive_consecutive_slots", 2))
            ),
            pv_reactive_min_samples=max(1, int(args.get("pv_reactive_min_samples", 3))),
            pv_sample_seconds=int(args.get("pv_sample_seconds", 60)),
            inverter_mode_sensor=args.get("inverter_mode_sensor", ""),

            # PV Forecast Service
            solcast_today_entity=args.get("solcast_today_entity", ""),
            solcast_tomorrow_entity=args.get("solcast_tomorrow_entity", ""),
            solcast_estimate_field=args.get("solcast_estimate_field", "pv_estimate"),
            forecast_solar_lat=float(args.get("forecast_solar_lat", 0.0)),
            forecast_solar_lon=float(args.get("forecast_solar_lon", 0.0)),
            forecast_solar_declination=int(args.get("forecast_solar_declination", 0)),
            forecast_solar_azimuth=int(args.get("forecast_solar_azimuth", 0)),
            forecast_solar_kwp=float(args.get("forecast_solar_kwp", 0.0)),
            forecast_solar_api_key=args.get("forecast_solar_api_key", ""),
            pv_forecast_cache_minutes=int(args.get("pv_forecast_cache_minutes", 60)),
            pv_forecast_failure_retry_minutes=int(
                args.get("pv_forecast_failure_retry_minutes", 10)
            ),

            # PV Forecast Bias
            pv_bias_enabled=bool(args.get("pv_bias_enabled", True)),
            pv_bias_window_minutes=int(args.get("pv_bias_window_minutes", 120)),
            pv_bias_min_slots=int(args.get("pv_bias_min_slots", 2)),
            pv_bias_min_factor=float(args.get("pv_bias_min_factor", 0.2)),
            pv_bias_max_factor=float(args.get("pv_bias_max_factor", 1.5)),
            pv_bias_decay_slots=int(args.get("pv_bias_decay_slots", 8)),
            pv_bias_next_day_weight=float(args.get("pv_bias_next_day_weight", 0.5)),
            pv_bias_next_day_min_factor=float(
                args.get("pv_bias_next_day_min_factor", 0.7)
            ),

            # Ambient / thermal model
            ambient_weather_entity=args.get("ambient_weather_entity", ""),
            outdoor_temp_sensor=args.get("outdoor_temp_sensor", ""),
            ambient_diurnal_amplitude_c=float(args.get("ambient_diurnal_amplitude_c", 4.0)),
            ambient_diurnal_peak_hour=float(args.get("ambient_diurnal_peak_hour", 15.0)),
            ambient_forecast_cache_minutes=int(
                args.get("ambient_forecast_cache_minutes", 60)
            ),
            ambient_forecast_failure_retry_minutes=int(
                args.get("ambient_forecast_failure_retry_minutes", 10)
            ),
            thermal_default_cooling_rate_per_min=float(
                args.get("thermal_default_cooling_rate_per_min", 0.012)
            ),
            thermal_default_heating_c_per_kwh=float(
                args.get("thermal_default_heating_c_per_kwh", 0.35)
            ),

            # SOC Limits
            default_min_soc=float(args.get("min_soc", 10)),
            default_max_soc=float(args.get("max_soc", 100)),
            default_pv_threshold=float(args.get("pv_threshold_w", 500)),
            soc_deviation_threshold=float(args.get("soc_deviation_threshold", 10)),
            soc_shortfall_recalc_threshold=float(args.get("soc_shortfall_recalc_threshold", 3.0)),

            # Pricing
            grid_fee=float(args.get("grid_fee_eur_kwh", 0.052)),
            grid_export_fee=float(args.get("grid_export_fee_eur_kwh", 0.02)),
            battery_wear_cost=float(args.get("battery_wear_cost_eur_kwh", 0.0)),
            export_rate_multiplier=float(args.get("export_rate_multiplier", 1.0)),
            inverter_efficiency=float(args.get("inverter_efficiency", 1.0)),
            import_price_multiplier=float(args.get("import_price_multiplier", 1.0)),
            terminal_energy_value_eur_kwh=terminal_energy_value,

            # HA Entities
            min_soc_entity=args.get("min_soc_entity", "input_number.battery_min_soc"),
            max_soc_entity=args.get("max_soc_entity", "input_number.battery_max_soc"),
            pv_threshold_entity=args.get("pv_threshold_entity", "input_number.battery_pv_threshold"),
            battery_cost_entity=args.get("battery_cost_entity", "input_number.battery_avg_cost"),
            battery_cost_basis_version_entity=args.get(
                "battery_cost_basis_version_entity",
                "input_number.battery_cost_basis_version",
            ),

            # Control Entities
            enabled_entity=args.get("enabled_entity", "input_boolean.battery_optimizer_enabled"),
            override_entity=args.get("override_entity", "input_boolean.battery_optimizer_override"),
            manual_mode_entity=args.get("manual_mode_entity", "input_select.battery_manual_mode"),

            # Persistence
            learning_data_file=args.get("learning_data_file", ""),

            # Logging
            decision_log_level=int(args.get("decision_log_level", 1)),
            callback_warn_seconds=float(args.get("callback_warn_seconds", 10.0)),
        )

    def log_summary(self, log_func, warn_func=None) -> None:
        """Log a summary of the configuration.

        Args:
            log_func: INFO-level logger.
            warn_func: Optional WARNING-level logger. Falls back to log_func so
                existing callers keep working; the degenerate terminal-value
                notice is then prefixed with "WARNING:" instead.
        """
        def warn(msg):
            if warn_func is not None:
                warn_func(msg)
            else:
                log_func(f"WARNING: {msg}")

        log_func(
            f"Nord Pool config: config_entry='{self.nordpool_config_entry}', "
            f"area='{self.nordpool_area}', sensor='{self.nordpool_sensor}'"
        )
        log_func(
            f"HA connection: ha_url='{self.ha_url or 'NOT SET'}', "
            f"ha_token={'SET' if self.ha_token else 'NOT SET'}"
        )
        if self.device_id:
            log_func(f"Direct control enabled via upstream growatt_modbus VPP registers (device: {self.device_id})")
        log_func(
            f"Inverter control timing: command timeout="
            f"{self.command_timeout_seconds}s (blocks the callback thread), "
            f"verify after {self.verify_delay_seconds}s, "
            f"re-check {self.verify_recheck_seconds}s after a resend; "
            f"slow-callback warning at {self.callback_warn_seconds:.0f}s"
        )
        log_func(f"Loaded grid_fee: {self.grid_fee} EUR/kWh")
        if self.terminal_energy_value_eur_kwh is None:
            log_func(
                "Terminal energy value: auto (derived from the median forecast "
                "import price, discharge conversion and wear)"
            )
        elif self.terminal_energy_value_eur_kwh == 0.0:
            log_func(TERMINAL_VALUE_ZERO_NOTICE)
        else:
            log_func(
                f"Terminal energy value: "
                f"{self.terminal_energy_value_eur_kwh:.4f} EUR/kWh (configured)"
            )
        log_func(
            f"Config loaded: capacity={self.battery_capacity}kWh, "
            f"charge_rate={self.charge_rate}kW, discharge_rate={self.discharge_rate}kW, "
            f"export_discharge_rate={self.effective_export_discharge_rate}kW, "
            f"efficiency={self.efficiency}, slot={self.slot_minutes}min"
        )
        pv_sources = []
        if self.solcast_today_entity:
            pv_sources.append(f"Solcast({self.solcast_estimate_field})")
        if self.forecast_solar_kwp > 0:
            pv_sources.append(f"Forecast.Solar({self.forecast_solar_kwp}kWp)")
        if pv_sources:
            log_func(f"PV forecast: {' + '.join(pv_sources)}")
        log_func(
            f"PV bias: enabled={self.pv_bias_enabled}, "
            f"window={self.pv_bias_window_minutes}min, "
            f"clamp=[{self.pv_bias_min_factor}, {self.pv_bias_max_factor}], "
            f"min_slots={self.pv_bias_min_slots}, decay={self.pv_bias_decay_slots} slots, "
            f"next-day weight={self.pv_bias_next_day_weight} "
            f"floor={self.pv_bias_next_day_min_factor}; "
            f"reactive: threshold={self.pv_reactive_threshold}, "
            f"consecutive_slots={self.pv_reactive_consecutive_slots}, "
            f"min_samples={self.pv_reactive_min_samples}, "
            f"sample_every={self.pv_sample_seconds}s"
        )
        if self.ambient_weather_entity:
            ambient_source = f"weather forecast ({self.ambient_weather_entity})"
        elif self.outdoor_temp_sensor:
            ambient_source = f"outdoor sensor ({self.outdoor_temp_sensor})"
        else:
            ambient_source = "battery min-window heuristic + diurnal profile"
        log_func(
            f"Ambient: source={ambient_source}, "
            f"diurnal amplitude=+-{self.ambient_diurnal_amplitude_c}C "
            f"peaking at {self.ambient_diurnal_peak_hour:.0f}:00; "
            f"thermal defaults k1={self.thermal_default_cooling_rate_per_min}/min, "
            f"k2={self.thermal_default_heating_c_per_kwh}C/kWh"
        )
