from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(ValueError):
    pass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().casefold()
    if value in {"1", "true", "yes", "on", "si", "sì"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} deve essere true o false")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} deve essere un intero") from exc


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} deve essere un numero") from exc


def _env_optional_float(name: str) -> float | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} deve essere un numero") from exc


def _env_list(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _secret(value_name: str, file_name: str) -> str | None:
    path = os.getenv(file_name)
    if path:
        try:
            return Path(path).expanduser().read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ConfigurationError(f"Impossibile leggere {file_name}={path}: {exc}") from exc
    value = os.getenv(value_name)
    return value.strip() if value else None


def load_env_file(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


@dataclass(frozen=True)
class Settings:
    mode: str
    control_mode: str
    energy_source: str
    poll_interval_seconds: int
    cloud_poll_interval_seconds: int
    expected_phases: int
    nominal_phase_voltage_v: float
    min_voltage_v: float
    max_voltage_v: float
    solar_utilization_percent: float
    solar_timezone: str
    solar_latitude: float | None
    solar_longitude: float | None
    target_grid_import_w: float
    max_grid_current_a: float
    min_charge_amps: int
    max_charge_amps: int
    command_hysteresis_a: int
    max_ramp_up_a: int

    solaredge_api_key: str | None
    solaredge_username: str | None
    solaredge_password: str | None
    solaredge_site_id: int | None
    solaredge_modbus_host: str | None
    solaredge_modbus_port: int
    solaredge_modbus_unit: int
    solaredge_modbus_poll_interval_seconds: int
    solaredge_inverter_base: int
    solaredge_meter_base: int
    solaredge_grid_power_sign: int
    solaredge_session_file: str
    alfa_modbus_host: str | None
    alfa_modbus_port: int
    alfa_modbus_unit: int
    alfa_modbus_timeout_seconds: float
    alfa_control_interval_seconds: int
    grid_import_limit_w: float
    grid_import_emergency_w: float
    grid_hold_band_w: float
    grid_surplus_stable_reads: int
    power_quota_target_w: float
    power_quota_hysteresis_w: float

    vimar_host: str | None
    vimar_port: int
    vimar_device_uid: str | None
    vimar_client_name: str
    vimar_third_party_tag: str | None
    vimar_setup_code: str | None
    vimar_private_key_file: str
    vimar_public_key_file: str
    vimar_credentials_file: str
    vimar_ca_cert: str | None
    vimar_tls_verify: bool
    vimar_tls_check_hostname: bool
    vimar_timeout_seconds: int
    vimar_protocol_version: str

    tesla_mock: bool
    tesla_transport: str
    tesla_vin: str | None
    tesla_control_binary: str
    tesla_ble_key_file: str
    tesla_ble_cache_file: str
    tesla_ble_timeout_seconds: int
    tesla_ble_connect_timeout_seconds: int
    tesla_ble_command_timeout_seconds: int
    tesla_ble_retries: int
    tesla_ble_retry_backoff_seconds: float
    tesla_ble_adapter: str | None
    tesla_ble_require_time_sync: bool
    tesla_ble_preflight_sleep_check: bool
    tesla_ble_recovery_enabled: bool
    tesla_ble_recovery_threshold: int
    tesla_data_source: str
    wall_connector_host: str | None
    wall_connector_timeout_seconds: float
    wall_connector_phases: int
    wall_connector_min_current_a: float
    wall_connector_poll_interval_seconds: int
    tesla_api_base_url: str
    tesla_ca_cert: str | None
    tesla_verify_ssl: bool
    tesla_client_id: str | None
    tesla_access_token: str | None
    tesla_refresh_token: str | None
    tesla_token_file: str
    tesla_token_url: str

    secret_key: str | None
    web_password: str | None
    web_host: str
    web_port: int
    web_secure_cookie: bool
    web_session_ttl_seconds: int
    runtime_settings_file: str
    energy_database_file: str
    data_retention_days: int
    web_username: str
    web_viewer_username: str
    web_viewer_password: str | None

    tuya_enabled: bool
    tuya_mqtt_host: str
    tuya_mqtt_port: int
    tuya_product_id: str | None
    tuya_device_id: str | None
    tuya_device_secret: str | None
    tuya_keepalive_seconds: int
    tuya_report_interval_seconds: int
    tuya_average_samples: int
    tuya_report_tesla: bool

    extra_grid_power_w: float
    manual_override_amps: int

    event_email_enabled: bool
    event_email_cooldown_seconds: int
    notify_backend: str
    notify_recipients: tuple[str, ...]
    notify_api_url: str | None
    notify_api_key: str | None
    notify_api_user: str | None
    notify_sender_name: str
    notify_reply_to: str
    notify_timeout_seconds: int
    smtp_host: str | None
    smtp_port: int
    smtp_username: str | None
    smtp_password: str | None
    smtp_from: str | None
    smtp_starttls: bool
    smtp_ssl: bool

    error_report_email_enabled: bool
    error_report_email_on_solaredge_failure: bool
    error_report_email_cooldown_seconds: int

    mock_grid_power_w: float
    mock_solar_power_w: float
    mock_tesla_current_a: int

    @classmethod
    def from_env(cls) -> "Settings":
        site_id_raw = os.getenv("SOLAREDGE_SITE_ID")
        tesla_mock = _env_bool("TESLA_MOCK", True)
        settings = cls(
            mode=os.getenv("MODE", "dry-run").strip().casefold(),
            control_mode=os.getenv("CONTROL_MODE", "solar-production").strip().casefold(),
            energy_source=os.getenv("ENERGY_SOURCE", "mock").strip().casefold(),
            poll_interval_seconds=_env_int("POLL_INTERVAL_SECONDS", 300),
            cloud_poll_interval_seconds=_env_int("CLOUD_POLL_INTERVAL_SECONDS", 300),
            expected_phases=_env_int("EXPECTED_PHASES", 3),
            nominal_phase_voltage_v=_env_float("NOMINAL_PHASE_VOLTAGE_V", 230.0),
            min_voltage_v=_env_float("MIN_VOLTAGE_V", 200.0),
            max_voltage_v=_env_float("MAX_VOLTAGE_V", 255.0),
            solar_utilization_percent=_env_float("SOLAR_UTILIZATION_PERCENT", 100.0),
            solar_timezone=os.getenv("SOLAR_TIMEZONE", "Europe/Rome").strip(),
            # Sede fissa Vittorio Veneto (TV): non spostabile, non esposta nel pannello.
            solar_latitude=_env_optional_float("SOLAR_LATITUDE") or 45.9869,
            solar_longitude=_env_optional_float("SOLAR_LONGITUDE") or 12.3045,
            target_grid_import_w=_env_float("TARGET_GRID_IMPORT_W", 200.0),
            max_grid_current_a=_env_float("MAX_GRID_CURRENT_A", 25.0),
            min_charge_amps=_env_int("MIN_CHARGE_AMPS", 5),
            max_charge_amps=_env_int("MAX_CHARGE_AMPS", 16),
            command_hysteresis_a=_env_int("COMMAND_HYSTERESIS_A", 1),
            max_ramp_up_a=_env_int("MAX_RAMP_UP_A", 2),
            solaredge_api_key=_secret("SOLAREDGE_API_KEY", "SOLAREDGE_API_KEY_FILE"),
            solaredge_username=_secret("SOLAREDGE_USERNAME", "SOLAREDGE_USERNAME_FILE"),
            solaredge_password=_secret("SOLAREDGE_PASSWORD", "SOLAREDGE_PASSWORD_FILE"),
            solaredge_site_id=int(site_id_raw) if site_id_raw else None,
            solaredge_modbus_host=os.getenv("SOLAREDGE_MODBUS_HOST") or None,
            solaredge_modbus_port=_env_int("SOLAREDGE_MODBUS_PORT", 1502),
            solaredge_modbus_unit=_env_int("SOLAREDGE_MODBUS_UNIT", 1),
            solaredge_modbus_poll_interval_seconds=_env_int(
                "SOLAREDGE_MODBUS_POLL_INTERVAL_SECONDS", 30
            ),
            solaredge_inverter_base=_env_int("SOLAREDGE_INVERTER_BASE", 40069),
            solaredge_meter_base=_env_int("SOLAREDGE_METER_BASE", 40121),
            solaredge_grid_power_sign=_env_int("SOLAREDGE_GRID_POWER_SIGN", 1),
            solaredge_session_file=os.getenv(
                "SOLAREDGE_SESSION_FILE", ".secrets/solaredge_session.json"
            ),
            alfa_modbus_host=(os.getenv("ALFA_MODBUS_HOST") or "").strip() or None,
            alfa_modbus_port=_env_int("ALFA_MODBUS_PORT", 502),
            alfa_modbus_unit=_env_int("ALFA_MODBUS_UNIT", 1),
            alfa_modbus_timeout_seconds=_env_float("ALFA_MODBUS_TIMEOUT_SECONDS", 5.0),
            alfa_control_interval_seconds=_env_int("ALFA_CONTROL_INTERVAL_SECONDS", 30),
            grid_import_limit_w=_env_float("GRID_IMPORT_LIMIT_W", 3000.0),
            grid_import_emergency_w=_env_float("GRID_IMPORT_EMERGENCY_W", 3300.0),
            grid_hold_band_w=_env_float("GRID_HOLD_BAND_W", 200.0),
            grid_surplus_stable_reads=_env_int("GRID_SURPLUS_STABLE_READS", 3),
            power_quota_target_w=_env_float("POWER_QUOTA_TARGET_W", 7000.0),
            power_quota_hysteresis_w=_env_float("POWER_QUOTA_HYSTERESIS_W", 500.0),
            vimar_host=(os.getenv("VIMAR_HOST") or "").strip() or None,
            vimar_port=_env_int("VIMAR_PORT", 20615),
            vimar_device_uid=(os.getenv("VIMAR_DEVICE_UID") or "").strip() or None,
            vimar_client_name=os.getenv(
                "VIMAR_CLIENT_NAME", "Raspberry Energy Monitor"
            ).strip(),
            vimar_third_party_tag=(os.getenv("VIMAR_THIRD_PARTY_TAG") or "").strip()
            or None,
            vimar_setup_code=_secret("VIMAR_SETUP_CODE", "VIMAR_SETUP_CODE_FILE"),
            vimar_private_key_file=os.getenv(
                "VIMAR_PRIVATE_KEY_FILE", ".secrets/vimar_private_key.pem"
            ).strip(),
            vimar_public_key_file=os.getenv(
                "VIMAR_PUBLIC_KEY_FILE", ".secrets/vimar_public_key.pem"
            ).strip(),
            vimar_credentials_file=os.getenv(
                "VIMAR_CREDENTIALS_FILE", ".secrets/vimar_credentials.json"
            ).strip(),
            vimar_ca_cert=(os.getenv("VIMAR_CA_CERT") or "").strip() or None,
            vimar_tls_verify=_env_bool("VIMAR_TLS_VERIFY", True),
            vimar_tls_check_hostname=_env_bool("VIMAR_TLS_CHECK_HOSTNAME", False),
            vimar_timeout_seconds=_env_int("VIMAR_TIMEOUT_SECONDS", 15),
            vimar_protocol_version=os.getenv("VIMAR_PROTOCOL_VERSION", "2.7").strip(),
            tesla_mock=tesla_mock,
            tesla_transport=os.getenv("TESLA_TRANSPORT", "mock" if tesla_mock else "ble")
            .strip()
            .casefold(),
            tesla_vin=(os.getenv("TESLA_VIN") or "").strip() or None,
            tesla_control_binary=os.getenv("TESLA_CONTROL_BINARY", "tesla-control").strip(),
            tesla_ble_key_file=os.getenv(
                "TESLA_BLE_KEY_FILE", ".secrets/tesla/private-key.pem"
            ).strip(),
            tesla_ble_cache_file=os.getenv(
                "TESLA_BLE_CACHE_FILE", ".secrets/tesla/session-cache.json"
            ).strip(),
            tesla_ble_timeout_seconds=_env_int("TESLA_BLE_TIMEOUT_SECONDS", 35),
            tesla_ble_connect_timeout_seconds=_env_int(
                "TESLA_BLE_CONNECT_TIMEOUT_SECONDS", 20
            ),
            tesla_ble_command_timeout_seconds=_env_int(
                "TESLA_BLE_COMMAND_TIMEOUT_SECONDS", 10
            ),
            tesla_ble_retries=_env_int("TESLA_BLE_RETRIES", 3),
            tesla_ble_retry_backoff_seconds=_env_float(
                "TESLA_BLE_RETRY_BACKOFF_SECONDS", 1.0
            ),
            tesla_ble_adapter=(os.getenv("TESLA_BLE_ADAPTER") or "").strip() or None,
            tesla_ble_require_time_sync=_env_bool("TESLA_BLE_REQUIRE_TIME_SYNC", True),
            tesla_ble_preflight_sleep_check=_env_bool(
                "TESLA_BLE_PREFLIGHT_SLEEP_CHECK", True
            ),
            tesla_ble_recovery_enabled=_env_bool("TESLA_BLE_RECOVERY_ENABLED", False),
            tesla_ble_recovery_threshold=_env_int("TESLA_BLE_RECOVERY_THRESHOLD", 3),
            tesla_data_source=os.getenv("TESLA_DATA_SOURCE", "vehicle").strip().casefold(),
            wall_connector_host=(os.getenv("WALL_CONNECTOR_HOST") or "").strip() or None,
            wall_connector_timeout_seconds=_env_float("WALL_CONNECTOR_TIMEOUT_SECONDS", 3.0),
            wall_connector_phases=_env_int(
                "WALL_CONNECTOR_PHASES", _env_int("EXPECTED_PHASES", 3)
            ),
            wall_connector_min_current_a=_env_float("WALL_CONNECTOR_MIN_CURRENT_A", 0.3),
            wall_connector_poll_interval_seconds=_env_int(
                "WALL_CONNECTOR_POLL_INTERVAL_SECONDS", 15
            ),
            tesla_api_base_url=(
                os.getenv("TESLA_API_BASE_URL") or "https://localhost:4443"
            ).rstrip("/"),
            tesla_ca_cert=os.getenv("TESLA_CA_CERT") or None,
            tesla_verify_ssl=_env_bool("TESLA_VERIFY_SSL", True),
            tesla_client_id=(os.getenv("TESLA_CLIENT_ID") or "").strip() or None,
            tesla_access_token=_secret("TESLA_ACCESS_TOKEN", "TESLA_ACCESS_TOKEN_FILE"),
            tesla_refresh_token=_secret("TESLA_REFRESH_TOKEN", "TESLA_REFRESH_TOKEN_FILE"),
            tesla_token_file=os.getenv("TESLA_TOKEN_FILE", ".secrets/tesla_tokens.json"),
            tesla_token_url=os.getenv(
                "TESLA_TOKEN_URL",
                "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token",
            ),
            secret_key=_secret("SECRET_KEY", "SECRET_KEY_FILE"),
            web_password=_secret("WEB_PASSWORD", "WEB_PASSWORD_FILE"),
            web_host=os.getenv("WEB_HOST", "127.0.0.1").strip(),
            web_port=_env_int("WEB_PORT", 8080),
            web_secure_cookie=_env_bool("WEB_SECURE_COOKIE", False),
            web_session_ttl_seconds=_env_int("WEB_SESSION_TTL_SECONDS", 43200),
            runtime_settings_file=os.getenv(
                "RUNTIME_SETTINGS_FILE", "data/runtime_settings.json"
            ).strip(),
            energy_database_file=os.getenv(
                "ENERGY_DATABASE_FILE", "data/energy.sqlite3"
            ).strip(),
            data_retention_days=_env_int("DATA_RETENTION_DAYS", 90),
            web_username=os.getenv("WEB_USERNAME", "admin").strip(),
            web_viewer_username=os.getenv("WEB_VIEWER_USERNAME", "utente").strip(),
            web_viewer_password=_secret("WEB_VIEWER_PASSWORD", "WEB_VIEWER_PASSWORD_FILE"),
            tuya_enabled=_env_bool("TUYA_ENABLED", False),
            tuya_mqtt_host=os.getenv("TUYA_MQTT_HOST", "m1.tuyacn.com").strip(),
            tuya_mqtt_port=_env_int("TUYA_MQTT_PORT", 8883),
            tuya_product_id=(os.getenv("TUYA_PRODUCT_ID") or "").strip() or None,
            tuya_device_id=(os.getenv("TUYA_DEVICE_ID") or "").strip() or None,
            tuya_device_secret=_secret("TUYA_DEVICE_SECRET", "TUYA_DEVICE_SECRET_FILE"),
            tuya_keepalive_seconds=_env_int("TUYA_KEEPALIVE_SECONDS", 60),
            tuya_report_interval_seconds=_env_int("TUYA_REPORT_INTERVAL_SECONDS", 30),
            tuya_average_samples=_env_int("TUYA_AVERAGE_SAMPLES", 3),
            tuya_report_tesla=_env_bool("TUYA_REPORT_TESLA", True),
            extra_grid_power_w=_env_float("EXTRA_GRID_POWER_W", 3000.0),
            manual_override_amps=_env_int("MANUAL_OVERRIDE_AMPS", 14),
            event_email_enabled=_env_bool("EVENT_EMAIL_ENABLED", False),
            event_email_cooldown_seconds=_env_int("EVENT_EMAIL_COOLDOWN_SECONDS", 1800),
            notify_backend=os.getenv("NOTIFY_BACKEND", "wordpress").strip().casefold(),
            notify_recipients=_env_list("NOTIFY_RECIPIENTS"),
            notify_api_url=(os.getenv("NOTIFY_API_URL") or "").strip() or None,
            notify_api_key=_secret("NOTIFY_API_KEY", "NOTIFY_API_KEY_FILE"),
            notify_api_user=_secret("NOTIFY_API_USER", "NOTIFY_API_USER_FILE"),
            notify_sender_name=os.getenv(
                "NOTIFY_SENDER_NAME", "Tesla Energy Controller"
            ).strip(),
            notify_reply_to=os.getenv("NOTIFY_REPLY_TO", "noreply@example.com").strip(),
            notify_timeout_seconds=_env_int("NOTIFY_TIMEOUT_SECONDS", 15),
            smtp_host=(os.getenv("SMTP_HOST") or "").strip() or None,
            smtp_port=_env_int("SMTP_PORT", 587),
            smtp_username=_secret("SMTP_USERNAME", "SMTP_USERNAME_FILE"),
            smtp_password=_secret("SMTP_PASSWORD", "SMTP_PASSWORD_FILE"),
            smtp_from=(os.getenv("SMTP_FROM") or "").strip() or None,
            smtp_starttls=_env_bool("SMTP_STARTTLS", True),
            smtp_ssl=_env_bool("SMTP_SSL", False),
            error_report_email_enabled=_env_bool("ERROR_REPORT_EMAIL_ENABLED", False),
            error_report_email_on_solaredge_failure=_env_bool(
                "ERROR_REPORT_EMAIL_ON_SOLAREDGE_FAILURE", True
            ),
            error_report_email_cooldown_seconds=_env_int(
                "ERROR_REPORT_EMAIL_COOLDOWN_SECONDS", 3600
            ),
            mock_grid_power_w=_env_float("MOCK_GRID_POWER_W", -2000.0),
            mock_solar_power_w=_env_float("MOCK_SOLAR_POWER_W", 5000.0),
            mock_tesla_current_a=_env_int("MOCK_TESLA_CURRENT_A", 6),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.mode not in {"dry-run", "live"}:
            raise ConfigurationError("MODE deve essere dry-run o live")
        if self.control_mode not in {"solar-production", "grid-surplus", "meter-closed-loop"}:
            raise ConfigurationError(
                "CONTROL_MODE deve essere solar-production, grid-surplus o meter-closed-loop"
            )
        if self.tesla_transport not in {"mock", "ble", "fleet"}:
            raise ConfigurationError("TESLA_TRANSPORT deve essere mock, ble o fleet")
        if self.energy_source not in {
            "mock",
            "alfa-modbus",
            "solaredge-web",
            "solaredge-cloud",
            "solaredge-modbus",
        }:
            raise ConfigurationError(
                "ENERGY_SOURCE deve essere mock, alfa-modbus, solaredge-web, "
                "solaredge-cloud o solaredge-modbus"
            )
        if self.poll_interval_seconds < 10:
            raise ConfigurationError("POLL_INTERVAL_SECONDS non può essere minore di 10")
        if self.energy_source == "alfa-modbus" and not self.alfa_modbus_host:
            raise ConfigurationError("ALFA_MODBUS_HOST è obbligatorio con ENERGY_SOURCE=alfa-modbus")
        if not 0 < self.alfa_modbus_timeout_seconds <= 30:
            raise ConfigurationError("ALFA_MODBUS_TIMEOUT_SECONDS deve essere tra 0 e 30")
        if not 10 <= self.alfa_control_interval_seconds <= 300:
            raise ConfigurationError("ALFA_CONTROL_INTERVAL_SECONDS deve essere tra 10 e 300")
        if not 10 <= self.solaredge_modbus_poll_interval_seconds <= 300:
            raise ConfigurationError(
                "SOLAREDGE_MODBUS_POLL_INTERVAL_SECONDS deve essere tra 10 e 300"
            )
        if self.grid_import_limit_w < 0:
            raise ConfigurationError("GRID_IMPORT_LIMIT_W non può essere negativo")
        if self.grid_import_emergency_w < self.grid_import_limit_w:
            raise ConfigurationError("GRID_IMPORT_EMERGENCY_W deve essere >= GRID_IMPORT_LIMIT_W")
        if not 0 <= self.grid_hold_band_w <= 2000:
            raise ConfigurationError("GRID_HOLD_BAND_W deve essere tra 0 e 2000 W")
        if not 1 <= self.grid_surplus_stable_reads <= 10:
            raise ConfigurationError("GRID_SURPLUS_STABLE_READS deve essere tra 1 e 10")
        if self.power_quota_target_w <= 0:
            raise ConfigurationError("POWER_QUOTA_TARGET_W deve essere positivo")
        if not 0 <= self.power_quota_hysteresis_w <= 5000:
            raise ConfigurationError("POWER_QUOTA_HYSTERESIS_W deve essere tra 0 e 5000 W")
        if self.cloud_poll_interval_seconds < 300:
            raise ConfigurationError(
                "CLOUD_POLL_INTERVAL_SECONDS deve essere >= 300 per rispettare 300 richieste/giorno"
            )
        if self.expected_phases not in {1, 3}:
            raise ConfigurationError("EXPECTED_PHASES deve essere 1 o 3")
        if not 100 <= self.min_voltage_v < self.max_voltage_v <= 300:
            raise ConfigurationError("Intervallo MIN_VOLTAGE_V/MAX_VOLTAGE_V non valido")
        if not 0 < self.solar_utilization_percent <= 100:
            raise ConfigurationError("SOLAR_UTILIZATION_PERCENT deve essere tra 0 e 100")
        if (self.solar_latitude is None) != (self.solar_longitude is None):
            raise ConfigurationError("SOLAR_LATITUDE e SOLAR_LONGITUDE vanno impostate insieme")
        if self.solar_latitude is not None and not -90 <= self.solar_latitude <= 90:
            raise ConfigurationError("SOLAR_LATITUDE non valida")
        if self.solar_longitude is not None and not -180 <= self.solar_longitude <= 180:
            raise ConfigurationError("SOLAR_LONGITUDE non valida")
        if self.max_grid_current_a <= 0:
            raise ConfigurationError("MAX_GRID_CURRENT_A deve essere positivo")
        if not 1 <= self.min_charge_amps <= self.max_charge_amps:
            raise ConfigurationError("Limiti MIN_CHARGE_AMPS/MAX_CHARGE_AMPS non validi")
        if self.solaredge_grid_power_sign not in {-1, 1}:
            raise ConfigurationError("SOLAREDGE_GRID_POWER_SIGN deve essere 1 o -1")
        if self.solaredge_inverter_base <= 0 or self.solaredge_meter_base <= 0:
            raise ConfigurationError("Basi registri SolarEdge Modbus non valide")
        if self.energy_source == "solaredge-cloud":
            if not self.solaredge_api_key or self.solaredge_site_id is None:
                raise ConfigurationError(
                    "Per solaredge-cloud servono SOLAREDGE_API_KEY(_FILE) e SOLAREDGE_SITE_ID"
                )
        if self.energy_source == "solaredge-web":
            if (
                not self.solaredge_username
                or not self.solaredge_password
                or self.solaredge_site_id is None
            ):
                raise ConfigurationError(
                    "Per solaredge-web servono SOLAREDGE_USERNAME_FILE, "
                    "SOLAREDGE_PASSWORD_FILE e SOLAREDGE_SITE_ID"
                )
        if self.energy_source == "solaredge-modbus" and not self.solaredge_modbus_host:
            raise ConfigurationError("Per solaredge-modbus serve SOLAREDGE_MODBUS_HOST")
        if not 1 <= self.vimar_port <= 65535:
            raise ConfigurationError("VIMAR_PORT non valido")
        if self.vimar_timeout_seconds < 1:
            raise ConfigurationError("VIMAR_TIMEOUT_SECONDS deve essere positivo")
        if not self.vimar_client_name:
            raise ConfigurationError("VIMAR_CLIENT_NAME non può essere vuoto")
        if self.tesla_transport == "ble" and not self.tesla_vin:
            raise ConfigurationError("Per Tesla BLE serve TESLA_VIN")
        if self.tesla_ble_retries < 0 or self.tesla_ble_recovery_threshold < 1:
            raise ConfigurationError("Retry/soglia recovery Tesla BLE non validi")
        if self.tesla_ble_retry_backoff_seconds < 0:
            raise ConfigurationError("TESLA_BLE_RETRY_BACKOFF_SECONDS non può essere negativo")
        if min(
            self.tesla_ble_timeout_seconds,
            self.tesla_ble_connect_timeout_seconds,
            self.tesla_ble_command_timeout_seconds,
        ) < 1:
            raise ConfigurationError("I timeout Tesla BLE devono essere positivi")
        if self.tesla_data_source not in {"vehicle", "wall-connector"}:
            raise ConfigurationError("TESLA_DATA_SOURCE deve essere vehicle o wall-connector")
        if self.tesla_data_source == "wall-connector" and not self.wall_connector_host:
            raise ConfigurationError(
                "WALL_CONNECTOR_HOST è obbligatorio con TESLA_DATA_SOURCE=wall-connector"
            )
        if self.wall_connector_phases not in {1, 3}:
            raise ConfigurationError("WALL_CONNECTOR_PHASES deve essere 1 o 3")
        if not 0 < self.wall_connector_timeout_seconds <= 10:
            raise ConfigurationError("WALL_CONNECTOR_TIMEOUT_SECONDS deve essere tra 0 e 10")
        if not 0 <= self.wall_connector_min_current_a <= 2:
            raise ConfigurationError("WALL_CONNECTOR_MIN_CURRENT_A deve essere tra 0 e 2")
        if self.wall_connector_poll_interval_seconds < 10:
            raise ConfigurationError("WALL_CONNECTOR_POLL_INTERVAL_SECONDS deve essere >= 10")
        if self.tesla_transport == "fleet" and (not self.tesla_vin or not self.tesla_client_id):
            raise ConfigurationError("Per Tesla Fleet servono TESLA_VIN e TESLA_CLIENT_ID")
        if not 1 <= self.web_port <= 65535:
            raise ConfigurationError("WEB_PORT non valido")
        if self.web_session_ttl_seconds < 300:
            raise ConfigurationError("WEB_SESSION_TTL_SECONDS deve essere almeno 300")
        if self.data_retention_days < 1:
            raise ConfigurationError("DATA_RETENTION_DAYS deve essere almeno 1")
        if not self.web_username:
            raise ConfigurationError("WEB_USERNAME non può essere vuoto")
        if not self.web_viewer_username:
            raise ConfigurationError("WEB_VIEWER_USERNAME non può essere vuoto")
        if self.web_username == self.web_viewer_username:
            raise ConfigurationError("WEB_USERNAME e WEB_VIEWER_USERNAME devono differire")
        if not 1 <= self.tuya_mqtt_port <= 65535:
            raise ConfigurationError("TUYA_MQTT_PORT non valido")
        if self.tuya_keepalive_seconds < 30:
            raise ConfigurationError("TUYA_KEEPALIVE_SECONDS deve essere almeno 30")
        if self.tuya_report_interval_seconds < 5:
            raise ConfigurationError("TUYA_REPORT_INTERVAL_SECONDS deve essere almeno 5")
        if not 1 <= self.tuya_average_samples <= 60:
            raise ConfigurationError("TUYA_AVERAGE_SAMPLES deve essere tra 1 e 60")
        if self.tuya_enabled and (not self.tuya_device_id or not self.tuya_device_secret):
            raise ConfigurationError(
                "Con TUYA_ENABLED=true servono TUYA_DEVICE_ID e TUYA_DEVICE_SECRET(_FILE)"
            )
        if self.extra_grid_power_w < 0:
            raise ConfigurationError("EXTRA_GRID_POWER_W non può essere negativo")
        if not self.min_charge_amps < self.manual_override_amps <= self.max_charge_amps:
            raise ConfigurationError(
                "MANUAL_OVERRIDE_AMPS deve stare sopra la corrente minima ed entro il limite hardware"
            )
        if self.event_email_cooldown_seconds < 0:
            raise ConfigurationError("EVENT_EMAIL_COOLDOWN_SECONDS non valido")
        if self.error_report_email_cooldown_seconds < 0:
            raise ConfigurationError("ERROR_REPORT_EMAIL_COOLDOWN_SECONDS non valido")
        if self.notify_timeout_seconds < 1:
            raise ConfigurationError("NOTIFY_TIMEOUT_SECONDS deve essere positivo")
        if self.notify_backend not in {"wordpress", "smtp"}:
            raise ConfigurationError("NOTIFY_BACKEND deve essere wordpress o smtp")
        if not 1 <= self.smtp_port <= 65535:
            raise ConfigurationError("SMTP_PORT non valida")
        if self.smtp_starttls and self.smtp_ssl:
            raise ConfigurationError("SMTP_STARTTLS e SMTP_SSL non possono essere entrambi true")
        if bool(self.smtp_username) != bool(self.smtp_password):
            raise ConfigurationError("SMTP_USERNAME e SMTP_PASSWORD vanno configurati insieme")
        if self.event_email_enabled or self.error_report_email_enabled:
            missing = []
            if self.notify_backend == "wordpress":
                if not self.notify_api_url:
                    missing.append("NOTIFY_API_URL")
                if not self.notify_api_key:
                    missing.append("NOTIFY_API_KEY")
                if not self.notify_api_user:
                    missing.append("NOTIFY_API_USER")
            else:
                if not self.smtp_host:
                    missing.append("SMTP_HOST")
                if not self.smtp_from:
                    missing.append("SMTP_FROM")
            if missing:
                raise ConfigurationError(
                    f"Per inviare mail via {self.notify_backend} configurare "
                    + ", ".join(missing)
                )
