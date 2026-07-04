import json
import re
import sqlite3
import stat
import time
import zipfile
from dataclasses import replace
from datetime import datetime
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from tesla_energy_controller.config import Settings
from tesla_energy_controller.main import build_controller
from tesla_energy_controller.diagnostics import EmailReportResult
from tesla_energy_controller.live_status import read_status_cache
from tesla_energy_controller.models import ChargeState, GridMeasurement
from tesla_energy_controller.runtime import RuntimeSettingsStore
from tesla_energy_controller.solar import SolarEdgeAccessError
from tesla_energy_controller.storage import EnergyDatabase
from tesla_energy_controller.tesla import BLEErrorCategory, TeslaBLEError, WallConnectorVitals
from tesla_energy_controller.vimar import VimarEnergyPoint
from tesla_energy_controller.web import create_app


def application(monkeypatch, tmp_path):
    monkeypatch.setenv("MODE", "dry-run")
    monkeypatch.setenv("CONTROL_MODE", "solar-production")
    monkeypatch.setenv("ENERGY_SOURCE", "mock")
    monkeypatch.setenv("TESLA_MOCK", "true")
    monkeypatch.setenv("TESLA_TRANSPORT", "mock")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "300")
    monkeypatch.setenv("WEB_PASSWORD", "password-locale-molto-lunga")
    monkeypatch.setenv("RUNTIME_SETTINGS_FILE", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("ENERGY_DATABASE_FILE", str(tmp_path / "energy.sqlite3"))
    settings = Settings.from_env()
    return create_app(settings, build_controller(settings), start_scheduler=False), settings


def login(client):
    response = client.post(
        "/login",
        data={"password": "password-locale-molto-lunga"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def login_as(client, username: str, password: str):
    response = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303


def csrf(page: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', page)
    assert match
    return match.group(1)


def valid_settings_payload(token: str, **overrides):
    payload = {
        "csrf": token,
        "enabled": "on",
        "schedule_mode": "fixed",
        "fixed_start_time": "06:00",
        "fixed_end_time": "19:00",
        "latitude": "",
        "longitude": "",
        "sunrise_offset_minutes": "0",
        "sunset_offset_minutes": "0",
        "solar_source": "mock",
        "expected_phases": "3",
        "extra_grid_power_a": "2",
        "manual_override_amps": "16",
        "min_voltage_v": "205",
        "max_voltage_v": "250",
        "min_charge_amps": "5",
        "command_hysteresis_a": "1",
        "max_ramp_up_a": "2",
        "tesla_ble_connect_timeout_seconds": "20",
        "tesla_ble_command_timeout_seconds": "10",
        "tesla_ble_retries": "2",
        "anomaly_peak_threshold_kw": "1.5",
        "anomaly_device_patterns": "forno, frighi, cucina, lavatrice, lavastoviglie",
        "anomaly_device_groups": (
            "Cucina | 1.5 | forno, cucina, lavastoviglie\n"
            "Frighi | 0.8 | frigo, frighi"
        ),
        "anomaly_window_mode": "sunset_sunrise",
        "anomaly_fixed_start_time": "22:00",
        "anomaly_fixed_end_time": "06:00",
    }
    payload.update(overrides)
    return payload


def test_dashboard_requires_password(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    with app.test_client() as client:
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_wrong_password_is_rejected(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    with app.test_client() as client:
        response = client.post("/login", data={"password": "sbagliata"})
        assert response.status_code == 401
        assert response.headers.getlist("Set-Cookie") == []


def test_login_cookie_and_security_headers(monkeypatch, tmp_path):
    app, settings = application(monkeypatch, tmp_path)
    with app.test_client() as client:
        login(client)
        response = client.get("/")
        assert response.status_code == 200
        assert 'class="metrics"' in response.text
        assert settings.web_password not in response.text
        assert response.headers["x-frame-options"] == "DENY"
        assert "default-src 'none'" in response.headers["content-security-policy"]
        assert "img-src 'self'" in response.headers["content-security-policy"]


def test_env_password_bootstrap_does_not_overwrite_existing_admin_user(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    assert runtime.authenticate("admin", "password-locale-molto-lunga")

    assert runtime.db.change_password(
        "admin", "password-locale-molto-lunga", "password-temporanea-nuova"
    )
    runtime.db.ensure_user("admin", "password-locale-molto-lunga", "admin")

    assert runtime.authenticate("admin", "password-locale-molto-lunga") is None
    assert runtime.authenticate("admin", "password-temporanea-nuova")


def test_runtime_defaults_enable_mail_and_ble_recovery(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]

    assert runtime.current.error_email_enabled is True
    assert runtime.current.anomaly_email_enabled is True
    assert runtime.current.tesla_ble_recovery_enabled is True
    assert runtime.current.power_quota_target_w == 7000
    assert runtime.reporter.enabled is True


def test_power_quota_pause_is_restored_from_hold_measurement_on_boot(monkeypatch, tmp_path):
    monkeypatch.setenv("MODE", "dry-run")
    monkeypatch.setenv("CONTROL_MODE", "solar-production")
    monkeypatch.setenv("ENERGY_SOURCE", "mock")
    monkeypatch.setenv("TESLA_MOCK", "true")
    monkeypatch.setenv("TESLA_TRANSPORT", "mock")
    monkeypatch.setenv("WEB_PASSWORD", "password-locale-molto-lunga")
    monkeypatch.setenv("RUNTIME_SETTINGS_FILE", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("ENERGY_DATABASE_FILE", str(tmp_path / "energy.sqlite3"))
    settings = Settings.from_env()

    store = RuntimeSettingsStore(settings.runtime_settings_file, settings)
    store.save(replace(store.load(), alfa_grid_reading_enabled=True))
    db = EnergyDatabase(settings.energy_database_file)
    db.add_measurement(
        {
            "observed_at": "2026-07-01T15:39:42+02:00",
            "solar_power_w": 1000,
            "vimar_power_w": 1000,
            "tesla_power_w": 0,
            "total_consumption_w": 1000,
            "import_power_w": 2500,
            "export_power_w": 0,
            "tesla_current_a": 5,
            "tesla_target_a": 0,
            "controller_enabled": True,
            "action": "hold",
            "reason": "Tesla sospesa: attendo margine sulla quota 15 min",
            "alfa_grid_reading_enabled": True,
        },
        [],
    )
    db.add_measurement(
        {
            "observed_at": "2026-07-01T15:42:48+02:00",
            "solar_power_w": 1000,
            "vimar_power_w": 1000,
            "tesla_power_w": 0,
            "total_consumption_w": 1000,
            "import_power_w": 2000,
            "export_power_w": 0,
            "tesla_current_a": 0,
            "tesla_target_a": 0,
            "controller_enabled": True,
            "action": "skip",
            "reason": "Tesla non in carica (Stopped)",
            "alfa_grid_reading_enabled": True,
        },
        [],
    )

    app = create_app(settings, build_controller(settings), start_scheduler=False)
    runtime = app.extensions["energy_runtime"]

    assert runtime.current.alfa_grid_reading_enabled is True
    assert runtime.controller._paused_for_power_quota is True


def test_admin_can_change_password_with_current_password(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    with app.test_client() as client:
        login(client)
        page = client.get("/").text
        token = csrf(page)
        assert 'id="current_password" name="current_password" type="password" autocomplete="off"' in page
        assert 'id="new_password" name="new_password" type="password" autocomplete="off"' in page

        wrong = client.post(
            "/password",
            data={
                "csrf": token,
                "current_password": "sbagliata",
                "new_password": "password-nuova-locale",
                "confirm_password": "password-nuova-locale",
            },
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        assert wrong.status_code == 400
        assert runtime.authenticate("admin", "password-locale-molto-lunga")

        changed = client.post(
            "/password",
            data={
                "csrf": token,
                "current_password": "password-locale-molto-lunga",
                "new_password": "password-nuova-locale",
                "confirm_password": "password-nuova-locale",
            },
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        assert changed.status_code == 200
        assert changed.get_json()["message"] == "Password aggiornata"
        assert runtime.authenticate("admin", "password-locale-molto-lunga") is None
        assert runtime.authenticate("admin", "password-nuova-locale")


def test_viewer_can_change_password_but_not_see_admin_panels(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.db.ensure_user("viewer", "password-viewer-locale", "viewer")
    with app.test_client() as client:
        login_as(client, "viewer", "password-viewer-locale")
        page = client.get("/").text
        token = csrf(page)

        assert 'data-tab="user"' in page
        assert 'data-tab="settings"' not in page
        assert 'data-tab="log"' not in page
        assert 'data-tab="backup"' not in page
        assert "Gestione utenti" not in page
        assert "Cambia password" in page

        runtime_payload = client.get("/api/runtime").get_json()
        assert runtime_payload["can_configure"] is False
        assert "users" not in runtime_payload
        assert "error_events" not in runtime_payload

        denied = client.post(
            "/settings",
            data=valid_settings_payload(token),
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        assert denied.status_code == 403

        denied_backup = client.post(
            "/backup/export",
            data={"csrf": token, "include_db": "on", "include_config": "on"},
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        assert denied_backup.status_code == 403

        denied_import = client.post(
            "/backup/import",
            data={"csrf": token, "backup_file": (BytesIO(b"not a zip"), "backup.zip")},
            content_type="multipart/form-data",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        assert denied_import.status_code == 403

        changed = client.post(
            "/password",
            data={
                "csrf": token,
                "current_password": "password-viewer-locale",
                "new_password": "password-viewer-nuova",
                "confirm_password": "password-viewer-nuova",
            },
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        assert changed.status_code == 200
        assert runtime.authenticate("viewer", "password-viewer-locale") is None
        assert runtime.authenticate("viewer", "password-viewer-nuova")


def test_admin_can_create_user_with_generated_password(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    with app.test_client() as client:
        login(client)
        token = csrf(client.get("/").text)

        response = client.post(
            "/users",
            data={
                "csrf": token,
                "email": "nuovo@example.com",
                "username": "",
                "role": "viewer",
            },
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["generated_username"] == "nuovo@example.com"
        assert len(payload["generated_password"]) >= 12
        assert "password_hash" not in json.dumps(payload)
        assert {"username": "nuovo@example.com", "email": "nuovo@example.com", "role": "viewer"} in payload["users"]
        assert runtime.authenticate("nuovo@example.com", payload["generated_password"])

        duplicate = client.post(
            "/users",
            data={
                "csrf": token,
                "email": "nuovo@example.com",
                "username": "nuovo@example.com",
                "role": "viewer",
            },
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        assert duplicate.status_code == 409


def test_viewer_can_update_self_but_not_manage_users(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.db.ensure_user("viewer", "password-viewer-locale", "viewer")
    with app.test_client() as client:
        login_as(client, "viewer", "password-viewer-locale")
        token = csrf(client.get("/").text)

        profile = client.post(
            "/account",
            data={"csrf": token, "email": "viewer@example.com"},
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        assert profile.status_code == 200
        assert profile.get_json()["account"]["email"] == "viewer@example.com"
        assert runtime.db.get_user("viewer")["email"] == "viewer@example.com"

        denied_update = client.post(
            "/users/update",
            data={
                "csrf": token,
                "username": "viewer",
                "email": "x@example.com",
                "role": "admin",
            },
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        assert denied_update.status_code == 403

        denied_delete = client.post(
            "/users/delete",
            data={"csrf": token, "username": "viewer"},
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        assert denied_delete.status_code == 403


def test_admin_can_update_and_delete_other_users(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.db.ensure_user("viewer", "password-viewer-locale", "viewer")
    with app.test_client() as client:
        login(client)
        token = csrf(client.get("/").text)

        updated = client.post(
            "/users/update",
            data={
                "csrf": token,
                "username": "viewer",
                "email": "viewer-updated@example.com",
                "role": "admin",
            },
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        assert updated.status_code == 200
        assert runtime.db.get_user("viewer") == {
            "username": "viewer",
            "email": "viewer-updated@example.com",
            "role": "admin",
        }
        assert runtime.authenticate("viewer", "password-viewer-locale")["role"] == "admin"

        self_delete = client.post(
            "/users/delete",
            data={"csrf": token, "username": "admin"},
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        assert self_delete.status_code == 400

        deleted = client.post(
            "/users/delete",
            data={"csrf": token, "username": "viewer"},
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        assert deleted.status_code == 200
        assert runtime.db.get_user("viewer") is None
        assert runtime.authenticate("viewer", "password-viewer-locale") is None


def test_admin_email_is_used_as_notification_recipient(monkeypatch, tmp_path):
    monkeypatch.setenv("EVENT_EMAIL_ENABLED", "true")
    monkeypatch.setenv("NOTIFY_API_URL", "https://example.com/wp-json/mail")
    monkeypatch.setenv("NOTIFY_API_KEY", "secret")
    monkeypatch.setenv("NOTIFY_API_USER", "user:pass")
    monkeypatch.delenv("NOTIFY_RECIPIENTS", raising=False)
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]

    assert runtime.event_reporter.sender.recipients == ()
    runtime.db.update_user_email("admin", "admin@example.com")
    runtime.refresh_mail_recipients()

    assert runtime.event_reporter.sender.recipients == ("admin@example.com",)
    assert runtime.reporter.sender.recipients == ("admin@example.com",)


def test_settings_are_validated_and_saved_with_private_permissions(monkeypatch, tmp_path):
    app, settings = application(monkeypatch, tmp_path)
    with app.test_client() as client:
        login(client)
        token = csrf(client.get("/").text)
        response = client.post(
            "/settings",
            data={
                "csrf": token,
                "enabled": "on",
                "schedule_mode": "fixed",
                "fixed_start_time": "06:00",
                "fixed_end_time": "19:00",
                "latitude": "",
                "longitude": "",
                "sunrise_offset_minutes": "0",
                "sunset_offset_minutes": "0",
                "min_voltage_v": "205",
                "max_voltage_v": "250",
                "min_charge_amps": "5",
                "max_charge_amps": "13",
                "command_hysteresis_a": "1",
                "max_ramp_up_a": "2",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

    path = tmp_path / "runtime.json"
    saved = json.loads(path.read_text())
    assert saved["min_charge_amps"] == 5
    assert saved["max_charge_amps"] == 13
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert settings.web_password not in path.read_text()


def test_settings_accept_min_charge_below_hardware_minimum(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    with app.test_client() as client:
        login(client)
        token = csrf(client.get("/").text)
        response = client.post(
            "/settings",
            data=valid_settings_payload(token, min_charge_amps="1"),
            follow_redirects=False,
        )

    assert response.status_code == 303
    saved = json.loads((tmp_path / "runtime.json").read_text())
    assert saved["min_charge_amps"] == 1


def test_notification_toggles_are_saved_separately(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    with app.test_client() as client:
        login(client)
        token = csrf(client.get("/").text)

        enabled = client.post(
            "/settings",
            data=valid_settings_payload(
                token,
                error_email_enabled="on",
                anomaly_email_enabled="on",
            ),
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        assert enabled.status_code == 200
        assert runtime.current.error_email_enabled is True
        assert runtime.current.anomaly_email_enabled is True
        assert runtime.reporter.enabled is True

        disabled = client.post(
            "/settings",
            data=valid_settings_payload(token),
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        assert disabled.status_code == 200
        assert runtime.current.error_email_enabled is False
        assert runtime.current.anomaly_email_enabled is False
        assert runtime.reporter.enabled is False


def test_settings_save_refreshes_cache_without_controller_decision(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.controller.grid.read = lambda: GridMeasurement(
        total_power_w=0,
        solar_power_w=2400,
        fresh=False,
    )
    runtime.controller.vehicle.state = replace(
        runtime.controller.vehicle.state,
        current_request_a=5,
        actual_current_a=5.0,
        charger_power_kw=5 * 230 * 3 / 1000,
    )
    with app.test_client() as client:
        login(client)
        token = csrf(client.get("/").text)

        response = client.post(
            "/settings",
            data=valid_settings_payload(
                token,
                fixed_start_time="00:00",
                fixed_end_time="23:59",
                extra_grid_power_a="0",
                manual_override_amps="14",
                min_charge_amps="6",
            ),
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"]["action"] == "preview"
    assert payload["status"]["solar_power_w"] == 2400
    assert "target_a" not in payload["status"]
    assert runtime.db.latest_measurements(1) == []


def test_csrf_is_required(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    with app.test_client() as client:
        login(client)
        response = client.post("/run-now", data={"csrf": "wrong"})
        assert response.status_code == 403


def test_run_now_json_queues_when_cycle_is_busy(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    with app.test_client() as client:
        login(client)
        token = csrf(client.get("/").text)
        assert runtime.lock.acquire(blocking=False)
        try:
            response = client.post(
                "/run-now",
                data={"csrf": token},
                headers={"Accept": "application/json", "X-Requested-With": "fetch"},
            )
        finally:
            runtime.lock.release()

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["message"] == "Aggiornamento accodato"
    assert payload["status"]["controller_enabled"] is True
    deadline = time.monotonic() + 2
    while runtime.run_now_running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runtime.run_now_running is False


def test_web_rejects_override_above_hardware(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    with app.test_client() as client:
        login(client)
        token = csrf(client.get("/").text)
        response = client.post(
            "/settings",
            data={
                "csrf": token,
                "enabled": "on",
                "schedule_mode": "fixed",
                "fixed_start_time": "06:00",
                "fixed_end_time": "19:00",
                "latitude": "",
                "longitude": "",
                "sunrise_offset_minutes": "0",
                "sunset_offset_minutes": "0",
                "min_voltage_v": "200",
                "max_voltage_v": "255",
                "min_charge_amps": "5",
                "manual_override_amps": "32",
                "command_hysteresis_a": "1",
                "max_ramp_up_a": "2",
            },
        )
        assert response.status_code == 400
        assert "override" in response.text.lower()
        assert not (tmp_path / "runtime.json").exists()


def test_run_now_refreshes_cache_without_saving_or_deciding(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current, schedule_mode="fixed", fixed_start_time="00:00", fixed_end_time="23:59"
    )
    runtime.store.save(runtime.current)
    with app.test_client() as client:
        login(client)
        token = csrf(client.get("/").text)
        response = client.post("/run-now", data={"csrf": token}, follow_redirects=False)
        assert response.status_code == 303
        status = client.get("/api/status").get_json()
        assert status["voltage_v"] == 230
        assert status["solar_power_w"] == 5000
        assert status["action"] == "preview"
        assert "target_a" not in status
        series = client.get("/api/series").get_json()
        assert series["points"] == []
        assert runtime.db.latest_measurements(1) == []


def test_preview_cycle_keeps_last_target_without_saving(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current, schedule_mode="fixed", fixed_start_time="00:00", fixed_end_time="23:59"
    )
    runtime.store.save(runtime.current)

    first = runtime.run_cycle(datetime(2026, 7, 1, 16, 10, tzinfo=ZoneInfo("Europe/Rome")))
    saved_before = len(runtime.db.latest_measurements(10))
    preview = runtime.run_cycle(
        datetime(2026, 7, 1, 16, 10, 30, tzinfo=ZoneInfo("Europe/Rome")),
        persist=False,
        control=False,
    )

    assert first["target_a"] is not None
    assert preview["action"] == "preview"
    assert preview["target_a"] == first["target_a"]
    assert len(runtime.db.latest_measurements(10)) == saved_before


def test_series_target_w_is_home_plus_tesla_target(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.db.add_measurement(
        {
            "observed_at": "2026-06-25T13:00:00+02:00",
            "solar_power_w": 5000,
            "vimar_power_w": 700,
            "tesla_power_w": 3450,
            "total_consumption_w": 4150,
            "import_power_w": 0,
            "export_power_w": 850,
            "tesla_current_a": 6,
            "tesla_target_a": 5,
            "controller_enabled": True,
        },
        [],
    )
    with app.test_client() as client:
        login(client)
        series = client.get("/api/series").get_json()
        assert series["points"][-1]["target"] == 5
        assert series["points"][-1]["target_w"] == 700 + 5 * 230 * 3


def test_series_flags_manual_override_target(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.db.add_measurement(
        {
            "observed_at": "2026-06-25T13:00:00+02:00",
            "solar_power_w": 5000,
            "vimar_power_w": 700,
            "tesla_power_w": 10488,
            "total_consumption_w": 11200,
            "import_power_w": 6200,
            "export_power_w": 0,
            "tesla_current_a": 15.8,
            "tesla_target_a": 16,
            "controller_enabled": True,
            "action": "manual-override",
            "reason": "override manuale: Tesla impostata a 16 A",
        },
        [],
    )
    with app.test_client() as client:
        login(client)
        point = client.get("/api/series").get_json()["points"][-1]
        assert point["target"] == 16
        assert point["manual_override"] is True


def test_series_exposes_house_for_main_chart_in_alfa_mode(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(runtime.current, alfa_grid_reading_enabled=True)
    runtime.store.save(runtime.current)
    runtime.db.add_measurement(
        {
            "observed_at": "2026-06-25T13:00:00+02:00",
            "solar_power_w": 5000,
            "vimar_power_w": 700,
            "tesla_power_w": 3450,
            "total_consumption_w": 6150,
            "import_power_w": 1150,
            "export_power_w": 0,
            "tesla_current_a": 5,
            "tesla_target_a": 5,
            "controller_enabled": True,
            "alfa_grid_reading_enabled": True,
        },
        [],
    )

    with app.test_client() as client:
        login(client)
        series = client.get("/api/series").get_json()
        assert series["alfa_grid_reading_enabled"] is True
        point = series["points"][-1]
        assert point["house"] == 2700
        assert point["appliances"] == 700
        assert point["device"] == 2000


def test_series_is_bucketed_to_five_minutes_with_recent_weight(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    for stamp, solar in (
        ("2026-06-25T12:01:00+02:00", 1000),
        ("2026-06-25T12:04:00+02:00", 5000),
        ("2026-06-25T12:07:00+02:00", 2000),
    ):
        runtime.db.add_measurement(
            {
                "observed_at": stamp,
                "solar_power_w": solar,
                "vimar_power_w": 700,
                "tesla_power_w": 0,
                "total_consumption_w": 700,
                "import_power_w": 0,
                "export_power_w": max(solar - 700, 0),
                "controller_enabled": True,
            },
            [],
        )

    with app.test_client() as client:
        login(client)
        series = client.get("/api/series?day=2026-06-25").get_json()

    assert series["sample_seconds"] == 300
    assert [point["t"] for point in series["points"]] == ["12:00", "12:05"]
    assert 3000 < series["points"][0]["solar"] < 5000
    assert series["points"][1]["solar"] == 2000


def test_series_target_w_stays_home_plus_tesla_target_when_tesla_power_is_zero(
    monkeypatch, tmp_path
):
    # Se il controller ha un target valido, la linea mostra casa + target Tesla
    # anche durante una ripartenza con assorbimento Tesla ancora a 0 W.
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.db.add_measurement(
        {
            "observed_at": "2026-06-25T13:00:00+02:00",
            "solar_power_w": 5000,
            "vimar_power_w": 700,
            "tesla_power_w": 0,
            "total_consumption_w": 700,
            "import_power_w": 0,
            "export_power_w": 4300,
            "tesla_current_a": 0,
            "tesla_target_a": 5,
            "controller_enabled": True,
            "action": "set",
        },
        [],
    )
    with app.test_client() as client:
        login(client)
        series = client.get("/api/series").get_json()
        assert series["points"][-1]["target"] == 5
        assert series["points"][-1]["target_w"] == 700 + 5 * 230 * 3


def test_series_target_w_is_zero_when_tesla_is_offline(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.db.add_measurement(
        {
            "observed_at": "2026-06-25T13:00:00+02:00",
            "solar_power_w": 5000,
            "vimar_power_w": 700,
            "tesla_power_w": 3500,
            "total_consumption_w": 4200,
            "import_power_w": 0,
            "export_power_w": 800,
            "tesla_current_a": 6,
            "tesla_target_a": 5,
            "controller_enabled": True,
            "action": "set",
        },
        [],
    )
    runtime.db.add_measurement(
        {
            "observed_at": "2026-06-25T13:05:00+02:00",
            "solar_power_w": 5000,
            "vimar_power_w": 700,
            "tesla_power_w": 0,
            "total_consumption_w": 700,
            "import_power_w": 0,
            "export_power_w": 4300,
            "tesla_current_a": None,
            "tesla_target_a": None,
            "controller_enabled": True,
            "action": None,
            "reason": "Tesla non raggiungibile via BLE",
        },
        [],
    )
    with app.test_client() as client:
        login(client)
        series = client.get("/api/series").get_json()
        assert series["points"][0]["target_w"] > 0
        assert series["points"][-1]["target_w"] == 0


def test_series_target_w_is_zero_when_wall_connector_is_disconnected(
    monkeypatch, tmp_path
):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.db.add_measurement(
        {
            "observed_at": "2026-06-25T13:00:00+02:00",
            "solar_power_w": 5000,
            "vimar_power_w": 700,
            "tesla_power_w": 3500,
            "total_consumption_w": 4200,
            "import_power_w": 0,
            "export_power_w": 800,
            "tesla_current_a": 6,
            "tesla_target_a": 5,
            "controller_enabled": True,
            "action": "set",
        },
        [],
    )
    runtime.db.add_measurement(
        {
            "observed_at": "2026-06-25T13:05:00+02:00",
            "solar_power_w": 5000,
            "vimar_power_w": 700,
            "tesla_power_w": 0,
            "total_consumption_w": 700,
            "import_power_w": 0,
            "export_power_w": 4300,
            "tesla_current_a": None,
            "tesla_target_a": None,
            "controller_enabled": True,
            "action": "wall-connector-monitor",
            "reason": "Dati Tesla da Wall Connector; BLE non interrogato",
        },
        [],
    )
    with app.test_client() as client:
        login(client)
        series = client.get("/api/series").get_json()
        assert series["points"][0]["target_w"] > 0
        assert series["points"][-1]["target"] == 0
        assert series["points"][-1]["target_w"] == 0


def test_series_target_w_uses_minimum_outside_solar_window_when_tesla_active(
    monkeypatch,
    tmp_path,
):
    # Fuori dalla finestra solare il target non insegue il FV, ma se la Tesla
    # sta assorbendo davvero resta visibile come minimo monitorato.
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.db.add_measurement(
        {
            "observed_at": "2026-06-25T13:00:00+02:00",
            "solar_power_w": 5000,
            "vimar_power_w": 700,
            "tesla_power_w": 3500,
            "total_consumption_w": 4200,
            "import_power_w": 0,
            "export_power_w": 800,
            "tesla_current_a": 6,
            "tesla_target_a": 5,
            "controller_enabled": True,
            "action": "set",
        },
        [],
    )
    runtime.db.add_measurement(
        {
            "observed_at": "2026-06-25T22:00:00+02:00",
            "solar_power_w": 0,
            "vimar_power_w": 400,
            "tesla_power_w": 3300,
            "total_consumption_w": 3700,
            "import_power_w": 3700,
            "export_power_w": 0,
            "tesla_current_a": 5,
            "tesla_target_a": None,
            "controller_enabled": True,
            "action": "outside-window",
            "reason": "Fuori dalla finestra solare 06:00–19:00",
        },
        [],
    )
    with app.test_client() as client:
        login(client)
        series = client.get("/api/series").get_json()
        assert series["points"][0]["target_w"] > 0
        assert series["points"][-1]["target"] == 5
        assert series["points"][-1]["target_w"] == 400 + 5 * 230 * 3


def test_static_assets_are_cache_busted(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    with app.test_client() as client:
        login(client)
        html = client.get("/").text
    assert "dashboard.css?v=" in html
    assert "dashboard.js?v=" in html


def test_monthly_peak_import_in_status(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    ym = datetime.now().strftime("%Y-%m")
    for day, imp in (("05", 2200), ("12", 4850), ("18", 3100)):
        for minute in ("00", "05", "10"):
            runtime.db.add_measurement(
                {
                    "observed_at": f"{ym}-{day}T13:{minute}:00+02:00",
                    "solar_power_w": 0,
                    "vimar_power_w": 800,
                    "tesla_power_w": 0,
                    "total_consumption_w": 800,
                    "import_power_w": imp,
                    "export_power_w": 0,
                    "controller_enabled": True,
                },
                [],
            )
    with app.test_client() as client:
        login(client)
        status = client.get("/api/status").get_json()
        assert status["monthly_peak_import_w"] == 4850


def test_runtime_api_requires_session(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    with app.test_client() as client:
        response = client.get("/api/runtime")
        assert response.status_code == 401


def test_runtime_api_exposes_current_settings(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    with app.test_client() as client:
        login(client)
        response = client.get("/api/runtime")
        payload = response.get_json()
        assert response.status_code == 200
        assert payload["current"]["enabled"] is True
        assert payload["current"]["alfa_grid_reading_enabled"] is False
        assert payload["current"]["solar_source"] == "mock"
        assert payload["status"]["solar_source"] == "mock"
        assert payload["config"]["solar_source"] == "mock"
        assert payload["status"]["controller_enabled"] is True
        assert payload["can_configure"] is True


def test_runtime_api_sanitizes_local_config(monkeypatch, tmp_path):
    monkeypatch.setenv("TESLA_VIN", "5YJ3E7EA7KF317000")
    monkeypatch.setenv("TESLA_CONTROL_BINARY", "/usr/local/bin/tesla-control")
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.db.add_event(
        observed_at="2026-06-25T12:00:00+02:00",
        kind="config_check",
        message="ok",
        details={
            "tesla_vin": "5YJ3E7EA7KF317000",
            "api_secret": "super-secret",
            "safe": "value",
        },
    )
    with app.test_client() as client:
        login(client)
        dashboard = client.get("/").get_data(as_text=True)
        response = client.get("/api/runtime")
        payload = response.get_json()
        raw = response.get_data(as_text=True)

        assert payload["config"]["tesla_vin_tail"] == "317000"
        assert payload["tesla_ble"]["vin_tail"] == "317000"
        assert "5YJ3*********7000" in dashboard
        assert 'data-vin="5YJ3E7EA7KF317000"' in dashboard
        assert payload["tesla_ble"]["binary"] == "tesla-control"
        assert "5YJ3E7EA7KF317000" not in raw
        assert "super-secret" not in raw
        assert "password-locale-molto-lunga" not in raw
        assert payload["events"][0]["details_json"]
        assert "redatto" in payload["events"][0]["details_json"]


def test_error_log_can_be_cleared_by_admin(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.db.add_event(
        observed_at="2026-06-25T12:00:00+02:00",
        kind="error_solaredge",
        level="error",
        message="SolarEdge webservice non disponibile",
        details={"endpoint": "https://monitoring.solaredge.com"},
    )
    runtime.db.add_event(
        observed_at="2026-06-25T12:01:00+02:00",
        kind="manual_override",
        level="info",
        message="info",
    )
    with app.test_client() as client:
        login(client)
        token = csrf(client.get("/").text)
        payload = client.get("/api/runtime").get_json()
        assert len(payload["error_events"]) == 1
        assert payload["error_events"][0]["kind"] == "error_solaredge"

        response = client.post(
            "/error-log/clear",
            data={"csrf": token},
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        payload = response.get_json()
        assert response.status_code == 200
        assert payload["error_events"] == []
        assert runtime.db.latest_events()[0]["kind"] == "manual_override"


def test_manual_override_event_is_logged_without_email(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    notifications = []
    runtime.event_reporter.notify = lambda *args, **kwargs: notifications.append((args, kwargs))

    car = ChargeState("Charging", 16, 32, 15.8, 3, 230)
    runtime._check_events({"window_active": True, "tesla_power_w": 11000}, car)

    events = runtime.db.latest_events()
    assert events[0]["kind"] == "manual_override"
    assert events[0]["level"] == "info"
    assert notifications == []

    car = ChargeState("Complete", 0, 32, 0, 3, 230)
    runtime._check_events({"window_active": True, "tesla_power_w": 0}, car)

    events = runtime.db.latest_events()
    assert events[0]["kind"] == "manual_override_recovered"
    assert notifications == []


def test_admin_can_download_backup_archive(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    Path(".env").write_text("WEB_PASSWORD=password-locale-molto-lunga\n", encoding="utf-8")
    runtime.store.save(runtime.current)
    runtime.db.add_measurement(
        {
            "observed_at": "2026-06-25T12:00:00+02:00",
            "solar_power_w": 5200,
            "vimar_power_w": 900,
            "tesla_power_w": 4000,
            "total_consumption_w": 4900,
            "import_power_w": 0,
            "export_power_w": 300,
            "controller_enabled": True,
        },
        [{"name": "forno", "power_w": 900}],
    )

    with app.test_client() as client:
        login(client)
        page = client.get("/").get_data(as_text=True)
        token = csrf(page)
        assert 'data-tab="backup"' in page
        assert "Scarica backup" in page

        response = client.post(
            "/backup/export",
            data={"csrf": token, "include_db": "on", "include_config": "on"},
        )

    assert response.status_code == 200
    assert response.mimetype == "application/zip"
    assert "attachment;" in response.headers["Content-Disposition"]
    assert "tesla-energy-controller-backup-" in response.headers["Content-Disposition"]

    with zipfile.ZipFile(BytesIO(response.data)) as archive:
        names = set(archive.namelist())
        assert "manifest.json" in names
        assert "data/energy.sqlite3" in names
        assert "config/.env" in names
        assert "config/runtime_settings.json" in names
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["energy_source"] == "mock"
        assert manifest["includes"] == {"db": True, "config": True}
        assert {item["archive"] for item in manifest["files"]} >= {
            "data/energy.sqlite3",
            "config/.env",
            "config/runtime_settings.json",
        }
        restored_db = tmp_path / "restored.sqlite3"
        restored_db.write_bytes(archive.read("data/energy.sqlite3"))

    with sqlite3.connect(restored_db) as db:
        assert db.execute("select count(*) from measurements").fetchone()[0] == 1
        assert db.execute("select count(*) from appliance_measurements").fetchone()[0] == 1
        assert db.execute("select count(*) from users where role = 'admin'").fetchone()[0] == 1


def test_backup_export_can_include_only_database(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    Path(".env").write_text("WEB_PASSWORD=password-locale-molto-lunga\n", encoding="utf-8")
    runtime.store.save(runtime.current)

    with app.test_client() as client:
        login(client)
        token = csrf(client.get("/").text)
        response = client.post(
            "/backup/export",
            data={"csrf": token, "include_db": "on"},
        )

    assert response.status_code == 200
    with zipfile.ZipFile(BytesIO(response.data)) as archive:
        names = set(archive.namelist())
        assert "manifest.json" in names
        assert "data/energy.sqlite3" in names
        assert "config/.env" not in names
        assert "config/runtime_settings.json" not in names
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["includes"] == {"db": True, "config": False}


def test_admin_can_import_backup_archive(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    env_path = Path(".env")
    env_path.write_text("WEB_PASSWORD=password-locale-molto-lunga\n", encoding="utf-8")
    runtime.store.save(runtime.current)
    runtime.db.add_measurement(
        {
            "observed_at": "2026-06-25T12:00:00+02:00",
            "solar_power_w": 5200,
            "vimar_power_w": 900,
            "tesla_power_w": 4000,
            "total_consumption_w": 4900,
            "import_power_w": 0,
            "export_power_w": 300,
            "controller_enabled": True,
        },
        [{"name": "forno", "power_w": 900}],
    )

    with app.test_client() as client:
        login(client)
        token = csrf(client.get("/").text)
        backup = client.post(
            "/backup/export",
            data={"csrf": token, "include_db": "on", "include_config": "on"},
        ).data

        env_path.write_text("WEB_PASSWORD=modificata\n", encoding="utf-8")
        runtime.current = replace(runtime.current, enabled=False)
        runtime.store.save(runtime.current)
        runtime.db.add_measurement(
            {
                "observed_at": "2026-06-25T12:05:00+02:00",
                "solar_power_w": 1000,
                "vimar_power_w": 1000,
                "tesla_power_w": 0,
                "total_consumption_w": 1000,
                "import_power_w": 0,
                "export_power_w": 0,
                "controller_enabled": False,
            },
            [],
        )
        assert len(runtime.db.latest_measurements(10)) == 2

        response = client.post(
            "/backup/import",
            data={
                "csrf": token,
                "backup_file": (BytesIO(backup), "backup.zip"),
                "restore_db": "on",
                "restore_config": "on",
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert env_path.read_text(encoding="utf-8") == "WEB_PASSWORD=password-locale-molto-lunga\n"
    assert runtime.current.enabled is True
    assert len(runtime.db.latest_measurements(10)) == 1
    latest_events = runtime.db.latest_events(5)
    assert latest_events[0]["kind"] == "backup_imported"


def test_backup_import_can_restore_only_config(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    env_path = Path(".env")
    env_path.write_text("WEB_PASSWORD=password-locale-molto-lunga\n", encoding="utf-8")
    runtime.current = replace(runtime.current, enabled=True)
    runtime.store.save(runtime.current)
    runtime.db.add_measurement(
        {
            "observed_at": "2026-06-25T12:00:00+02:00",
            "solar_power_w": 5200,
            "vimar_power_w": 900,
            "tesla_power_w": 4000,
            "total_consumption_w": 4900,
            "import_power_w": 0,
            "export_power_w": 300,
            "controller_enabled": True,
        },
        [],
    )

    with app.test_client() as client:
        login(client)
        token = csrf(client.get("/").text)
        backup = client.post(
            "/backup/export",
            data={"csrf": token, "include_config": "on"},
        ).data

        env_path.write_text("WEB_PASSWORD=modificata\n", encoding="utf-8")
        runtime.current = replace(runtime.current, enabled=False)
        runtime.store.save(runtime.current)
        runtime.db.add_measurement(
            {
                "observed_at": "2026-06-25T12:05:00+02:00",
                "solar_power_w": 1000,
                "vimar_power_w": 1000,
                "tesla_power_w": 0,
                "total_consumption_w": 1000,
                "import_power_w": 0,
                "export_power_w": 0,
                "controller_enabled": False,
            },
            [],
        )

        response = client.post(
            "/backup/import",
            data={
                "csrf": token,
                "backup_file": (BytesIO(backup), "backup.zip"),
                "restore_config": "on",
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert env_path.read_text(encoding="utf-8") == "WEB_PASSWORD=password-locale-molto-lunga\n"
    assert runtime.current.enabled is True
    assert len(runtime.db.latest_measurements(10)) == 2


def test_backup_import_rejects_invalid_zip(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.db.add_measurement(
        {
            "observed_at": "2026-06-25T12:00:00+02:00",
            "solar_power_w": 5200,
            "vimar_power_w": 900,
            "tesla_power_w": 4000,
            "total_consumption_w": 4900,
            "import_power_w": 0,
            "export_power_w": 300,
            "controller_enabled": True,
        },
        [],
    )

    with app.test_client() as client:
        login(client)
        token = csrf(client.get("/").text)
        response = client.post(
            "/backup/import",
            data={
                "csrf": token,
                "backup_file": (BytesIO(b"not a zip"), "backup.zip"),
                "restore_db": "on",
                "restore_config": "on",
            },
            content_type="multipart/form-data",
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )

    assert response.status_code == 400
    assert "ZIP non leggibile" in response.get_json()["error"]
    assert len(runtime.db.latest_measurements(10)) == 1


def test_controller_json_toggle_updates_runtime(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    with app.test_client() as client:
        login(client)
        token = csrf(client.get("/").text)
        response = client.post(
            "/controller",
            data={"csrf": token, "enabled": "0"},
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        payload = response.get_json()
        assert response.status_code == 200
        assert payload["current"]["enabled"] is False
        assert payload["status"]["controller_enabled"] is False


def test_settings_json_save_and_validation(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    with app.test_client() as client:
        login(client)
        token = csrf(client.get("/").text)
        response = client.post(
            "/settings",
            data=valid_settings_payload(
                token,
                manual_override_amps="15",
                alfa_grid_reading_enabled="on",
            ),
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        payload = response.get_json()
        assert response.status_code == 200
        assert payload["message"] == "Configurazione salvata"
        assert payload["current"]["manual_override_amps"] == 15
        assert payload["current"]["max_charge_amps"] == 14
        assert payload["current"]["alfa_grid_reading_enabled"] is True
        assert payload["current"]["extra_grid_power_w"] == 2 * 230 * 3
        assert payload["current"]["anomaly_peak_threshold_w"] == 1500

        response = client.post(
            "/settings",
            data=valid_settings_payload(token, extra_grid_power_a="-1"),
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        payload = response.get_json()
        assert response.status_code == 400
        assert "Extra rete" in payload["error"]


def test_settings_can_switch_to_single_phase(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    with app.test_client() as client:
        login(client)
        page = client.get("/").text
        token = csrf(page)
        assert '<option value="1"' in page
        assert "Monofase" in page
        response = client.post(
            "/settings",
            data=valid_settings_payload(token, expected_phases="1", extra_grid_power_a="2"),
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        payload = response.get_json()

    assert response.status_code == 200
    assert payload["current"]["expected_phases"] == 1
    assert payload["current"]["extra_grid_power_w"] == 2 * 230
    assert payload["config"]["expected_phases"] == 1
    assert runtime.current.expected_phases == 1
    assert runtime.controller.expected_phases == 1


def test_settings_rejects_unconfigured_solaredge_modbus(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    with app.test_client() as client:
        login(client)
        token = csrf(client.get("/").text)
        response = client.post(
            "/settings",
            data=valid_settings_payload(token, solar_source="solaredge-modbus"),
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        payload = response.get_json()
        assert response.status_code == 400
        assert "SOLAREDGE_MODBUS_HOST" in payload["error"]


def test_device_series_reads_history_and_sanitizes_names(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(runtime.current, alfa_grid_reading_enabled=True)
    runtime.db.add_measurement(
        {
            "observed_at": "2026-06-25T12:00:00+02:00",
            "solar_power_w": 5000,
            "vimar_power_w": 1200,
            "tesla_power_w": 3200,
            "total_consumption_w": 4400,
            "import_power_w": 0,
            "export_power_w": 600,
            "controller_enabled": True,
        },
        [{"name": "Lavatrice<script>", "power_w": 450}],
    )
    runtime.db.add_measurement(
        {
            "observed_at": "2026-06-25T12:10:00+02:00",
            "solar_power_w": 5100,
            "vimar_power_w": 1500,
            "tesla_power_w": 3300,
            "total_consumption_w": 4800,
            "import_power_w": 0,
            "export_power_w": 300,
            "controller_enabled": True,
        },
        [{"name": "Lavatrice<script>", "power_w": 650}],
    )
    runtime.db.add_measurement(
        {
            "observed_at": "2026-06-25T12:20:00+02:00",
            "solar_power_w": 5600,
            "vimar_power_w": 1800,
            "tesla_power_w": 3300,
            "total_consumption_w": 4600,
            "import_power_w": 0,
            "export_power_w": 1000,
            "controller_enabled": True,
        },
        [{"name": "Lavatrice<script>", "power_w": 700}],
    )
    with app.test_client() as client:
        login(client)
        response = client.get("/api/device-series?day=2026-06-25")
        payload = response.get_json()
        names = [item["name"] for item in payload["series"]]

        assert response.status_code == 200
        assert payload["labels"] == ["12:00", "12:10", "12:20"]
        assert "Elettrodomestici" in names
        assert "Casa" not in names
        assert "Altri device" not in names
        assert "Tesla" not in names
        assert "Lavatrice_script_" in names
        assert "<script>" not in response.get_data(as_text=True)
        assert next(item for item in payload["series"] if item["name"] == "Elettrodomestici")[
            "data"
        ] == [
            1200,
            1500,
            1800,
        ]
        assert next(item for item in payload["series"] if item["name"] == "Lavatrice_script_")[
            "data"
        ] == [450, 650, 700]


def test_device_series_is_bucketed_to_five_minutes_with_recent_weight(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(runtime.current, alfa_grid_reading_enabled=True)
    for stamp, vimar, washer in (
        ("2026-06-25T12:01:00+02:00", 1000, 100),
        ("2026-06-25T12:04:00+02:00", 3000, 1000),
        ("2026-06-25T12:07:00+02:00", 2000, 200),
    ):
        runtime.db.add_measurement(
            {
                "observed_at": stamp,
                "solar_power_w": 5000,
                "vimar_power_w": vimar,
                "tesla_power_w": 0,
                "total_consumption_w": vimar,
                "import_power_w": 0,
                "export_power_w": max(5000 - vimar, 0),
                "controller_enabled": True,
                "alfa_grid_reading_enabled": True,
            },
            [{"name": "Lavatrice", "power_w": washer}],
        )

    with app.test_client() as client:
        login(client)
        payload = client.get("/api/device-series?day=2026-06-25").get_json()

    assert payload["sample_seconds"] == 300
    assert payload["labels"] == ["12:00", "12:05"]
    aggregate = next(item for item in payload["series"] if item["name"] == "Elettrodomestici")
    washer = next(item for item in payload["series"] if item["name"] == "Lavatrice")
    assert 2000 < aggregate["data"][0] < 3000
    assert aggregate["data"][1] == 2000
    assert 550 < washer["data"][0] < 1000
    assert washer["data"][1] == 200
    assert payload["latest"] == [{"name": "Lavatrice", "power_w": 200}]


def test_device_series_reports_configurable_anomaly_peaks(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current,
        anomaly_peak_threshold_w=1500,
        anomaly_device_patterns="forno, frighi, cucina",
        anomaly_device_groups="Cucina | 1.5 | forno, cucina\nFrighi | 0.8 | frigo, frighi",
        anomaly_window_mode="fixed",
        anomaly_fixed_start_time="00:00",
        anomaly_fixed_end_time="23:59",
    )
    runtime.db.add_measurement(
        {
            "observed_at": "2026-06-25T13:00:00+02:00",
            "solar_power_w": 5000,
            "vimar_power_w": 3800,
            "tesla_power_w": 0,
            "total_consumption_w": 3800,
            "import_power_w": 0,
            "export_power_w": 1200,
            "controller_enabled": True,
        },
        [
            {"name": "Forno 1", "power_w": 1700},
            {"name": "Frigo", "power_w": 900},
            {"name": "Luce corridoio", "power_w": 2000},
        ],
    )
    with app.test_client() as client:
        login(client)
        payload = client.get("/api/device-series?day=2026-06-25").get_json()
        assert payload["anomaly_threshold_w"] == 1500
        assert payload["anomalies"] == [
            {
                "t": "13:00",
                "observed_at": "2026-06-25T13:00:00+02:00",
                "name": "Forno 1",
                "group": "Cucina",
                "power_w": 1700.0,
                "threshold_w": 1500.0,
            },
            {
                "t": "13:00",
                "observed_at": "2026-06-25T13:00:00+02:00",
                "name": "Frigo",
                "group": "Frighi",
                "power_w": 900.0,
                "threshold_w": 800.0,
            },
        ]
        assert payload["anomaly_groups"][0]["name"] == "Cucina"
        assert payload["anomaly_window"] == {"mode": "fixed", "label": "00:00–23:59"}


def test_device_anomalies_respect_fixed_window(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current,
        anomaly_device_groups="Cucina | 1.5 | forno",
        anomaly_window_mode="fixed",
        anomaly_fixed_start_time="22:00",
        anomaly_fixed_end_time="06:00",
    )
    for stamp in ("2026-06-25T13:00:00+02:00", "2026-06-25T23:00:00+02:00"):
        runtime.db.add_measurement(
            {
                "observed_at": stamp,
                "solar_power_w": 0,
                "vimar_power_w": 2000,
                "tesla_power_w": 0,
                "total_consumption_w": 2000,
                "import_power_w": 2000,
                "export_power_w": 0,
                "controller_enabled": True,
            },
            [{"name": "Forno 1", "power_w": 1700}],
        )
    with app.test_client() as client:
        login(client)
        payload = client.get("/api/device-series?day=2026-06-25").get_json()
        assert [item["t"] for item in payload["anomalies"]] == ["23:00"]


def test_live_anomaly_events_have_separate_email_toggle(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current,
        anomaly_device_groups="Cucina | 1.5 | forno",
        anomaly_window_mode="fixed",
        anomaly_fixed_start_time="00:00",
        anomaly_fixed_end_time="23:59",
        anomaly_email_enabled=False,
    )
    notifications = []

    def notify(*args, **kwargs):
        if runtime.event_reporter.enabled:
            notifications.append((args, kwargs))

    runtime.event_reporter.notify = notify
    stamp = "2026-06-25T23:00:00+02:00"

    runtime._check_anomaly_events(stamp, [{"name": "Forno 1", "power_w": 1700}])
    assert any(
        event["kind"] == "anomaly_peak:Cucina:Forno 1"
        for event in runtime.db.latest_events()
    )
    assert notifications == []

    runtime.active_events.clear()
    runtime.current = replace(runtime.current, anomaly_email_enabled=True)
    runtime._check_anomaly_events(stamp, [{"name": "Forno 1", "power_w": 1700}])
    runtime._check_anomaly_events(stamp, [{"name": "Forno 1", "power_w": 1800}])
    assert len(notifications) == 1
    assert notifications[0][0][0] == "anomaly_peak:Cucina:Forno 1"


def test_outside_window_does_not_call_controller(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    calls = []
    runtime.controller.run_once = lambda: calls.append(True)
    # Default calendario "alba/tramonto": le 23:30 di giugno sono fuori finestra a Vittorio Veneto.
    status = runtime.run_cycle(datetime(2026, 6, 22, 23, 30, tzinfo=ZoneInfo("Europe/Rome")))
    assert status["state"] == "outside-window"
    assert calls == []


def test_scheduler_reloads_controller_switch_saved_by_tuya(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current, schedule_mode="fixed", fixed_start_time="00:00", fixed_end_time="23:59"
    )
    runtime.store.save(replace(runtime.current, enabled=False))

    status = runtime.run_cycle(datetime(2026, 6, 22, 12, tzinfo=ZoneInfo("Europe/Rome")))

    assert status["state"] == "disabled"
    assert status["controller_enabled"] is False


def test_scheduler_error_exposes_debug_without_mail(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current, schedule_mode="fixed", fixed_start_time="00:00", fixed_end_time="23:59"
    )

    def fail():
        raise SolarEdgeAccessError(
            "Login SolarEdge rifiutato",
            phase="authorization-code",
            endpoint="https://login.solaredge.com/login?code=secret",
            status_code=200,
            response_excerpt="login page",
            hints=("Verificare credenziali",),
        )

    runtime.controller.grid.read = fail
    status = runtime.run_cycle(datetime(2026, 6, 22, 12, tzinfo=ZoneInfo("Europe/Rome")))
    assert status["state"] == "error"
    assert status["debug"]["component"] == "solaredge"
    assert status["debug"]["phase"] == "authorization-code"
    assert status["debug"]["endpoint"] == "https://login.solaredge.com/login"
    assert status["email_report"].startswith("config WordPress mail incompleta")


def test_vimar_timeout_keeps_energy_monitoring_without_error_mail(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current,
        schedule_mode="fixed",
        fixed_start_time="00:00",
        fixed_end_time="23:59",
    )
    runtime.store.save(runtime.current)

    def fail(_settings):
        raise TimeoutError("Connection timed out")

    reports = []
    monkeypatch.setattr("tesla_energy_controller.web.read_energy_points_from_settings", fail)
    runtime.reporter.notify = lambda *args, **kwargs: reports.append(
        (args, kwargs)
    ) or EmailReportResult(True, True, "report mail inviato")

    status = runtime.run_cycle(datetime(2026, 7, 2, 0, 5, tzinfo=ZoneInfo("Europe/Rome")))

    assert status["state"] != "error"
    assert reports == []
    events = runtime.db.latest_events()
    vimar_event = next(event for event in events if event["kind"] == "vimar_unreachable")
    details = json.loads(vimar_event["details_json"])
    assert details["component"] == "vimar"
    assert details["source"] == "vimar"
    assert "solar_source" not in details
    assert not any(event["kind"] == "error_application" for event in events)


def test_solaredge_modbus_connect_error_is_debounced(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLAREDGE_MODBUS_HOST", "192.168.2.126")
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current,
        schedule_mode="fixed",
        fixed_start_time="00:00",
        fixed_end_time="23:59",
        solar_source="solaredge-modbus",
    )
    runtime.store.save(runtime.current)
    runtime.last_status.update(
        {
            "state": "cached",
            "message": "Ultima misura salvata",
            "updated_at": "2026-07-01T12:00:00+02:00",
            "solar_power_w": 4500,
        }
    )

    def fail():
        raise SolarEdgeAccessError(
            "Connessione Modbus SolarEdge non riuscita",
            phase="modbus-connect",
            endpoint="tcp://192.168.2.126:1502",
        )

    reports = []
    runtime.controller.grid.read = fail
    runtime.reporter.notify = lambda *args, **kwargs: reports.append(
        (args, kwargs)
    ) or EmailReportResult(True, True, "report mail inviato")
    now = datetime(2026, 7, 1, 12, tzinfo=ZoneInfo("Europe/Rome"))

    status = runtime.run_cycle(now)
    assert status["state"] == "degraded"
    assert status["solar_power_w"] == 4500
    assert status["email_report"] == "mail non inviata: debounce Modbus SolarEdge"
    assert reports == []
    events = runtime.db.latest_events()
    assert any(event["kind"] == "solaredge_modbus_connect_degraded" for event in events)
    assert not any(event["kind"] == "error_solaredge" for event in events)

    status = runtime.run_cycle(now.replace(minute=4, second=59))
    assert status["state"] == "degraded"
    assert reports == []

    status = runtime.run_cycle(now.replace(minute=5))
    assert status["state"] == "degraded"
    assert status["solar_power_w"] == 4500
    assert reports == []
    assert not any(event["kind"] == "error_solaredge" for event in runtime.db.latest_events())


def test_solaredge_modbus_connect_failure_falls_back_to_alfa(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLAREDGE_MODBUS_HOST", "192.168.2.126")
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current,
        alfa_grid_reading_enabled=True,
        schedule_mode="fixed",
        fixed_start_time="00:00",
        fixed_end_time="23:59",
        solar_source="solaredge-modbus",
    )
    runtime.store.save(runtime.current)

    def fail():
        raise SolarEdgeAccessError(
            "Connessione Modbus SolarEdge non riuscita",
            phase="modbus-connect",
            endpoint="tcp://192.168.2.126:1502",
        )

    class AlfaMeter:
        @staticmethod
        def read():
            return GridMeasurement(
                total_power_w=-500,
                solar_power_w=1200,
                import_power_w=0,
                export_power_w=500,
                source="alfa-modbus",
            )

    reports = []
    runtime.controller.grid.read = fail
    runtime.alfa_grid = AlfaMeter()
    runtime.reporter.notify = lambda *args, **kwargs: reports.append(
        (args, kwargs)
    ) or EmailReportResult(True, True, "report mail inviato")

    status = runtime.run_cycle(datetime(2026, 7, 4, 7, tzinfo=ZoneInfo("Europe/Rome")))

    assert status["state"] == "degraded"
    assert status["message"] == "SolarEdge Modbus non disponibile; controllo su ALFA"
    assert status["energy_source"] == "alfa-modbus"
    assert status["solar_source"] == "solaredge-modbus"
    assert status["solar_source_degraded"] is True
    assert status["alfa_grid_reading_enabled"] is True
    assert status["solar_power_w"] == 1200
    assert status["import_power_w"] == 0
    assert status["export_power_w"] == 500
    assert reports == []
    assert any(
        event["kind"] == "solaredge_modbus_connect_degraded"
        for event in runtime.db.latest_events()
    )
    assert not any(event["kind"] == "error_solaredge" for event in runtime.db.latest_events())

    runtime.controller.grid.read = lambda: GridMeasurement(
        total_power_w=-400,
        solar_power_w=1300,
        source="solaredge-modbus",
    )
    recovered = runtime.run_cycle(datetime(2026, 7, 4, 7, 1, tzinfo=ZoneInfo("Europe/Rome")))

    assert recovered["state"] == "ok"
    assert recovered["energy_source"] == "solaredge-modbus+alfa-modbus"
    assert recovered["solar_source_degraded"] is False
    assert 1200 < recovered["solar_power_w"] < 1300
    assert any(
        event["kind"] == "solaredge_modbus_connect_degraded_recovered"
        for event in runtime.db.latest_events()
    )


def test_solaredge_web_grid_only_payload_reconstructs_solar(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.controller.grid.read = lambda: GridMeasurement(
        total_power_w=-5074,
        solar_power_w=None,
        source="solaredge-web",
    )
    runtime.controller.vehicle.state = replace(
        runtime.controller.vehicle.state,
        charging_state="Complete",
        actual_current_a=0,
        charger_power_kw=0,
    )
    monkeypatch.setattr(
        "tesla_energy_controller.web.read_energy_points_from_settings",
        lambda _settings: [
            VimarEnergyPoint(
                idsf=1,
                name="Casa",
                sftype="",
                sstype="",
                power_w=1056,
                production_w=None,
                exchange_w=None,
            )
        ],
    )

    status = runtime.run_cycle(datetime(2026, 6, 22, 12, tzinfo=ZoneInfo("Europe/Rome")))

    assert status["solar_power_w"] == 6130
    assert status["export_power_w"] == 5074
    assert status["tesla_power_w"] == 0
    assert status["reason"] == "Tesla non in carica (Complete)"


def test_wall_connector_data_source_does_not_query_tesla_ble(monkeypatch, tmp_path):
    monkeypatch.setenv("TESLA_DATA_SOURCE", "wall-connector")
    monkeypatch.setenv("WALL_CONNECTOR_HOST", "192.168.1.23")
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current,
        schedule_mode="fixed",
        fixed_start_time="00:00",
        fixed_end_time="23:59",
    )
    runtime.store.save(runtime.current)
    runtime.controller.grid.read = lambda: GridMeasurement(
        total_power_w=0,
        solar_power_w=5000,
        source="mock",
    )
    runtime.controller.vehicle.get_charge_state = lambda: (_ for _ in ()).throw(
        AssertionError("BLE non deve essere interrogato")
    )

    class Wall:
        @staticmethod
        def read_vitals():
            return WallConnectorVitals(
                vehicle_connected=False,
                contactor_closed=False,
                grid_v=230,
                vehicle_current_a=0.4,
                phase_currents_a=(0.4, 0.0, 0.2),
                power_w=0,
                evse_state=1,
            )

    runtime.wall_connector = Wall()

    status = runtime.run_cycle(datetime(2026, 6, 22, 12, tzinfo=ZoneInfo("Europe/Rome")))

    assert status["state"] == "monitor-only"
    assert status["action"] == "wall-connector-monitor"
    assert status["tesla_power_w"] == 0
    assert status["tesla_power_source"] == "wall-connector"
    assert status["tesla_connected"] is False
    assert status["target_a"] == 0
    assert status["tesla_ble_control_required"] is False
    assert status["tesla_ble_control_state"] == "not-needed"
    assert status["actual_current_a"] is None
    assert status["voltage_v"] == 230


def test_wall_connector_active_charge_uses_ble_for_control(monkeypatch, tmp_path):
    monkeypatch.setenv("TESLA_DATA_SOURCE", "wall-connector")
    monkeypatch.setenv("WALL_CONNECTOR_HOST", "192.168.1.23")
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current,
        schedule_mode="fixed",
        fixed_start_time="00:00",
        fixed_end_time="23:59",
    )
    runtime.store.save(runtime.current)
    runtime.controller.grid.read = lambda: GridMeasurement(
        total_power_w=0,
        solar_power_w=6000,
        source="mock",
    )
    ble_calls = {"count": 0}

    def get_charge_state():
        ble_calls["count"] += 1
        return ChargeState(
            charging_state="Charging",
            current_request_a=6,
            current_request_max_a=16,
            actual_current_a=6,
            phases=3,
            voltage_v=230,
            charger_power_kw=4.0,
        )

    runtime.controller.vehicle.get_charge_state = get_charge_state

    class Wall:
        @staticmethod
        def read_vitals():
            return WallConnectorVitals(
                vehicle_connected=True,
                contactor_closed=True,
                grid_v=230,
                vehicle_current_a=6.0,
                phase_currents_a=(6.0, 6.0, 6.0),
                power_w=4140,
                evse_state=9,
            )

    runtime.wall_connector = Wall()

    status = runtime.run_cycle(datetime(2026, 6, 22, 12, tzinfo=ZoneInfo("Europe/Rome")))

    assert ble_calls["count"] == 1
    assert status["state"] == "ok"
    assert status["tesla_power_w"] == 4140
    assert status["tesla_power_source"] == "wall-connector"
    assert status["tesla_connected"] is True
    assert status["tesla_ble_connected"] is True
    assert status["tesla_ble_control_required"] is True
    assert status["tesla_ble_control_state"] == "connected"


def test_wall_connector_preview_marks_manual_override_from_actual_current(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("TESLA_DATA_SOURCE", "wall-connector")
    monkeypatch.setenv("WALL_CONNECTOR_HOST", "192.168.1.23")
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current,
        schedule_mode="fixed",
        fixed_start_time="00:00",
        fixed_end_time="23:59",
        manual_override_amps=16,
    )
    runtime.store.save(runtime.current)
    runtime.controller.grid.read = lambda: GridMeasurement(
        total_power_w=4200,
        solar_power_w=7200,
        import_power_w=4200,
        export_power_w=0,
        source="mock",
    )
    runtime.controller.vehicle.get_charge_state = lambda: ChargeState(
        charging_state="Charging",
        current_request_a=6,
        current_request_max_a=16,
        actual_current_a=15.8,
        phases=3,
        voltage_v=230,
        charger_power_kw=10.5,
    )

    class Wall:
        @staticmethod
        def read_vitals():
            return WallConnectorVitals(
                vehicle_connected=True,
                contactor_closed=True,
                grid_v=230,
                vehicle_current_a=15.8,
                phase_currents_a=(15.8, 15.8, 15.8),
                power_w=10488,
                evse_state=11,
            )

    runtime.wall_connector = Wall()

    status = runtime.run_cycle(
        datetime(2026, 7, 3, 13, 28, tzinfo=ZoneInfo("Europe/Rome")),
        control=False,
        persist=False,
    )

    assert status["manual_override_active"] is True
    assert status["action"] == "manual-override"
    assert status["target_a"] == 16
    assert status["message"] == "override manuale: Tesla impostata a 16 A"
    assert status["house_power_w"] >= status["vimar_power_w"]


def test_wall_connector_complete_stops_repeated_ble_polling(monkeypatch, tmp_path):
    monkeypatch.setenv("TESLA_DATA_SOURCE", "wall-connector")
    monkeypatch.setenv("WALL_CONNECTOR_HOST", "192.168.1.23")
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current,
        schedule_mode="fixed",
        fixed_start_time="00:00",
        fixed_end_time="23:59",
    )
    runtime.store.save(runtime.current)
    runtime.controller.grid.read = lambda: GridMeasurement(
        total_power_w=0,
        solar_power_w=6000,
        source="mock",
    )
    ble_calls = {"count": 0}
    ble_states = [
        ChargeState(
            charging_state="Complete",
            current_request_a=5,
            current_request_max_a=16,
            actual_current_a=0,
            phases=3,
            voltage_v=230,
            charger_power_kw=0,
        ),
        ChargeState(
            charging_state="Charging",
            current_request_a=5,
            current_request_max_a=16,
            actual_current_a=5,
            phases=3,
            voltage_v=230,
            charger_power_kw=3.4,
        ),
    ]

    def get_charge_state():
        ble_calls["count"] += 1
        return ble_states.pop(0)

    runtime.controller.vehicle.get_charge_state = get_charge_state
    wall = {"current_a": 1.7, "power_w": 1080.0}

    class Wall:
        @staticmethod
        def read_vitals():
            return WallConnectorVitals(
                vehicle_connected=True,
                contactor_closed=True,
                grid_v=230,
                vehicle_current_a=wall["current_a"],
                phase_currents_a=(wall["current_a"], wall["current_a"], wall["current_a"]),
                power_w=wall["power_w"],
                evse_state=9,
            )

    runtime.wall_connector = Wall()

    first = runtime.run_cycle(datetime(2026, 7, 3, 0, 43, tzinfo=ZoneInfo("Europe/Rome")))
    second = runtime.run_cycle(datetime(2026, 7, 3, 0, 48, tzinfo=ZoneInfo("Europe/Rome")))
    wall.update({"current_a": 5.0, "power_w": 3450.0})
    third = runtime.run_cycle(datetime(2026, 7, 3, 0, 53, tzinfo=ZoneInfo("Europe/Rome")))

    assert first["reason"] == "Tesla non in carica (Complete)"
    assert first["target_a"] == 0
    assert second["action"] == "wall-connector-monitor"
    assert second["tesla_ble_control_required"] is False
    assert second["tesla_ble_control_state"] == "standby"
    assert second["tesla_ble_control_message"] == "Bluetooth in standby dopo carica completa"
    assert abs(second["tesla_power_w"] - 1080) < 0.01
    assert second["target_a"] == 0
    assert third["tesla_ble_control_required"] is True
    assert third["tesla_ble_control_state"] == "connected"
    assert ble_calls["count"] == 2


def test_wall_connector_standby_outside_window_does_not_query_tesla_ble(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("TESLA_DATA_SOURCE", "wall-connector")
    monkeypatch.setenv("WALL_CONNECTOR_HOST", "192.168.1.23")
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current,
        schedule_mode="fixed",
        fixed_start_time="06:00",
        fixed_end_time="19:00",
    )
    runtime.store.save(runtime.current)
    runtime.controller.grid.read = lambda: GridMeasurement(
        total_power_w=1000,
        solar_power_w=0,
        import_power_w=1000,
        export_power_w=0,
        source="mock",
    )
    runtime.controller.vehicle.get_charge_state = lambda: (_ for _ in ()).throw(
        AssertionError("BLE non deve essere interrogato in standby Wall Connector")
    )

    class Wall:
        @staticmethod
        def read_vitals():
            return WallConnectorVitals(
                vehicle_connected=True,
                contactor_closed=False,
                grid_v=230,
                vehicle_current_a=0.4,
                phase_currents_a=(0.4, 0.0, 0.0),
                power_w=92,
                evse_state=9,
            )

    runtime.wall_connector = Wall()

    status = runtime.run_cycle(datetime(2026, 7, 2, 22, tzinfo=ZoneInfo("Europe/Rome")))

    assert status["state"] == "outside-window"
    assert status["action"] == "outside-window"
    assert status["tesla_power_w"] == 92
    assert status["tesla_ble_control_required"] is False
    assert status.get("target_a") is None


def test_wall_connector_logs_tesla_night_power_over_300_w(monkeypatch, tmp_path):
    monkeypatch.setenv("TESLA_DATA_SOURCE", "wall-connector")
    monkeypatch.setenv("WALL_CONNECTOR_HOST", "192.168.1.23")
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current,
        schedule_mode="fixed",
        fixed_start_time="06:00",
        fixed_end_time="19:00",
    )
    runtime.store.save(runtime.current)
    runtime.controller.grid.read = lambda: GridMeasurement(
        total_power_w=1200,
        solar_power_w=0,
        import_power_w=1200,
        export_power_w=0,
        source="mock",
    )
    runtime.controller.vehicle.get_charge_state = lambda: (_ for _ in ()).throw(
        AssertionError("BLE non deve essere interrogato per il residuo notturno")
    )
    wall = {"current_a": 0.4, "power_w": 350.0}

    class Wall:
        @staticmethod
        def read_vitals():
            return WallConnectorVitals(
                vehicle_connected=True,
                contactor_closed=False,
                grid_v=230,
                vehicle_current_a=wall["current_a"],
                phase_currents_a=(wall["current_a"], 0.0, 0.0),
                power_w=wall["power_w"],
                evse_state=9,
            )

    runtime.wall_connector = Wall()

    high = runtime.run_cycle(datetime(2026, 7, 3, 23, tzinfo=ZoneInfo("Europe/Rome")))
    wall.update({"power_w": 100.0})
    recovered = runtime.run_cycle(datetime(2026, 7, 3, 23, 5, tzinfo=ZoneInfo("Europe/Rome")))

    assert high["tesla_power_w"] == 350
    assert recovered["tesla_power_w"] < 300
    events = runtime.db.latest_events(5)
    assert events[1]["kind"] == "tesla_night_power_high"
    assert events[1]["level"] == "warning"
    assert events[1]["message"] == "Assorbimento Tesla notturno sopra soglia: 350 W > 300 W"
    details = json.loads(events[1]["details_json"])
    assert details["threshold_w"] == 300
    assert events[0]["kind"] == "tesla_night_power_high_recovered"


def test_wall_connector_active_outside_window_uses_minimum_target(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("TESLA_DATA_SOURCE", "wall-connector")
    monkeypatch.setenv("WALL_CONNECTOR_HOST", "192.168.1.23")
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current,
        alfa_grid_reading_enabled=True,
        schedule_mode="fixed",
        fixed_start_time="06:00",
        fixed_end_time="19:00",
        extra_grid_power_w=3000,
        power_quota_target_w=7000,
        power_quota_hysteresis_w=500,
        min_charge_amps=5,
    )
    runtime.store.save(runtime.current)
    runtime.controller.grid.read = lambda: GridMeasurement(
        total_power_w=0,
        solar_power_w=0,
        import_power_w=None,
        export_power_w=None,
        source="solaredge-modbus",
    )

    class AlfaMeter:
        @staticmethod
        def read():
            return GridMeasurement(
                total_power_w=4445,
                solar_power_w=0,
                import_power_w=4445,
                export_power_w=0,
                source="alfa-modbus",
            )

    ble_calls = {"count": 0}

    def get_charge_state():
        ble_calls["count"] += 1
        return ChargeState(
            charging_state="Charging",
            current_request_a=5,
            current_request_max_a=16,
            actual_current_a=5,
            phases=3,
            voltage_v=230,
            charger_power_kw=3.4,
        )

    class Wall:
        @staticmethod
        def read_vitals():
            return WallConnectorVitals(
                vehicle_connected=True,
                contactor_closed=True,
                grid_v=230,
                vehicle_current_a=5.0,
                phase_currents_a=(5.0, 5.0, 5.0),
                power_w=3442,
                evse_state=9,
            )

    runtime.alfa_grid = AlfaMeter()
    runtime.wall_connector = Wall()
    runtime.controller.vehicle.get_charge_state = get_charge_state

    status = runtime.run_cycle(datetime(2026, 7, 2, 22, tzinfo=ZoneInfo("Europe/Rome")))

    assert ble_calls["count"] == 1
    assert status["window_active"] is False
    assert status["state"] == "ok"
    assert status["action"] == "hold"
    assert status["target_a"] == 5
    assert status["tesla_ble_control_required"] is True
    assert status["tesla_ble_control_state"] == "connected"
    assert status["message"] == "fuori finestra solare: corrente minima Tesla entro quota"


def test_wall_connector_preview_keeps_outside_window_target(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("TESLA_DATA_SOURCE", "wall-connector")
    monkeypatch.setenv("WALL_CONNECTOR_HOST", "192.168.1.23")
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current,
        alfa_grid_reading_enabled=True,
        schedule_mode="fixed",
        fixed_start_time="06:00",
        fixed_end_time="19:00",
        power_quota_target_w=7000,
        min_charge_amps=5,
    )
    runtime.store.save(runtime.current)
    runtime.controller.grid.read = lambda: GridMeasurement(
        total_power_w=0,
        solar_power_w=0,
        source="solaredge-modbus",
    )

    class AlfaMeter:
        @staticmethod
        def read():
            return GridMeasurement(
                total_power_w=4500,
                solar_power_w=0,
                import_power_w=4500,
                export_power_w=0,
                source="alfa-modbus",
            )

    class Wall:
        @staticmethod
        def read_vitals():
            return WallConnectorVitals(
                vehicle_connected=True,
                contactor_closed=True,
                grid_v=230,
                vehicle_current_a=5.0,
                phase_currents_a=(5.0, 5.0, 5.0),
                power_w=3450,
                evse_state=9,
            )

    runtime.alfa_grid = AlfaMeter()
    runtime.wall_connector = Wall()
    runtime.controller.vehicle.get_charge_state = lambda: ChargeState(
        charging_state="Charging",
        current_request_a=5,
        current_request_max_a=16,
        actual_current_a=5,
        phases=3,
        voltage_v=230,
    )

    now = datetime(2026, 7, 2, 22, tzinfo=ZoneInfo("Europe/Rome"))
    control_status = runtime.run_cycle(now)
    preview_status = runtime.run_cycle(now.replace(minute=1), control=False, persist=False)

    assert control_status["target_a"] == 5
    assert preview_status["state"] == "preview"
    assert preview_status["target_a"] == 5


def test_wall_connector_paused_quota_uses_ble_to_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("TESLA_DATA_SOURCE", "wall-connector")
    monkeypatch.setenv("WALL_CONNECTOR_HOST", "192.168.1.23")
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current,
        alfa_grid_reading_enabled=True,
        schedule_mode="fixed",
        fixed_start_time="00:00",
        fixed_end_time="23:59",
        extra_grid_power_w=3000,
        power_quota_target_w=7000,
        power_quota_hysteresis_w=500,
    )
    runtime.store.save(runtime.current)
    runtime.controller.dry_run = False
    runtime.controller.restore_power_quota_pause()
    runtime.controller.vehicle.state = ChargeState("Stopped", 5, 32, 0, 3, 230)
    runtime.controller.grid.read = lambda: GridMeasurement(
        total_power_w=0,
        solar_power_w=0,
        import_power_w=None,
        export_power_w=None,
        source="solaredge-modbus",
    )

    class Wall:
        @staticmethod
        def read_vitals():
            return WallConnectorVitals(
                vehicle_connected=True,
                contactor_closed=False,
                grid_v=230,
                vehicle_current_a=0,
                phase_currents_a=(0.0, 0.0, 0.0),
                power_w=0,
                evse_state=9,
            )

    class AlfaMeter:
        @staticmethod
        def read():
            return GridMeasurement(
                total_power_w=2000,
                solar_power_w=0,
                import_power_w=2000,
                export_power_w=0,
                source="alfa-modbus",
            )

    runtime.wall_connector = Wall()
    runtime.alfa_grid = AlfaMeter()

    status = runtime.run_cycle(datetime(2026, 6, 22, 12, tzinfo=ZoneInfo("Europe/Rome")))

    assert status["state"] == "ok"
    assert status["action"] == "start"
    assert status["target_a"] == 5
    assert status["tesla_ble_control_required"] is True
    assert status["tesla_ble_control_state"] == "connected"
    assert runtime.controller.vehicle.commands == [5, "start"]


def test_alfa_grid_delta_is_added_to_house_consumption(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current,
        alfa_grid_reading_enabled=True,
        schedule_mode="fixed",
        fixed_start_time="00:00",
        fixed_end_time="23:59",
    )
    runtime.store.save(runtime.current)
    runtime.controller.grid.read = lambda: GridMeasurement(
        total_power_w=-900,
        solar_power_w=5200,
        import_power_w=300,
        export_power_w=1200,
        total_consumption_w=9999,
        source="alfa-modbus",
    )

    status = runtime.run_cycle(datetime(2026, 6, 22, 12, tzinfo=ZoneInfo("Europe/Rome")))

    assert status["total_consumption_w"] == 4300
    assert status["import_power_w"] == 300
    assert status["export_power_w"] == 1200
    assert status["appliances_power_w"] == status["vimar_power_w"]
    expected_house_w = status["total_consumption_w"] - status["tesla_power_w"]
    expected_device_w = expected_house_w - status["appliances_power_w"]
    assert status["device_power_w"] == expected_device_w
    assert status["house_power_w"] == expected_house_w
    assert status["total_consumption_w"] == 5200 + 300 - 1200
    latest = runtime.db.latest_measurements(1)[0]
    assert latest["total_consumption_w"] == 4300
    assert latest["import_power_w"] == 300
    assert latest["export_power_w"] == 1200


def test_alfa_meter_can_overlay_solaredge_solar_source(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current,
        alfa_grid_reading_enabled=True,
        schedule_mode="fixed",
        fixed_start_time="00:00",
        fixed_end_time="23:59",
    )
    runtime.store.save(runtime.current)
    runtime.controller.grid.read = lambda: GridMeasurement(
        total_power_w=-700,
        solar_power_w=5200,
        import_power_w=None,
        export_power_w=None,
        source="solaredge-web",
    )

    class AlfaMeter:
        def read(self):
            return GridMeasurement(
                total_power_w=-900,
                solar_power_w=0,
                import_power_w=300,
                export_power_w=1200,
                imported_energy_wh=12345,
                exported_energy_wh=6789,
                source="alfa-modbus",
            )

    runtime.alfa_grid = AlfaMeter()

    status = runtime.run_cycle(datetime(2026, 6, 22, 12, tzinfo=ZoneInfo("Europe/Rome")))

    assert status["solar_power_w"] == 5200
    assert status["grid_power_w"] == -900
    assert status["import_power_w"] == 300
    assert status["export_power_w"] == 1200
    assert status["imported_energy_wh"] == 12345
    assert status["exported_energy_wh"] == 6789


def test_alfa_overlay_failure_keeps_energy_monitoring(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current,
        alfa_grid_reading_enabled=True,
        schedule_mode="fixed",
        fixed_start_time="00:00",
        fixed_end_time="23:59",
    )
    runtime.store.save(runtime.current)
    runtime.controller.grid.read = lambda: GridMeasurement(
        total_power_w=-700,
        solar_power_w=5200,
        import_power_w=None,
        export_power_w=None,
        source="solaredge-web",
    )

    class BrokenAlfaMeter:
        @staticmethod
        def read():
            raise ConnectionError("Connessione Modbus ALFA non riuscita")

    runtime.alfa_grid = BrokenAlfaMeter()
    status = runtime.run_cycle(datetime(2026, 6, 22, 12, tzinfo=ZoneInfo("Europe/Rome")))

    assert status["state"] == "ok"
    assert status["solar_power_w"] == 5200
    assert status["alfa_grid_reading_enabled"] is False
    assert status["meter_balance_available"] is False
    assert status["total_consumption_w"] == status["vimar_power_w"] + status["tesla_power_w"]
    assert any(event["kind"] == "alfa_grid_unreachable" for event in runtime.db.latest_events())
    assert not any(event["kind"] == "error_application" for event in runtime.db.latest_events())


def test_alfa_exposes_completed_quarter_hour_peak_without_resetting_month(
    monkeypatch, tmp_path
):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current,
        alfa_grid_reading_enabled=True,
        schedule_mode="fixed",
        fixed_start_time="00:00",
        fixed_end_time="23:59",
        power_quota_target_w=17000,
    )
    runtime.store.save(runtime.current)
    for minute in (0, 5, 10):
        runtime.db.add_measurement(
            {
                "observed_at": f"2026-06-28T12:{minute:02d}:00+02:00",
                "solar_power_w": 0,
                "vimar_power_w": 30000,
                "tesla_power_w": 0,
                "total_consumption_w": 30000,
                "import_power_w": 30000,
                "export_power_w": 0,
                "controller_enabled": True,
                "alfa_grid_reading_enabled": False,
            },
            [],
        )
    imports = iter((12000, 18000, 21000))

    def read_grid():
        imported = next(imports)
        return GridMeasurement(
            total_power_w=imported,
            solar_power_w=0,
            import_power_w=imported,
            export_power_w=0,
            source="alfa-modbus",
        )

    runtime.controller.grid.read = read_grid
    for minute in (0, 5, 10):
        status = runtime.run_cycle(
            datetime(2026, 6, 28, 13, minute, tzinfo=ZoneInfo("Europe/Rome"))
        )

    assert status["power_quota_sample_count"] == 3
    assert 16000 < status["quarter_hour_import_w"] < 17000
    assert runtime.monthly_peak_import_w(
        datetime(2026, 6, 28, 13, 10, tzinfo=ZoneInfo("Europe/Rome"))
    ) == 30000


def test_alfa_control_uses_five_minute_average_for_resume_target(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current,
        alfa_grid_reading_enabled=True,
        schedule_mode="fixed",
        fixed_start_time="00:00",
        fixed_end_time="23:59",
        extra_grid_power_w=2000,
        power_quota_target_w=7000,
        power_quota_hysteresis_w=500,
    )
    runtime.store.save(runtime.current)
    runtime.controller.restore_power_quota_pause()
    runtime.controller.vehicle.state = ChargeState("Stopped", 5, 32, 0, 3, 230)
    runtime.controller.grid.read = lambda: GridMeasurement(
        total_power_w=0,
        solar_power_w=1000,
        import_power_w=None,
        export_power_w=None,
        source="solaredge-modbus",
    )

    alfa_imports = iter((2500, 2500, 2500, 2500, 0))

    class AlfaMeter:
        @staticmethod
        def read():
            imported = next(alfa_imports)
            return GridMeasurement(
                total_power_w=imported,
                solar_power_w=0,
                import_power_w=imported,
                export_power_w=0,
                source="alfa-modbus",
            )

    runtime.alfa_grid = AlfaMeter()
    for minute in (56, 57, 58, 59):
        runtime.run_cycle(
            datetime(2026, 6, 28, 12, minute, tzinfo=ZoneInfo("Europe/Rome")),
            persist=False,
            control=False,
        )

    status = runtime.run_cycle(datetime(2026, 6, 28, 13, 0, tzinfo=ZoneInfo("Europe/Rome")))

    assert status["action"] == "dry-run"
    assert status["target_a"] == 5
    assert "riavvio Tesla" in status["reason"]
    assert status["control_sample_count"] == 5
    assert 0 < status["control_import_power_w"] < 2000
    assert runtime.controller.vehicle.commands == []


def test_run_cycle_saves_rolling_ewma_sample(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current,
        alfa_grid_reading_enabled=True,
        schedule_mode="fixed",
        fixed_start_time="00:00",
        fixed_end_time="23:59",
        power_quota_target_w=7000,
    )
    runtime.store.save(runtime.current)
    imports = iter((1000, 5000))

    def read_grid():
        imported = next(imports)
        return GridMeasurement(
            total_power_w=imported,
            solar_power_w=0,
            import_power_w=imported,
            export_power_w=0,
            source="alfa-modbus",
        )

    runtime.controller.grid.read = read_grid
    runtime.run_cycle(
        datetime(2026, 6, 28, 12, 56, tzinfo=ZoneInfo("Europe/Rome")),
        persist=False,
        control=False,
    )

    status = runtime.run_cycle(datetime(2026, 6, 28, 13, 0, tzinfo=ZoneInfo("Europe/Rome")))
    latest = runtime.db.latest_measurements(1)[0]

    assert status["control_average_method"] == "ewma"
    assert status["control_sample_count"] == 2
    assert 3000 < status["import_power_w"] < 5000
    assert latest["import_power_w"] == status["import_power_w"]
    cached = read_status_cache(_settings, max_age_seconds=3600)
    assert cached is not None
    assert cached["total_consumption_w"] == status["total_consumption_w"]
    assert cached["house_power_w"] == status["house_power_w"]


def test_alfa_disabled_restores_estimated_balance(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current,
        alfa_grid_reading_enabled=False,
        schedule_mode="fixed",
        fixed_start_time="00:00",
        fixed_end_time="23:59",
    )
    runtime.controller.grid.read = lambda: GridMeasurement(
        total_power_w=-900,
        solar_power_w=5200,
        import_power_w=300,
        export_power_w=1200,
        source="alfa-modbus",
    )

    status = runtime.run_cycle(datetime(2026, 6, 22, 12, tzinfo=ZoneInfo("Europe/Rome")))
    estimated_grid_w = status["vimar_power_w"] + status["tesla_power_w"] - 5200

    assert status["alfa_grid_reading_enabled"] is False
    assert status["device_power_w"] == 0
    assert status["house_power_w"] == status["vimar_power_w"]
    assert status["total_consumption_w"] == status["vimar_power_w"] + status["tesla_power_w"]
    assert status["import_power_w"] == max(estimated_grid_w, 0)
    assert status["export_power_w"] == max(-estimated_grid_w, 0)


def test_dashboard_switches_between_main_and_alfa_interface(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]

    with app.test_client() as client:
        login(client)
        previous = client.get("/").get_data(as_text=True)
        assert 'data-alfa-grid-reading="0"' in previous
        assert 'data-metric="vimar_power_w"' in previous
        assert 'data-metric="appliances_power_w"' not in previous
        previous_series = client.get("/api/device-series").get_json()
        assert previous_series["alfa_grid_reading_enabled"] is False
        assert [item["name"] for item in previous_series["series"]][:1] == ["Consumo casa"]

        runtime.current = replace(runtime.current, alfa_grid_reading_enabled=True)
        runtime.store.save(runtime.current)
        alfa = client.get("/").get_data(as_text=True)
        assert 'data-alfa-grid-reading="1"' in alfa
        assert 'data-refresh-ms="300000"' in alfa
        assert "Campionamento 300s" in alfa
        assert "<span>Quota Potenza</span>" in alfa
        assert "<span>Target Tesla</span>" in alfa
        assert "Obiettivo quota potenza" in alfa
        assert "Extra rete" in alfa
        assert "Picco 15 min" not in alfa
        assert 'data-metric="house_power_w"' in alfa
        assert '<span>Casa</span>' in alfa
        assert 'data-metric="device_power_w"' not in alfa
        assert 'data-metric="vimar_power_w"' not in alfa
        alfa_series = client.get("/api/device-series").get_json()
        assert alfa_series["alfa_grid_reading_enabled"] is True
        assert [item["name"] for item in alfa_series["series"]][:1] == [
            "Elettrodomestici",
        ]


def test_alfa_keeps_five_minute_control_interval(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current,
        alfa_grid_reading_enabled=True,
    )
    runtime.store.save(runtime.current)

    assert runtime.control_interval_seconds() == 300
    assert runtime.status_payload()["poll_interval_seconds"] == 300


def test_dashboard_flow_metric_uses_net_grid_balance(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.last_status.update(
        {
            "import_power_w": 560,
            "export_power_w": 448,
            "solar_power_w": 6200,
            "tesla_power_w": 4600,
            "total_consumption_w": 6312,
        }
    )

    with app.test_client() as client:
        login(client)
        html = client.get("/").get_data(as_text=True)

    flow = re.search(r'<article class="metric panel" id="flowMetric".*?</article>', html)
    assert flow
    assert "112 W" in flow.group(0)
    assert "448 W" not in flow.group(0)


def test_dashboard_flow_metric_shows_net_export(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.last_status.update({"import_power_w": 100, "export_power_w": 450})

    with app.test_client() as client:
        login(client)
        html = client.get("/").get_data(as_text=True)

    flow = re.search(r'<article class="metric panel" id="flowMetric".*?</article>', html)
    assert flow
    assert '(<span class="flow-export">350 W</span>)' in flow.group(0)


def test_dashboard_shows_selected_tesla_data_source(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    with app.test_client() as client:
        login(client)
        page = client.get("/").get_data(as_text=True)
        assert "Bluetooth Tesla" in page
        assert "Wall Connector standby" not in page

    monkeypatch.setenv("TESLA_DATA_SOURCE", "wall-connector")
    monkeypatch.setenv("WALL_CONNECTOR_HOST", "192.168.1.23")
    wall_dir = tmp_path / "wall"
    wall_dir.mkdir()
    wall_app, _settings = application(monkeypatch, wall_dir)
    runtime = wall_app.extensions["energy_runtime"]
    runtime.last_status.update(
        {
            "tesla_connected": True,
            "tesla_power_source": "wall-connector",
            "wall_connector_vehicle_connected": True,
            "wall_connector_contactor_closed": False,
            "wall_connector_evse_state": 9,
            "tesla_ble_control_state": "not-needed",
            "tesla_ble_control_message": "Bluetooth non interrogato",
        }
    )
    with wall_app.test_client() as client:
        login(client)
        page = client.get("/").get_data(as_text=True)
        assert "Wall Connector collegato" in page
        assert "Bluetooth standby" in page
        assert "Controllo BLE" in page
        assert "192.168.1.23" in page
        assert "Contattore" in page
        assert "EVSE" in page


def test_dashboard_bootstraps_status_from_latest_measurement(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.db.add_measurement(
        {
            "observed_at": "2026-06-30T16:12:03+02:00",
            "solar_power_w": 4940,
            "vimar_power_w": 770,
            "tesla_power_w": 0,
            "total_consumption_w": 1651,
            "import_power_w": 0,
            "export_power_w": 3289,
            "controller_enabled": True,
            "action": "tesla-offline",
            "reason": "Tesla non raggiungibile via BLE",
            "alfa_grid_reading_enabled": True,
        },
        [],
    )

    restarted_app, _settings = application(monkeypatch, tmp_path)
    restarted_runtime = restarted_app.extensions["energy_runtime"]
    assert restarted_runtime.last_status["state"] == "cached"
    assert restarted_runtime.last_status["solar_power_w"] == 4940
    assert restarted_runtime.last_status["total_consumption_w"] == 1651
    assert restarted_runtime.last_status["export_power_w"] == 3289

    with restarted_app.test_client() as client:
        login(client)
        page = client.get("/").get_data(as_text=True)
        assert "4940 W" in page
        assert "1651 W" in page


def test_tesla_unreachable_keeps_energy_monitoring(monkeypatch, tmp_path):
    app, _settings = application(monkeypatch, tmp_path)
    runtime = app.extensions["energy_runtime"]
    runtime.current = replace(
        runtime.current, schedule_mode="fixed", fixed_start_time="00:00", fixed_end_time="23:59"
    )

    def fail():
        raise TeslaBLEError(
            "Tesla non raggiungibile via Bluetooth entro il timeout",
            BLEErrorCategory.OUT_OF_RANGE,
            retryable=True,
        )

    runtime.controller.vehicle.get_charge_state = fail
    notifications = []
    runtime.event_reporter.notify = lambda *args, **kwargs: notifications.append((args, kwargs))
    status = runtime.run_cycle(datetime(2026, 6, 22, 12, tzinfo=ZoneInfo("Europe/Rome")))

    # L'auto irraggiungibile non è più un errore bloccante: l'energia resta monitorata.
    assert status["state"] == "tesla-offline"
    assert status["message"] == "Tesla non raggiungibile via BLE"
    assert status["tesla_connected"] is False
    assert status["target_a"] == 0
    assert "solar_power_w" in status
    assert status["tesla_power_w"] == 0
    assert status["tesla_current_a"] is None
    # La misura energia viene comunque registrata; BLE fuori portata è stato operativo,
    # non un errore da Error Log.
    assert runtime.db.latest_measurements()
    assert any(event["kind"] == "tesla_ble_unreachable" for event in runtime.db.latest_events())
    assert not any(
        event["kind"] == "tesla_ble_unreachable" for event in runtime.db.latest_error_events()
    )
    assert notifications == []
