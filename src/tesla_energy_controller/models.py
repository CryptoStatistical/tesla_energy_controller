from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class GridMeasurement:
    """Snapshot energetico del contatore/inverter.

    ``total_power_w`` è la potenza di rete (positiva in importazione), quando
    disponibile. ``solar_power_w`` è la produzione FV istantanea.
    """

    total_power_w: float
    solar_power_w: float | None = None
    import_power_w: float | None = None
    export_power_w: float | None = None
    total_consumption_w: float | None = None
    imported_energy_wh: float | None = None
    exported_energy_wh: float | None = None
    produced_energy_wh: float | None = None
    quarter_hour_import_power_w: float | None = None
    quarter_hour_export_power_w: float | None = None
    alfa_power_limit_remaining_seconds: float | None = None
    alfa_current_tariff: int | None = None
    alfa_event_timestamp_raw: int | None = None
    imported_energy_by_tariff_wh: tuple[float, ...] = ()
    exported_energy_by_tariff_wh: tuple[float, ...] = ()
    phase_power_w: tuple[float, ...] = ()
    phase_current_a: tuple[float, ...] = ()
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    fresh: bool = True
    source: str = "unknown"


@dataclass(frozen=True)
class ChargeState:
    charging_state: str
    current_request_a: int
    current_request_max_a: int
    actual_current_a: float
    phases: int
    voltage_v: float
    charger_power_kw: float = 0.0

    @property
    def is_charging(self) -> bool:
        return self.charging_state.casefold() == "charging"

    @property
    def charging_power_w(self) -> float:
        """Potenza assorbita dalla Tesla in W.

        Preferisce ``charger_power`` riportato dall'auto quando presente,
        perché rappresenta direttamente la potenza del charger vista dalla
        Tesla, anche con auto collegata ma non in carica. Se non è disponibile
        ripiega su ``corrente × tensione × fasi``.
        """
        reported = max(self.charger_power_kw * 1000.0, 0.0)
        if reported > 0:
            return reported
        if not self.is_charging:
            return 0.0
        return max(self.actual_current_a * self.voltage_v * max(self.phases, 1), 0.0)


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    current_a: int | None = None
    target_a: int | None = None
    grid_power_w: float | None = None
    solar_power_w: float | None = None
    voltage_v: float | None = None
    actual_current_a: float | None = None
    charger_power_kw: float | None = None
