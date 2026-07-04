from __future__ import annotations

import json
import logging
import math
import sys
import threading
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .backup import BackupService
from .config import ConfigurationError, Settings
from .controller import EnergyController
from .demand import calculate_power_demand
from .diagnostics import ErrorReporter, EventReporter, diagnostic_payload, render_event_report
from .energy import reconcile_energy_flows
from .factories import build_grid_source
from .live_status import write_status_cache
from .runtime import (
    RuntimeSettings,
    RuntimeSettingsError,
    RuntimeSettingsStore,
    parse_anomaly_device_groups,
)
from .schedule import OperatingWindow, anomaly_window, operating_window
from .solar import AlfaModbusSource, SolarEdgeAccessError
from .storage import EnergyDatabase
from .tesla import TeslaBLEError, WallConnectorClient
from .vimar import read_energy_points_from_settings as _default_read_energy_points_from_settings

LOG = logging.getLogger("tesla_energy_controller.web")
SERIES_BUCKET_SECONDS = 300
SERIES_WEIGHT_TAU_SECONDS = SERIES_BUCKET_SECONDS / 4
ROLLING_WEIGHT_TAU_SECONDS = SERIES_BUCKET_SECONDS / 3
SOLAREDGE_MODBUS_CONNECT_GRACE = timedelta(minutes=5)
SOLAREDGE_MODBUS_CONNECT_EVENT = "solaredge_modbus_connect_degraded"
WALL_CONNECTOR_CHARGE_MIN_CURRENT_A = 1.0
WALL_CONNECTOR_CHARGE_MIN_POWER_W = 500.0
TESLA_COMPLETE_BLE_RESUME_CURRENT_A = 3.0
TESLA_COMPLETE_BLE_RESUME_POWER_W = 2000.0
TESLA_NIGHT_POWER_LIMIT_W = 300.0
MANUAL_OVERRIDE_TOLERANCE_A = 0.5


def _read_energy_points_from_settings(settings: Settings):
    legacy_web = sys.modules.get("tesla_energy_controller.web")
    reader = getattr(
        legacy_web,
        "read_energy_points_from_settings",
        _default_read_energy_points_from_settings,
    )
    return reader(settings)


def _safe_text(value, *, limit: int = 240) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\x00", "").strip()
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _safe_name(value, *, fallback: str = "device") -> str:
    text = _safe_text(value, limit=80) or fallback
    return "".join(ch if ch.isalnum() or ch in " ._-/()" else "_" for ch in text)


def _stored_house_power_w(row: dict) -> float:
    stored_house = float(row.get("total_consumption_w") or 0) - float(
        row.get("tesla_power_w") or 0
    )
    return max(stored_house, float(row.get("vimar_power_w") or 0), 0.0)


def _stored_device_power_w(row: dict) -> float:
    return max(_stored_house_power_w(row) - float(row.get("vimar_power_w") or 0), 0.0)


def _stored_power_quota_pause(row: dict) -> bool:
    action = str(row.get("action") or "").casefold()
    reason = str(row.get("reason") or "").casefold()
    return action == "stop" or "tesla sospesa" in reason or "sospensione tesla" in reason


def _stored_power_quota_pause_active(rows: list[dict]) -> bool:
    for row in rows:
        action = str(row.get("action") or "").casefold()
        reason = str(row.get("reason") or "").casefold()
        if _stored_power_quota_pause(row):
            return True
        if action in {"start", "set"}:
            return False
        if "ricarica tesla riavviata" in reason:
            return False
    return False


def _vin_tail(vin: str | None) -> str | None:
    clean = "".join(ch for ch in (vin or "").upper() if ch.isalnum())
    return clean[-6:] if clean else None


def _masked_vin(vin: str | None) -> str:
    clean = "".join(ch for ch in (vin or "").upper() if ch.isalnum())
    if not clean:
        return "—"
    if len(clean) <= 8:
        return clean[0] + "*" * max(len(clean) - 2, 1) + clean[-1]
    return f"{clean[:4]}{'*' * (len(clean) - 8)}{clean[-4:]}"


def _public_path(value: str | None) -> str | None:
    if not value:
        return None
    return Path(value).name or "configurato"


def _sanitize_public_value(value):
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            key_text = _safe_name(key, fallback="field")
            if any(part in key_text.casefold() for part in ("password", "secret", "token", "vin")):
                clean[key_text] = "redatto"
            else:
                clean[key_text] = _sanitize_public_value(item)
        return clean
    if isinstance(value, list):
        return [_sanitize_public_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_public_value(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value, limit=500)
    return value


def _selected_day(days: list[str], requested_day: str | None = None) -> str | None:
    requested = (requested_day or "").strip()
    if requested and requested in days:
        return requested
    return days[-1] if days else None


def _parse_observed_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _series_bucket_start(stamp: datetime) -> datetime:
    seconds = stamp.hour * 3600 + stamp.minute * 60 + stamp.second
    bucket_seconds = seconds - (seconds % SERIES_BUCKET_SECONDS)
    hour, remainder = divmod(bucket_seconds, 3600)
    minute, second = divmod(remainder, 60)
    return stamp.replace(hour=hour, minute=minute, second=second, microsecond=0)


def _series_weight(stamp: datetime, bucket_start: datetime) -> float:
    offset = (stamp - bucket_start).total_seconds()
    offset = max(0.0, min(float(SERIES_BUCKET_SECONDS), offset))
    return math.exp((offset - SERIES_BUCKET_SECONDS) / SERIES_WEIGHT_TAU_SECONDS)


def _rolling_weight(sample_stamp: datetime, observed_at: datetime) -> float:
    age = (observed_at - sample_stamp).total_seconds()
    age = max(0.0, min(float(SERIES_BUCKET_SECONDS), age))
    return math.exp(-age / ROLLING_WEIGHT_TAU_SECONDS)


def _as_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _weighted_samples(values: list[tuple[datetime, float]], bucket_start: datetime) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0][1]

    total = 0.0
    total_weight = 0.0
    for stamp, value in values:
        weight = _series_weight(stamp, bucket_start)
        total += value * weight
        total_weight += weight
    return total / total_weight


def _weighted_value(entries: list[tuple[datetime, dict]], bucket_start: datetime, getter) -> float | None:
    values = []
    for stamp, row in entries:
        value = _as_float(getter(row))
        if value is not None:
            values.append((stamp, value))
    return _weighted_samples(values, bucket_start)


def _series_buckets(rows: list[dict]) -> list[dict]:
    by_key: dict[str, dict] = {}
    for row in rows:
        stamp = _parse_observed_at(row.get("observed_at"))
        if stamp is None:
            continue
        bucket_start = _series_bucket_start(stamp)
        key = bucket_start.isoformat()
        bucket = by_key.get(key)
        if bucket is None:
            bucket = {"start": bucket_start, "entries": []}
            by_key[key] = bucket
        bucket["entries"].append((stamp, row))
    return sorted(by_key.values(), key=lambda item: item["start"])


class LoginLimiter:
    def __init__(self) -> None:
        self.failures: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def allow(self, client: str, authenticated: bool) -> bool:
        now = time.time()
        with self.lock:
            failures = [stamp for stamp in self.failures.get(client, []) if now - stamp < 300]
            self.failures[client] = failures
            if len(failures) >= 5:
                return False
            if not authenticated:
                failures.append(now)
                return False
            self.failures.pop(client, None)
            return True


CONTROL_AVERAGE_WINDOW = timedelta(minutes=5)


