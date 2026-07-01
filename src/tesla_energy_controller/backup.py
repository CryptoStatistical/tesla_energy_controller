from __future__ import annotations

import io
import json
import secrets
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .runtime import RuntimeSettings, RuntimeSettingsError
from .storage import EnergyDatabase


class BackupImportError(ValueError):
    pass


@dataclass(frozen=True)
class BackupRestoreResult:
    db: EnergyDatabase
    imported_runtime: RuntimeSettings | None
    restored: list[str]


def _safe_text(value, *, limit: int = 240) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\x00", "").strip()
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _safe_archive_part(value, *, fallback: str) -> str:
    text = _safe_text(value, limit=80) or fallback
    clean = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in text)
    return clean.strip("._-") or fallback


class BackupService:
    def __init__(self, hard: Settings, db: EnergyDatabase) -> None:
        self.hard = hard
        self.db = db

    @staticmethod
    def _local_path(value: str | None) -> Path | None:
        if not value:
            return None
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return path

    @staticmethod
    def _write_private_file(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
        try:
            tmp_path.write_bytes(data)
            tmp_path.chmod(0o600)
            tmp_path.replace(path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    @staticmethod
    def _read_archive_member(zipf: zipfile.ZipFile, name: str) -> bytes | None:
        try:
            info = zipf.getinfo(name)
        except KeyError:
            return None
        if info.is_dir():
            return None
        return zipf.read(info)

    def _referenced_config_files(self) -> tuple[tuple[str, Path | None, str], ...]:
        rows = []
        for label, value in (
            ("tesla_ble_key_file", self.hard.tesla_ble_key_file),
            ("tesla_ble_cache_file", self.hard.tesla_ble_cache_file),
            ("tesla_token_file", self.hard.tesla_token_file),
            ("tesla_ca_cert", self.hard.tesla_ca_cert),
            ("solaredge_session_file", self.hard.solaredge_session_file),
            ("vimar_private_key_file", self.hard.vimar_private_key_file),
            ("vimar_public_key_file", self.hard.vimar_public_key_file),
            ("vimar_credentials_file", self.hard.vimar_credentials_file),
            ("vimar_ca_cert", self.hard.vimar_ca_cert),
        ):
            path = self._local_path(value)
            filename = path.name if path else "file"
            archive_name = (
                f"referenced-files/{_safe_archive_part(label, fallback='file')}-"
                f"{_safe_archive_part(filename, fallback='config')}"
            )
            rows.append((archive_name, path, f"file referenziato: {label}"))
        return tuple(rows)

    def _validate_database_backup(self, data: bytes) -> Path:
        db_path = self.db.path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = db_path.parent / f".{db_path.name}.restore-{secrets.token_hex(8)}.tmp"
        tmp_path.write_bytes(data)
        try:
            with sqlite3.connect(tmp_path) as candidate:
                integrity = candidate.execute("pragma integrity_check").fetchone()
                if not integrity or integrity[0] != "ok":
                    raise BackupImportError("Database SQLite nel backup non integro")
                tables = {
                    row[0]
                    for row in candidate.execute(
                        "select name from sqlite_master where type = 'table'"
                    ).fetchall()
                }
                required = {"measurements", "appliance_measurements", "events", "users"}
                missing = required - tables
                if missing:
                    raise BackupImportError(
                        "Database backup incompleto: mancano "
                        + ", ".join(sorted(missing))
                    )
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise
        tmp_path.chmod(0o600)
        return tmp_path

    def backup_archive(
        self,
        *,
        include_db: bool = True,
        include_config: bool = True,
    ) -> tuple[io.BytesIO, str]:
        if not include_db and not include_config:
            raise BackupImportError("Seleziona almeno DB o configurazione")
        created_at = datetime.now(timezone.utc).astimezone().isoformat()
        stamp = created_at.replace(":", "").replace("-", "").split(".", 1)[0]
        buffer = io.BytesIO()
        seen: set[Path] = set()
        manifest: dict = {
            "created_at": created_at,
            "mode": self.hard.mode,
            "energy_source": self.hard.energy_source,
            "control_mode": self.hard.control_mode,
            "includes": {"db": include_db, "config": include_config},
            "files": [],
            "missing": [],
        }

        def remember(archive_name: str, path: Path, description: str) -> None:
            manifest["files"].append(
                {
                    "archive": archive_name,
                    "description": description,
                    "source": str(path),
                    "size": path.stat().st_size,
                }
            )

        def add_file(
            zipf: zipfile.ZipFile,
            path: Path | None,
            archive_name: str,
            description: str,
        ) -> None:
            if path is None:
                return
            try:
                resolved = path.resolve()
            except OSError:
                manifest["missing"].append({"description": description, "source": str(path)})
                return
            if resolved in seen:
                return
            if not resolved.is_file():
                manifest["missing"].append({"description": description, "source": str(path)})
                return
            seen.add(resolved)
            zipf.write(resolved, archive_name)
            remember(archive_name, resolved, description)

        with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zipf:
            if include_db:
                with tempfile.NamedTemporaryFile(suffix=".sqlite3") as db_copy:
                    with sqlite3.connect(self.db.path) as source, sqlite3.connect(
                        db_copy.name
                    ) as snapshot:
                        source.backup(snapshot)
                    zipf.write(db_copy.name, "data/energy.sqlite3")
                    manifest["files"].append(
                        {
                            "archive": "data/energy.sqlite3",
                            "description": "snapshot consistente del database SQLite",
                            "source": str(self.db.path),
                            "size": Path(db_copy.name).stat().st_size,
                        }
                    )

            if include_config:
                add_file(zipf, self._local_path(".env"), "config/.env", "configurazione ambiente")
                add_file(
                    zipf,
                    self._local_path(self.hard.runtime_settings_file),
                    "config/runtime_settings.json",
                    "configurazione runtime dashboard",
                )
                for archive_name, path, description in self._referenced_config_files():
                    add_file(zipf, path, archive_name, description)

            zipf.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
            )

        buffer.seek(0)
        return buffer, f"tesla-energy-controller-backup-{stamp}.zip"

    def restore_archive(
        self,
        archive_data: bytes,
        *,
        restore_db: bool = True,
        restore_config: bool = True,
    ) -> BackupRestoreResult:
        if not archive_data:
            raise BackupImportError("Carica un file backup ZIP")
        if not restore_db and not restore_config:
            raise BackupImportError("Seleziona almeno DB o configurazione da ripristinare")

        restored = []
        db_candidate: Path | None = None
        runtime_data = None
        imported_runtime = None
        config_files: list[tuple[Path, bytes]] = []
        try:
            with zipfile.ZipFile(io.BytesIO(archive_data)) as zipf:
                if restore_db:
                    db_data = self._read_archive_member(zipf, "data/energy.sqlite3")
                    if db_data is None:
                        raise BackupImportError("Backup non valido: manca data/energy.sqlite3")
                    db_candidate = self._validate_database_backup(db_data)

                if restore_config:
                    runtime_data = self._read_archive_member(zipf, "config/runtime_settings.json")
                    if runtime_data is not None:
                        try:
                            runtime_json = json.loads(runtime_data.decode("utf-8"))
                            imported_runtime = RuntimeSettings.from_mapping(runtime_json, self.hard)
                        except (UnicodeDecodeError, json.JSONDecodeError, RuntimeSettingsError) as exc:
                            raise BackupImportError(
                                "runtime_settings.json nel backup non valido"
                            ) from exc

                    env_data = self._read_archive_member(zipf, "config/.env")
                    if env_data is not None:
                        config_files.append((self._local_path(".env") or Path(".env"), env_data))

                    if runtime_data is not None:
                        config_files.append(
                            (
                                self._local_path(self.hard.runtime_settings_file)
                                or Path(self.hard.runtime_settings_file),
                                runtime_data,
                            )
                        )

                    for archive_name, path, _description in self._referenced_config_files():
                        file_data = self._read_archive_member(zipf, archive_name)
                        if file_data is not None and path is not None:
                            config_files.append((path, file_data))
        except zipfile.BadZipFile as exc:
            raise BackupImportError("Backup non valido: ZIP non leggibile") from exc
        except Exception:
            if db_candidate is not None and db_candidate.exists():
                db_candidate.unlink()
            raise

        if restore_config and not config_files:
            if db_candidate is not None and db_candidate.exists():
                db_candidate.unlink()
            raise BackupImportError("Backup non contiene configurazioni ripristinabili")

        db = self.db
        try:
            if db_candidate is not None:
                db_candidate.replace(self.db.path)
                db = EnergyDatabase(str(self.db.path))
                restored.append("database")

            for path, data in config_files:
                self._write_private_file(path, data)

            if imported_runtime is not None:
                restored.append("runtime")
            if any(path.name == ".env" for path, _data in config_files):
                restored.append(".env")
            if any(str(path) for path, _data in config_files if path.name != ".env"):
                restored.append("config")
        finally:
            if db_candidate is not None and db_candidate.exists():
                db_candidate.unlink()

        return BackupRestoreResult(
            db=db,
            imported_runtime=imported_runtime,
            restored=restored,
        )
