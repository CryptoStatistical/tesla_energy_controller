from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EnergyBreakdown:
    solar_power_w: float
    appliances_power_w: float
    device_power_w: float
    house_power_w: float
    tesla_power_w: float
    total_consumption_w: float
    import_power_w: float
    export_power_w: float
    estimated_import_power_w: float
    estimated_export_power_w: float
    meter_balance_available: bool


def reconcile_energy_flows(
    *,
    solar_power_w: float | None,
    appliances_power_w: float,
    tesla_power_w: float,
    import_power_w: float | None,
    export_power_w: float | None,
) -> EnergyBreakdown:
    """Ricostruisce i flussi usando il contatore reale quando disponibile."""

    solar = max(float(solar_power_w or 0.0), 0.0)
    appliances = max(float(appliances_power_w), 0.0)
    tesla = max(float(tesla_power_w), 0.0)

    meter_available = import_power_w is not None and export_power_w is not None

    if meter_available:
        imported = max(float(import_power_w), 0.0)
        exported = max(float(export_power_w), 0.0)
        known_load_w = appliances + tesla
        minimum_solar_w = max(known_load_w - imported + exported, 0.0)
        solar = max(solar, minimum_solar_w)
        total = max(solar + imported - exported, known_load_w, 0.0)
        house = max(total - tesla, appliances, 0.0)
        device = max(house - appliances, 0.0)
    else:
        estimated_grid_w = appliances + tesla - solar
        estimated_import = max(estimated_grid_w, 0.0)
        estimated_export = max(-estimated_grid_w, 0.0)
        imported = estimated_import
        exported = estimated_export
        device = 0.0
        house = appliances
        total = house + tesla

    estimated_grid_w = appliances + tesla - solar
    estimated_import = max(estimated_grid_w, 0.0)
    estimated_export = max(-estimated_grid_w, 0.0)

    return EnergyBreakdown(
        solar_power_w=solar,
        appliances_power_w=appliances,
        device_power_w=device,
        house_power_w=house,
        tesla_power_w=tesla,
        total_consumption_w=total,
        import_power_w=imported,
        export_power_w=exported,
        estimated_import_power_w=estimated_import,
        estimated_export_power_w=estimated_export,
        meter_balance_available=meter_available,
    )
