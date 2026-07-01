from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astral import Observer
from astral.sun import sun

from .config import Settings
from .runtime import RuntimeSettings, RuntimeSettingsError


@dataclass(frozen=True)
class OperatingWindow:
    active: bool
    starts_at: datetime
    ends_at: datetime
    mode: str

    @property
    def label(self) -> str:
        return f"{self.starts_at:%H:%M}–{self.ends_at:%H:%M}"


def _clock(value: str) -> time:
    hour, minute = (int(part) for part in value.split(":"))
    return time(hour=hour, minute=minute)


def anomaly_window(
    runtime: RuntimeSettings,
    hard: Settings,
    now: datetime | None = None,
) -> OperatingWindow:
    try:
        timezone = ZoneInfo(hard.solar_timezone)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeSettingsError(f"Fuso orario non valido: {hard.solar_timezone}") from exc
    local_now = (now or datetime.now(timezone)).astimezone(timezone)

    if runtime.anomaly_window_mode == "sunset_sunrise":
        if runtime.latitude is None or runtime.longitude is None:
            raise RuntimeSettingsError("Coordinate mancanti per la finestra anomalie")
        observer = Observer(latitude=runtime.latitude, longitude=runtime.longitude)
        today = sun(observer, date=local_now.date(), tzinfo=timezone)
        if local_now <= today["sunrise"]:
            yesterday = sun(
                observer,
                date=local_now.date() - timedelta(days=1),
                tzinfo=timezone,
            )
            starts_at = yesterday["sunset"]
            ends_at = today["sunrise"]
        else:
            tomorrow = sun(
                observer,
                date=local_now.date() + timedelta(days=1),
                tzinfo=timezone,
            )
            starts_at = today["sunset"]
            ends_at = tomorrow["sunrise"]
        mode = "sunset_sunrise"
    else:
        start_clock = _clock(runtime.anomaly_fixed_start_time)
        end_clock = _clock(runtime.anomaly_fixed_end_time)
        starts_at = datetime.combine(local_now.date(), start_clock, timezone)
        ends_at = datetime.combine(local_now.date(), end_clock, timezone)
        if starts_at > ends_at:
            if local_now <= ends_at:
                starts_at -= timedelta(days=1)
            else:
                ends_at += timedelta(days=1)
        mode = "fixed"

    return OperatingWindow(
        active=starts_at <= local_now <= ends_at,
        starts_at=starts_at,
        ends_at=ends_at,
        mode=mode,
    )


def operating_window(
    runtime: RuntimeSettings,
    hard: Settings,
    now: datetime | None = None,
) -> OperatingWindow:
    try:
        timezone = ZoneInfo(hard.solar_timezone)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeSettingsError(f"Fuso orario non valido: {hard.solar_timezone}") from exc
    local_now = (now or datetime.now(timezone)).astimezone(timezone)

    if runtime.schedule_mode == "sun":
        if runtime.latitude is None or runtime.longitude is None:
            raise RuntimeSettingsError("Coordinate mancanti per il calendario solare")
        events = sun(
            Observer(latitude=runtime.latitude, longitude=runtime.longitude),
            date=local_now.date(),
            tzinfo=timezone,
        )
        starts_at = events["sunrise"] + timedelta(minutes=runtime.sunrise_offset_minutes)
        ends_at = events["sunset"] + timedelta(minutes=runtime.sunset_offset_minutes)
        mode = "sun"
    else:
        starts_at = datetime.combine(local_now.date(), _clock(runtime.fixed_start_time), timezone)
        ends_at = datetime.combine(local_now.date(), _clock(runtime.fixed_end_time), timezone)
        mode = "fixed"

    return OperatingWindow(
        active=starts_at <= local_now <= ends_at,
        starts_at=starts_at,
        ends_at=ends_at,
        mode=mode,
    )
