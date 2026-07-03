from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATUS_FIELDS = (
    "updated_at",
    "state",
    "message",
    "action",
    "controller_enabled",
    "expected_phases",
    "solar_power_w",
    "house_power_w",
    "appliances_power_w",
    "vimar_power_w",
    "tesla_power_w",
    "total_consumption_w",
    "import_power_w",
    "export_power_w",
    "target_a",
    "manual_override_active",
    "manual_override_a",
    "tesla_current_a",
    "current_a",
    "tesla_connected",
    "tesla_power_source",
    "wall_connector_vehicle_connected",
    "wall_connector_contactor_closed",
)


def status_cache_file(settings: Any) -> Path:
    configured = (os.getenv("DASHBOARD_STATUS_CACHE_FILE") or "").strip()
    if configured:
        return Path(configured)
    database_file = getattr(settings, "energy_database_file", None)
    if database_file:
        return Path(database_file).expanduser().parent / "dashboard_status.json"
    return Path("data/dashboard_status.json")


def write_status_cache(settings: Any, status: dict[str, Any]) -> None:
    path = status_cache_file(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cached_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "status": {key: status.get(key) for key in STATUS_FIELDS if key in status},
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_status_cache(settings: Any, *, max_age_seconds: int) -> dict[str, Any] | None:
    try:
        payload = json.loads(status_cache_file(settings).read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(str(payload.get("cached_at") or ""))
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc).astimezone() - cached_at.astimezone()
        if age.total_seconds() > max_age_seconds:
            return None
        status = payload.get("status")
        return status if isinstance(status, dict) else None
    except Exception:
        return None
