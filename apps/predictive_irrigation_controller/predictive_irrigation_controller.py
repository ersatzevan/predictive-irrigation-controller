"""
Predictive Irrigation Controller
========================
An AppDaemon app that implements a discrete Proportional-Integral (PI) controller
for smart garden irrigation. Replaces fixed-duration watering timers with durations
that scale intelligently based on actual soil moisture deficit, accumulated over time.

PROCESS VARIABLE (PV):  Soil moisture sensor reading (%)
SETPOINT (SP):           Target moisture level (configurable per zone)
ERROR (e):               SP - PV  (positive = too dry, negative = too wet)
OUTPUT:                  Watering duration in minutes

PI FORMULA (discrete, runs once daily):
    integral[k]  = clamp(integral[k-1] + e[k], -max_I, +max_I)
    output[k]    = Kp * e[k] + Ki * integral[k]
    duration     = clamp(output[k] * temp_factor, min_duration, max_duration)

ANTI-WINDUP:
    Integral is clamped to ±(max_duration / Ki) to prevent runaway during
    extended dry or wet periods. When moisture exceeds setpoint the integral
    decays by 50% per day rather than going negative.

FEATURES:
    ✓ Proportional-Integral control with anti-windup
    ✓ Temperature feed-forward (soil temp preferred, air temp fallback)
    ✓ Sunrise-based scheduling — finishes before sunrise daily
    ✓ Weather inhibit — skips on rain, high forecast probability
    ✓ Sequential zone execution with configurable RF gap
    ✓ Hardware timer passthrough (LinkTap dead-man switch)
    ✓ Last-known-good moisture fallback when sensor offline
    ✓ Phantom rain detection via forecast cross-check
    ✓ Emergency evening water check on hot dry skipped days
    ✓ Overnight rain watchdog with proportional integral decay
    ✓ Per-zone enable/disable via HA input_boolean
    ✓ Master enable/disable switch
    ✓ Force run button
    ✓ MQTT discovery — live dashboard sensors in HA
    ✓ Flow cutoff watchdog — detects unexpected valve termination
    ✓ Monthly structured JSONL logging for tuning analysis
    ✓ State persistence across restarts

REQUIREMENTS:
    - AppDaemon 4.x
    - Home Assistant with MQTT integration
    - Soil moisture sensors (optional — see require_moisture_sensors)
    - Valve switches (LinkTap, generic switch, or any HA switch entity)

INSTALLATION:
    1. Install AppDaemon via HACS or HA Add-on Store
    2. Install this app via HACS (AppDaemon Apps category)
    3. Copy predictive_irrigation_controller.yaml entries to your apps.yaml
    4. Restart AppDaemon

HELPERS REQUIRED IN HA:
    Create these in Settings → Helpers:
    - input_boolean.garden_pi_controller_enabled  (Toggle — master on/off)
    - input_button.pi_force_run                   (Button — manual trigger)
    - input_boolean.pi_zone_1_enabled             (Toggle — per-zone, one per zone)

MQTT DASHBOARD:
    After first run, entities appear under "Garden PI Controller" device in HA.
    Import the example dashboard from the /docs/ folder.

LICENSE: MIT
AUTHOR:  github.com/YOURUSERNAME
"""

import appdaemon.plugins.hass.hassapi as hass
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

STATE_FILE      = "/conf/pi_irrigation_state.json"
LOG_DIR         = "/conf/logs"
MQTT_TOPIC_BASE = "garden/pi/zone"
MQTT_CTRL_BASE  = "garden/pi/controller"


