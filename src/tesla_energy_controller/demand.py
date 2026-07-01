from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable


@dataclass(frozen=True)
class PowerDemand:
    quarter_start: str
    sample_count: int
    sampled_average_w: float
    projected_average_w: float
    completed_average_w: float | None


def _parse_stamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _quarter_key(stamp: datetime) -> tuple[str, int]:
    start = stamp.replace(minute=(stamp.minute // 15) * 15, second=0, microsecond=0)
    slot = (stamp.minute % 15) // 5
    return start.isoformat(), slot


def calculate_power_demand(
    samples: Iterable[tuple[str | datetime, float]],
    *,
    observed_at: str | datetime,
    import_power_w: float,
) -> PowerDemand:
    """Calcola la domanda su tre campioni allineati da cinque minuti."""
    current_stamp = _parse_stamp(observed_at)
    current_quarter, current_slot = _quarter_key(current_stamp)
    slots: dict[int, list[float]] = {}

    for raw_stamp, raw_power in samples:
        stamp = _parse_stamp(raw_stamp)
        quarter, slot = _quarter_key(stamp)
        if quarter != current_quarter:
            continue
        slots.setdefault(slot, []).append(max(float(raw_power), 0.0))

    slots.setdefault(current_slot, []).append(max(float(import_power_w), 0.0))
    values = [sum(item) / len(item) for item in slots.values()]
    sampled_average = sum(values) / len(values)

    # Gli slot ancora mancanti vengono stimati mantenendo il prelievo corrente.
    projected_values = values + [max(float(import_power_w), 0.0)] * (3 - len(values))
    projected_average = sum(projected_values) / 3
    completed_average = sum(values) / 3 if len(slots) == 3 else None
    return PowerDemand(
        quarter_start=current_quarter,
        sample_count=len(slots),
        sampled_average_w=sampled_average,
        projected_average_w=projected_average,
        completed_average_w=completed_average,
    )


def monthly_peak_power_demand(
    samples: Iterable[tuple[str | datetime, float]],
    year_month: str,
) -> float:
    """Restituisce il massimo mensile dei soli quarti con tutti e tre gli slot."""
    quarters: dict[str, dict[int, list[float]]] = {}
    for raw_stamp, raw_power in samples:
        stamp = _parse_stamp(raw_stamp)
        if stamp.strftime("%Y-%m") != year_month:
            continue
        quarter, slot = _quarter_key(stamp)
        quarters.setdefault(quarter, {}).setdefault(slot, []).append(
            max(float(raw_power), 0.0)
        )

    completed = [
        sum(sum(item) / len(item) for item in slots.values()) / 3
        for slots in quarters.values()
        if len(slots) == 3
    ]
    return max(completed, default=0.0)
