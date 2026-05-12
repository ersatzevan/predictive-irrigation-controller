# Predictive Irrigation Controller

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![AppDaemon](https://img.shields.io/badge/AppDaemon-4.x-blue.svg)](https://appdaemon.readthedocs.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A smart irrigation controller for Home Assistant using a **Proportional-Integral (PI) algorithm** — the same class of control logic used in industrial process automation. Instead of watering for a fixed duration every day, this controller calculates exactly how much water each zone needs based on actual soil moisture deficit, accumulated over time.

---

## Why PI Control?

Most smart irrigation systems ask: *is it time to water?*

This controller asks: *how dry is the soil, how long has it been dry, and how much water does it actually need right now?*

| Feature | Fixed Timer | Weather-Based | PI Controller |
|---------|-------------|---------------|---------------|
| Adapts to actual soil moisture | ❌ | ❌ | ✅ |
| Remembers multi-day deficits | ❌ | ❌ | ✅ |
| Scales with temperature | ❌ | Partial | ✅ |
| Self-tuning over time | ❌ | ❌ | ✅ |
| Works without moisture sensors | ✅ | ✅ | ✅ |

---

## Features

- **Proportional-Integral control** with anti-windup — duration scales with deficit and accumulated memory
- **Sunrise-based scheduling** — all watering finishes before sunrise, foliage stays dry
- **Temperature feed-forward** — soil temp preferred, air temp fallback
- **Weather inhibit** — skips on rain, high forecast probability, or wet conditions
- **Sequential zone execution** — configurable RF gap between valves
- **Hardware timer passthrough** — sets LinkTap duration before opening (dead-man switch if Pi crashes)
- **Last-known-good moisture fallback** — uses cached reading if sensor goes offline while you travel
- **Phantom rain detection** — cross-validates rain sensor against forecast probability
- **Emergency evening water** — fires shortened cycle if morning was skipped + hot + dry + no rain
- **Overnight rain watchdog** — decays integrals proportionally if significant rain fell
- **Flow cutoff watchdog** — detects unexpected valve termination, adjusts integral, notifies you
- **Per-zone enable/disable** — toggle individual zones from HA dashboard
- **Master on/off switch** — disable entire system without touching config
- **Force run button** — manual cycle bypassing moisture checks
- **MQTT discovery** — live dashboard sensors auto-created in HA
- **Monthly JSONL logging** — structured log for end-of-month tuning analysis

---

## Requirements

- Home Assistant 2023.1.0 or newer
- [AppDaemon 4.x](https://github.com/AppDaemon/appdaemon) (add-on or Docker)
- MQTT integration configured in HA (Mosquitto recommended)
- Valve switches as HA entities (LinkTap, ESPHome, Tasmota, etc.)
- Soil moisture sensors (optional — see `require_moisture_sensors`)

---

## Installation

### Via HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the three-dot menu → **Custom repositories**
3. Add `https://github.com/YOURUSERNAME/predictive-irrigation-controller` as **AppDaemon** type
4. Search for **Predictive Irrigation Controller** and install
5. Restart AppDaemon

### Manual

1. Copy `apps/predictive_irrigation_controller/` to your AppDaemon apps directory
2. Merge the example config into your `apps.yaml`
3. Restart AppDaemon

---

## Setup

### Step 1 — Create HA Helpers

Go to **Settings → Helpers** and create:

| Helper | Type | Entity ID |
|--------|------|-----------|
| Garden PI Controller Enabled | Toggle | `input_boolean.garden_pi_controller_enabled` |
| PI Force Run | Button | `input_button.pi_force_run` |
| PI Zone 1 Enabled | Toggle | `input_boolean.pi_zone_1_enabled` |
| PI Zone 2 Enabled | Toggle | `input_boolean.pi_zone_2_enabled` |
| *(repeat per zone)* | Toggle | `input_boolean.pi_zone_N_enabled` |

Turn all toggles **ON** after creating them.

### Step 2 — Configure apps.yaml

Copy the example from `apps/predictive_irrigation_controller/predictive_irrigation_controller.yaml` and adjust for your setup. Minimum required config:

```yaml
predictive_irrigation_controller:
  module: predictive_irrigation_controller
  class: PredictiveIrrigationController
  temp_sensor: sensor.your_outdoor_temperature
  weather_entity: weather.forecast_home
  zones:
    "1":
      moisture_sensor: sensor.your_soil_moisture_zone_1
      valve_switch: switch.your_zone_1_valve
      setpoint: 60
      kp: 1.5
      ki: 0.3
      max_duration: 60
      min_duration: 5
      recovery_threshold: 40
```

### Step 3 — Create Log Directory

```bash
mkdir -p /config/appdaemon/logs
```

### Step 4 — Restart AppDaemon

After restarting, MQTT sensors will appear under **Garden PI Controller** device in HA within 30 seconds.

---

## Configuration Reference

### Top-Level Settings

| Key | Default | Description |
|-----|---------|-------------|
| `temp_sensor` | required | Air temperature sensor entity ID |
| `soil_temp_sensor` | `null` | Soil temperature sensor (preferred over air temp) |
| `weather_entity` | `weather.forecast_home` | HA weather entity for condition check |
| `rain_prob_sensor` | `null` | Forecast precipitation probability sensor (0.0–1.0) |
| `rain_day_sensor` | `null` | Daily rain accumulation sensor (inches) |
| `rain_today_threshold` | `0.5` | Skip watering if rain exceeds this (inches) |
| `notify_service` | `notify/notify` | HA notification service |
| `require_moisture_sensors` | `true` | If false, uses default_moisture when sensor unavailable |
| `default_moisture` | `30` | Assumed moisture % when sensors unavailable |
| `sunrise_offset_minutes` | `15` | Minutes before sunrise to finish watering |
| `fallback_run_time` | `05:30:00` | Fixed start time if sunrise calculation fails |
| `overlap_seconds` | `10` | Gap between closing one valve and opening next |
| `force_run_duration` | `10` | Minutes per zone during force run |

### Emergency Evening Check

| Key | Default | Description |
|-----|---------|-------------|
| `emergency_check_time` | `16:00:00` | Time to run emergency check |
| `emergency_temp_threshold` | `85.0` | Minimum temp to trigger emergency (°F) |
| `emergency_moisture_threshold` | `40.0` | Moisture below which zone is considered dry (%) |
| `emergency_duration_pct` | `0.5` | Fraction of normal duration for emergency run |

### Rain Watchdog

| Key | Default | Description |
|-----|---------|-------------|
| `rain_watchdog_time` | `05:00:00` | Time to run overnight rain check |
| `rain_watchdog_threshold` | `0.5` | Rain depth that triggers integral decay (inches) |

### Per-Zone Settings

| Key | Default | Description |
|-----|---------|-------------|
| `moisture_sensor` | required | Soil moisture sensor entity ID |
| `valve_switch` | required | Valve switch entity ID |
| `name` | `Zone N` | Display name for MQTT sensors |
| `setpoint` | `50` | Target moisture level (%) |
| `kp` | `1.5` | Proportional gain |
| `ki` | `0.3` | Integral gain |
| `max_duration` | `60` | Maximum watering duration (minutes) |
| `min_duration` | `5` | Minimum watering duration (minutes) |
| `recovery_threshold` | `40` | Moisture below which recovery is flagged failed (%) |
| `recovery_delay_minutes` | `60` | Minutes after close to check moisture recovery |

---

## Tuning Guide

### Starting Values

The defaults (`kp: 1.5`, `ki: 0.3`) work well for containers and pots. For in-ground beds with more soil buffer, try `kp: 1.0`, `ki: 0.2`.

### Interpreting the Logs

Each morning cycle logs a line per zone:

```
Zone 1: moisture=38% | setpoint=60% | error=+22 | integral=28.4 | raw=39.6 | temp_factor=0.80 | adjusted=31.7 | duration=32min
```

- **error** too large consistently → lower `setpoint` or check sensor placement
- **integral** growing without bound → sensor may be offline, check `require_moisture_sensors`
- **duration** always hitting `max_duration` → increase `max_duration` or raise `kp`
- **recovery check failing** → check emitters/drippers for clogs

### Monthly Log Analysis

Export the monthly log from your AppDaemon container:

```bash
# From your Mac / local machine
scp user@your-pi:/config/appdaemon/logs/pi_YYYY_MM.jsonl ~/Downloads/
```

The `.jsonl` file contains structured JSON entries for every decision the controller made — zone evaluations, inhibits, emergency runs, recovery checks, and rain events. You can share this log for analysis and tuning recommendations.

---

## MQTT Sensors

After the first run, these entities appear in HA under the **Garden PI Controller** device:

| Sensor | Description |
|--------|-------------|
| `sensor.pi_controller_status` | Current status: idle, running, inhibited, emergency_run |
| `sensor.pi_inhibit_reason` | Why watering was skipped today |
| `sensor.pi_last_run` | Timestamp of last completed cycle |
| `sensor.pi_next_run` | Scheduled start time for tomorrow |
| `sensor.pi_zone_N_moisture` | Last moisture reading used |
| `sensor.pi_zone_N_setpoint` | Configured target moisture |
| `sensor.pi_zone_N_deficit` | Current deficit (setpoint − moisture) |
| `sensor.pi_zone_N_integral` | Accumulated integral value |
| `sensor.pi_zone_N_last_duration` | Duration of last watering run |
| `sensor.pi_zone_N_temp_factor` | Temperature multiplier applied |
| `sensor.pi_zone_N_status` | Zone status: watered, skipped_wet, disabled, cutoff |
| `sensor.pi_zone_N_recovery_delta` | Moisture change 60 min after watering |

---

## LinkTap Notes

This controller was developed with LinkTap valves and includes specific optimizations:

- **Hardware timer** — sets `number.linktap_zone_N_watering_duration` before opening the valve. If the Pi crashes mid-cycle the valve still closes on schedule.
- **RF air gap** — configurable delay (`overlap_seconds`) between zone transitions prevents dropped commands from the LinkTap gateway radio.
- **Flow cutoff detection** — monitors `binary_sensor.linktap_zone_N_is_watering` and `is_cutoff`, `is_clogged`, `is_leaking` for unexpected shutoffs.

The controller works with any switch entity — LinkTap-specific features gracefully skip if those entities don't exist.

---

## Troubleshooting

**Zones all show 30% moisture** — `require_moisture_sensors: false` and no real sensors. Flip to `true` once sensors are installed.

**Watering never fires** — Check `input_boolean.garden_pi_controller_enabled` is ON and AppDaemon logs for the `cycle_inhibited` event.

**MQTT sensors not appearing** — Verify MQTT integration is set up in HA and AppDaemon has MQTT configured in `appdaemon.yaml`.

**All zones running simultaneously** — Upgrade to this version. Older versions opened all valves at once.

**Recovery check always failing** — Check emitter flow rate matches zone soil type. Increase `recovery_delay_minutes` for dense soil.

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

## Contributing

Issues and pull requests welcome. Please include your AppDaemon logs and apps.yaml (sanitized) when reporting bugs.