class PredictiveIrrigationController(hass.Hass):

    # ------------------------------------------------------------------
    # INITIALIZATION
    # ------------------------------------------------------------------
    def initialize(self):
        self.log("Predictive Irrigation Controller initializing...")

        self.zones                       = self.args.get("zones", {})
        self.notify_service              = self.args.get("notify_service", "notify/notify")
        self.temp_sensor                 = self.args.get("temp_sensor", "sensor.outdoor_temperature")
        self.soil_temp_sensor            = self.args.get("soil_temp_sensor", None)
        self.weather_entity              = self.args.get("weather_entity", "weather.forecast_home")
        self.rain_prob_sensor            = self.args.get("rain_prob_sensor", None)
        self.rain_day_sensor             = self.args.get("rain_day_sensor", None)
        self.rain_threshold              = float(self.args.get("rain_today_threshold", 0.5))
        self.require_moisture_sensors    = self.args.get("require_moisture_sensors", True)
        self.default_moisture            = float(self.args.get("default_moisture", 30))
        self.sunrise_offset              = int(self.args.get("sunrise_offset_minutes", 15))
        self.fallback_run_time           = self.args.get("fallback_run_time", "05:30:00")
        self.overlap_seconds             = int(self.args.get("overlap_seconds", 10))
        self.force_run_duration          = int(self.args.get("force_run_duration", 10))
        self.emergency_check_time        = self.args.get("emergency_check_time", "16:00:00")
        self.emergency_temp_threshold    = float(self.args.get("emergency_temp_threshold", 85.0))
        self.emergency_moisture_threshold = float(self.args.get("emergency_moisture_threshold", 40.0))
        self.emergency_duration_pct      = float(self.args.get("emergency_duration_pct", 0.5))
        self.rain_watchdog_time          = self.args.get("rain_watchdog_time", "05:00:00")
        self.rain_watchdog_threshold     = float(self.args.get("rain_watchdog_threshold", 0.5))

        # Track active valve sessions for cutoff watchdog
        self.active_valves = {}

        # MQTT discovery
        self.run_in(self.publish_mqtt_discovery, 10)

        # Midnight planning
        self.run_daily(self.plan_watering_day, "00:01:00")
        self.run_in(self.plan_watering_day, 30)

        # Rain watchdog at 5 AM
        self.run_daily(self.check_overnight_rain, self.rain_watchdog_time)

        # Emergency check at 4 PM
        self.run_daily(self.check_emergency_water, self.emergency_check_time)

        # Startup safety check
        self.run_in(self.startup_safety_check, 20)
        self.run_in(self._publish_idle_status, 15)

        # Force run listener
        self.listen_state(self.handle_force_run, "input_button.pi_force_run")

        self.log(f"Initialized. Sunrise-based scheduling for zones: {list(self.zones.keys())}")

    # ------------------------------------------------------------------
    # SUNRISE PLANNING
    # ------------------------------------------------------------------
    def plan_watering_day(self, kwargs):
        self.check_monthly_rotation(kwargs)
        self.log_event("planning_started")

        try:
            sunrise_dt = self.sunrise()
        except Exception as e:
            self.log(f"Sunrise unavailable: {e}. Using fallback {self.fallback_run_time}.", level="WARNING")
            h, m, s = [int(x) for x in self.fallback_run_time.split(":")]
            now = datetime.now()
            start_dt = now.replace(hour=h, minute=m, second=s, microsecond=0)
            if start_dt < now:
                start_dt += timedelta(days=1)
            delay = (start_dt - now).total_seconds()
            self.run_in(self.run_controller, delay)
            return

        estimated_total_secs = sum(
            int(config.get("max_duration", 60)) * 60 + 30
            for config in self.zones.values()
        )

        target_finish = sunrise_dt - timedelta(minutes=self.sunrise_offset)
        start_dt = target_finish - timedelta(seconds=estimated_total_secs)
        now = datetime.now()

        if start_dt < now:
            self.log("Calculated start is in the past — skipping today.")
            self.mqtt_publish(f"{MQTT_CTRL_BASE}/status", "idle")
            return

        delay = (start_dt - now).total_seconds()
        self.run_in(self.run_controller, delay)

        self.log(
            f"Sunrise: {sunrise_dt.strftime('%H:%M')} | "
            f"Target finish: {target_finish.strftime('%H:%M')} | "
            f"Watering starts: {start_dt.strftime('%H:%M')}"
        )
        self.mqtt_publish(f"{MQTT_CTRL_BASE}/next_run", start_dt.strftime("%Y-%m-%d %H:%M"))

    # ------------------------------------------------------------------
    # MAIN CONTROLLER LOOP
    # ------------------------------------------------------------------
    def run_controller(self, kwargs):
        self.log("=" * 50)
        self.log("PI Controller cycle starting")
        self.mqtt_publish(f"{MQTT_CTRL_BASE}/status", "running")

        inhibit = self.get_global_inhibit()
        if inhibit:
            self.log(f"Global inhibit: {inhibit}. All zones skipped.")
            self.notify("🚫 PI Watering Skipped", f"All zones skipped today: {inhibit}")
            self.mqtt_publish(f"{MQTT_CTRL_BASE}/status", "inhibited")
            self.mqtt_publish(f"{MQTT_CTRL_BASE}/inhibit_reason", inhibit)
            for zone_id in self.zones:
                self.publish_zone_state(zone_id, {"status": "skipped_inhibit"})
            self.log_event("cycle_inhibited", inhibit_reason=inhibit)
            return

        self.mqtt_publish(f"{MQTT_CTRL_BASE}/inhibit_reason", "none")
        state = self.load_state()

        schedule = []
        cumulative_delay = 0

        for zone_id, config in self.zones.items():
            try:
                result = self.evaluate_zone(zone_id, config, state)
                state = result["state"]
                if result["should_water"]:
                    schedule.append((cumulative_delay, zone_id, config, result["duration"]))
                    cumulative_delay += result["duration"] * 60 + 30
            except Exception as e:
                self.log(f"Zone {zone_id}: evaluation error — {e}", level="ERROR")

        self.save_state(state)
        self.mqtt_publish(f"{MQTT_CTRL_BASE}/last_run", datetime.now().strftime("%Y-%m-%d %H:%M"))

        if not schedule:
            self.log("No zones need watering today.")
            self.mqtt_publish(f"{MQTT_CTRL_BASE}/status", "idle")
            self.log("PI Controller cycle complete")
            self.log("=" * 50)
            return

        total_mins = sum(d for _, _, _, d in schedule) + (len(schedule) - 1) * 0.5
        self.log(f"Watering schedule: {len(schedule)} zone(s), ~{total_mins:.0f} min total")

        for i, (delay, zone_id, config, duration) in enumerate(schedule):
            next_valve    = schedule[i + 1][2]["valve_switch"] if i + 1 < len(schedule) else None
            next_zone_id  = schedule[i + 1][1] if i + 1 < len(schedule) else None
            self.run_in(
                self.open_valve_scheduled,
                delay,
                zone_id=zone_id,
                config=config,
                duration=duration,
                next_valve_switch=next_valve,
                next_zone_id=next_zone_id,
            )
            self.log(f"Zone {zone_id}: scheduled in {delay//60}min {delay%60}s for {duration}min")

        total_delay = cumulative_delay + 60
        self.run_in(lambda kw: self.mqtt_publish(f"{MQTT_CTRL_BASE}/status", "idle"), total_delay)

        self.log("PI Controller cycle complete — zones scheduled sequentially")
        self.log("=" * 50)

    # ------------------------------------------------------------------
    # ZONE EVALUATION
    # ------------------------------------------------------------------
    def evaluate_zone(self, zone_id, config, state):
        # Per-zone enable/disable
        zone_enabled = self.get_state(f"input_boolean.pi_zone_{zone_id}_enabled")
        if zone_enabled == "off":
            self.log(f"Zone {zone_id}: disabled — skipping.")
            self.publish_zone_state(zone_id, {"status": "disabled"})
            return {"state": state, "should_water": False, "duration": 0}

        moisture_sensor    = config["moisture_sensor"]
        setpoint           = float(config.get("setpoint", 50))
        kp                 = float(config.get("kp", 1.5))
        ki                 = float(config.get("ki", 0.3))
        max_duration       = int(config.get("max_duration", 60))
        min_duration       = int(config.get("min_duration", 5))

        # Emergency watered flag — reduce integral to prevent over-watering
        zone_st_check = state.get(zone_id, {})
        if zone_st_check.get("emergency_watered"):
            self.log(f"Zone {zone_id}: emergency water ran — reducing integral.")
            if zone_id in state:
                state[zone_id]["integral"] = state[zone_id].get("integral", 0) * 0.5
                state[zone_id]["emergency_watered"] = False

        moisture_raw = self.get_state(moisture_sensor)
        if moisture_raw in (None, "unavailable", "unknown"):
            if self.require_moisture_sensors:
                last_known = state.get(zone_id, {}).get("last_moisture")
                if last_known is not None:
                    moisture = float(last_known)
                    self.log(f"Zone {zone_id}: sensor offline — using last known {moisture:.1f}%.", level="WARNING")
                    self.notify(
                        f"⚠️ Zone {zone_id} Sensor Offline",
                        f"Using last known moisture: {moisture:.0f}%."
                    )
                else:
                    moisture = self.default_moisture
                    self.log(f"Zone {zone_id}: sensor offline, no prior reading — using default {moisture}%.", level="WARNING")
                    self.notify(
                        f"⚠️ Zone {zone_id} Sensor Offline",
                        f"No prior reading. Using default {moisture:.0f}%."
                    )
            else:
                moisture = self.default_moisture
                self.log(f"Zone {zone_id}: sensor unavailable — using default {moisture}%.", level="WARNING")
        else:
            moisture = float(moisture_raw)

        error = setpoint - moisture

        if zone_id not in state:
            state[zone_id] = {"integral": 0.0, "last_error": 0.0, "last_run": None}

        zone_st      = state[zone_id]
        integral     = float(zone_st.get("integral", 0.0))
        max_integral = (max_duration / ki) if ki > 0 else 9999.0
        integral     = max(-max_integral, min(max_integral, integral + error))

        if error <= 0:
            self.log(f"Zone {zone_id}: {moisture:.1f}% >= setpoint {setpoint:.0f}%. No watering.")
            decayed = integral * 0.5
            zone_st.update({"integral": decayed, "last_error": error, "last_run": datetime.now().isoformat()})
            state[zone_id] = zone_st
            self.publish_zone_state(zone_id, {
                "status": "skipped_wet", "moisture": round(moisture, 1),
                "setpoint": setpoint, "error": round(error, 1),
                "integral": round(decayed, 2), "duration": 0, "temp_factor": 1.0,
            })
            self.log_event("zone_skipped_wet", zone=zone_id, moisture=round(moisture, 1),
                           setpoint=setpoint, error=round(error, 1))
            return {"state": state, "should_water": False, "duration": 0}

        raw_output  = (kp * error) + (ki * integral)
        temp_factor = self.get_temp_factor()
        adjusted    = raw_output * temp_factor
        duration    = int(max(min_duration, min(max_duration, adjusted)))

        self.log(
            f"Zone {zone_id}: moisture={moisture:.1f}% | setpoint={setpoint:.0f}% | "
            f"error={error:+.1f} | integral={integral:.2f} | raw={raw_output:.1f} | "
            f"temp_factor={temp_factor:.2f} | adjusted={adjusted:.1f} | duration={duration}min"
        )

        zone_st.update({
            "integral": integral, "last_error": error,
            "last_run": datetime.now().isoformat(),
            "last_duration": duration, "last_moisture": moisture,
        })
        state[zone_id] = zone_st

        self.publish_zone_state(zone_id, {
            "status": "scheduled", "moisture": round(moisture, 1),
            "setpoint": setpoint, "error": round(error, 1),
            "integral": round(integral, 2), "duration": duration,
            "temp_factor": round(temp_factor, 2),
            "last_watered": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })

        self.notify(
            f"💧 PI Zone {zone_id}: Scheduled {duration} min",
            f"Moisture: {moisture:.0f}% (target {setpoint:.0f}%, deficit {error:.1f}%). "
            f"Temp factor: {temp_factor:.2f}×."
        )

        self.log_event("zone_scheduled", zone=zone_id, moisture=round(moisture, 1),
                       setpoint=setpoint, error=round(error, 1), integral=round(integral, 2),
                       temp_factor=round(temp_factor, 2), duration_min=duration)

        return {"state": state, "should_water": True, "duration": duration}

    # ------------------------------------------------------------------
    # VALVE OPEN — with hardware timer and cutoff watchdog
    # ------------------------------------------------------------------
    def open_valve_scheduled(self, kwargs):
        zone_id             = kwargs["zone_id"]
        config              = kwargs["config"]
        duration            = kwargs["duration"]
        valve_switch        = config["valve_switch"]
        moisture_sensor     = config["moisture_sensor"]
        recovery_threshold  = float(config.get("recovery_threshold", 40))
        recovery_delay_mins = int(config.get("recovery_delay_minutes", 60))
        next_valve_switch   = kwargs.get("next_valve_switch")
        next_zone_id        = kwargs.get("next_zone_id")

        moisture_raw = self.get_state(moisture_sensor)
        moisture_before = float(moisture_raw) if moisture_raw not in (None, "unavailable", "unknown") else self.default_moisture

        # Set hardware duration timer on LinkTap (dead-man switch)
        duration_entity = valve_switch.replace("switch.", "number.") + "_watering_duration"
        try:
            self.call_service("number/set_value", entity_id=duration_entity, value=duration)
            self.log(f"Zone {zone_id}: hardware timer set to {duration}min")
        except Exception as e:
            self.log(f"Zone {zone_id}: could not set hardware timer — {e}", level="WARNING")

        self.call_service("switch/turn_on", entity_id=valve_switch)
        self.log(f"Zone {zone_id}: valve opened ({valve_switch}) for {duration}min")

        self.mqtt_publish(f"{MQTT_TOPIC_BASE}/{zone_id}/status", "watered")
        self.mqtt_publish(f"{MQTT_TOPIC_BASE}/{zone_id}/last_watered", datetime.now().strftime("%Y-%m-%d %H:%M"))

        # Register cutoff watchdog
        is_watering_sensor = valve_switch.replace("switch.", "binary_sensor.") + "_is_watering"
        valve_open_time = datetime.now()
        self.active_valves[zone_id] = {"valve_switch": valve_switch, "open_time": valve_open_time,
                                        "scheduled_duration": duration, "watchdog_handle": None}
        handle = self.listen_state(
            self.handle_valve_cutoff, is_watering_sensor, new="off",
            zone_id=zone_id, valve_open_time=valve_open_time,
            scheduled_duration=duration, moisture_sensor=moisture_sensor,
            recovery_threshold=recovery_threshold, recovery_delay_mins=recovery_delay_mins,
            next_valve_switch=next_valve_switch, next_zone_id=next_zone_id,
        )
        self.active_valves[zone_id]["watchdog_handle"] = handle

        close_delay = duration * 60
        if next_valve_switch:
            self.run_in(self.close_valve, close_delay, zone_id=zone_id, valve_switch=valve_switch,
                        moisture_sensor=moisture_sensor, recovery_threshold=recovery_threshold,
                        moisture_before=moisture_before, recovery_delay_minutes=recovery_delay_mins,
                        next_valve_switch=next_valve_switch, next_zone_id=next_zone_id)
        else:
            self.run_in(self.close_valve, close_delay, zone_id=zone_id, valve_switch=valve_switch,
                        moisture_sensor=moisture_sensor, recovery_threshold=recovery_threshold,
                        moisture_before=moisture_before, recovery_delay_minutes=recovery_delay_mins)

    # ------------------------------------------------------------------
    # VALVE CLOSE
    # ------------------------------------------------------------------
    def close_valve(self, kwargs):
        zone_id             = kwargs["zone_id"]
        valve_switch        = kwargs["valve_switch"]
        moisture_sensor     = kwargs["moisture_sensor"]
        recovery_threshold  = kwargs["recovery_threshold"]
        moisture_before     = kwargs["moisture_before"]
        recovery_delay_mins = kwargs.get("recovery_delay_minutes", 60)
        next_valve_switch   = kwargs.get("next_valve_switch")
        next_zone_id        = kwargs.get("next_zone_id")

        if zone_id in self.active_valves:
            handle = self.active_valves[zone_id].get("watchdog_handle")
            if handle:
                try:
                    self.cancel_listen_state(handle)
                except Exception:
                    pass
            del self.active_valves[zone_id]

        self.call_service("switch/turn_off", entity_id=valve_switch)
        self.log(f"Zone {zone_id}: valve closed ({valve_switch})")

        if next_valve_switch:
            self.run_in(self.open_next_zone_after_gap, self.overlap_seconds,
                        next_valve_switch=next_valve_switch, next_zone_id=next_zone_id)

        self.run_in(self.check_recovery, recovery_delay_mins * 60,
                    zone_id=zone_id, moisture_sensor=moisture_sensor,
                    recovery_threshold=recovery_threshold, moisture_before=moisture_before)

    def open_next_zone_after_gap(self, kwargs):
        next_valve_switch = kwargs["next_valve_switch"]
        next_zone_id      = kwargs.get("next_zone_id", "?")
        self.call_service("switch/turn_on", entity_id=next_valve_switch)
        self.log(f"Zone {next_zone_id}: valve opened after {self.overlap_seconds}s gap")

    # ------------------------------------------------------------------
    # FLOW CUTOFF WATCHDOG
    # ------------------------------------------------------------------
    def handle_valve_cutoff(self, entity, attribute, old, new, kwargs):
        zone_id             = kwargs["zone_id"]
        valve_open_time     = kwargs["valve_open_time"]
        scheduled_duration  = kwargs["scheduled_duration"]
        moisture_sensor     = kwargs["moisture_sensor"]
        recovery_threshold  = kwargs["recovery_threshold"]
        recovery_delay_mins = kwargs["recovery_delay_mins"]
        next_valve_switch   = kwargs.get("next_valve_switch")
        next_zone_id        = kwargs.get("next_zone_id")

        actual_seconds = (datetime.now() - valve_open_time).total_seconds()
        actual_minutes = round(actual_seconds / 60, 1)
        pct_delivered  = round(actual_seconds / (scheduled_duration * 60) * 100, 1)

        self.log(f"Zone {zone_id}: CUTOFF — ran {actual_minutes}min of {scheduled_duration}min ({pct_delivered}%)", level="WARNING")

        if zone_id in self.active_valves:
            handle = self.active_valves[zone_id].get("watchdog_handle")
            if handle:
                try:
                    self.cancel_listen_state(handle)
                except Exception:
                    pass
            del self.active_valves[zone_id]

        # Adjust integral proportionally
        state = self.load_state()
        if zone_id in state:
            old_integral = float(state[zone_id].get("integral", 0.0))
            fraction_missed = 1.0 - (actual_seconds / (scheduled_duration * 60))
            new_integral = old_integral - (old_integral * fraction_missed * 0.5)
            state[zone_id]["integral"] = new_integral
            state[zone_id]["cutoff_detected"] = True
            self.save_state(state)

        # Detect cause from LinkTap binary sensors
        valve_switch = self.zones[zone_id]["valve_switch"]
        base = valve_switch.replace("switch.", "binary_sensor.")
        causes = []
        if self.get_state(f"{base}_is_cutoff")  == "on": causes.append("flow cutoff")
        if self.get_state(f"{base}_is_clogged") == "on": causes.append("clog detected")
        if self.get_state(f"{base}_is_leaking") == "on": causes.append("leak detected")
        if self.get_state(f"{base}_is_fall")    == "on": causes.append("fall/tilt")
        cause_str = ", ".join(causes) if causes else "unknown reason"

        self.notify(
            f"⚠️ Zone {zone_id} Watering Cutoff",
            f"Cause: {cause_str}\n"
            f"Ran: {actual_minutes} min of {scheduled_duration} min ({pct_delivered}%).\n"
            f"Integral adjusted. Check emitters on Zone {zone_id}."
        )
        self.log_event("valve_cutoff", zone=zone_id, scheduled_duration_min=scheduled_duration,
                       actual_duration_min=actual_minutes, pct_delivered=pct_delivered, cause=cause_str)
        self.mqtt_publish(f"{MQTT_TOPIC_BASE}/{zone_id}/status", "cutoff")

        if next_valve_switch:
            self.run_in(self.open_next_zone_after_gap, 10,
                        next_valve_switch=next_valve_switch, next_zone_id=next_zone_id)

    # ------------------------------------------------------------------
    # RECOVERY CHECK
    # ------------------------------------------------------------------
    def check_recovery(self, kwargs):
        zone_id            = kwargs["zone_id"]
        moisture_sensor    = kwargs["moisture_sensor"]
        recovery_threshold = kwargs["recovery_threshold"]
        moisture_before    = kwargs["moisture_before"]

        moisture_raw = self.get_state(moisture_sensor)
        if moisture_raw in (None, "unavailable", "unknown"):
            self.log(f"Zone {zone_id}: recovery check — sensor unavailable.", level="WARNING")
            return

        moisture_after = float(moisture_raw)
        delta          = moisture_after - moisture_before
        recovered      = moisture_after >= recovery_threshold

        self.log(f"Zone {zone_id}: recovery — before={moisture_before:.1f}% after={moisture_after:.1f}% Δ={delta:+.1f}%")

        self.mqtt_publish(f"{MQTT_TOPIC_BASE}/{zone_id}/moisture", round(moisture_after, 1))
        self.mqtt_publish(f"{MQTT_TOPIC_BASE}/{zone_id}/recovery_delta", round(delta, 1))

        if not recovered:
            self.notify(
                f"⚠️ Zone {zone_id} Not Recovering",
                f"Moisture still {moisture_after:.0f}% after watering. Check dripper/emitter/valve."
            )
            self.mqtt_publish(f"{MQTT_TOPIC_BASE}/{zone_id}/status", "recovery_failed")
        else:
            self.log(f"Zone {zone_id}: healthy recovery to {moisture_after:.1f}%. ✓")
            self.mqtt_publish(f"{MQTT_TOPIC_BASE}/{zone_id}/status", "recovered")

        self.log_event("zone_recovery_check", zone=zone_id, moisture_before=round(moisture_before, 1),
                       moisture_after=round(moisture_after, 1), delta=round(delta, 1),
                       recovery_threshold=recovery_threshold, recovered=recovered)

    # ------------------------------------------------------------------
    # TEMPERATURE FEED-FORWARD
    # ------------------------------------------------------------------
    def get_temp_factor(self):
        # Try soil temp first if configured
        if self.soil_temp_sensor:
            soil_raw = self.get_state(self.soil_temp_sensor)
            if soil_raw not in (None, "unavailable", "unknown"):
                soil_temp = float(soil_raw)
                if soil_temp >= 85: factor = 1.40
                elif soil_temp >= 80: factor = 1.25
                elif soil_temp >= 75: factor = 1.10
                elif soil_temp >= 65: factor = 1.00
                elif soil_temp >= 55: factor = 0.80
                elif soil_temp >= 50: factor = 0.65
                else: factor = 0.50
                self.log(f"Soil temp feed-forward: {soil_temp:.1f}°F → ×{factor:.2f}")
                return factor

        # Fallback to air temp
        temp_raw = self.get_state(self.temp_sensor)
        if temp_raw in (None, "unavailable", "unknown"):
            return 1.0

        temp = float(temp_raw)
        breakpoints = [(100, 1.40), (95, 1.30), (90, 1.20), (85, 1.10),
                       (70, 1.00), (60, 0.85), (45, 0.70), (0, 0.55)]
        for threshold, factor in breakpoints:
            if temp >= threshold:
                self.log(f"Air temp feed-forward: {temp:.0f}°F → ×{factor:.2f}")
                return factor
        return 0.55

    # ------------------------------------------------------------------
    # GLOBAL INHIBIT
    # ------------------------------------------------------------------
    def get_global_inhibit(self):
        enabled = self.get_state("input_boolean.garden_pi_controller_enabled")
        if enabled == "off":
            return "system disabled by user"

        weather = self.get_state(self.weather_entity)
        if weather in ("rainy", "pouring"):
            return f"weather state is '{weather}'"

        if self.rain_prob_sensor:
            rain_prob_raw = self.get_state(self.rain_prob_sensor)
            if rain_prob_raw not in (None, "unavailable", "unknown"):
                if float(rain_prob_raw) >= 0.5:
                    return f"rain probability at {float(rain_prob_raw)*100:.0f}%"

        if self.rain_day_sensor:
            rain_raw = self.get_state(self.rain_day_sensor)
            if rain_raw not in (None, "unavailable", "unknown"):
                if float(rain_raw) >= self.rain_threshold:
                    return f'{rain_raw}" of rain already logged today'

        return None

    # ------------------------------------------------------------------
    # OVERNIGHT RAIN WATCHDOG
    # ------------------------------------------------------------------
    def check_overnight_rain(self, kwargs):
        if not self.rain_day_sensor:
            return

        self.log("Overnight rain watchdog running...")
        rain_raw = self.get_state(self.rain_day_sensor)
        if rain_raw in (None, "unavailable", "unknown"):
            return

        rain_today = float(rain_raw)
        if rain_today < self.rain_watchdog_threshold:
            return

        # Phantom rain check — cross-validate with forecast probability
        if self.rain_prob_sensor:
            prob_raw = self.get_state(self.rain_prob_sensor)
            if prob_raw not in (None, "unavailable", "unknown"):
                prob = float(prob_raw)
                if prob < 0.10:
                    self.log(f"Rain watchdog: {rain_today:.2f}\" logged but forecast prob {prob*100:.0f}% — likely phantom rain.", level="WARNING")
                    self.notify("⚠️ Possible Phantom Rain", f"Sensor logged {rain_today:.2f}\" but forecast was {prob*100:.0f}%. Skipping decay.")
                    self.log_event("phantom_rain_detected", rain_in=round(rain_today, 2), forecast_prob=round(prob, 2))
                    return

        # Scale decay by rain amount
        if rain_today >= 2.0: decay_factor = 0.10
        elif rain_today >= 1.0: decay_factor = 0.25
        elif rain_today >= 0.5: decay_factor = 0.50
        else: decay_factor = 0.75

        state = self.load_state()
        for zone_id in self.zones:
            if zone_id in state:
                old_i = float(state[zone_id].get("integral", 0.0))
                state[zone_id]["integral"] = old_i * decay_factor

        self.save_state(state)
        self.notify("🌧️ Overnight Rain — Integral Adjusted",
                    f"{rain_today:.2f}\" fell overnight. Zone integrals decayed to {decay_factor*100:.0f}%.")
        self.log_event("rain_watchdog", rain_in=round(rain_today, 2), decay_factor=decay_factor)

    # ------------------------------------------------------------------
    # EMERGENCY EVENING WATER CHECK
    # ------------------------------------------------------------------
    def check_emergency_water(self, kwargs):
        self.log("Emergency water check running...")

        inhibit_reason = self.get_state(f"sensor.garden_pi_controller_pi_inhibit_reason")
        morning_was_inhibited = (inhibit_reason not in ("none", None, "unknown"))
        if not morning_was_inhibited:
            return

        if self.rain_day_sensor:
            rain_raw = self.get_state(self.rain_day_sensor)
            if rain_raw not in (None, "unavailable", "unknown"):
                if float(rain_raw) >= 0.1:
                    return

        temp_raw = self.get_state(self.temp_sensor)
        if temp_raw not in (None, "unavailable", "unknown"):
            if float(temp_raw) < self.emergency_temp_threshold:
                return

        zones_needing_water = []
        for zone_id, config in self.zones.items():
            moisture_raw = self.get_state(config["moisture_sensor"])
            if moisture_raw in (None, "unavailable", "unknown"):
                zones_needing_water.append(zone_id)
            elif float(moisture_raw) < self.emergency_moisture_threshold:
                zones_needing_water.append(zone_id)

        if not zones_needing_water:
            return

        temp_display = f"{float(temp_raw):.0f}°F" if temp_raw not in (None, "unavailable", "unknown") else "unknown"
        rain_display = f"{float(self.get_state(self.rain_day_sensor)):.2f}\"" if self.rain_day_sensor else "N/A"

        self.notify("🚨 Emergency Evening Watering",
                    f"Morning was skipped. Temp: {temp_display} | Rain: {rain_display}\n"
                    f"Running {int(self.emergency_duration_pct * 100)}% duration on zones: {', '.join(zones_needing_water)}")

        self.log_event("emergency_water_triggered", zones=zones_needing_water,
                       duration_pct=self.emergency_duration_pct)
        self.mqtt_publish(f"{MQTT_CTRL_BASE}/status", "emergency_run")

        schedule = []
        cumulative_delay = 0
        state = self.load_state()

        for zone_id, config in self.zones.items():
            if zone_id not in zones_needing_water:
                continue
            max_dur = int(config.get("max_duration", 60))
            min_dur = int(config.get("min_duration", 5))
            duration = max(min_dur, int(max_dur * self.emergency_duration_pct))
            schedule.append((cumulative_delay, zone_id, config, duration))
            cumulative_delay += duration * 60 + 30

        for i, (delay, zone_id, config, duration) in enumerate(schedule):
            next_valve = schedule[i + 1][2]["valve_switch"] if i + 1 < len(schedule) else None
            next_zone_id = schedule[i + 1][1] if i + 1 < len(schedule) else None
            self.run_in(self.open_valve_scheduled, delay, zone_id=zone_id, config=config,
                        duration=duration, next_valve_switch=next_valve, next_zone_id=next_zone_id)

        def finalize_emergency(kwargs):
            state = self.load_state()
            for zone_id in zones_needing_water:
                if zone_id not in state:
                    state[zone_id] = {}
                state[zone_id]["last_run"] = datetime.now().isoformat()
                state[zone_id]["emergency_watered"] = True
            self.save_state(state)
            self.mqtt_publish(f"{MQTT_CTRL_BASE}/status", "idle")
            self.mqtt_publish(f"{MQTT_CTRL_BASE}/inhibit_reason", "none")

        self.run_in(finalize_emergency, cumulative_delay + 60)

    # ------------------------------------------------------------------
    # FORCE RUN
    # ------------------------------------------------------------------
    def handle_force_run(self, entity, attribute, old, new, kwargs):
        self.log("Force run triggered.")
        self.notify("🔧 PI Force Run", f"Running all enabled zones for {self.force_run_duration} min.")
        self.mqtt_publish(f"{MQTT_CTRL_BASE}/status", "force_run")

        schedule = []
        cumulative_delay = 0
        for zone_id, config in self.zones.items():
            zone_enabled = self.get_state(f"input_boolean.pi_zone_{zone_id}_enabled")
            if zone_enabled == "off":
                continue
            schedule.append((cumulative_delay, zone_id, config, self.force_run_duration))
            cumulative_delay += self.force_run_duration * 60 + 30

        for i, (delay, zone_id, config, duration) in enumerate(schedule):
            next_valve   = schedule[i + 1][2]["valve_switch"] if i + 1 < len(schedule) else None
            next_zone_id = schedule[i + 1][1] if i + 1 < len(schedule) else None
            self.run_in(self.open_valve_scheduled, delay, zone_id=zone_id, config=config,
                        duration=duration, next_valve_switch=next_valve, next_zone_id=next_zone_id)

        self.run_in(lambda kw: (
            self.mqtt_publish(f"{MQTT_CTRL_BASE}/status", "idle"),
            self.mqtt_publish(f"{MQTT_CTRL_BASE}/inhibit_reason", "none"),
        ), cumulative_delay + 60)

    # ------------------------------------------------------------------
    # STARTUP SAFETY CHECK
    # ------------------------------------------------------------------
    def startup_safety_check(self, kwargs):
        self.log("Running startup safety check...")
        for zone_id, config in self.zones.items():
            valve_switch = config["valve_switch"]
            if self.get_state(valve_switch) == "on":
                self.log(f"Zone {zone_id}: valve open on startup — force closing.", level="WARNING")
                self.call_service("switch/turn_off", entity_id=valve_switch)
                self.notify("🚨 Valve Closed on Startup",
                            f"Zone {zone_id} was open at startup. Closed automatically.")

    # ------------------------------------------------------------------
    # MQTT DISCOVERY
    # ------------------------------------------------------------------
    def publish_mqtt_discovery(self, kwargs):
        zone_names = {z: config.get("name", f"Zone {z}") for z, config in self.zones.items()}

        ctrl_sensors = [
            ("status",        "PI Controller Status", None, "mdi:robot"),
            ("inhibit_reason","PI Inhibit Reason",    None, "mdi:cancel"),
            ("last_run",      "PI Last Run",           None, "mdi:history"),
            ("next_run",      "PI Next Run",           None, "mdi:clock-start"),
            ("emergency_run", "PI Emergency Run",      None, "mdi:alert"),
        ]

        for field, name, unit, icon in ctrl_sensors:
            unique_id = f"pi_controller_{field}"
            config = {
                "name": name, "unique_id": unique_id,
                "state_topic": f"{MQTT_CTRL_BASE}/{field}", "icon": icon,
                "device": {"identifiers": ["pi_controller"], "name": "Garden PI Controller",
                           "model": "Predictive Irrigation Controller", "manufacturer": "HACS AppDaemon"},
            }
            if unit:
                config["unit_of_measurement"] = unit
            topic = f"homeassistant/sensor/{unique_id}/config"
            self.mqtt_publish(topic, json.dumps(config), retain=True)

        zone_sensors = [
            ("moisture",     "Moisture",      "%",   "moisture", "mdi:water-percent"),
            ("setpoint",     "Setpoint",      "%",   None,       "mdi:target"),
            ("error",        "Deficit",       "%",   None,       "mdi:minus-circle"),
            ("integral",     "Integral",      "",    None,       "mdi:sigma"),
            ("duration",     "Last Duration", "min", None,       "mdi:timer"),
            ("temp_factor",  "Temp Factor",   "×",   None,       "mdi:thermometer"),
            ("last_watered", "Last Watered",  "",    None,       "mdi:calendar-clock"),
            ("status",       "Status",        "",    None,       "mdi:information"),
            ("recovery_delta","Recovery Δ",   "%",   None,       "mdi:trending-up"),
        ]

        for zone_id in self.zones:
            name = zone_names.get(zone_id, f"Zone {zone_id}")
            for field, label, unit, dev_class, icon in zone_sensors:
                unique_id = f"pi_zone_{zone_id}_{field}"
                config = {
                    "name": f"PI {name} {label}", "unique_id": unique_id,
                    "state_topic": f"{MQTT_TOPIC_BASE}/{zone_id}/{field}", "icon": icon,
                    "device": {"identifiers": ["pi_controller"], "name": "Garden PI Controller",
                               "model": "Predictive Irrigation Controller", "manufacturer": "HACS AppDaemon"},
                }
                if unit:
                    config["unit_of_measurement"] = unit
                if dev_class:
                    config["device_class"] = dev_class
                topic = f"homeassistant/sensor/{unique_id}/config"
                self.mqtt_publish(topic, json.dumps(config), retain=True)

        self.log("MQTT discovery configs published.")

    def publish_zone_state(self, zone_id, fields):
        for field, value in fields.items():
            self.mqtt_publish(f"{MQTT_TOPIC_BASE}/{zone_id}/{field}", str(value))

    def _publish_idle_status(self, kwargs):
        self.mqtt_publish(f"{MQTT_CTRL_BASE}/status", "idle")
        self.mqtt_publish(f"{MQTT_CTRL_BASE}/inhibit_reason", "none")
        self.mqtt_publish(f"{MQTT_CTRL_BASE}/last_run", "not yet run")
        self.mqtt_publish(f"{MQTT_CTRL_BASE}/next_run", "calculating...")

    # ------------------------------------------------------------------
    # MONTHLY LOG
    # ------------------------------------------------------------------
    def _log_path(self):
        Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
        return f"{LOG_DIR}/pi_{datetime.now().strftime('%Y_%m')}.jsonl"

    def log_event(self, event, **fields):
        entry = {"ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "event": event}
        try:
            if self.rain_day_sensor:
                r = self.get_state(self.rain_day_sensor)
                entry["rain_today_in"] = float(r) if r not in (None, "unavailable", "unknown") else None
            if self.rain_prob_sensor:
                p = self.get_state(self.rain_prob_sensor)
                entry["rain_prob"] = float(p) if p not in (None, "unavailable", "unknown") else None
            t = self.get_state(self.temp_sensor)
            entry["temp_f"] = float(t) if t not in (None, "unavailable", "unknown") else None
        except Exception:
            pass
        entry.update(fields)
        try:
            with open(self._log_path(), "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            self.log(f"Log write failed: {e}", level="WARNING")

    def check_monthly_rotation(self, kwargs):
        now = datetime.now()
        if now.day != 1:
            return
        from datetime import date
        prev = date(now.year, now.month, 1) - timedelta(days=1)
        prev_log = f"{LOG_DIR}/pi_{prev.strftime('%Y_%m')}.jsonl"
        if os.path.exists(prev_log):
            size_kb = os.path.getsize(prev_log) // 1024
            self.notify(f"📊 Monthly PI Log — {prev.strftime('%B %Y')}",
                        f"Log ready: pi_{prev.strftime('%Y_%m')}.jsonl ({size_kb} KB)\nLocation: {LOG_DIR}")
            self.log_event("monthly_rotation", prev_log=prev_log, size_kb=size_kb)

    # ------------------------------------------------------------------
    # STATE PERSISTENCE
    # ------------------------------------------------------------------
    def load_state(self):
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, "r") as f:
                    data = json.load(f)
                    self.log(f"Loaded PI state for zones: {list(data.keys())}")
                    return data
        except Exception as e:
            self.log(f"Could not load PI state: {e}", level="WARNING")
        return {}

    def save_state(self, state):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            self.log(f"Could not save PI state: {e}", level="ERROR")

    # ------------------------------------------------------------------
    # NOTIFY
    # ------------------------------------------------------------------
    def notify(self, title, message):
        try:
            self.call_service(self.notify_service, title=title, message=message)
        except Exception as e:
            self.log(f"Notify failed: {e}", level="WARNING")

    def mqtt_publish(self, topic, payload, retain=False):
        try:
            self.call_service("mqtt/publish", topic=topic, payload=payload, retain=retain)
        except Exception as e:
            self.log(f"MQTT publish failed: {e}", level="WARNING")
