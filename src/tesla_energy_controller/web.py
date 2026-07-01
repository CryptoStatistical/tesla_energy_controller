from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import timedelta
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for

from .backup import BackupImportError
from .config import ConfigurationError, Settings
from .controller import EnergyController
from .web_runtime import (
    LoginLimiter,
    RuntimeSettingsError,
    WebRuntime,
    _masked_vin,
    _safe_text,
)
from .vimar import read_energy_points_from_settings  # noqa: F401 - compatibility hook


def create_app(
    settings: Settings,
    controller: EnergyController,
    *,
    start_scheduler: bool = True,
) -> Flask:
    if not settings.web_password or len(settings.web_password) < 8:
        raise ConfigurationError("WEB_PASSWORD deve contenere almeno 8 caratteri")

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.secret_key = settings.secret_key or hashlib.sha256(
        f"tesla-energy-controller:{settings.web_password}".encode()
    ).digest()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=settings.web_secure_cookie,
        SESSION_COOKIE_SAMESITE="Strict",
        PERMANENT_SESSION_LIFETIME=timedelta(seconds=settings.web_session_ttl_seconds),
        MAX_CONTENT_LENGTH=64 * 1024 * 1024,
    )

    @app.url_defaults
    def _static_cache_bust(endpoint, values):
        # Versione = mtime del file: l'URL cambia a ogni deploy, niente cache stantia.
        if endpoint == "static" and "filename" in values and app.static_folder:
            try:
                asset = Path(app.static_folder) / values["filename"]
                values["v"] = int(asset.stat().st_mtime)
            except OSError:
                pass

    runtime = WebRuntime(settings, controller)
    limiter = LoginLimiter()
    app.extensions["energy_runtime"] = runtime
    if start_scheduler:
        runtime.start()

    def authenticated() -> bool:
        return session.get("authenticated") is True

    def can_configure() -> bool:
        return session.get("role") == "admin"

    def current_user() -> dict:
        return runtime.public_user(session.get("username"))

    def scoped_ui_payload() -> dict:
        payload = runtime.ui_payload()
        payload["can_configure"] = can_configure()
        payload["account"] = current_user()
        if can_configure():
            payload["users"] = runtime.public_users()
        else:
            payload.pop("error_events", None)
        return payload

    def clean_username(value: str) -> str:
        username = (_safe_text(value, limit=120) or "").strip()
        allowed = set("@._+-")
        if not username or any((not ch.isalnum()) and ch not in allowed for ch in username):
            return ""
        return username

    def clean_email(value: str) -> str:
        email = (_safe_text(value, limit=180) or "").strip().lower()
        if not email:
            return ""
        if "@" not in email or any(ch.isspace() for ch in email):
            return ""
        return email

    def csrf_valid() -> bool:
        supplied = request.form.get("csrf", "")
        expected = session.get("csrf", "")
        return bool(expected) and hmac.compare_digest(supplied, expected)

    def wants_json() -> bool:
        return (
            request.headers.get("X-Requested-With") == "fetch"
            or "application/json" in request.headers.get("Accept", "")
        )

    def form_error(message: str, status: int):
        if wants_json():
            return jsonify(error=message), status
        return message, status

    def api_auth_guard():
        if authenticated():
            return None
        return jsonify(error="unauthorized"), 401

    def form_guard(*, admin: bool = False):
        if not authenticated():
            if wants_json():
                return jsonify(error="unauthorized"), 401
            return redirect(url_for("login"), code=303)
        if admin and not can_configure():
            return form_error("Solo admin", 403)
        if not csrf_valid():
            return form_error("CSRF non valido", 403)
        return None

    @app.after_request
    def security_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; "
            "frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if authenticated():
            return redirect(url_for("dashboard"), code=303)
        error = ""
        status = 200
        if request.method == "POST":
            client = request.remote_addr or "unknown"
            username = (request.form.get("username") or settings.web_username).strip()
            user = runtime.authenticate(username, request.form.get("password", ""))
            if limiter.allow(client, user is not None) and user:
                session.clear()
                session.permanent = True
                session["authenticated"] = True
                session["username"] = user["username"]
                session["email"] = user.get("email") or ""
                session["role"] = user["role"]
                session["csrf"] = secrets.token_urlsafe(24)
                return redirect(url_for("dashboard"), code=303)
            error = "Password errata o troppi tentativi. Riprova tra qualche minuto."
            status = 401
        return render_template("login.html", error=error), status

    @app.get("/")
    def dashboard():
        if not authenticated():
            return redirect(url_for("login"), code=303)
        runtime.reload_settings()
        try:
            window = runtime.window()
            window_error = ""
        except RuntimeSettingsError as exc:
            window = None
            window_error = str(exc)
        return render_template(
            "dashboard.html",
            hard=settings,
            current=runtime.current,
            status=runtime.last_status,
            window=window,
            error=window_error,
            csrf=session["csrf"],
            user=current_user(),
            can_configure=can_configure(),
            users=runtime.public_users() if can_configure() else [],
            history=runtime.db.latest_measurements(),
            events=runtime.public_events(runtime.db.latest_events()),
            error_events=runtime.public_events(runtime.db.latest_error_events()),
            tesla_ble=runtime.tesla_ble_status(),
            masked_vin=_masked_vin(settings.tesla_vin),
            monthly_peak_import_w=runtime.monthly_peak_import_w(),
            show_appliances=request.args.get("details") == "1",
            appliances=runtime.db.latest_appliances() if request.args.get("details") == "1" else [],
        )

    @app.get("/api/status")
    def api_status():
        guard = api_auth_guard()
        if guard is not None:
            return guard
        return jsonify(runtime.status_payload())

    @app.get("/api/runtime")
    def api_runtime():
        guard = api_auth_guard()
        if guard is not None:
            return guard
        return jsonify(scoped_ui_payload())

    @app.get("/api/series")
    def api_series():
        # Dati per i grafici: autenticazione via sessione (cookie), nessuna credenziale inviata.
        guard = api_auth_guard()
        if guard is not None:
            return guard
        return jsonify(runtime.energy_series_payload(request.args.get("day")))

    @app.get("/api/appliances")
    def api_appliances():
        # Consumi per elettrodomestico (Vimar). Autenticazione via sessione (cookie).
        guard = api_auth_guard()
        if guard is not None:
            return guard
        return jsonify(runtime.appliances_payload(request.args.get("day")))

    @app.get("/api/device-series")
    def api_device_series():
        # Serie storiche aggregate + singoli device salvati in SQLite.
        guard = api_auth_guard()
        if guard is not None:
            return guard
        return jsonify(runtime.device_series_payload(request.args.get("day")))

    @app.post("/settings")
    def save_settings():
        guard = form_guard(admin=True)
        if guard is not None:
            return guard
        values = request.form.to_dict()
        values.setdefault("enabled", "on" if runtime.current.enabled else "")
        values.setdefault("alfa_grid_reading_enabled", "")
        values.setdefault("solar_source", runtime.current.solar_source)
        values.setdefault("tesla_ble_recovery_enabled", "")
        values.setdefault("error_email_enabled", "")
        values.setdefault("anomaly_email_enabled", "")
        # "Extra rete" è inserito in Ampere nel pannello: converti in W per il controllo.
        extra_a = values.pop("extra_grid_power_a", None)
        if extra_a not in (None, ""):
            try:
                watts_per_amp = settings.nominal_phase_voltage_v * max(settings.expected_phases, 1)
                values["extra_grid_power_w"] = str(float(extra_a) * watts_per_amp)
            except ValueError:
                pass
        power_quota_kw = values.pop("power_quota_target_kw", None)
        if power_quota_kw not in (None, ""):
            try:
                values["power_quota_target_w"] = str(float(power_quota_kw) * 1000)
            except ValueError:
                pass
        power_quota_hysteresis_kw = values.pop("power_quota_hysteresis_kw", None)
        if power_quota_hysteresis_kw not in (None, ""):
            try:
                values["power_quota_hysteresis_w"] = str(
                    float(power_quota_hysteresis_kw) * 1000
                )
            except ValueError:
                pass
        threshold_kw = values.pop("anomaly_peak_threshold_kw", None)
        if threshold_kw not in (None, ""):
            try:
                values["anomaly_peak_threshold_w"] = str(float(threshold_kw) * 1000)
            except ValueError:
                pass
        # Posizione fissa (sede non spostabile): coordinate da config, non dal pannello.
        values["latitude"] = settings.solar_latitude
        values["longitude"] = settings.solar_longitude
        try:
            runtime.update(values)
        except RuntimeSettingsError as exc:
            if wants_json():
                return jsonify(error=str(exc), **scoped_ui_payload()), 400
            try:
                window = runtime.window()
            except RuntimeSettingsError:
                window = None
            return (
                render_template(
                    "dashboard.html",
                    hard=settings,
                    current=runtime.current,
                    status=runtime.last_status,
                    window=window,
                    error=str(exc),
                    csrf=session["csrf"],
                    user=current_user(),
                    can_configure=can_configure(),
                    users=runtime.public_users() if can_configure() else [],
                    history=runtime.db.latest_measurements(),
                    events=runtime.public_events(runtime.db.latest_events()),
                    error_events=runtime.public_events(runtime.db.latest_error_events()),
                    tesla_ble=runtime.tesla_ble_status(),
                    masked_vin=_masked_vin(settings.tesla_vin),
                    show_appliances=False,
                    appliances=[],
                ),
                400,
            )
        runtime.run_cycle(force_measurement=True, persist=False, control=False)
        if wants_json():
            return jsonify(message="Configurazione salvata", **scoped_ui_payload())
        return redirect(url_for("dashboard"), code=303)

    @app.post("/password")
    def change_password():
        guard = form_guard()
        if guard is not None:
            return guard

        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        if len(new_password) < 8:
            return form_error("La nuova password deve contenere almeno 8 caratteri", 400)
        if new_password != confirm_password:
            return form_error("Le nuove password non coincidono", 400)
        username = session.get("username") or settings.web_username
        if not runtime.db.change_password(username, current_password, new_password):
            return form_error("Password attuale non corretta", 400)
        if wants_json():
            return jsonify(message="Password aggiornata", **scoped_ui_payload())
        return redirect(url_for("dashboard"), code=303)

    @app.post("/users")
    def create_user():
        guard = form_guard(admin=True)
        if guard is not None:
            return guard

        email = clean_email(request.form.get("email", ""))
        username = clean_username(request.form.get("username", ""))
        if not username and email:
            username = email
        role = request.form.get("role", "viewer")
        if role not in {"admin", "viewer"}:
            return form_error("Ruolo non valido", 400)
        if not username:
            return form_error("Inserisci username valido o email valida", 400)
        password = secrets.token_urlsafe(12)
        try:
            runtime.db.create_user(username=username, email=email, password=password, role=role)
        except sqlite3.IntegrityError:
            return form_error("Username gia' esistente", 409)
        runtime.refresh_mail_recipients()
        payload = scoped_ui_payload()
        payload["generated_username"] = username
        payload["generated_password"] = password
        return jsonify(message="Utente creato", **payload)

    @app.post("/account")
    def update_account():
        guard = form_guard()
        if guard is not None:
            return guard

        email = clean_email(request.form.get("email", ""))
        raw_email = (request.form.get("email") or "").strip()
        if raw_email and not email:
            return form_error("Email non valida", 400)
        username = session.get("username") or settings.web_username
        if not runtime.db.update_user_email(username, email):
            return form_error("Utente non trovato", 404)
        session["email"] = email
        runtime.refresh_mail_recipients()
        if wants_json():
            return jsonify(message="Profilo aggiornato", **scoped_ui_payload())
        return redirect(url_for("dashboard"), code=303)

    @app.post("/users/update")
    def update_user():
        guard = form_guard(admin=True)
        if guard is not None:
            return guard

        username = clean_username(request.form.get("username", ""))
        email = clean_email(request.form.get("email", ""))
        raw_email = (request.form.get("email") or "").strip()
        role = request.form.get("role", "viewer")
        if not username:
            return form_error("Username non valido", 400)
        if raw_email and not email:
            return form_error("Email non valida", 400)
        if role not in {"admin", "viewer"}:
            return form_error("Ruolo non valido", 400)
        existing = runtime.db.get_user(username)
        if not existing:
            return form_error("Utente non trovato", 404)
        if existing["role"] == "admin" and role != "admin" and runtime.db.admin_count() <= 1:
            return form_error("Non puoi rimuovere l'ultimo admin", 400)
        if not runtime.db.update_user(username, email=email, role=role):
            return form_error("Utente non trovato", 404)
        if username == session.get("username"):
            session["email"] = email
            session["role"] = role
        runtime.refresh_mail_recipients()
        if wants_json():
            return jsonify(message="Utente aggiornato", **scoped_ui_payload())
        return redirect(url_for("dashboard"), code=303)

    @app.post("/users/delete")
    def delete_user():
        guard = form_guard(admin=True)
        if guard is not None:
            return guard

        username = clean_username(request.form.get("username", ""))
        if not username:
            return form_error("Username non valido", 400)
        if username == session.get("username"):
            return form_error("Non puoi cancellare il tuo utente", 400)
        existing = runtime.db.get_user(username)
        if not existing:
            return form_error("Utente non trovato", 404)
        if existing["role"] == "admin" and runtime.db.admin_count() <= 1:
            return form_error("Non puoi cancellare l'ultimo admin", 400)
        if not runtime.db.delete_user(username):
            return form_error("Utente non trovato", 404)
        runtime.refresh_mail_recipients()
        if wants_json():
            return jsonify(message="Utente cancellato", **scoped_ui_payload())
        return redirect(url_for("dashboard"), code=303)

    @app.post("/error-log/clear")
    def clear_error_log():
        guard = form_guard(admin=True)
        if guard is not None:
            return guard
        cleared = runtime.db.clear_error_events()
        if wants_json():
            return jsonify(message=f"Error Log cancellato ({cleared})", **scoped_ui_payload())
        return redirect(url_for("dashboard"), code=303)

    @app.post("/backup/export")
    def export_backup():
        guard = form_guard(admin=True)
        if guard is not None:
            return guard
        try:
            archive, filename = runtime.backup_archive(
                include_db=request.form.get("include_db") in {"on", "true", "1", "yes"},
                include_config=request.form.get("include_config") in {"on", "true", "1", "yes"},
            )
        except BackupImportError as exc:
            return form_error(str(exc), 400)
        return send_file(
            archive,
            mimetype="application/zip",
            as_attachment=True,
            download_name=filename,
            max_age=0,
        )

    @app.post("/backup/import")
    def import_backup():
        guard = form_guard(admin=True)
        if guard is not None:
            return guard
        uploaded = request.files.get("backup_file")
        if uploaded is None or not uploaded.filename:
            return form_error("Carica un file backup ZIP", 400)
        restore_db = request.form.get("restore_db") in {"on", "true", "1", "yes"}
        restore_config = request.form.get("restore_config") in {"on", "true", "1", "yes"}
        try:
            result = runtime.import_backup_archive(
                uploaded.read(),
                session.get("username") or "unknown",
                restore_db=restore_db,
                restore_config=restore_config,
            )
        except BackupImportError as exc:
            return form_error(str(exc), 400)
        message = "Backup importato: " + ", ".join(result["restored"])
        if wants_json():
            return jsonify(message=message, **scoped_ui_payload())
        return redirect(url_for("dashboard"), code=303)

    @app.post("/controller")
    def controller_enabled():
        guard = form_guard()
        if guard is not None:
            return guard
        runtime.set_controller_enabled(
            request.form.get("enabled") in {"on", "true", "1", "yes"},
            session.get("username") or "unknown",
        )
        if wants_json():
            return jsonify(message="Controller aggiornato", **scoped_ui_payload())
        return redirect(url_for("dashboard"), code=303)

    @app.post("/run-now")
    def run_now():
        guard = form_guard()
        if guard is not None:
            return guard
        if wants_json():
            request_state = runtime.request_run_now(force_measurement=True)
            message = (
                "Aggiornamento avviato"
                if request_state == "started"
                else "Aggiornamento accodato"
            )
            return jsonify(message=message, **scoped_ui_payload())
        runtime.run_cycle(force_measurement=True, persist=False, control=False)
        return redirect(url_for("dashboard"), code=303)

    @app.post("/logout")
    def logout():
        if not authenticated():
            return redirect(url_for("login"), code=303)
        if not csrf_valid():
            return "CSRF non valido", 403
        session.clear()
        return redirect(url_for("login"), code=303)

    return app
