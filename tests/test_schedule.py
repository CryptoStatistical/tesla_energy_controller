from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from tesla_energy_controller.config import Settings
from tesla_energy_controller.runtime import RuntimeSettings
from tesla_energy_controller.schedule import operating_window


def settings(monkeypatch):
    monkeypatch.setenv("ENERGY_SOURCE", "mock")
    monkeypatch.setenv("TESLA_TRANSPORT", "mock")
    monkeypatch.setenv("TESLA_MOCK", "true")
    monkeypatch.setenv("SOLAR_TIMEZONE", "Europe/Rome")
    return Settings.from_env()


def test_fixed_window_is_active_only_between_six_and_nineteen(monkeypatch):
    hard = settings(monkeypatch)
    runtime = replace(RuntimeSettings.defaults(hard), schedule_mode="fixed")
    timezone = ZoneInfo("Europe/Rome")
    assert operating_window(runtime, hard, datetime(2026, 6, 22, 12, tzinfo=timezone)).active
    assert not operating_window(runtime, hard, datetime(2026, 6, 22, 5, 59, tzinfo=timezone)).active
    assert not operating_window(runtime, hard, datetime(2026, 6, 22, 19, 1, tzinfo=timezone)).active


def test_solar_window_shrinks_in_winter(monkeypatch):
    hard = settings(monkeypatch)
    runtime = replace(
        RuntimeSettings.defaults(hard),
        schedule_mode="sun",
        latitude=41.9,
        longitude=12.5,
    )
    timezone = ZoneInfo("Europe/Rome")
    summer = operating_window(runtime, hard, datetime(2026, 6, 22, 12, tzinfo=timezone))
    winter = operating_window(runtime, hard, datetime(2026, 12, 22, 12, tzinfo=timezone))
    assert winter.starts_at.time() > summer.starts_at.time()
    assert winter.ends_at.time() < summer.ends_at.time()
    assert (winter.ends_at - winter.starts_at) < (summer.ends_at - summer.starts_at)
