from __future__ import annotations

from .config import ConfigurationError, Settings
from .controller import EnergyController
from .solar import (
    AlfaModbusSource,
    MockGridSource,
    SolarEdgeCloudSource,
    SolarEdgeModbusSource,
    SolarEdgeWebSource,
)
from .tesla import MockTeslaClient, TeslaBLEClient, TeslaFleetClient, TeslaTokenStore


def build_grid_source(
    settings: Settings,
    source: str | None = None,
    *,
    expected_phases: int | None = None,
):
    selected = (source or settings.energy_source).strip().casefold()
    phase_count = settings.expected_phases if expected_phases is None else expected_phases
    if selected == "mock":
        return MockGridSource(
            settings.mock_grid_power_w,
            solar_power_w=settings.mock_solar_power_w,
        )
    if selected == "alfa-modbus":
        return AlfaModbusSource(
            settings.alfa_modbus_host or "",
            settings.alfa_modbus_port,
            settings.alfa_modbus_unit,
            settings.alfa_modbus_timeout_seconds,
        )
    if selected == "solaredge-web":
        return SolarEdgeWebSource(
            settings.solaredge_username or "",
            settings.solaredge_password or "",
            settings.solaredge_site_id or 0,
            session_file=settings.solaredge_session_file,
            minimum_interval_seconds=settings.cloud_poll_interval_seconds,
        )
    if selected == "solaredge-cloud":
        return SolarEdgeCloudSource(
            settings.solaredge_api_key or "",
            settings.solaredge_site_id or 0,
            settings.cloud_poll_interval_seconds,
        )
    if selected == "solaredge-modbus":
        return SolarEdgeModbusSource(
            settings.solaredge_modbus_host or "",
            settings.solaredge_modbus_port,
            settings.solaredge_modbus_unit,
            settings.solaredge_meter_base,
            phase_count,
            settings.solaredge_grid_power_sign,
            settings.solaredge_inverter_base,
        )
    raise ConfigurationError(f"Sorgente fotovoltaico non supportata: {selected}")


def build_vehicle_client(settings: Settings):
    if settings.tesla_transport == "mock":
        return MockTeslaClient(
            settings.mock_tesla_current_a,
            phases=settings.expected_phases,
            voltage_v=settings.nominal_phase_voltage_v,
        )
    if settings.tesla_transport == "ble":
        return TeslaBLEClient(
            settings.tesla_vin or "",
            settings.tesla_ble_key_file,
            binary=settings.tesla_control_binary,
            cache_file=settings.tesla_ble_cache_file,
            timeout_seconds=settings.tesla_ble_timeout_seconds,
            connect_timeout_seconds=settings.tesla_ble_connect_timeout_seconds,
            command_timeout_seconds=settings.tesla_ble_command_timeout_seconds,
            retries=settings.tesla_ble_retries,
            retry_backoff_seconds=settings.tesla_ble_retry_backoff_seconds,
            bt_adapter=settings.tesla_ble_adapter,
            require_time_sync=settings.tesla_ble_require_time_sync,
            preflight_sleep_check=settings.tesla_ble_preflight_sleep_check,
            recovery_enabled=settings.tesla_ble_recovery_enabled,
            recovery_threshold=settings.tesla_ble_recovery_threshold,
        )
    tokens = TeslaTokenStore(
        settings.tesla_token_file,
        settings.tesla_client_id or "",
        settings.tesla_token_url,
        settings.tesla_access_token,
        settings.tesla_refresh_token,
    )
    return TeslaFleetClient(
        settings.tesla_vin or "",
        settings.tesla_api_base_url,
        tokens,
        settings.tesla_ca_cert,
        settings.tesla_verify_ssl,
    )


def build_controller(settings: Settings) -> EnergyController:
    return EnergyController(
        build_grid_source(settings),
        build_vehicle_client(settings),
        dry_run=settings.mode == "dry-run",
        control_mode=settings.control_mode,
        expected_phases=settings.expected_phases,
        nominal_phase_voltage_v=settings.nominal_phase_voltage_v,
        min_voltage_v=settings.min_voltage_v,
        max_voltage_v=settings.max_voltage_v,
        solar_utilization_percent=settings.solar_utilization_percent,
        target_grid_import_w=settings.target_grid_import_w,
        max_grid_current_a=settings.max_grid_current_a,
        min_charge_amps=settings.min_charge_amps,
        max_charge_amps=settings.max_charge_amps,
        command_hysteresis_a=settings.command_hysteresis_a,
        max_ramp_up_a=settings.max_ramp_up_a,
        grid_import_limit_w=settings.grid_import_limit_w,
        grid_import_emergency_w=settings.grid_import_emergency_w,
        grid_hold_band_w=settings.grid_hold_band_w,
        grid_surplus_stable_reads=settings.grid_surplus_stable_reads,
    )