class WebRuntime:
    def __init__(self, hard: Settings, controller: EnergyController) -> None:
        self.hard = hard
        self.controller = controller
        self.store = RuntimeSettingsStore(hard.runtime_settings_file, hard)
        self.current = self.store.load()
        self._active_solar_source = ""
        self._active_expected_phases = 0
        self._solar_source_started_at = datetime.now(timezone.utc).astimezone()
        self._apply_runtime_settings(self.current)
        self.alfa_grid = None
        if hard.energy_source != "alfa-modbus" and hard.alfa_modbus_host:
            self.alfa_grid = AlfaModbusSource(
                hard.alfa_modbus_host,
                hard.alfa_modbus_port,
                hard.alfa_modbus_unit,
                hard.alfa_modbus_timeout_seconds,
            )
        self.wall_connector = None
        if hard.wall_connector_host:
            self.wall_connector = WallConnectorClient(
                hard.wall_connector_host,
                timeout_seconds=hard.wall_connector_timeout_seconds,
                phases=self.current.expected_phases,
                min_current_a=hard.wall_connector_min_current_a,
                minimum_interval_seconds=hard.wall_connector_poll_interval_seconds,
            )
        self.db = EnergyDatabase(hard.energy_database_file)
        latest_measurement = self.db.latest_measurements(10)
        if (
            self.current.alfa_grid_reading_enabled
            and latest_measurement
            and _stored_power_quota_pause_active(latest_measurement)
        ):
            self.controller.restore_power_quota_pause()
        self.db.ensure_user(hard.web_username, hard.web_password, "admin")
        self.db.ensure_user(hard.web_viewer_username, hard.web_viewer_password, "viewer")
        self.reporter = ErrorReporter.from_settings(hard)
        self.event_reporter = EventReporter.from_settings(hard)
        self.refresh_mail_recipients()
        self.lock = threading.Lock()
        self.run_now_lock = threading.Lock()
        self.run_now_running = False
        self.run_now_pending = False
        self.stop_event = threading.Event()
        self.active_events: set[str] = set()
        self._monthly_peak_cache: tuple[str, float] | None = None
        self._rolling_samples: list[tuple[datetime, dict, list[dict]]] = []
        self._solaredge_modbus_down_since: datetime | None = None
        self._last_appliances: list[dict] = []
        self._vimar_retry_after: datetime | None = None
        self._tesla_complete_ble_standby = False
        self.last_status: dict = (
            self._status_from_measurement(latest_measurement[0])
            if latest_measurement
            else {
                "state": "waiting",
                "message": "In attesa della prima verifica",
                "updated_at": None,
            }
        )

    def _status_from_measurement(self, row: dict) -> dict:
        house_power_w = _stored_house_power_w(row)
        return {
            "state": "cached",
            "message": row.get("reason") or "Ultima misura salvata",
            "updated_at": row.get("observed_at"),
            "controller_enabled": bool(row.get("controller_enabled")),
            "solar_source": self.current.solar_source,
            "solar_power_w": row.get("solar_power_w"),
            "vimar_power_w": row.get("vimar_power_w"),
            "appliances_power_w": row.get("vimar_power_w"),
            "device_power_w": _stored_device_power_w(row),
            "house_power_w": house_power_w,
            "tesla_power_w": row.get("tesla_power_w"),
            "tesla_power_source": "stored",
            "tesla_data_source": self.hard.tesla_data_source,
            "tesla_ble_connected": None,
            "tesla_ble_control_required": False,
            "tesla_ble_control_state": "cached",
            "tesla_ble_control_message": "Stato Bluetooth non disponibile dalla cache",
            "total_consumption_w": row.get("total_consumption_w"),
            "import_power_w": row.get("import_power_w"),
            "export_power_w": row.get("export_power_w"),
            "tesla_current_a": row.get("tesla_current_a"),
            "target_a": row.get("tesla_target_a"),
            "action": row.get("action"),
            "alfa_grid_reading_enabled": bool(row.get("alfa_grid_reading_enabled")),
        }

    def _solar_grid_for(self, source: str, expected_phases: int):
        try:
            return build_grid_source(
                self.hard,
                source,
                expected_phases=expected_phases,
            )
        except ConfigurationError as exc:
            raise RuntimeSettingsError(str(exc)) from exc

    def _apply_runtime_settings(self, settings: RuntimeSettings) -> None:
        if (
            settings.solar_source != self._active_solar_source
            or settings.expected_phases != self._active_expected_phases
        ):
            self.controller.grid = self._solar_grid_for(
                settings.solar_source,
                settings.expected_phases,
            )
            self._active_solar_source = settings.solar_source
            self._active_expected_phases = settings.expected_phases
            self._solar_source_started_at = datetime.now(timezone.utc).astimezone()
        settings.apply(self.controller)
        if getattr(self, "wall_connector", None) is not None:
            self.wall_connector.phases = settings.expected_phases

    def control_interval_seconds(self) -> int:
        # Decisioni controller e campioni SQLite restano volutamente lenti:
        # refresh dashboard/Tuya possono leggere lo stato in cache piu' spesso,
        # ma non devono moltiplicare storico e comandi.
        return self.hard.poll_interval_seconds

    def preview_interval_seconds(self) -> int:
        return min(self.hard.alfa_control_interval_seconds, self.control_interval_seconds())

    def window(self, now: datetime | None = None) -> OperatingWindow:
        return operating_window(self.current, self.hard, now)

    def authenticate(self, username: str, password: str) -> dict | None:
        return self.db.authenticate(username, password)

    def mail_recipients(self) -> tuple[str, ...]:
        recipients = [*self.hard.notify_recipients, *self.db.admin_emails()]
        unique = []
        seen = set()
        for email in recipients:
            clean = _safe_text(email, limit=180)
            if clean and clean.casefold() not in seen:
                seen.add(clean.casefold())
                unique.append(clean)
        return tuple(unique)

    def refresh_mail_recipients(self) -> None:
        recipients = self.mail_recipients()
        self.reporter.sender.recipients = recipients
        self.event_reporter.sender.recipients = recipients
        self.reporter.enabled = bool(self.current.error_email_enabled)
        self.reporter.solar_only = False

    def reload_settings(self) -> None:
        try:
            disk_settings = self.store.load()
        except RuntimeSettingsError:
            LOG.exception("lettura impostazioni runtime fallita")
            return
        if disk_settings != self.current:
            self.current = disk_settings
            self._apply_runtime_settings(self.current)

    def _publish_status_cache(self) -> None:
        try:
            write_status_cache(self.hard, self.last_status)
        except Exception:
            LOG.exception("scrittura cache stato dashboard fallita")

    def _defer_solaredge_modbus_connect_error(
        self,
        exc: Exception,
        cycle_time: datetime,
        window_data: dict,
    ) -> bool:
        if not (
            isinstance(exc, SolarEdgeAccessError)
            and exc.phase == "modbus-connect"
            and self.current.solar_source == "solaredge-modbus"
        ):
            return False
        payload = self._mark_solaredge_modbus_connect_degraded(exc, cycle_time)
        cached = dict(self.last_status or {})
        cached.update(window_data)
        cached.update(
            {
                "state": "degraded" if cached.get("updated_at") else "waiting",
                "message": "SolarEdge Modbus non disponibile; ritento e uso ultimo campione",
                "debug": payload,
                "email_report": "mail non inviata: debounce Modbus SolarEdge",
                "solar_source": self.current.solar_source,
            }
        )
        self.last_status = cached
        self._publish_status_cache()
        return True

    def _mark_solaredge_modbus_connect_degraded(
        self,
        exc: Exception,
        cycle_time: datetime,
    ) -> dict:
        now = cycle_time.astimezone()
        if self._solaredge_modbus_down_since is None:
            self._solaredge_modbus_down_since = now
        elapsed = now - self._solaredge_modbus_down_since
        payload = diagnostic_payload(exc)
        self._threshold_event(
            SOLAREDGE_MODBUS_CONNECT_EVENT,
            True,
            "SolarEdge Modbus temporaneamente non disponibile",
            "warning",
            {
                "diagnostic": payload,
                "elapsed_seconds": round(elapsed.total_seconds()),
                "grace_seconds": round(SOLAREDGE_MODBUS_CONNECT_GRACE.total_seconds()),
                "retry": "continuo",
            },
            mail_enabled=False,
        )
        return payload

    def _recover_solaredge_modbus_connect(self) -> None:
        if self._solaredge_modbus_down_since is None:
            return
        started_at = self._solaredge_modbus_down_since
        self._solaredge_modbus_down_since = None
        self._threshold_event(
            SOLAREDGE_MODBUS_CONNECT_EVENT,
            False,
            "SolarEdge Modbus temporaneamente non disponibile",
            "info",
            {
                "duration_seconds": round(
                    (datetime.now(timezone.utc).astimezone() - started_at).total_seconds()
                )
            },
            mail_enabled=False,
        )

    @staticmethod
    def _tesla_power_w(car) -> float:
        return car.charging_power_w

    def _read_appliances(self) -> list[dict]:
        now = datetime.now(timezone.utc).astimezone()
        if self._vimar_retry_after is not None and now < self._vimar_retry_after:
            return [dict(item) for item in self._last_appliances]
        try:
            appliances = [
                {"name": _safe_name(point.name or str(point.idsf)), "power_w": point.power_w}
                for point in _read_energy_points_from_settings(self.hard)
                if point.power_w is not None
            ]
        except Exception as exc:
            LOG.warning("lettura Vimar non disponibile, uso ultima cache: %s", exc)
            self._vimar_retry_after = now + timedelta(minutes=2)
            self._threshold_event(
                "vimar_unreachable",
                True,
                "Vimar non raggiungibile",
                "warning",
                {
                    "component": "vimar",
                    "source": "vimar",
                    "exception_type": exc.__class__.__name__,
                    "message": str(exc),
                    "retry_after": self._vimar_retry_after.isoformat(),
                    "cached_appliances": len(self._last_appliances),
                },
                mail_enabled=False,
            )
            return [dict(item) for item in self._last_appliances]
        self._vimar_retry_after = None
        self._last_appliances = appliances
        self._threshold_event(
            "vimar_unreachable",
            False,
            "Vimar non raggiungibile",
            "info",
            {"component": "vimar"},
            mail_enabled=False,
        )
        return [dict(item) for item in appliances]

    def _control_measurement(
        self,
        measurement,
        energy: dict,
        stamp: str,
    ) -> tuple:
        observed_at = datetime.fromisoformat(stamp)
        start = observed_at - CONTROL_AVERAGE_WINDOW
        samples = [
            sample
            for sample_stamp, sample, _appliances in self._rolling_samples
            if start <= sample_stamp <= observed_at
        ]

        def average(key: str) -> float | None:
            total = 0.0
            total_weight = 0.0
            for item in samples:
                value = _as_float(item.get(key))
                sample_stamp = _parse_observed_at(item.get("observed_at"))
                if value is None or sample_stamp is None:
                    continue
                weight = _rolling_weight(sample_stamp, observed_at)
                total += value * weight
                total_weight += weight
            return total / total_weight if total_weight > 0 else None

        solar_power_w = average("solar_power_w")
        import_power_w = average("import_power_w")
        export_power_w = average("export_power_w")
        if import_power_w is None or export_power_w is None:
            return measurement, {"control_sample_count": len(samples)}

        total_power_w = import_power_w - export_power_w
        total_consumption_w = average("total_consumption_w")
        if solar_power_w is not None and energy.get("alfa_grid_reading_enabled"):
            total_consumption_w = max(solar_power_w + total_power_w, 0.0)
        return replace(
            measurement,
            total_power_w=total_power_w,
            solar_power_w=solar_power_w,
            import_power_w=import_power_w,
            export_power_w=export_power_w,
            total_consumption_w=total_consumption_w,
        ), {
            "control_sample_count": len(samples),
            "control_average_window_seconds": int(CONTROL_AVERAGE_WINDOW.total_seconds()),
            "control_average_method": "ewma",
            "control_smoothed": True,
            "control_solar_power_w": solar_power_w,
            "control_import_power_w": import_power_w,
            "control_export_power_w": export_power_w,
            "control_tesla_power_w": average("tesla_power_w"),
            "control_vimar_power_w": average("vimar_power_w"),
            "control_total_consumption_w": total_consumption_w,
        }

    def _record_rolling_sample(
        self,
        observed_at: datetime,
        energy: dict,
        appliances: list[dict],
    ) -> None:
        sample = {
            "observed_at": observed_at.isoformat(),
            "solar_power_w": energy.get("solar_power_w"),
            "import_power_w": energy.get("import_power_w"),
            "export_power_w": energy.get("export_power_w"),
            "tesla_power_w": energy.get("tesla_power_w"),
            "vimar_power_w": energy.get("vimar_power_w"),
            "total_consumption_w": energy.get("total_consumption_w"),
        }
        appliance_samples = [
            {"name": item.get("name", ""), "power_w": item.get("power_w")}
            for item in appliances
        ]
        self._rolling_samples.append((observed_at, sample, appliance_samples))
        cutoff = observed_at - CONTROL_AVERAGE_WINDOW
        self._rolling_samples = [
            item for item in self._rolling_samples if item[0] >= cutoff
        ]

    def _rolling_appliances(self, observed_at: datetime) -> list[dict]:
        totals: dict[str, list[float]] = {}
        start = observed_at - CONTROL_AVERAGE_WINDOW
        for sample_stamp, _sample, appliances in self._rolling_samples:
            if not start <= sample_stamp <= observed_at:
                continue
            weight = _rolling_weight(sample_stamp, observed_at)
            for item in appliances:
                value = _as_float(item.get("power_w"))
                if value is None:
                    continue
                name = _safe_name(item.get("name"), fallback="device")
                total = totals.setdefault(name, [0.0, 0.0])
                total[0] += value * weight
                total[1] += weight
        return [
            {"name": name, "power_w": total / weight}
            for name, (total, weight) in sorted(totals.items())
            if weight > 0
        ]

    def _preview_decision_data(self, action: str, energy: dict, window_active: bool) -> dict:
        data = {"action": action}
        if not energy.get("tesla_connected"):
            data["target_a"] = 0
            return data
        if not self.current.enabled:
            return data
        target_a = self.last_status.get("target_a")
        if target_a is not None:
            data["target_a"] = target_a
            return data
        if (
            action in {"outside-window", "preview"}
            and not window_active
            and self._wall_connector_charge_active(energy)
        ):
            data["target_a"] = self.current.min_charge_amps
            return data
        return data

    def _manual_override_current_a(self, status: dict) -> float | None:
        threshold = float(self.current.manual_override_amps)
        values = [
            _as_float(status.get("current_a")),
            _as_float(status.get("tesla_current_a")),
            _as_float(status.get("actual_current_a")),
        ]
        current_a = max((value for value in values if value is not None), default=None)
        if current_a is None or current_a < threshold - MANUAL_OVERRIDE_TOLERANCE_A:
            return None
        return current_a

    def _annotate_manual_override_status(self, status: dict) -> None:
        override_a = self._manual_override_current_a(status)
        active = override_a is not None
        status["manual_override_active"] = active
        if not active:
            return
        display_a = int(round(override_a))
        status["manual_override_a"] = display_a
        status["target_a"] = display_a
        status["action"] = "manual-override"
        status["state"] = "manual-override"
        status["message"] = f"override manuale: Tesla impostata a {display_a} A"

    @staticmethod
    def _wall_connector_charge_active(energy: dict) -> bool:
        if not energy.get("tesla_connected"):
            return False
        if bool(energy.get("wall_connector_contactor_closed")):
            return True
        current_a = _as_float(energy.get("actual_current_a"))
        if current_a is None:
            current_a = _as_float(energy.get("tesla_current_a"))
        power_w = _as_float(energy.get("tesla_power_w"))
        return (
            (current_a is not None and current_a >= WALL_CONNECTOR_CHARGE_MIN_CURRENT_A)
            or (power_w is not None and power_w >= WALL_CONNECTOR_CHARGE_MIN_POWER_W)
        )

    def _complete_ble_standby_active(self, wall_vitals) -> bool:
        if not self._tesla_complete_ble_standby:
            return False
        if not (wall_vitals.vehicle_connected or wall_vitals.contactor_closed):
            self._tesla_complete_ble_standby = False
            return False
        if (
            wall_vitals.vehicle_current_a >= TESLA_COMPLETE_BLE_RESUME_CURRENT_A
            or wall_vitals.power_w >= TESLA_COMPLETE_BLE_RESUME_POWER_W
        ):
            self._tesla_complete_ble_standby = False
            return False
        return True

    def _remember_ble_charge_state(self, car) -> None:
        charging_state = str(getattr(car, "charging_state", "") or "").casefold()
        if charging_state == "complete":
            self._tesla_complete_ble_standby = True
        elif getattr(car, "is_charging", False):
            self._tesla_complete_ble_standby = False

    def _alfa_meter_measurement(self, primary):
        if primary.source == "alfa-modbus":
            return primary
        if self.alfa_grid is None:
            raise ConfigurationError(
                "ALFA_MODBUS_HOST è obbligatorio quando la lettura rete ALFA è attiva"
            )

        meter = self.alfa_grid.read()
        return replace(
            primary,
            total_power_w=meter.total_power_w,
            import_power_w=meter.import_power_w,
            export_power_w=meter.export_power_w,
            imported_energy_wh=meter.imported_energy_wh,
            exported_energy_wh=meter.exported_energy_wh,
            quarter_hour_import_power_w=meter.quarter_hour_import_power_w,
            quarter_hour_export_power_w=meter.quarter_hour_export_power_w,
            alfa_power_limit_remaining_seconds=meter.alfa_power_limit_remaining_seconds,
            alfa_current_tariff=meter.alfa_current_tariff,
            alfa_event_timestamp_raw=meter.alfa_event_timestamp_raw,
            imported_energy_by_tariff_wh=meter.imported_energy_by_tariff_wh,
            exported_energy_by_tariff_wh=meter.exported_energy_by_tariff_wh,
            source=f"{primary.source}+alfa-modbus",
        )

    def _read_snapshot(self, stamp: str, *, wants_ble_control: bool = True) -> tuple:
        # FV/casa/rete non dipendono dall'auto: leggiamoli sempre, per primi.
        solar_source_degraded = False
        solar_source_error = None
        try:
            measurement = self.controller.grid.read()
        except SolarEdgeAccessError as exc:
            if not (
                exc.phase == "modbus-connect"
                and self.current.solar_source == "solaredge-modbus"
                and self.current.alfa_grid_reading_enabled
                and self.alfa_grid is not None
            ):
                raise
            LOG.warning(
                "SolarEdge Modbus non disponibile, uso temporaneamente ALFA: %s",
                exc,
            )
            solar_source_error = self._mark_solaredge_modbus_connect_degraded(
                exc,
                datetime.fromisoformat(stamp),
            )
            measurement = self.alfa_grid.read()
            solar_source_degraded = True
        appliances = self._read_appliances()
        vimar_power_w = sum(max(float(item["power_w"] or 0), 0.0) for item in appliances)
        car = None
        wall_vitals = None
        tesla_power_source = self.hard.tesla_data_source
        tesla_ble_control_required = False
        tesla_ble_control_state = "not-needed"
        tesla_ble_control_message = "Bluetooth non interrogato"
        if self.hard.tesla_data_source == "wall-connector":
            if self.wall_connector is None:
                raise ConfigurationError(
                    "WALL_CONNECTOR_HOST è obbligatorio con TESLA_DATA_SOURCE=wall-connector"
                )
            try:
                wall_vitals = self.wall_connector.read_vitals()
            except Exception as exc:
                LOG.warning("Wall Connector non raggiungibile, Tesla a 0 W: %s", exc)
                self._threshold_event(
                    "wall_connector_unreachable",
                    True,
                    "Wall Connector non raggiungibile",
                    "warning",
                    {"component": "wall_connector", "message": str(exc)},
                    mail_enabled=False,
                )
                tesla_power_w = 0.0
                tesla_power_source = "wall-connector-unavailable"
            else:
                tesla_power_w = wall_vitals.power_w
                quota_resume_pending = bool(
                    getattr(self.controller, "_paused_for_power_quota", False)
                )
                wall_charge_active = (
                    wall_vitals.contactor_closed
                    or wall_vitals.vehicle_current_a >= WALL_CONNECTOR_CHARGE_MIN_CURRENT_A
                    or wall_vitals.power_w >= WALL_CONNECTOR_CHARGE_MIN_POWER_W
                )
                complete_ble_standby = self._complete_ble_standby_active(wall_vitals)
                if complete_ble_standby:
                    tesla_ble_control_required = False
                    tesla_ble_control_state = "standby"
                    tesla_ble_control_message = "Bluetooth in standby dopo carica completa"
                else:
                    tesla_ble_control_required = wants_ble_control and (
                        wall_charge_active
                        or quota_resume_pending
                    )
                self._threshold_event(
                    "wall_connector_unreachable",
                    False,
                    "Wall Connector non raggiungibile",
                    "info",
                    {"component": "wall_connector"},
                    mail_enabled=False,
                )
                if tesla_ble_control_required:
                    try:
                        car = self.controller.vehicle.get_charge_state()
                    except TeslaBLEError as exc:
                        LOG.warning(
                            "Bluetooth Tesla non raggiungibile per controllo ricarica: %s",
                            exc,
                        )
                        self._threshold_event(
                            "tesla_ble_unreachable",
                            True,
                            "Tesla non raggiungibile via BLE",
                            "info",
                            {
                                "component": "tesla_ble",
                                "category": str(getattr(exc, "category", "") or ""),
                                "retryable": getattr(exc, "retryable", None),
                                "message": str(exc),
                            },
                        )
                        tesla_ble_control_state = "unreachable"
                        tesla_ble_control_message = "Bluetooth non raggiungibile per controllo"
                    else:
                        self._threshold_event(
                            "tesla_ble_unreachable",
                            False,
                            "Tesla non raggiungibile via BLE",
                            "info",
                            {"component": "tesla_ble"},
                        )
                        tesla_ble_control_state = "connected"
                        tesla_ble_control_message = "Bluetooth pronto per controllo"
                        self._remember_ble_charge_state(car)
        else:
            # La lettura della Tesla via BLE è opzionale: se l'auto è addormentata o
            # fuori portata mostriamo comunque l'energia, senza i dati dell'auto.
            tesla_ble_control_required = wants_ble_control
            try:
                car = self.controller.vehicle.get_charge_state()
            except TeslaBLEError as exc:
                LOG.warning(
                    "Tesla non raggiungibile via BLE, monitoraggio energia comunque attivo: %s",
                    exc,
                )
                self._threshold_event(
                    "tesla_ble_unreachable",
                    True,
                    "Tesla non raggiungibile via BLE",
                    "info",
                    {
                        "component": "tesla_ble",
                        "category": str(getattr(exc, "category", "") or ""),
                        "retryable": getattr(exc, "retryable", None),
                        "message": str(exc),
                    },
                )
                car = None
                tesla_ble_control_state = "unreachable"
                tesla_ble_control_message = "Bluetooth non raggiungibile"
            else:
                self._threshold_event(
                    "tesla_ble_unreachable",
                    False,
                    "Tesla non raggiungibile via BLE",
                    "info",
                    {"component": "tesla_ble"},
                )
                tesla_ble_control_state = "connected"
                tesla_ble_control_message = "Bluetooth connesso"
                self._remember_ble_charge_state(car)
            tesla_power_w = self._tesla_power_w(car) if car is not None else 0.0
        solar_power_w = measurement.solar_power_w
        if solar_power_w is None and measurement.source == "solaredge-web":
            # L'endpoint privato SolarEdge può restituire solo il ramo GRID.
            # Con rete firmata positiva in import: FV = carichi - rete.
            solar_power_w = max(
                vimar_power_w + tesla_power_w - measurement.total_power_w,
                0.0,
            )
            measurement = replace(measurement, solar_power_w=solar_power_w)
        use_alfa_grid = False
        if self.current.alfa_grid_reading_enabled:
            try:
                measurement = self._alfa_meter_measurement(measurement)
            except Exception as exc:
                LOG.warning("Lettura ALFA non disponibile, uso stima energia: %s", exc)
                self._threshold_event(
                    "alfa_grid_unreachable",
                    True,
                    "Contatore ALFA non raggiungibile",
                    "warning",
                    {
                        "component": "alfa_modbus",
                        "message": str(exc),
                        "solar_source": self.current.solar_source,
                    },
                    mail_enabled=False,
                )
            else:
                use_alfa_grid = True
                solar_power_w = measurement.solar_power_w
                self._threshold_event(
                    "alfa_grid_unreachable",
                    False,
                    "Contatore ALFA non raggiungibile",
                    "info",
                    {"component": "alfa_modbus"},
                    mail_enabled=False,
                )
        breakdown = reconcile_energy_flows(
            solar_power_w=solar_power_w,
            appliances_power_w=vimar_power_w,
            tesla_power_w=tesla_power_w,
            import_power_w=measurement.import_power_w if use_alfa_grid else None,
            export_power_w=measurement.export_power_w if use_alfa_grid else None,
        )
        energy = {
            "observed_at": stamp,
            "solar_source": self.current.solar_source,
            "energy_source": measurement.source,
            "solar_source_degraded": solar_source_degraded,
            "solar_source_error": solar_source_error,
            "solar_power_w": solar_power_w,
            "grid_power_w": measurement.total_power_w,
            "vimar_power_w": vimar_power_w,
            "appliances_power_w": breakdown.appliances_power_w,
            "device_power_w": breakdown.device_power_w,
            "house_power_w": breakdown.house_power_w,
            "estimated_import_power_w": breakdown.estimated_import_power_w,
            "estimated_export_power_w": breakdown.estimated_export_power_w,
            "meter_balance_available": breakdown.meter_balance_available,
            "alfa_grid_reading_enabled": use_alfa_grid,
            "tesla_power_w": tesla_power_w,
            "tesla_power_source": tesla_power_source,
            "tesla_data_source": self.hard.tesla_data_source,
            "tesla_ble_connected": car is not None,
            "tesla_ble_control_required": tesla_ble_control_required,
            "tesla_ble_control_state": tesla_ble_control_state,
            "tesla_ble_control_message": tesla_ble_control_message,
            "total_consumption_w": breakdown.total_consumption_w,
            "export_power_w": breakdown.export_power_w,
            "import_power_w": breakdown.import_power_w,
            "imported_energy_wh": measurement.imported_energy_wh,
            "exported_energy_wh": measurement.exported_energy_wh,
            "quarter_hour_import_power_w": measurement.quarter_hour_import_power_w,
            "quarter_hour_export_power_w": measurement.quarter_hour_export_power_w,
            "alfa_power_limit_remaining_seconds": (
                measurement.alfa_power_limit_remaining_seconds
            ),
            "alfa_current_tariff": measurement.alfa_current_tariff,
            "alfa_event_timestamp_raw": measurement.alfa_event_timestamp_raw,
            "imported_energy_by_tariff_wh": measurement.imported_energy_by_tariff_wh,
            "exported_energy_by_tariff_wh": measurement.exported_energy_by_tariff_wh,
            "tesla_current_a": (
                wall_vitals.vehicle_current_a
                if wall_vitals is not None and wall_vitals.power_w > 0
                else (car.current_request_a if car and car.is_charging else None)
            ),
            "voltage_v": wall_vitals.grid_v if wall_vitals is not None else (car.voltage_v if car else None),
            "actual_current_a": (
                wall_vitals.vehicle_current_a
                if wall_vitals is not None and wall_vitals.power_w > 0
                else (car.actual_current_a if car else None)
            ),
            "charger_power_kw": (
                tesla_power_w / 1000.0 if wall_vitals is not None else (car.charger_power_kw if car else None)
            ),
            "tesla_connected": (
                bool(wall_vitals.vehicle_connected or wall_vitals.contactor_closed)
                if wall_vitals is not None
                else car is not None
            ),
            "wall_connector_vehicle_connected": (
                wall_vitals.vehicle_connected if wall_vitals is not None else None
            ),
            "wall_connector_contactor_closed": (
                wall_vitals.contactor_closed if wall_vitals is not None else None
            ),
            "wall_connector_evse_state": wall_vitals.evse_state if wall_vitals is not None else None,
        }
        return car, measurement, energy, appliances

    def tesla_ble_status(self) -> dict:
        vehicle = self.controller.vehicle
        key_file = getattr(vehicle, "key_file", self.hard.tesla_ble_key_file)
        return {
            "transport": self.hard.tesla_transport,
            "vin_tail": _vin_tail(self.hard.tesla_vin),
            "binary": _public_path(getattr(vehicle, "binary", self.hard.tesla_control_binary)),
            "adapter": getattr(vehicle, "bt_adapter", self.hard.tesla_ble_adapter) or "auto",
            "key_configured": bool(key_file),
            "cache_configured": bool(getattr(vehicle, "cache_file", self.hard.tesla_ble_cache_file)),
            "require_time_sync": getattr(
                vehicle, "require_time_sync", self.hard.tesla_ble_require_time_sync
            ),
            "preflight_sleep_check": getattr(
                vehicle, "preflight_sleep_check", self.hard.tesla_ble_preflight_sleep_check
            ),
            "connect_timeout_seconds": self.current.tesla_ble_connect_timeout_seconds,
            "command_timeout_seconds": self.current.tesla_ble_command_timeout_seconds,
            "retries": self.current.tesla_ble_retries,
            "recovery_enabled": self.current.tesla_ble_recovery_enabled,
        }

    def public_config(self) -> dict:
        control_interval_seconds = self.control_interval_seconds()
        return {
            "mode": self.hard.mode,
            "control_mode": self.hard.control_mode,
            "energy_source": self.hard.energy_source,
            "solar_source": self.current.solar_source,
            "poll_interval_seconds": control_interval_seconds,
            "expected_phases": self.current.expected_phases,
            "max_charge_amps": self.hard.max_charge_amps,
            "tesla_transport": self.hard.tesla_transport,
            "tesla_data_source": self.hard.tesla_data_source,
            "wall_connector_configured": bool(self.hard.wall_connector_host),
            "tesla_vin_tail": _vin_tail(self.hard.tesla_vin),
            "solar_timezone": self.hard.solar_timezone,
            "web_port": self.hard.web_port,
        }

    def run_cycle(
        self,
        now: datetime | None = None,
        *,
        force_measurement: bool = False,
        persist: bool = True,
        control: bool = True,
    ) -> dict:
        with self.lock:
            self.reload_settings()
            cycle_time = now or datetime.now(timezone.utc).astimezone()
            if cycle_time.tzinfo is None:
                cycle_time = cycle_time.astimezone()
            stamp = cycle_time.astimezone().isoformat()
            try:
                window = self.window(now)
            except Exception as exc:
                self.last_status = self._error(exc)
                self._record_error(
                    exc,
                    context={"component": "web-window"},
                    email_report="non tentata",
                )
                self._publish_status_cache()
                return dict(self.last_status)

            window_data = {
                "window_active": window.active,
                "window_label": window.label,
                "window_mode": window.mode,
            }
            try:
                car, measurement, energy, appliances = self._read_snapshot(
                    stamp,
                    wants_ble_control=self.current.enabled
                    and (window.active or self.hard.tesla_data_source == "wall-connector"),
                )
            except Exception as exc:
                if self._defer_solaredge_modbus_connect_error(exc, cycle_time, window_data):
                    return dict(self.last_status)
                LOG.exception("monitoraggio energia fallito")
                self.last_status = {**self._error(exc), **window_data}
                self.refresh_mail_recipients()
                mail_result = self.reporter.notify(
                    exc,
                    context={
                        "component": "web-monitor",
                        "solar_source": self.current.solar_source,
                        "control_mode": self.hard.control_mode,
                    },
                )
                self.last_status["email_report"] = mail_result.message
                self._record_error(
                    exc,
                    context={
                        "component": "web-monitor",
                        "solar_source": self.current.solar_source,
                        "control_mode": self.hard.control_mode,
                    },
                    email_report=mail_result.message,
                )
                self._publish_status_cache()
                return dict(self.last_status)
            if not energy.get("solar_source_degraded"):
                self._recover_solaredge_modbus_connect()
            if force_measurement and not measurement.fresh:
                measurement = replace(measurement, fresh=True)

            self._apply_runtime_settings(self.current)
            observed_at = datetime.fromisoformat(stamp)
            self._record_rolling_sample(observed_at, energy, appliances)
            control_measurement, control_energy = self._control_measurement(
                measurement, energy, stamp
            )
            energy.update(control_energy)
            if control_energy.get("control_smoothed"):
                for source_key, target_key in (
                    ("control_solar_power_w", "solar_power_w"),
                    ("control_import_power_w", "import_power_w"),
                    ("control_export_power_w", "export_power_w"),
                    ("control_tesla_power_w", "tesla_power_w"),
                    ("control_vimar_power_w", "vimar_power_w"),
                    ("control_vimar_power_w", "appliances_power_w"),
                    ("control_total_consumption_w", "total_consumption_w"),
                ):
                    if control_energy.get(source_key) is not None:
                        energy[target_key] = control_energy[source_key]
                breakdown = reconcile_energy_flows(
                    solar_power_w=energy.get("solar_power_w"),
                    appliances_power_w=energy.get("appliances_power_w", 0.0),
                    tesla_power_w=energy.get("tesla_power_w", 0.0),
                    import_power_w=(
                        energy.get("import_power_w")
                        if energy.get("alfa_grid_reading_enabled")
                        else None
                    ),
                    export_power_w=(
                        energy.get("export_power_w")
                        if energy.get("alfa_grid_reading_enabled")
                        else None
                    ),
                )
                energy.update(
                    {
                        "appliances_power_w": breakdown.appliances_power_w,
                        "device_power_w": breakdown.device_power_w,
                        "house_power_w": breakdown.house_power_w,
                        "estimated_import_power_w": breakdown.estimated_import_power_w,
                        "estimated_export_power_w": breakdown.estimated_export_power_w,
                        "meter_balance_available": breakdown.meter_balance_available,
                        "total_consumption_w": breakdown.total_consumption_w,
                        "export_power_w": breakdown.export_power_w,
                        "import_power_w": breakdown.import_power_w,
                    }
                )
                rolling_appliances = self._rolling_appliances(observed_at)
                if rolling_appliances:
                    appliances = rolling_appliances

            demand = None
            effective_power_quota_w = None
            meter_available = bool(energy.get("alfa_grid_reading_enabled"))
            if meter_available:
                demand = calculate_power_demand(
                    self.db.import_samples_for_quarter(stamp),
                    observed_at=stamp,
                    import_power_w=energy["import_power_w"],
                )
                effective_power_quota_w = self.current.power_quota_target_w
                energy.update(
                    {
                        "quarter_hour_import_w": demand.completed_average_w,
                        "sampled_quarter_hour_import_w": demand.sampled_average_w,
                        "projected_quarter_hour_import_w": demand.projected_average_w,
                        "power_quota_limit_w": effective_power_quota_w,
                        "power_quota_sample_count": demand.sample_count,
                    }
                )
            outside_window_wall_control = (
                control
                and self.current.enabled
                and not window.active
                and self.hard.tesla_data_source == "wall-connector"
                and self._wall_connector_charge_active(energy)
            )
            decision = None
            try:
                if not control:
                    state = "preview"
                    message = (
                        "Dashboard aggiornata: "
                        f"campionamento {self.control_interval_seconds()}s, "
                        f"refresh {self.preview_interval_seconds()}s"
                    )
                    action = "preview"
                elif not self.current.enabled:
                    state = "disabled"
                    message = "Controller ricarica disabilitato dal pannello"
                    action = "disabled"
                elif not window.active and not outside_window_wall_control:
                    state = "outside-window"
                    message = f"Fuori dalla finestra solare {window.label}"
                    action = "outside-window"
                elif car is None:
                    if self.hard.tesla_data_source == "wall-connector":
                        if energy.get("tesla_ble_control_required"):
                            state = "tesla-offline"
                            message = "Bluetooth Tesla non raggiungibile per controllo ricarica"
                            action = "tesla-ble-control-offline"
                        else:
                            state = "monitor-only"
                            message = "Dati Tesla da Wall Connector; BLE non interrogato"
                            action = "wall-connector-monitor"
                    else:
                        state = "tesla-offline"
                        message = "Tesla non raggiungibile via BLE"
                        action = "tesla-offline"
                else:
                    if outside_window_wall_control:
                        decision = self.controller.decide_minimum_from_snapshot(
                            control_measurement,
                            car,
                            projected_quarter_hour_import_w=(
                                demand.projected_average_w if demand is not None else None
                            ),
                            power_quota_limit_w=effective_power_quota_w,
                            power_quota_hysteresis_w=self.current.power_quota_hysteresis_w,
                            manual_override_amps=self.current.manual_override_amps,
                        )
                    else:
                        decision = self.controller.decide_from_snapshot(
                            control_measurement,
                            car,
                            non_tesla_power_w=max(energy["house_power_w"], 0.0),
                            extra_grid_power_w=self.current.extra_grid_power_w,
                            manual_override_amps=self.current.manual_override_amps,
                            use_meter_reading=meter_available,
                            projected_quarter_hour_import_w=(
                                demand.projected_average_w if demand is not None else None
                            ),
                            power_quota_limit_w=effective_power_quota_w,
                            power_quota_hysteresis_w=self.current.power_quota_hysteresis_w,
                        )
                    state = "ok"
                    message = decision.reason
                    action = decision.action
                    LOG.info("ciclo web action=%s reason=%s", decision.action, decision.reason)
                decision_data = (
                    asdict(decision)
                    if decision
                    else self._preview_decision_data(action, energy, window.active)
                )
                self.last_status = {
                    "state": state,
                    "message": message,
                    "updated_at": stamp,
                    "controller_enabled": self.current.enabled,
                    "extra_grid_power_w": self.current.extra_grid_power_w,
                    "power_quota_target_w": self.current.power_quota_target_w,
                    "power_quota_hysteresis_w": self.current.power_quota_hysteresis_w,
                    "manual_override_amps": self.current.manual_override_amps,
                    **window_data,
                    **decision_data,
                    # Lo snapshot energetico resta autorevole: le Decision di
                    # skip possono avere solar/grid/voltage opzionali a None.
                    **energy,
                }
                if energy.get("solar_source_degraded"):
                    self.last_status["state"] = "degraded"
                    self.last_status["message"] = (
                        "SolarEdge Modbus non disponibile; controllo su ALFA"
                    )
                    self.last_status["debug"] = energy.get("solar_source_error")
                    self.last_status["email_report"] = (
                        "mail non inviata: SolarEdge Modbus in retry"
                    )
            except Exception as exc:
                LOG.exception("ciclo controller fallito; nessun comando inviato")
                self.last_status = {
                    **self._error(exc),
                    "controller_enabled": self.current.enabled,
                    **window_data,
                    **energy,
                }
                self.refresh_mail_recipients()
                mail_result = self.reporter.notify(
                    exc,
                    context={
                        "component": "web-controller",
                        "solar_source": self.current.solar_source,
                        "control_mode": self.hard.control_mode,
                    },
                )
                self.last_status["email_report"] = mail_result.message
                self._record_error(
                    exc,
                    context={
                        "component": "web-controller",
                        "solar_source": self.current.solar_source,
                        "control_mode": self.hard.control_mode,
                    },
                    email_report=mail_result.message,
                )
            self._annotate_manual_override_status(self.last_status)
            if persist:
                self._store_measurement(self.last_status, appliances)
                self._check_events(self.last_status, car)
                self._check_anomaly_events(self.last_status["updated_at"], appliances)
            self._publish_status_cache()
            return dict(self.last_status)

    def request_run_now(self, *, force_measurement: bool = False) -> str:
        with self.run_now_lock:
            if self.run_now_running:
                self.run_now_pending = True
                return "queued"
            queued = self.lock.locked()
            self.run_now_running = True
            self.run_now_pending = False

        def worker() -> None:
            while True:
                try:
                    self.run_cycle(
                        force_measurement=force_measurement,
                        persist=False,
                        control=False,
                    )
                except Exception:
                    LOG.exception("aggiornamento manuale fallito")
                with self.run_now_lock:
                    if self.run_now_pending:
                        self.run_now_pending = False
                        continue
                    self.run_now_running = False
                    return

        threading.Thread(target=worker, name="manual-energy-cycle", daemon=True).start()
        return "queued" if queued else "started"

    def _record_error(
        self,
        exc: Exception,
        *,
        context: dict,
        email_report: str,
    ) -> None:
        payload = diagnostic_payload(exc)
        details = {
            "diagnostic": payload,
            "context": context,
            "email_report": email_report,
        }
        stamp = payload.get("occurred_at") or datetime.now(timezone.utc).astimezone().isoformat()
        try:
            self.db.add_event(
                observed_at=stamp,
                kind=f"error_{payload.get('component', 'application')}",
                message=str(payload.get("message") or exc),
                level="error",
                details=details,
            )
        except Exception:
            LOG.exception("salvataggio evento errore fallito")

    def _store_measurement(self, status: dict, appliances: list[dict]) -> None:
        try:
            self.db.add_measurement(
                {
                    "observed_at": status["updated_at"],
                    "solar_power_w": status.get("solar_power_w"),
                    "vimar_power_w": status.get("vimar_power_w", 0.0),
                    "tesla_power_w": status.get("tesla_power_w", 0.0),
                    "total_consumption_w": status.get("total_consumption_w", 0.0),
                    "import_power_w": status.get("import_power_w", 0.0),
                    "export_power_w": status.get("export_power_w", 0.0),
                    "tesla_current_a": status.get("current_a") or status.get("tesla_current_a"),
                    "tesla_target_a": status.get("target_a"),
                    "controller_enabled": status.get("controller_enabled", False),
                    "action": status.get("action"),
                    "reason": status.get("message"),
                    "quarter_hour_import_w": status.get("quarter_hour_import_w"),
                    "projected_quarter_hour_import_w": status.get(
                        "projected_quarter_hour_import_w"
                    ),
                    "imported_energy_wh": status.get("imported_energy_wh"),
                    "exported_energy_wh": status.get("exported_energy_wh"),
                    "alfa_grid_reading_enabled": status.get(
                        "alfa_grid_reading_enabled",
                        False,
                    ),
                },
                appliances,
            )
            self.db.prune(self.hard.data_retention_days)
            self._monthly_peak_cache = None
        except Exception:
            LOG.exception("salvataggio misura SQLite fallito")

    def _check_events(self, status: dict, car) -> None:
        if car is not None:
            manual_override = (
                car.is_charging and car.current_request_a >= self.current.manual_override_amps
            )
            self._threshold_event(
                "manual_override",
                manual_override,
                f"Tesla impostata manualmente a {car.current_request_a} A: controller non interviene",
                "info",
                {"current_a": car.current_request_a, "threshold_a": self.current.manual_override_amps},
                mail_enabled=False,
            )
        self._check_tesla_night_power(status)

    def _check_tesla_night_power(self, status: dict) -> None:
        power_w = _as_float(status.get("tesla_power_w")) or 0.0
        active = (
            self.hard.tesla_data_source == "wall-connector"
            and status.get("window_active") is False
            and power_w > TESLA_NIGHT_POWER_LIMIT_W
        )
        details = {
            "component": "wall_connector",
            "tesla_power_w": round(power_w),
            "threshold_w": round(TESLA_NIGHT_POWER_LIMIT_W),
            "window_active": status.get("window_active"),
            "action": status.get("action"),
            "target_a": status.get("target_a"),
            "tesla_connected": status.get("tesla_connected"),
            "wall_connector_vehicle_connected": status.get("wall_connector_vehicle_connected"),
            "wall_connector_contactor_closed": status.get("wall_connector_contactor_closed"),
        }
        if active and "tesla_night_power_high" not in self.active_events:
            LOG.warning(
                "assorbimento Tesla notturno sopra soglia: %.0f W > %.0f W",
                power_w,
                TESLA_NIGHT_POWER_LIMIT_W,
            )
        self._threshold_event(
            "tesla_night_power_high",
            active,
            f"Assorbimento Tesla notturno sopra soglia: {power_w:.0f} W > "
            f"{TESLA_NIGHT_POWER_LIMIT_W:.0f} W",
            "warning",
            details,
            mail_enabled=False,
        )

    def _check_anomaly_events(self, stamp: str, appliances: list[dict]) -> None:
        active_keys = set()
        for item in appliances:
            name = _safe_name(item.get("name"), fallback="device")
            anomaly = self.appliance_anomaly(stamp, name, item.get("power_w"))
            if not anomaly:
                continue
            key = f"anomaly_peak:{anomaly['group']}:{anomaly['name']}"
            active_keys.add(key)
            message = (
                f"Picco anomalo {anomaly['name']} "
                f"({anomaly['power_w']:.0f} W > {anomaly['threshold_w']:.0f} W)"
            )
            self._threshold_event(
                key,
                True,
                message,
                "warning",
                anomaly,
                mail_enabled=self.current.anomaly_email_enabled,
            )
        for key in [
            item
            for item in self.active_events
            if item.startswith("anomaly_peak:") and item not in active_keys
        ]:
            self._threshold_event(
                key,
                False,
                "Picco anomalo rientrato",
                "info",
                {"event": key},
                mail_enabled=False,
            )

    def _threshold_event(
        self,
        kind: str,
        active: bool,
        message: str,
        level: str,
        details: dict,
        *,
        mail_enabled: bool | None = None,
    ) -> None:
        stamp = datetime.now(timezone.utc).astimezone().isoformat()
        if active and kind not in self.active_events:
            self.active_events.add(kind)
            self._event(stamp, kind, message, level, details, mail_enabled=mail_enabled)
        elif not active and kind in self.active_events:
            self.active_events.remove(kind)
            self._event(
                stamp,
                f"{kind}_recovered",
                f"Rientrato: {message}",
                "info",
                details,
                mail_enabled=mail_enabled,
            )

    def _event(
        self,
        stamp: str,
        kind: str,
        message: str,
        level: str = "info",
        details: dict | None = None,
        *,
        mail_enabled: bool | None = None,
    ) -> None:
        self.db.add_event(
            observed_at=stamp,
            kind=kind,
            message=message,
            level=level,
            details=details,
        )
        if kind.startswith("tesla_ble_unreachable"):
            return
        if mail_enabled is False:
            return
        self.refresh_mail_recipients()
        original_enabled = self.event_reporter.enabled
        if mail_enabled is not None:
            self.event_reporter.enabled = mail_enabled
        try:
            self.event_reporter.notify(
                kind,
                f"[tesla-energy-controller] {message}",
                render_event_report(kind, message, details or {}),
            )
        finally:
            self.event_reporter.enabled = original_enabled

    @staticmethod
    def _error(exc: Exception) -> dict:
        return {
            "state": "error",
            "message": str(exc),
            "debug": diagnostic_payload(exc),
            "updated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        }

    def update(self, values: dict) -> RuntimeSettings:
        merged = asdict(self.current)
        merged.update(values)
        new_settings = RuntimeSettings.from_mapping(merged, self.hard)
        self.store.save(new_settings)
        self.current = new_settings
        self._apply_runtime_settings(self.current)
        self.refresh_mail_recipients()
        return new_settings

    def set_controller_enabled(self, enabled: bool, username: str) -> RuntimeSettings:
        if enabled == self.current.enabled:
            return self.current
        self.current = replace(self.current, enabled=enabled)
        self.store.save(self.current)
        self._apply_runtime_settings(self.current)
        state = "attivato" if enabled else "disattivato"
        stamp = datetime.now(timezone.utc).astimezone().isoformat()
        self._event(
            stamp,
            "controller_enabled_changed",
            f"Controller ricarica {state} da {username}",
            "info",
            {"enabled": enabled, "username": username},
        )
        return self.current

    def scheduler(self) -> None:
        next_control_at = 0.0
        while not self.stop_event.is_set():
            started = time.monotonic()
            control_due = started >= next_control_at
            self.run_cycle(persist=control_due, control=control_due)
            if control_due:
                next_control_at = time.monotonic() + self.control_interval_seconds()
            elapsed = time.monotonic() - started
            interval = self.preview_interval_seconds()
            self.stop_event.wait(max(0, interval - elapsed))

    def start(self) -> None:
        threading.Thread(target=self.scheduler, name="energy-scheduler", daemon=True).start()

    def status_payload(self) -> dict:
        self.reload_settings()
        status = dict(self.last_status)
        status["message"] = _safe_text(status.get("message")) or ""
        status["controller_enabled"] = self.current.enabled
        status["extra_grid_power_w"] = self.current.extra_grid_power_w
        status["power_quota_target_w"] = self.current.power_quota_target_w
        status["power_quota_hysteresis_w"] = self.current.power_quota_hysteresis_w
        status["manual_override_amps"] = self.current.manual_override_amps
        status["expected_phases"] = self.current.expected_phases
        status["monthly_peak_import_w"] = self.monthly_peak_import_w()
        status["tesla_data_source"] = self.hard.tesla_data_source
        status["wall_connector_configured"] = bool(self.hard.wall_connector_host)
        status.setdefault("tesla_ble_connected", None)
        status.setdefault("tesla_ble_control_required", False)
        status.setdefault("tesla_ble_control_state", "cached")
        status.setdefault(
            "tesla_ble_control_message",
            "Stato Bluetooth non disponibile dalla cache",
        )
        self._annotate_manual_override_status(status)
        uptime = max(
            0,
            int(
                (
                    datetime.now(timezone.utc).astimezone()
                    - self._solar_source_started_at
                ).total_seconds()
            ),
        )
        status["solar_source"] = self.current.solar_source
        status["solar_source_started_at"] = self._solar_source_started_at.isoformat()
        status["solar_source_uptime_seconds"] = uptime
        status["poll_interval_seconds"] = self.control_interval_seconds()
        try:
            window = self.window()
        except RuntimeSettingsError as exc:
            status["window_error"] = str(exc)
        else:
            status["window_active"] = window.active
            status["window_label"] = window.label
            status["window_mode"] = window.mode
        return status

    def monthly_peak_import_w(self, now: datetime | None = None) -> float:
        """Massima potenza media quartoraria completa del mese corrente."""
        local_now = now or datetime.now(timezone.utc).astimezone()
        year_month = local_now.astimezone().strftime("%Y-%m")
        if self._monthly_peak_cache and self._monthly_peak_cache[0] == year_month:
            return self._monthly_peak_cache[1]
        value = self.db.max_import_for_month(year_month)
        self._monthly_peak_cache = (year_month, value)
        return value

    def anomaly_groups(self) -> list[dict]:
        groups = parse_anomaly_device_groups(self.current.anomaly_device_groups)
        return [
            {
                "name": _safe_name(group.name, fallback="gruppo"),
                "threshold_w": group.threshold_w,
                "patterns": tuple(pattern.casefold() for pattern in group.patterns),
            }
            for group in groups
        ]

    def anomaly_window(self, stamp: str | None = None) -> OperatingWindow:
        parsed = None
        if stamp:
            try:
                parsed = datetime.fromisoformat(stamp)
            except ValueError:
                parsed = None
        return anomaly_window(self.current, self.hard, parsed)

    def appliance_anomaly(self, stamp: str, name: str, power_w) -> dict | None:
        try:
            power_value = float(power_w)
        except (TypeError, ValueError):
            return None
        try:
            if not self.anomaly_window(stamp).active:
                return None
        except RuntimeSettingsError:
            return None
        lowered = name.casefold()
        matched = [
            group
            for group in self.anomaly_groups()
            if power_value >= group["threshold_w"]
            and any(pattern in lowered for pattern in group["patterns"])
        ]
        if not matched:
            return None
        group = min(matched, key=lambda item: item["threshold_w"])
        return {
            "t": stamp[11:16],
            "observed_at": stamp,
            "name": name,
            "group": group["name"],
            "power_w": power_value,
            "threshold_w": group["threshold_w"],
        }

    def device_anomalies_for_day(self, day: str | None, limit: int = 50) -> list[dict]:
        if not day:
            return []
        measurements, appliances = self.db.device_series_for_day(day)
        return self.device_anomalies_from_rows(measurements, appliances, limit=limit)

    def device_anomalies_from_rows(
        self,
        measurements: list[dict],
        appliances: list[dict],
        limit: int = 50,
    ) -> list[dict]:
        observed_at = {row["id"]: row.get("observed_at") for row in measurements}
        groups = self.anomaly_groups()
        if not groups:
            return []
        anomalies = []
        window_cache: dict[str, bool] = {}
        name_cache: dict[str, tuple[str, str]] = {}
        for row in appliances:
            raw_name = str(row.get("name") or "")
            cached_name = name_cache.get(raw_name)
            if cached_name is None:
                cached_name = (
                    _safe_name(raw_name, fallback="device"),
                    raw_name.casefold(),
                )
                name_cache[raw_name] = cached_name
            name, lowered = cached_name
            stamp = observed_at.get(row["mid"]) or ""
            try:
                power_value = float(row.get("power_w"))
            except (TypeError, ValueError):
                continue
            group = None
            for candidate in groups:
                if power_value < candidate["threshold_w"]:
                    continue
                if not any(pattern in lowered for pattern in candidate["patterns"]):
                    continue
                if group is None or candidate["threshold_w"] < group["threshold_w"]:
                    group = candidate
            if group is None:
                continue
            if stamp not in window_cache:
                try:
                    window_cache[stamp] = self.anomaly_window(stamp).active
                except RuntimeSettingsError:
                    window_cache[stamp] = False
            if not window_cache[stamp]:
                continue
            anomalies.append(
                {
                    "t": stamp[11:16],
                    "observed_at": stamp,
                    "name": name,
                    "group": group["name"],
                    "power_w": power_value,
                    "threshold_w": group["threshold_w"],
                }
            )
        return anomalies[-limit:]

    @staticmethod
    def _target_outside_window_marker(row: dict) -> bool:
        tesla_state_text = f"{row.get('action') or ''} {row.get('reason') or ''}".casefold()
        return "outside-window" in tesla_state_text or "fuori dalla finestra" in tesla_state_text

    @staticmethod
    def _target_row_tesla_active(row: dict) -> bool:
        current_a = _as_float(row.get("tesla_current_a"))
        power_w = _as_float(row.get("tesla_power_w"))
        return (
            (current_a is not None and current_a >= WALL_CONNECTOR_CHARGE_MIN_CURRENT_A)
            or (power_w is not None and power_w >= WALL_CONNECTOR_CHARGE_MIN_POWER_W)
        )

    def _target_zero_marker(self, row: dict) -> bool:
        # Controller non attivo (disabilitato, Tesla offline, oppure fuori
        # finestra senza ricarica reale): nessun target da mostrare.
        # Ritorna 0 esplicito così la linea scende a zero senza forward-fill.
        tesla_state_text = f"{row.get('action') or ''} {row.get('reason') or ''}".casefold()
        if self._target_outside_window_marker(row):
            return not self._target_row_tesla_active(row)
        return any(
            marker in tesla_state_text
            for marker in (
                "tesla-offline",
                "tesla offline",
                "non raggiungibile",
                "disabled",
                "disabilitato",
            )
        )

    @staticmethod
    def _tesla_power_is_zero(row: dict) -> bool:
        try:
            return float(row.get("tesla_power_w") or 0.0) <= 0
        except (TypeError, ValueError):
            return True

    def _target_a_for_series(self, row: dict) -> float | None:
        if self._target_zero_marker(row):
            return 0.0
        target_a = row.get("tesla_target_a")
        if target_a is None:
            if self._target_outside_window_marker(row) and self._target_row_tesla_active(row):
                return float(self.current.min_charge_amps)
            if self._tesla_power_is_zero(row):
                return 0.0
            return None
        try:
            return max(float(target_a), 0.0)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _row_manual_override(row: dict) -> bool:
        text = f"{row.get('action') or ''} {row.get('reason') or ''}".casefold()
        return "manual-override" in text or "override manuale" in text

    def _target_power_w(self, row: dict, house_power_w: float) -> float | None:
        target_a = self._target_a_for_series(row)
        if target_a is None:
            return None
        if target_a <= 0:
            return 0.0
        try:
            tesla_target_w = target_a * self.hard.nominal_phase_voltage_v * max(
                self.current.expected_phases,
                1,
            )
            return max(house_power_w, 0.0) + tesla_target_w
        except (TypeError, ValueError):
            return None

    def energy_series_payload(self, requested_day: str | None = None) -> dict:
        days = self.db.measurement_days()
        day = _selected_day(days, requested_day)
        rows = self.db.measurements_for_day(day) if day else []
        points = []
        for bucket in _series_buckets(rows):
            bucket_start = bucket["start"]
            entries = bucket["entries"]
            house_power_w = _weighted_value(entries, bucket_start, _stored_house_power_w)
            device_power_w = _weighted_value(entries, bucket_start, _stored_device_power_w)
            points.append(
                {
                    "t": bucket_start.strftime("%H:%M"),
                    "solar": _weighted_value(
                        entries, bucket_start, lambda row: row.get("solar_power_w")
                    ),
                    "vimar": _weighted_value(
                        entries, bucket_start, lambda row: row.get("vimar_power_w")
                    ),
                    "appliances": _weighted_value(
                        entries, bucket_start, lambda row: row.get("vimar_power_w")
                    ),
                    "device": device_power_w,
                    "house": house_power_w,
                    "tesla": _weighted_value(
                        entries, bucket_start, lambda row: row.get("tesla_power_w")
                    ),
                    "import": _weighted_value(
                        entries, bucket_start, lambda row: row.get("import_power_w")
                    ),
                    "export": _weighted_value(
                        entries, bucket_start, lambda row: row.get("export_power_w")
                    ),
                    "current": _weighted_value(
                        entries, bucket_start, lambda row: row.get("tesla_current_a")
                    ),
                    "target": _weighted_value(
                        entries, bucket_start, self._target_a_for_series
                    ),
                    "manual_override": any(
                        self._row_manual_override(row) for _stamp, row in entries
                    ),
                    "target_w": _weighted_value(
                        entries,
                        bucket_start,
                        lambda row: self._target_power_w(row, _stored_house_power_w(row)),
                    ),
                }
            )
        return {
            "days": days,
            "day": day,
            "points": points,
            "alfa_grid_reading_enabled": self.current.alfa_grid_reading_enabled,
            "sample_seconds": SERIES_BUCKET_SECONDS,
        }

    def appliances_payload(self, requested_day: str | None = None) -> dict:
        days = self.db.measurement_days()
        day = _selected_day(days, requested_day)
        rows = self.db.appliances_for_day(day) if day else []
        buckets: dict[str, dict] = {}
        for row in rows:
            stamp = _parse_observed_at(row.get("observed_at"))
            if stamp is None:
                continue
            bucket_start = _series_bucket_start(stamp)
            key = bucket_start.isoformat()
            bucket = buckets.setdefault(
                key,
                {"start": bucket_start, "devices": {}},
            )
            name = _safe_name(row.get("name"), fallback="device")
            value = _as_float(row.get("power_w"))
            if value is None:
                continue
            bucket["devices"].setdefault(name, []).append((stamp, value))
        ordered = sorted(buckets.values(), key=lambda item: item["start"])
        labels = [bucket["start"].strftime("%H:%M") for bucket in ordered]
        names = sorted({name for bucket in ordered for name in bucket["devices"]})
        series = [
            {
                "name": name,
                "data": [
                    (
                        _weighted_samples(bucket["devices"][name], bucket["start"])
                        if name in bucket["devices"]
                        else None
                    )
                    for bucket in ordered
                ],
            }
            for name in names
        ]
        latest = []
        if ordered:
            last_devices = ordered[-1]["devices"]
            latest = [
                {"name": name, "power_w": _weighted_samples(values, ordered[-1]["start"])}
                for name, values in sorted(last_devices.items())
                if values
            ]
        return {
            "days": days,
            "day": day,
            "labels": labels,
            "series": series,
            "latest": latest,
            "sample_seconds": SERIES_BUCKET_SECONDS,
        }

    def device_series_payload(self, requested_day: str | None = None) -> dict:
        days = self.db.measurement_days()
        day = _selected_day(days, requested_day)
        measurements, appliances = self.db.device_series_for_day(day) if day else ([], [])
        measurement_buckets = _series_buckets(measurements)
        labels = [bucket["start"].strftime("%H:%M") for bucket in measurement_buckets]
        id_to_slot: dict[int, int] = {}
        id_to_stamp: dict[int, datetime] = {}
        for index, bucket in enumerate(measurement_buckets):
            for stamp, row in bucket["entries"]:
                id_to_slot[row["id"]] = index
                id_to_stamp[row["id"]] = stamp
        if self.current.alfa_grid_reading_enabled:
            series = [
                {
                    "name": "Elettrodomestici",
                    "kind": "aggregate",
                    "data": [
                        _weighted_value(
                            bucket["entries"],
                            bucket["start"],
                            lambda row: row.get("vimar_power_w"),
                        )
                        for bucket in measurement_buckets
                    ],
                },
            ]
        else:
            series = [
                {
                    "name": "Consumo casa",
                    "kind": "aggregate",
                    "data": [
                        _weighted_value(
                            bucket["entries"],
                            bucket["start"],
                            lambda row: row.get("vimar_power_w"),
                        )
                        for bucket in measurement_buckets
                    ],
                }
            ]
        by_device: dict[str, list[list[tuple[datetime, float]]]] = {}
        safe_names: dict[str, str] = {}
        for row in appliances:
            raw_name = str(row.get("name") or "")
            name = safe_names.get(raw_name)
            if name is None:
                name = _safe_name(raw_name, fallback="device")
                safe_names[raw_name] = name
            slot = id_to_slot.get(row["mid"])
            stamp = id_to_stamp.get(row["mid"])
            power_w = _as_float(row.get("power_w"))
            if slot is None or stamp is None or power_w is None:
                continue
            values = by_device.get(name)
            if values is None:
                values = [[] for _ in labels]
                by_device[name] = values
            values[slot].append((stamp, power_w))
        for name in sorted(by_device):
            series.append(
                {
                    "name": name,
                    "kind": "appliance",
                    "data": [
                        _weighted_samples(samples, measurement_buckets[index]["start"])
                        for index, samples in enumerate(by_device[name])
                    ],
                }
            )
        latest = []
        if labels:
            last_slot = len(labels) - 1
            latest = [
                {
                    "name": name,
                    "power_w": _weighted_samples(
                        values[last_slot], measurement_buckets[last_slot]["start"]
                    ),
                }
                for name, values in sorted(by_device.items())
                if values[last_slot]
            ]
        try:
            anomaly_window_data = self.anomaly_window(
                measurements[-1].get("observed_at") if measurements else None
            )
            anomaly_window_payload = {
                "mode": anomaly_window_data.mode,
                "label": anomaly_window_data.label,
            }
        except RuntimeSettingsError:
            anomaly_window_payload = {
                "mode": self.current.anomaly_window_mode,
                "label": "non disponibile",
            }
        return {
            "days": days,
            "day": day,
            "labels": labels,
            "series": series,
            "latest": latest,
            "anomalies": self.device_anomalies_from_rows(measurements, appliances),
            "anomaly_threshold_w": self.current.anomaly_peak_threshold_w,
            "anomaly_groups": [
                {
                    "name": group["name"],
                    "threshold_w": group["threshold_w"],
                    "patterns": list(group["patterns"]),
                }
                for group in self.anomaly_groups()
            ],
            "anomaly_window": anomaly_window_payload,
            "alfa_grid_reading_enabled": self.current.alfa_grid_reading_enabled,
            "sample_seconds": SERIES_BUCKET_SECONDS,
        }

    @staticmethod
    def public_runtime(settings: RuntimeSettings) -> dict:
        return asdict(settings)

    @staticmethod
    def public_events(events: list[dict]) -> list[dict]:
        safe_events = []
        for event in events:
            details_json = event.get("details_json")
            if details_json and details_json != "{}":
                try:
                    details_json = json.dumps(
                        _sanitize_public_value(json.loads(details_json)),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                except (TypeError, json.JSONDecodeError):
                    details_json = "{}"
            safe_events.append(
                {
                    "observed_at": _safe_text(event.get("observed_at"), limit=80),
                    "kind": _safe_name(event.get("kind"), fallback="event"),
                    "level": _safe_name(event.get("level"), fallback="info"),
                    "message": _safe_text(event.get("message"), limit=500) or "",
                    "details_json": details_json if details_json in {None, "{}"} else details_json,
                }
            )
        return safe_events

    def public_users(self) -> list[dict]:
        return [
            {
                "username": _safe_text(user.get("username"), limit=120) or "",
                "email": _safe_text(user.get("email"), limit=180) or "",
                "role": user.get("role") if user.get("role") in {"admin", "viewer"} else "viewer",
            }
            for user in self.db.list_users()
        ]

    def public_user(self, username: str | None) -> dict:
        user = self.db.get_user(username or "") if username else None
        if not user:
            return {"username": _safe_text(username, limit=120) or "", "email": "", "role": "viewer"}
        return {
            "username": _safe_text(user.get("username"), limit=120) or "",
            "email": _safe_text(user.get("email"), limit=180) or "",
            "role": user.get("role") if user.get("role") in {"admin", "viewer"} else "viewer",
        }

    def backup_archive(
        self,
        *,
        include_db: bool = True,
        include_config: bool = True,
    ):
        return BackupService(self.hard, self.db).backup_archive(
            include_db=include_db,
            include_config=include_config,
        )

    def import_backup_archive(
        self,
        archive_data: bytes,
        username: str,
        *,
        restore_db: bool = True,
        restore_config: bool = True,
    ) -> dict:
        result = BackupService(self.hard, self.db).restore_archive(
            archive_data,
            restore_db=restore_db,
            restore_config=restore_config,
        )
        self.db = result.db
        if result.imported_runtime is not None:
            self.current = result.imported_runtime
            self._apply_runtime_settings(self.current)
        stamp = datetime.now(timezone.utc).astimezone().isoformat()
        self._event(
            stamp,
            "backup_imported",
            f"Backup importato da {username}",
            "info",
            {
                "restored": result.restored,
                "username": username,
                "restore_db": restore_db,
                "restore_config": restore_config,
            },
            mail_enabled=False,
        )
        self.refresh_mail_recipients()
        return {"restored": result.restored}

    def ui_payload(self) -> dict:
        return {
            "status": self.status_payload(),
            "current": self.public_runtime(self.current),
            "config": self.public_config(),
            "tesla_ble": self.tesla_ble_status(),
            "events": self.public_events(self.db.latest_events()),
            "error_events": self.public_events(self.db.latest_error_events()),
        }
