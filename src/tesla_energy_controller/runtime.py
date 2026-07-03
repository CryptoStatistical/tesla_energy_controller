from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from .config import Settings
from .controller import EnergyController


class RuntimeSettingsError(ValueError):
    pass


@dataclass(frozen=True)
class AnomalyDeviceGroup:
    name: str
    threshold_w: float
    patterns: tuple[str, ...]


def _threshold_to_w(value: str) -> float:
    text = str(value).strip().casefold().replace(",", ".")
    if not text:
        raise ValueError("soglia vuota")
    is_kw = "kw" in text
    is_w = text.endswith("w") and not is_kw
    number_text = text.replace("kw", "").replace("w", "").strip()
    number = float(number_text)
    return number if is_w or number > 20 else number * 1000


def parse_anomaly_device_groups(value: str) -> list[AnomalyDeviceGroup]:
    groups = []
    for index, raw_line in enumerate(str(value or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) == 2:
            name = f"Gruppo {index}"
            threshold_raw, patterns_raw = parts
        elif len(parts) >= 3:
            name, threshold_raw, patterns_raw = parts[0], parts[1], "|".join(parts[2:])
        else:
            raise RuntimeSettingsError(
                "Formato gruppi anomalie: Nome | soglia kW | elettrodomestico, elettrodomestico"
            )
        patterns = tuple(
            item.strip()
            for chunk in patterns_raw.splitlines()
            for item in chunk.split(",")
            if item.strip()
        )
        if not name:
            name = f"Gruppo {index}"
        if not patterns:
            raise RuntimeSettingsError(f"Gruppo anomalie senza elettrodomestici: {name}")
        try:
            threshold_w = _threshold_to_w(threshold_raw)
        except ValueError as exc:
            raise RuntimeSettingsError(f"Soglia anomalie non valida per {name}") from exc
        groups.append(AnomalyDeviceGroup(name=name, threshold_w=threshold_w, patterns=patterns))
    return groups


@dataclass(frozen=True)
class RuntimeSettings:
    enabled: bool
    alfa_grid_reading_enabled: bool
    solar_source: str
    schedule_mode: str
    fixed_start_time: str
    fixed_end_time: str
    latitude: float | None
    longitude: float | None
    expected_phases: int
    sunrise_offset_minutes: int
    sunset_offset_minutes: int
    extra_grid_power_w: float
    power_quota_target_w: float
    power_quota_hysteresis_w: float
    manual_override_amps: int
    min_voltage_v: float
    max_voltage_v: float
    min_charge_amps: int
    max_charge_amps: int
    command_hysteresis_a: int
    max_ramp_up_a: int
    tesla_ble_connect_timeout_seconds: int
    tesla_ble_command_timeout_seconds: int
    tesla_ble_retries: int
    tesla_ble_recovery_enabled: bool
    anomaly_peak_threshold_w: float
    anomaly_device_patterns: str
    anomaly_device_groups: str
    anomaly_window_mode: str
    anomaly_fixed_start_time: str
    anomaly_fixed_end_time: str
    error_email_enabled: bool
    anomaly_email_enabled: bool

    @classmethod
    def defaults(cls, settings: Settings) -> "RuntimeSettings":
        return cls(
            enabled=True,
            alfa_grid_reading_enabled=False,
            solar_source=(
                settings.energy_source
                if settings.energy_source
                in {
                    "mock",
                    "alfa-modbus",
                    "solaredge-web",
                    "solaredge-cloud",
                    "solaredge-modbus",
                }
                else "mock"
            ),
            # Sede fissa (Vittorio Veneto): default calendario su alba/tramonto.
            schedule_mode="sun",
            fixed_start_time="06:00",
            fixed_end_time="19:00",
            latitude=settings.solar_latitude,
            longitude=settings.solar_longitude,
            expected_phases=settings.expected_phases,
            sunrise_offset_minutes=0,
            sunset_offset_minutes=0,
            extra_grid_power_w=settings.extra_grid_power_w,
            power_quota_target_w=settings.power_quota_target_w,
            power_quota_hysteresis_w=settings.power_quota_hysteresis_w,
            manual_override_amps=settings.manual_override_amps,
            min_voltage_v=settings.min_voltage_v,
            max_voltage_v=settings.max_voltage_v,
            min_charge_amps=settings.min_charge_amps,
            # La massima gestita è sempre una sotto la soglia di override manuale.
            max_charge_amps=settings.manual_override_amps - 1,
            command_hysteresis_a=settings.command_hysteresis_a,
            max_ramp_up_a=settings.max_ramp_up_a,
            tesla_ble_connect_timeout_seconds=settings.tesla_ble_connect_timeout_seconds,
            tesla_ble_command_timeout_seconds=settings.tesla_ble_command_timeout_seconds,
            tesla_ble_retries=settings.tesla_ble_retries,
            tesla_ble_recovery_enabled=True,
            anomaly_peak_threshold_w=1500.0,
            anomaly_device_patterns=(
                "forno, forni, pompa di calore, frigo, frighi, cucina, "
                "lavatrice, lavastoviglie"
            ),
            anomaly_device_groups=(
                "Cucina | 1.5 | forno, forni, cucina, induzione\n"
                "Frighi | 1.5 | frigo, frighi\n"
                "Lavaggio | 1.5 | lavatrice, lavastoviglie\n"
                "Pompa di calore | 1.5 | pompa di calore"
            ),
            anomaly_window_mode="sunset_sunrise",
            anomaly_fixed_start_time="22:00",
            anomaly_fixed_end_time="06:00",
            error_email_enabled=True,
            anomaly_email_enabled=True,
        )

    @classmethod
    def from_mapping(cls, values: dict, settings: Settings) -> "RuntimeSettings":
        def optional_float(value) -> float | None:
            return None if value in {None, ""} else float(value)

        def boolean(value) -> bool:
            return value in {True, "true", "1", "on", "yes"}

        def clock(value) -> str:
            hour, minute = (int(part) for part in str(value).strip().split(":", 1))
            return f"{hour:02d}:{minute:02d}"

        try:
            threshold_w = float(values["anomaly_peak_threshold_w"])
            patterns_text = str(values["anomaly_device_patterns"]).strip()
            groups_text = str(values.get("anomaly_device_groups") or "").strip()
            if not groups_text:
                groups_text = f"Default | {threshold_w / 1000:g} | {patterns_text}"
            result = cls(
                enabled=boolean(values.get("enabled")),
                alfa_grid_reading_enabled=boolean(
                    values.get("alfa_grid_reading_enabled")
                ),
                solar_source=str(values.get("solar_source") or settings.energy_source)
                .strip()
                .casefold(),
                schedule_mode=str(values["schedule_mode"]).strip().casefold(),
                fixed_start_time=clock(values["fixed_start_time"]),
                fixed_end_time=clock(values["fixed_end_time"]),
                latitude=optional_float(values.get("latitude")),
                longitude=optional_float(values.get("longitude")),
                expected_phases=int(values.get("expected_phases", settings.expected_phases)),
                sunrise_offset_minutes=int(values["sunrise_offset_minutes"]),
                sunset_offset_minutes=int(values["sunset_offset_minutes"]),
                extra_grid_power_w=float(values["extra_grid_power_w"]),
                power_quota_target_w=float(values["power_quota_target_w"]),
                power_quota_hysteresis_w=float(values["power_quota_hysteresis_w"]),
                manual_override_amps=int(values["manual_override_amps"]),
                min_voltage_v=float(values["min_voltage_v"]),
                max_voltage_v=float(values["max_voltage_v"]),
                min_charge_amps=int(values["min_charge_amps"]),
                max_charge_amps=int(values["manual_override_amps"]) - 1,
                command_hysteresis_a=int(values["command_hysteresis_a"]),
                max_ramp_up_a=int(values["max_ramp_up_a"]),
                tesla_ble_connect_timeout_seconds=int(
                    values["tesla_ble_connect_timeout_seconds"]
                ),
                tesla_ble_command_timeout_seconds=int(
                    values["tesla_ble_command_timeout_seconds"]
                ),
                tesla_ble_retries=int(values["tesla_ble_retries"]),
                tesla_ble_recovery_enabled=boolean(
                    values.get("tesla_ble_recovery_enabled")
                ),
                anomaly_peak_threshold_w=threshold_w,
                anomaly_device_patterns=patterns_text,
                anomaly_device_groups=groups_text,
                anomaly_window_mode=str(values["anomaly_window_mode"]).strip().casefold(),
                anomaly_fixed_start_time=clock(values["anomaly_fixed_start_time"]),
                anomaly_fixed_end_time=clock(values["anomaly_fixed_end_time"]),
                error_email_enabled=boolean(values.get("error_email_enabled")),
                anomaly_email_enabled=boolean(values.get("anomaly_email_enabled")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeSettingsError("Impostazioni mancanti o non numeriche") from exc
        result.validate(settings)
        return result

    def validate(self, hard: Settings) -> None:
        if self.solar_source not in {
            "mock",
            "alfa-modbus",
            "solaredge-web",
            "solaredge-cloud",
            "solaredge-modbus",
        }:
            raise RuntimeSettingsError(
                "Sorgente fotovoltaico: scegliere mock, ALFA, SolarEdge web, cloud o Modbus"
            )
        if self.solar_source == "alfa-modbus" and not hard.alfa_modbus_host:
            raise RuntimeSettingsError("ALFA Modbus richiede ALFA_MODBUS_HOST in .env")
        if self.solar_source == "solaredge-web" and (
            not hard.solaredge_username
            or not hard.solaredge_password
            or hard.solaredge_site_id is None
        ):
            raise RuntimeSettingsError(
                "SolarEdge web richiede credenziali e SOLAREDGE_SITE_ID in .env"
            )
        if self.solar_source == "solaredge-cloud" and (
            not hard.solaredge_api_key or hard.solaredge_site_id is None
        ):
            raise RuntimeSettingsError("SolarEdge cloud richiede API key e SOLAREDGE_SITE_ID")
        if self.solar_source == "solaredge-modbus" and not hard.solaredge_modbus_host:
            raise RuntimeSettingsError("SolarEdge Modbus richiede SOLAREDGE_MODBUS_HOST in .env")
        if self.schedule_mode not in {"fixed", "sun"}:
            raise RuntimeSettingsError("Modalità calendario non valida")
        try:
            start_hour, start_minute = (int(value) for value in self.fixed_start_time.split(":"))
            end_hour, end_minute = (int(value) for value in self.fixed_end_time.split(":"))
        except (ValueError, TypeError) as exc:
            raise RuntimeSettingsError("Gli orari devono usare il formato HH:MM") from exc
        if not (0 <= start_hour <= 23 and 0 <= start_minute <= 59):
            raise RuntimeSettingsError("Orario iniziale non valido")
        if not (0 <= end_hour <= 23 and 0 <= end_minute <= 59):
            raise RuntimeSettingsError("Orario finale non valido")
        if (start_hour, start_minute) >= (end_hour, end_minute):
            raise RuntimeSettingsError("L'orario finale deve essere successivo a quello iniziale")
        if self.schedule_mode == "sun" and (self.latitude is None or self.longitude is None):
            raise RuntimeSettingsError("Per alba/tramonto servono latitudine e longitudine")
        if self.latitude is not None and not -90 <= self.latitude <= 90:
            raise RuntimeSettingsError("Latitudine non valida")
        if self.longitude is not None and not -180 <= self.longitude <= 180:
            raise RuntimeSettingsError("Longitudine non valida")
        if self.expected_phases not in {1, 3}:
            raise RuntimeSettingsError("Tipo impianto non valido: usare monofase o trifase")
        if not -180 <= self.sunrise_offset_minutes <= 180:
            raise RuntimeSettingsError("Offset alba ammesso: -180/+180 minuti")
        if not -180 <= self.sunset_offset_minutes <= 180:
            raise RuntimeSettingsError("Offset tramonto ammesso: -180/+180 minuti")
        if self.extra_grid_power_w < 0:
            raise RuntimeSettingsError("Extra rete non può essere negativo")
        if self.power_quota_target_w <= 0:
            raise RuntimeSettingsError("Obiettivo quota potenza deve essere positivo")
        if not 0 <= self.power_quota_hysteresis_w <= 5000:
            raise RuntimeSettingsError("Isteresi quota potenza ammessa: 0-5 kW")
        if not hard.min_charge_amps <= self.manual_override_amps <= hard.max_charge_amps:
            raise RuntimeSettingsError("Soglia override manuale non valida")
        if not hard.min_voltage_v <= self.min_voltage_v < self.max_voltage_v:
            raise RuntimeSettingsError("Soglia minima di tensione non valida")
        if self.max_voltage_v > hard.max_voltage_v:
            raise RuntimeSettingsError(
                f"La tensione massima non può superare {hard.max_voltage_v:g} V"
            )
        if not 1 <= self.min_charge_amps <= self.max_charge_amps:
            raise RuntimeSettingsError("Intervallo ampere non valido")
        if self.max_charge_amps > hard.max_charge_amps:
            raise RuntimeSettingsError(
                f"Il massimo non può superare il limite hardware di {hard.max_charge_amps} A"
            )
        if self.max_charge_amps >= self.manual_override_amps:
            raise RuntimeSettingsError(
                "Il massimo gestito deve restare sotto la soglia override manuale"
            )
        if not 1 <= self.command_hysteresis_a <= 5:
            raise RuntimeSettingsError("Isteresi: valore ammesso 1-5 A")
        if not 1 <= self.max_ramp_up_a <= hard.max_ramp_up_a:
            raise RuntimeSettingsError(f"Rampa massima: valore ammesso 1-{hard.max_ramp_up_a} A")
        if not 5 <= self.tesla_ble_connect_timeout_seconds <= 90:
            raise RuntimeSettingsError("Timeout connessione Tesla BLE ammesso: 5-90 secondi")
        if not 3 <= self.tesla_ble_command_timeout_seconds <= 60:
            raise RuntimeSettingsError("Timeout comando Tesla BLE ammesso: 3-60 secondi")
        if not 0 <= self.tesla_ble_retries <= 5:
            raise RuntimeSettingsError("Retry Tesla BLE ammessi: 0-5")
        if not 100 <= self.anomaly_peak_threshold_w <= 20000:
            raise RuntimeSettingsError("Soglia picchi anomali ammessa: 0.1-20 kW")
        legacy_patterns = [
            item.strip()
            for chunk in self.anomaly_device_patterns.splitlines()
            for item in chunk.split(",")
            if item.strip()
        ]
        groups = parse_anomaly_device_groups(self.anomaly_device_groups)
        if not groups and not legacy_patterns:
            raise RuntimeSettingsError(
                "Configurare almeno un gruppo di elettrodomestici per i picchi anomali"
            )
        if len(groups) > 30:
            raise RuntimeSettingsError("Troppi gruppi anomalie: massimo 30")
        pattern_count = sum(len(group.patterns) for group in groups)
        if pattern_count > 90:
            raise RuntimeSettingsError("Troppi pattern elettrodomestici: massimo 90")
        for group in groups:
            if not 100 <= group.threshold_w <= 20000:
                raise RuntimeSettingsError(
                    f"Soglia gruppo {group.name} ammessa: 0.1-20 kW"
                )
        if self.anomaly_window_mode not in {"sunset_sunrise", "fixed"}:
            raise RuntimeSettingsError("Modalità finestra anomalie non valida")
        try:
            anomaly_start_hour, anomaly_start_minute = (
                int(value) for value in self.anomaly_fixed_start_time.split(":")
            )
            anomaly_end_hour, anomaly_end_minute = (
                int(value) for value in self.anomaly_fixed_end_time.split(":")
            )
        except (ValueError, TypeError) as exc:
            raise RuntimeSettingsError("Orari anomalie: usare formato HH:MM") from exc
        if not (0 <= anomaly_start_hour <= 23 and 0 <= anomaly_start_minute <= 59):
            raise RuntimeSettingsError("Orario iniziale anomalie non valido")
        if not (0 <= anomaly_end_hour <= 23 and 0 <= anomaly_end_minute <= 59):
            raise RuntimeSettingsError("Orario finale anomalie non valido")
        if (anomaly_start_hour, anomaly_start_minute) == (
            anomaly_end_hour,
            anomaly_end_minute,
        ):
            raise RuntimeSettingsError("La finestra anomalie deve avere inizio e fine diversi")

    def apply(self, controller: EnergyController) -> None:
        controller.expected_phases = self.expected_phases
        controller.min_voltage_v = self.min_voltage_v
        controller.max_voltage_v = self.max_voltage_v
        controller.min_charge_amps = self.min_charge_amps
        controller.max_charge_amps = self.max_charge_amps
        controller.command_hysteresis_a = self.command_hysteresis_a
        controller.max_ramp_up_a = self.max_ramp_up_a
        vehicle = controller.vehicle
        if hasattr(vehicle, "connect_timeout_seconds"):
            vehicle.connect_timeout_seconds = self.tesla_ble_connect_timeout_seconds
        if hasattr(vehicle, "command_timeout_seconds"):
            vehicle.command_timeout_seconds = self.tesla_ble_command_timeout_seconds
        if hasattr(vehicle, "retries"):
            vehicle.retries = self.tesla_ble_retries
        if hasattr(vehicle, "recovery_enabled"):
            vehicle.recovery_enabled = self.tesla_ble_recovery_enabled
        if hasattr(vehicle, "state") and getattr(vehicle.state, "phases", None) is not None:
            state = vehicle.state
            charger_power_kw = (
                state.actual_current_a
                * state.voltage_v
                * max(self.expected_phases, 1)
                / 1000.0
            )
            vehicle.state = replace(
                state,
                phases=self.expected_phases,
                charger_power_kw=charger_power_kw,
            )


class RuntimeSettingsStore:
    def __init__(self, path: str, hard_settings: Settings) -> None:
        self.path = Path(path).expanduser()
        self.hard_settings = hard_settings

    def load(self) -> RuntimeSettings:
        defaults = RuntimeSettings.defaults(self.hard_settings)
        if not self.path.exists():
            return defaults
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeSettingsError(f"Impossibile leggere {self.path}: {exc}") from exc
        allowed = {item.name for item in fields(RuntimeSettings)}
        merged = asdict(defaults)
        merged.update({key: value for key, value in raw.items() if key in allowed})
        return RuntimeSettings.from_mapping(merged, self.hard_settings)

    def save(self, settings: RuntimeSettings) -> None:
        settings.validate(self.hard_settings)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(
                json.dumps(asdict(settings), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            temporary.replace(self.path)
        except OSError as exc:
            raise RuntimeSettingsError(f"Impossibile salvare {self.path}: {exc}") from exc
