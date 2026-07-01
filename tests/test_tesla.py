import json
import subprocess
from unittest.mock import patch

import pytest

from tesla_energy_controller.tesla import (
    BLEErrorCategory,
    TeslaBLEClient,
    TeslaBLEError,
    TeslaFleetClient,
    WallConnectorClient,
)


def test_parse_tesla_charge_state():
    state = TeslaFleetClient.parse_charge_state(
        {
            "response": {
                "charge_state": {
                    "charging_state": "Charging",
                    "charge_current_request": 8,
                    "charge_current_request_max": 16,
                    "charger_actual_current": 8,
                    "charger_phases": 3,
                    "charger_voltage": 231,
                    "charger_power": 5,
                }
            }
        }
    )
    assert state.is_charging
    assert state.phases == 3
    assert state.current_request_a == 8


def test_parse_tesla_ble_charge_state():
    state = TeslaBLEClient.parse_charge_state(
        {
            "chargeState": {
                "chargingState": {"charging": {}},
                "chargeCurrentRequest": 8,
                "chargeCurrentRequestMax": 16,
                "chargerActualCurrent": 8,
                "chargerPhases": 3,
                "chargerVoltage": 231,
                "chargerPower": 5,
            }
        }
    )
    assert state.is_charging
    assert state.phases == 3
    assert state.voltage_v == 231
    assert state.current_request_a == 8
    assert state.charging_power_w == 5000


def test_ble_uses_charger_power_when_plugged_but_not_charging():
    state = TeslaBLEClient.parse_charge_state(
        {
            "chargeState": {
                "chargingState": {"complete": {}},
                "chargerActualCurrent": 0,
                "chargerPhases": 3,
                "chargerVoltage": 231,
                "chargerPower": 1,
            }
        }
    )
    assert not state.is_charging
    assert state.charging_power_w == 1000


def test_wall_connector_ignores_idle_sensor_noise_when_disconnected():
    vitals = WallConnectorClient.parse_vitals(
        {
            "vehicle_connected": False,
            "contactor_closed": False,
            "grid_v": 236.3,
            "vehicle_current_a": 0.4,
            "currentA_a": 0.4,
            "currentB_a": 0.0,
            "currentC_a": 0.2,
            "evse_state": 1,
        }
    )
    assert vitals.power_w == 0
    assert vitals.vehicle_connected is False


def test_wall_connector_calculates_three_phase_power_when_connected():
    vitals = WallConnectorClient.parse_vitals(
        {
            "vehicle_connected": True,
            "contactor_closed": True,
            "grid_v": 232.0,
            "vehicle_current_a": 6.0,
            "currentA_a": 6.0,
            "currentB_a": 6.0,
            "currentC_a": 6.0,
        }
    )
    assert vitals.power_w == 4176


def test_tesla_ble_infers_phases_when_field_is_missing():
    state = TeslaBLEClient.parse_charge_state(
        {
            "chargeState": {
                "chargingState": {"charging": {}},
                "chargeCurrentRequest": 8,
                "chargerActualCurrent": 8,
                "chargerVoltage": 230,
                "chargerPower": 5,
            }
        }
    )
    assert state.phases == 3


def test_tesla_ble_infers_phases_when_reported_value_is_not_supported():
    state = TeslaBLEClient.parse_charge_state(
        {
            "chargeState": {
                "chargingState": {"Charging": {}},
                "chargeCurrentRequest": 6,
                "chargerActualCurrent": 6,
                "chargerVoltage": 242,
                "chargerPower": 4,
                "chargerPhases": 2,
            }
        }
    )
    assert state.phases == 3


def ble_client(**kwargs):
    return TeslaBLEClient(
        "VIN123",
        "/tmp/private-key.pem",
        cache_file="/tmp/session-cache.json",
        require_time_sync=False,
        retry_backoff_seconds=0,
        **kwargs,
    )


def completed(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")


def test_sleep_preflight_does_not_query_detailed_state():
    client = ble_client()
    payload = {"vehicleStatus": {"vehicleSleepStatus": "VEHICLE_SLEEP_STATUS_ASLEEP"}}
    with patch("tesla_energy_controller.tesla.subprocess.run", return_value=completed(payload)) as run:
        state = client.get_charge_state()
    assert state.charging_state == "Asleep"
    assert run.call_count == 1
    assert run.call_args.args[0][-1] == "body-controller-state"


def test_awake_preflight_then_reads_charge_state():
    client = ble_client()
    awake = {"vehicleStatus": {"vehicleSleepStatus": "VEHICLE_SLEEP_STATUS_AWAKE"}}
    charge = {"chargeState": {"chargingState": {"charging": {}}, "chargerVoltage": 230}}
    with patch(
        "tesla_energy_controller.tesla.subprocess.run",
        side_effect=[completed(awake), completed(charge)],
    ) as run:
        state = client.get_charge_state()
    assert state.is_charging
    assert run.call_count == 2


def test_ble_start_and_stop_use_official_commands():
    client = ble_client()
    with patch(
        "tesla_energy_controller.tesla.subprocess.run",
        side_effect=[completed({}), completed({})],
    ) as run:
        client.stop_charging()
        client.start_charging()

    assert run.call_args_list[0].args[0][-1] == "charging-stop"
    assert run.call_args_list[1].args[0][-1] == "charging-start"


def test_retryable_beacon_error_is_retried():
    client = ble_client(retries=1)
    failure = subprocess.CalledProcessError(
        1, ["tesla-control"], stderr="failed to find BLE beacon: context deadline exceeded"
    )
    with (
        patch("tesla_energy_controller.tesla.subprocess.run", side_effect=[failure, completed({})]),
        patch("tesla_energy_controller.tesla.time.sleep") as sleep,
        patch("tesla_energy_controller.tesla.random.uniform", return_value=0),
    ):
        assert client._run("body-controller-state") == "{}"
    sleep.assert_called_once_with(0)


def test_auth_error_is_not_retried():
    client = ble_client(retries=3)
    failure = subprocess.CalledProcessError(1, ["tesla-control"], stderr="unauthorized key")
    with patch("tesla_energy_controller.tesla.subprocess.run", side_effect=failure) as run:
        with pytest.raises(TeslaBLEError) as captured:
            client._run("state", "charge")
    assert captured.value.category is BLEErrorCategory.AUTH
    assert run.call_count == 1


def test_unsynchronized_clock_fails_before_spawning_process():
    client = TeslaBLEClient(
        "VIN123", "/tmp/key.pem", clock_checker=lambda: False, require_time_sync=True
    )
    with patch("tesla_energy_controller.tesla.subprocess.run") as run:
        with pytest.raises(TeslaBLEError) as captured:
            client._run("state", "charge")
    assert captured.value.category is BLEErrorCategory.CLOCK
    assert not run.called


def test_command_contains_adapter_timeouts_and_cache():
    client = ble_client(
        bt_adapter="hci0", connect_timeout_seconds=25, command_timeout_seconds=12
    )
    assert client._command("charging-set-amps", "8") == [
        "tesla-control",
        "-ble",
        "-vin",
        "VIN123",
        "-key-file",
        "/tmp/private-key.pem",
        "-connect-timeout",
        "25s",
        "-command-timeout",
        "12s",
        "-bt-adapter",
        "hci0",
        "-session-cache",
        "/tmp/session-cache.json",
        "charging-set-amps",
        "8",
    ]
