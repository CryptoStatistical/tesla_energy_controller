# Tesla Energy Controller

English version of the project README.

The main documentation is intentionally kept in Italian because this project is built around a
real Italian residential energy setup. In particular, the ALFA by Sinapsi integration used here is
designed for the Italian Enel meter context and should not be treated as a generic international
smart-meter driver.

## Presentation Materials

- [Technical presentation PDF](docs/presentation/tesla-energy-controller-presentation.pdf)
- [Technical presentation HTML](docs/presentation/tesla-energy-controller-presentation.html)
- [Rendered HTML preview](https://htmlpreview.github.io/?https://github.com/CryptoStatistical/tesla_energy_controller/blob/main/docs/presentation/tesla-energy-controller-presentation.html)

## What It Does

Tesla Energy Controller is a Python service that reads:

- photovoltaic production, usually from SolarEdge web/cloud;
- home consumption, optionally from Vimar loads;
- grid import/export, preferably from ALFA by Sinapsi on an Enel meter;
- Tesla charging power, either from Tesla BLE or from a local Tesla Wall Connector Gen 3.

When the Tesla is charging, the controller calculates a target current from the available energy
budget and can update the charging amps. The default mode is `dry-run`: it records the decision but
does not send commands.

The project is meant for a local Raspberry Pi deployment. It serves a local Flask dashboard, stores
measurements in SQLite, and can publish aggregate values to Tuya/Smart Life.

Historical sampling and controller decisions are intentionally kept at a five-minute cadence by
default (`POLL_INTERVAL_SECONDS=300`) to keep SQLite compact. Fast dashboard readings are kept in an
in-memory rolling window; before each historical save, the service applies a five-minute EWMA,
giving more weight to the most recent readings. The dashboard and Tuya can refresh cached status
more frequently without creating extra historical samples.

## Italy-First Note

This repository can be useful as a reference for other countries, but the production setup is
Italy-first:

- ALFA by Sinapsi support is for the Enel meter scenario used in Italy.
- Power quota logic is modeled around the Italian quarter-hour demand concept.
- Dashboard labels and operational assumptions are currently optimized for the original Italian
  installation.

SolarEdge Modbus, Tesla BLE, Tesla Wall Connector, SQLite, Flask, and TuyaLink are more general.
The ALFA/Enel part is the local piece.

## Safety Model

- `MODE=dry-run` is the default.
- The controller never sends a command when source data is missing or invalid.
- It verifies voltage and phase assumptions before controlling charge current.
- Ramp-up is limited; reductions are applied faster.
- With ALFA enabled, stop/start can be used only for the economic power-quota branch.
- This software is an application-level optimizer. It does not replace electrical protections,
  breakers, certified load balancing, or a properly configured charging installation.

## Control Modes

### `solar-production`

Uses instantaneous PV production as the main budget. It does not require a bidirectional grid
meter and does not try to zero grid exchange.

Conceptually:

```text
target_amps = floor((solar_w + extra_grid_w - house_w) / (voltage * phases))
```

At 230 V three-phase, 1 A is about 690 W.

### `grid-surplus`

Uses import/export feedback from a grid meter or compatible SolarEdge meter.

### `meter-closed-loop`

Uses a real meter as authoritative import/export feedback. In this project that meter is ALFA by
Sinapsi for an Enel meter. The controller can react to export, import, and projected quarter-hour
power demand.

With ALFA enabled, the configured power quota is the hard cap. The extra-grid value is a soft target
when solar is available: the controller tries not to import beyond that extra allowance, but it does
not stop the Tesla while the quota still has room. If there is little or no solar, it uses the
remaining quota headroom instead. `MIN_CHARGE_AMPS` is therefore a normal preferred minimum, not an
absolute one in this branch: current can go below it, or charging can be stopped and later restarted,
to stay inside the configured quota.

## SolarEdge Photovoltaic Sources

`ENERGY_SOURCE=solaredge-web` is the recommended primary PV source. It uses the SolarEdge
web/cloud monitoring connector and keeps Modbus TCP optional for local diagnostics or explicit
testing.

If the primary web/cloud connector fails, the dashboard, logs, and email diagnostics mark it
explicitly as a SolarEdge connector failure. That matters because SolarEdge may have changed login
or endpoint behavior, and the web connector may need an update.

`ENERGY_SOURCE=solaredge-modbus` remains available as an optional local source. The official
SolarEdge SunSpec technical note documents one Modbus TCP session and a two-minute TCP idle time.
For that reason, when Modbus is selected, `SOLAREDGE_MODBUS_POLL_INTERVAL_SECONDS` is treated as a
light refresh/keepalive interval, validated between 10 and 110 seconds. The recommended value is
30 seconds.

This is separate from `POLL_INTERVAL_SECONDS=300`: historical SQLite samples and control decisions
remain at the five-minute cadence, while the optional Modbus session is kept alive in memory only
when that source is selected.

When ALFA grid reading is enabled, ALFA is authoritative for import/export. SolarEdge Modbus is then
used only for the inverter PV model, and the SolarEdge meter model is skipped to reduce pressure on
the single Modbus session. Outside the solar window, if ALFA is available, the service avoids polling
SolarEdge Modbus and keeps monitoring grid/house values from ALFA. This guard always uses the
astronomical sunrise/sunset window, even if the charging calendar is configured as fixed time or
00:00-23:59. If the SolarEdge Modbus connection fails during the solar window and SolarEdge web
credentials are configured, the service temporarily uses SolarEdge web for PV production while ALFA
remains authoritative for import/export. Energy flows are normalized so the dashboard does not show
`solar=0` together with positive export.

Operational recommendations:

- prefer wired Ethernet for the inverter; Wi-Fi is often unstable for Modbus TCP;
- make sure no other Home Assistant/Node-RED/script client is polling SolarEdge at the same time;
- if the TCP port remains closed after standby, re-enable Modbus TCP from SetApp or request a firmware update.

## Tesla Power And Control

There are two separate concepts:

- measurement source: where Tesla power comes from;
- control channel: how charging amps are changed.

### Tesla BLE

With:

```dotenv
TESLA_DATA_SOURCE=vehicle
TESLA_TRANSPORT=ble
```

the service reads Tesla charge state via BLE and uses BLE to set charging amps.

### Tesla Wall Connector

With:

```dotenv
TESLA_DATA_SOURCE=wall-connector
WALL_CONNECTOR_HOST=wall-connector-hostname-or-ip
WALL_CONNECTOR_PHASES=3
WALL_CONNECTOR_POLL_INTERVAL_SECONDS=15
WALL_CONNECTOR_TIMEOUT_SECONDS=3
WALL_CONNECTOR_MIN_CURRENT_A=0.3
```

the service reads charging power from the local Wall Connector endpoint:

```text
http://WALL_CONNECTOR_HOST/api/1/vitals
```

The Wall Connector can tell whether a vehicle is connected, whether the contactor is closed, and
how much current is flowing. It does not expose the VIN of the connected car.

BLE remains configured and visible in the dashboard. In Wall Connector mode:

- if the Wall Connector is idle, the service does not query BLE and does not wake the car;
- if the Wall Connector reports real charging/power and the controller is active, the service uses
  BLE to identify/control the configured Tesla;
- if BLE is unavailable during an active Wall Connector session, the dashboard shows measurement
  from the Wall Connector but control as offline.

## Dashboard

The dashboard is served locally by Flask and stores history in SQLite. It shows:

- controller state and solar window;
- PV production;
- import/export;
- house consumption;
- Tesla power;
- target current;
- ALFA power quota information when enabled;
- Wall Connector state and BLE control state;
- event and error logs;
- backup/export and import tools for admins.

The main chart draws the Tesla target as expected total load, `house_w + target_tesla_w`, so it
stacks conceptually on top of house consumption even when the Wall Connector has not yet reported
new Tesla power after a restart.

Dashboard information cards refresh about every 30 seconds and show the current in-memory EWMA
sample rather than raw instantaneous readings. The main chart and the appliances chart are also
exposed as five-minute server-side buckets; if the local database contains denser historical points,
they are merged with the same recency-weighted EWMA logic before being sent to the browser.
The dashboard **Refresh** button only refreshes the cache: it does not write SQLite history and does
not send Tesla commands.

Admin users can change configuration. Viewer users can only inspect the dashboard and toggle the
controller on/off when allowed by the application role model.

## Local Development

```bash
cp .env.example .env
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
tesla-energy-controller once
pytest
```

With mock settings, the expected result is a `dry-run` decision.

## Raspberry Pi Deployment

The project is designed to run under `/opt/tesla-energy-controller` on Raspberry Pi OS 64-bit.
Persistent files are preserved across deploys:

```text
/opt/tesla-energy-controller/
|-- src/tesla_energy_controller/
|-- deploy/
|-- .venv/
|-- .env
|-- .secrets/
`-- data/
```

Typical deploy from a development machine:

```bash
scripts/deploy_raspberry_pi.sh user@raspberry-pi
```

The deploy script runs local tests and lint, syncs code, preserves `.env`, `.secrets`, `data`, and
`.venv`, installs systemd units, and restarts the web and Tuya services.

Useful checks on the Raspberry Pi:

```bash
systemctl status tesla-energy-controller.service tesla-energy-controller-tuya.service
journalctl -u tesla-energy-controller.service -f
curl -fsS http://127.0.0.1:8080/health
```

## Codex-Friendly Workflow

This repository is intentionally Codex-friendly. The file layout, deploy script, Raspberry map, and
verification steps are documented so an agent can do useful operational work without constant
manual guidance.

Example prompt:

```text
Run local tests, deploy to user@raspberry-pi, then verify health, systemd status, and latest logs.
```

Secrets must remain local. Configure real credentials on your own machine or Raspberry Pi and do
not commit `.env`, `.secrets`, private keys, API tokens, or local network details.

## Email Notifications

The project supports event/error notifications through either:

- the WordPress Secure REST Mailer plugin:
  <https://github.com/CryptoStatistical/Wordpress_Secure_REST_Mailer>
- SMTP, when explicitly configured.

Use file-based secrets such as:

```dotenv
NOTIFY_API_KEY_FILE=.secrets/notify_api_key
NOTIFY_API_USER_FILE=.secrets/notify_api_user
SMTP_PASSWORD_FILE=.secrets/smtp_password
```

## Tuya / Smart Life

The Tuya bridge prefers the fresh dashboard status cache and falls back to the latest SQLite
measurement, so opening Smart Life can show recent values without forcing new SolarEdge/Vimar/Tesla
reads. `TUYA_REPORT_INTERVAL_SECONDS=10` keeps the app responsive while the five-minute SQLite
sampling cadence stays unchanged.

Property reports are published with MQTT QoS 1 and Tuya `sys.ack=1`; the bridge subscribes to
`property/report_response`, so cloud-side report failures are visible in the service journal.

When `TESLA_DATA_SOURCE=wall-connector`, Tesla power comes from the Wall Connector measurement and
does not wake the car. When `TESLA_DATA_SOURCE=vehicle`, `TUYA_REPORT_TESLA=false` can be used to
avoid live BLE fallback when the car is away.

## Backup

Admin users can export and import a ZIP backup from the dashboard. The backup can include the
database, runtime configuration, or both. Backups may contain local secrets if configuration files
are included, so store them securely.

## Going Live

Keep `MODE=dry-run` until:

- PV production matches the inverter/dashboard;
- import/export signs are correct;
- ALFA/Enel readings match the meter interface;
- Tesla phase and voltage assumptions are correct;
- dry-run decisions match what you expect.

Only then switch to:

```dotenv
MODE=live
```

and restart the service.
