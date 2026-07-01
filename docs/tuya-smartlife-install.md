# Tuya Smart Life setup

This project can expose the Raspberry energy meter to TuyaLink and Smart Life
with the `tuya-run` command.

## Required Tuya product setup

Use a device certificate from the same data center as the Smart Life account.
For an EU/Italy Smart Life account, use Central Europe:

```env
TUYA_DATA_CENTER=eu
TUYA_MQTT_HOST=m1.tuyaeu.com
TUYA_MQTT_PORT=8883
TUYA_PRODUCT_ID=v0hvfnujisazsjrc
TUYA_DEVICE_ID=...
TUYA_DEVICE_SECRET=...
TUYA_REPORT_INTERVAL_SECONDS=20
TUYA_KEEPALIVE_SECONDS=60
TUYA_AVERAGE_SAMPLES=3
TUYA_REPORT_TESLA=true
```

Tesla power is normally published from the latest SQLite measurement produced by
the main web/scheduler service. When `TESLA_DATA_SOURCE=wall-connector`, that
measurement comes from the local Wall Connector API and does not wake the car.
BLE remains a controller channel only: the main service uses it when charging is
active and amp changes are needed.

The China endpoint is `m1.tuyacn.com`, but a China-only device will not bind
cleanly to an EU Smart Life account.

`TUYA_AVERAGE_SAMPLES` controls the moving average shown in Smart Life. With
`TUYA_REPORT_INTERVAL_SECONDS=20` and `TUYA_AVERAGE_SAMPLES=3`, the panel shows
about one minute of averaged watt readings.

Set `TUYA_REPORT_TESLA=false` when `TESLA_DATA_SOURCE=vehicle` and the car is
away or BLE is temporarily unavailable. This only disables the live BLE fallback;
Tuya still publishes a Tesla value when the latest SQLite measurement already
contains one. With `TESLA_DATA_SOURCE=wall-connector`, leave it enabled unless
you explicitly want to hide Tesla power from Smart Life: the value comes from
SQLite/Wall Connector, not from waking the car.

`meter_switch=false` disables the charge controller runtime switch, but the
Tuya bridge stays online and keeps reporting the solar/house meter values.

Tuya reports are built from the latest SQLite measurement when available. This
keeps Smart Life responsive on app open without triggering an extra live poll.

## Manual test

From the project directory:

```bash
PYTHONPATH=src python -m tesla_energy_controller.main tuya-run
```

For a panel/binding test without touching the real integrations:

```bash
PYTHONPATH=src \
ENERGY_SOURCE=mock \
TESLA_MOCK=true \
TESLA_TRANSPORT=mock \
MOCK_SOLAR_POWER_W=5000 \
MOCK_TESLA_CURRENT_A=0 \
python -m tesla_energy_controller.main tuya-run
```

## Raspberry Pi service

Install the project under `/opt/tesla-energy-controller`, create a virtualenv,
install the package, and put the real `.env` file in that directory.

```bash
cd /opt/tesla-energy-controller
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -e .
```

Install and enable the TuyaLink bridge service:

```bash
sudo cp deploy/tesla-energy-controller-tuya.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tesla-energy-controller-tuya.service
```

Useful checks:

```bash
systemctl status tesla-energy-controller-tuya.service
journalctl -u tesla-energy-controller-tuya.service -f
```

To stop the bridge:

```bash
sudo systemctl stop tesla-energy-controller-tuya.service
```

## Smart Life panel notes

The selected `Tuyalink General Panel` is a generated all-in-one panel. It shows
reported DPs as cards and issue/report DPs as controls.

Text changes are configured in Tuya Developer Platform:

`Product Configuration > Multilingual > Data Point`

The panel can show readable labels such as `Solar power`, `House consumption`,
`Tesla power`, and `Meter switch`, but the generic panel has limited control
over button layout and graphics.
