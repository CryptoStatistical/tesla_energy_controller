from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .demand import monthly_peak_power_demand


def _hash_password(password: str, iterations: int = 200_000) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_iterations, salt, expected = encoded.split("$", 3)
        iterations = int(raw_iterations)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    return hmac.compare_digest(digest.hex(), expected)


class EnergyDatabase:
    def __init__(self, path: str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                create table if not exists measurements (
                  id integer primary key,
                  observed_at text not null,
                  solar_power_w real,
                  vimar_power_w real not null,
                  tesla_power_w real not null,
                  total_consumption_w real not null,
                  import_power_w real not null,
                  export_power_w real not null,
                  tesla_current_a real,
                  tesla_target_a real,
                  controller_enabled integer not null,
                  action text,
                  reason text
                );
                create table if not exists appliance_measurements (
                  measurement_id integer not null,
                  name text not null,
                  power_w real,
                  foreign key(measurement_id) references measurements(id)
                );
                create table if not exists events (
                  id integer primary key,
                  observed_at text not null,
                  kind text not null,
                  level text not null,
                  message text not null,
                  details_json text
                );
                create table if not exists users (
                  username text primary key,
                  email text,
                  password_hash text not null,
                  role text not null check(role in ('admin', 'viewer'))
                );
                create index if not exists idx_measurements_observed_at
                  on measurements(observed_at);
                create index if not exists idx_measurements_day
                  on measurements(substr(observed_at, 1, 10));
                create index if not exists idx_measurements_month
                  on measurements(substr(observed_at, 1, 7));
                create index if not exists idx_appliance_measurement_id
                  on appliance_measurements(measurement_id);
                """
            )
            columns = {
                row["name"]
                for row in db.execute("pragma table_info(users)").fetchall()
            }
            if "email" not in columns:
                db.execute("alter table users add column email text")
            measurement_columns = {
                row["name"]
                for row in db.execute("pragma table_info(measurements)").fetchall()
            }
            for name in (
                "quarter_hour_import_w",
                "projected_quarter_hour_import_w",
                "imported_energy_wh",
                "exported_energy_wh",
                "alfa_grid_reading_enabled",
            ):
                if name not in measurement_columns and name != "alfa_grid_reading_enabled":
                    db.execute(f"alter table measurements add column {name} real")
                elif name not in measurement_columns:
                    db.execute(
                        "alter table measurements add column "
                        "alfa_grid_reading_enabled integer"
                    )
        os.chmod(self.path, 0o600)

    def add_measurement(self, data: dict, appliances: list[dict]) -> None:
        with self._connect() as db:
            cursor = db.execute(
                """
                insert into measurements (
                  observed_at, solar_power_w, vimar_power_w, tesla_power_w,
                  total_consumption_w, import_power_w, export_power_w,
                  tesla_current_a, tesla_target_a, controller_enabled, action, reason
                  , quarter_hour_import_w, projected_quarter_hour_import_w,
                  imported_energy_wh, exported_energy_wh, alfa_grid_reading_enabled
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["observed_at"],
                    data.get("solar_power_w"),
                    data["vimar_power_w"],
                    data["tesla_power_w"],
                    data["total_consumption_w"],
                    data["import_power_w"],
                    data["export_power_w"],
                    data.get("tesla_current_a"),
                    data.get("tesla_target_a"),
                    int(bool(data.get("controller_enabled"))),
                    data.get("action"),
                    data.get("reason"),
                    data.get("quarter_hour_import_w"),
                    data.get("projected_quarter_hour_import_w"),
                    data.get("imported_energy_wh"),
                    data.get("exported_energy_wh"),
                    int(bool(data.get("alfa_grid_reading_enabled"))),
                ),
            )
            measurement_id = cursor.lastrowid
            db.executemany(
                """
                insert into appliance_measurements (measurement_id, name, power_w)
                values (?, ?, ?)
                """,
                [
                    (measurement_id, item.get("name", ""), item.get("power_w"))
                    for item in appliances
                ],
            )

    def add_event(
        self,
        *,
        observed_at: str,
        kind: str,
        message: str,
        level: str = "info",
        details: dict | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                insert into events (observed_at, kind, level, message, details_json)
                values (?, ?, ?, ?, ?)
                """,
                (
                    observed_at,
                    kind,
                    level,
                    message,
                    json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                ),
            )

    def latest_measurements(self, limit: int = 24) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "select * from measurements order by id desc limit ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def measurement_days(self) -> list[str]:
        """Date locali (YYYY-MM-DD) per cui esistono misure, in ordine crescente."""
        with self._connect() as db:
            rows = db.execute(
                "select distinct substr(observed_at, 1, 10) as day "
                "from measurements order by day"
            ).fetchall()
        return [row["day"] for row in rows]

    def measurements_for_day(self, day: str) -> list[dict]:
        """Tutte le misure di un giorno locale, in ordine cronologico crescente."""
        start, end = self._day_bounds(day)
        with self._connect() as db:
            rows = db.execute(
                "select * from measurements "
                "where observed_at >= ? and observed_at < ? order by id asc",
                (start, end),
            ).fetchall()
        return [dict(row) for row in rows]

    def max_import_for_month(self, year_month: str) -> float:
        """Massima domanda quartoraria completa nel mese ``YYYY-MM`` locale."""
        start, end = self._month_bounds(year_month)
        with self._connect() as db:
            rows = db.execute(
                "select observed_at, import_power_w from measurements "
                "where observed_at >= ? and observed_at < ? order by observed_at",
                (start, end),
            ).fetchall()
        return monthly_peak_power_demand(
            ((row["observed_at"], row["import_power_w"]) for row in rows),
            year_month,
        )

    def max_instant_import_for_month(self, year_month: str) -> float:
        start, end = self._month_bounds(year_month)
        with self._connect() as db:
            row = db.execute(
                "select max(import_power_w) from measurements "
                "where observed_at >= ? and observed_at < ? "
                "and coalesce(alfa_grid_reading_enabled, 0) = 0",
                (start, end),
            ).fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    def import_samples_for_quarter(self, observed_at: str) -> list[tuple[str, float]]:
        stamp = datetime.fromisoformat(observed_at)
        quarter_start = stamp.replace(
            minute=(stamp.minute // 15) * 15,
            second=0,
            microsecond=0,
        )
        quarter_end = quarter_start + timedelta(minutes=15)
        with self._connect() as db:
            rows = db.execute(
                "select observed_at, import_power_w from measurements "
                "where observed_at >= ? and observed_at < ? "
                "and alfa_grid_reading_enabled = 1 order by observed_at",
                (quarter_start.isoformat(), quarter_end.isoformat()),
            ).fetchall()
        return [(row["observed_at"], float(row["import_power_w"])) for row in rows]

    def appliances_for_day(self, day: str) -> list[dict]:
        """Letture elettrodomestici di un giorno locale, in ordine cronologico."""
        start, end = self._day_bounds(day)
        with self._connect() as db:
            rows = db.execute(
                "select m.id as mid, m.observed_at as observed_at, "
                "a.name as name, a.power_w as power_w "
                "from appliance_measurements a "
                "join measurements m on a.measurement_id = m.id "
                "where m.observed_at >= ? and m.observed_at < ? order by m.id asc",
                (start, end),
            ).fetchall()
        return [dict(row) for row in rows]

    def device_series_for_day(self, day: str) -> tuple[list[dict], list[dict]]:
        """Misure aggregate e letture device di un giorno, allineabili su measurement id."""
        start, end = self._day_bounds(day)
        with self._connect() as db:
            measurements = db.execute(
                "select * from measurements "
                "where observed_at >= ? and observed_at < ? order by id asc",
                (start, end),
            ).fetchall()
            appliances = db.execute(
                "select m.id as mid, a.name as name, a.power_w as power_w "
                "from appliance_measurements a "
                "join measurements m on a.measurement_id = m.id "
                "where m.observed_at >= ? and m.observed_at < ? "
                "order by m.id asc, a.name asc",
                (start, end),
            ).fetchall()
        return [dict(row) for row in measurements], [dict(row) for row in appliances]

    def latest_appliances(self, limit: int = 50) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                """
                select m.observed_at, a.name, a.power_w
                from appliance_measurements a
                join measurements m on m.id = a.measurement_id
                order by m.id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_events(self, limit: int = 10) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("select * from events order by id desc limit ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def latest_error_events(self, limit: int = 100) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "select * from events where level = 'error' order by id desc limit ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_error_events(self) -> int:
        with self._connect() as db:
            cursor = db.execute("delete from events where level = 'error'")
            return cursor.rowcount

    def prune(self, retention_days: int) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self._connect() as db:
            db.execute(
                """
                delete from appliance_measurements
                where measurement_id in (
                  select id from measurements where observed_at < ?
                )
                """,
                (cutoff,),
            )
            db.execute("delete from measurements where observed_at < ?", (cutoff,))
            db.execute("delete from events where observed_at < ?", (cutoff,))

    @staticmethod
    def _day_bounds(day: str) -> tuple[str, str]:
        start = datetime.strptime(day, "%Y-%m-%d")
        end = start + timedelta(days=1)
        return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    @staticmethod
    def _month_bounds(year_month: str) -> tuple[str, str]:
        start = datetime.strptime(year_month, "%Y-%m")
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        return start.strftime("%Y-%m"), end.strftime("%Y-%m")

    def ensure_user(self, username: str | None, password: str | None, role: str) -> None:
        if not username or not password:
            return
        role = role if role in {"admin", "viewer"} else "viewer"
        with self._connect() as db:
            db.execute(
                """
                insert into users (username, email, password_hash, role)
                values (?, null, ?, ?)
                on conflict(username) do update set
                  role = excluded.role
                """,
                (username, _hash_password(password), role),
            )

    def create_user(
        self,
        *,
        username: str,
        email: str | None,
        password: str,
        role: str = "viewer",
    ) -> dict:
        role = role if role in {"admin", "viewer"} else "viewer"
        with self._connect() as db:
            db.execute(
                """
                insert into users (username, email, password_hash, role)
                values (?, ?, ?, ?)
                """,
                (username, email or None, _hash_password(password), role),
            )
        return {"username": username, "email": email or "", "role": role}

    def update_user_email(self, username: str, email: str | None) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "update users set email = ? where username = ?",
                (email or None, username),
            )
        return cursor.rowcount > 0

    def update_user(self, username: str, *, email: str | None, role: str) -> bool:
        role = role if role in {"admin", "viewer"} else "viewer"
        with self._connect() as db:
            cursor = db.execute(
                "update users set email = ?, role = ? where username = ?",
                (email or None, role, username),
            )
        return cursor.rowcount > 0

    def delete_user(self, username: str) -> bool:
        with self._connect() as db:
            cursor = db.execute("delete from users where username = ?", (username,))
        return cursor.rowcount > 0

    def admin_count(self) -> int:
        with self._connect() as db:
            row = db.execute("select count(*) from users where role = 'admin'").fetchone()
        return int(row[0]) if row else 0

    def admin_emails(self) -> tuple[str, ...]:
        with self._connect() as db:
            rows = db.execute(
                "select email from users where role = 'admin' and email is not null and email != ''"
            ).fetchall()
        return tuple(row["email"] for row in rows)

    def get_user(self, username: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "select username, email, role from users where username = ?",
                (username,),
            ).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "select username, email, role from users order by role asc, username asc"
            ).fetchall()
        return [dict(row) for row in rows]

    def change_password(self, username: str, current_password: str, new_password: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "select password_hash from users where username = ?",
                (username,),
            ).fetchone()
            if not row or not _verify_password(current_password, row["password_hash"]):
                return False
            db.execute(
                "update users set password_hash = ? where username = ?",
                (_hash_password(new_password), username),
            )
        return True

    def authenticate(self, username: str, password: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "select username, email, password_hash, role from users where username = ?",
                (username,),
            ).fetchone()
        if row and _verify_password(password, row["password_hash"]):
            return {"username": row["username"], "email": row["email"] or "", "role": row["role"]}
        return None
