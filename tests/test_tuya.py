import hashlib
import hmac
import json
from types import SimpleNamespace

from tesla_energy_controller.models import ChargeState, GridMeasurement
from tesla_energy_controller.storage import EnergyDatabase
from tesla_energy_controller.tuya import (
    MqttMessage,
    TuyaEnergyMeterBridge,
    TuyaLinkConfig,
    build_property_report,
    build_property_response,
    build_tuya_auth,
    encode_faults,
)


def test_tuya_auth_uses_open_protocol_hmac_shape():
    config = TuyaLinkConfig(
        host="m1.tuyacn.com",
        port=8883,
        device_id="device123",
        device_secret="secret123",
    )

    auth = build_tuya_auth(config, timestamp=1_720_000_000)

    content = "deviceId=device123,timestamp=1720000000,secureMode=1,accessType=1"
    assert auth.client_id == "tuyalink_device123"
    assert auth.username == (
        "device123|signMethod=hmacSha256,timestamp=1720000000,"
        "secureMode=1,accessType=1"
    )
    assert auth.password == hmac.new(
        b"secret123",
        content.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def test_build_property_report_wraps_each_dp_with_value_and_time():
    payload = build_property_report(
        {"meter_switch": True, "solar_power_w": 1234},
        msg_id="abc",
        time_ms=123456789,
    )

    assert payload == {
        "msgId": "abc",
        "time": 123456789,
        "data": {
            "meter_switch": {"value": True, "time": 123456789},
            "solar_power_w": {"value": 1234, "time": 123456789},
        },
    }


def test_build_property_get_response_uses_same_wrapped_data_shape():
    payload = build_property_response(
        {"meter_switch": True},
        msg_id="get-1",
        time_ms=123456789,
    )

    assert payload == {
        "msgId": "get-1",
        "time": 123456789,
        "code": 0,
        "data": {
            "meter_switch": {"value": True, "time": 123456789},
        },
    }


def test_encode_faults_uses_tuya_fault_bitmap():
    assert encode_faults(set()) == 0
    assert encode_faults({"solar_error", "tesla_error"}) == 5
    assert encode_faults({"controller_disabled"}) == 8


def test_meter_switch_set_disables_control_but_keeps_meter_values():
    class Grid:
        def read(self):
            return GridMeasurement(total_power_w=0, solar_power_w=1800)

    class Vehicle:
        def get_charge_state(self):
            return ChargeState("Disconnected", 0, 16, 0, 3, 230)

    class Client:
        property_set_topic = "tylink/device123/thing/property/set"

        def __init__(self):
            self.responses = []
            self.reports = []

        def respond_property_set(self, msg_id, code=0):
            self.responses.append((msg_id, code))

        def report_properties(self, properties):
            self.reports.append(properties)

    switched = []
    settings = SimpleNamespace(energy_source="mock", tuya_average_samples=1)
    controller = SimpleNamespace(grid=Grid(), vehicle=Vehicle())
    bridge = TuyaEnergyMeterBridge(
        settings=settings,
        controller=controller,
        on_switch=switched.append,
    )
    client = Client()
    message = MqttMessage(
        client.property_set_topic,
        json.dumps({"msgId": "set-1", "data": {"meter_switch": False}}).encode(),
    )

    bridge.handle_property_set(client, message)

    assert bridge.meter_enabled is False
    assert switched == [False]
    assert client.responses == [("set-1", 0)]
    assert client.reports[-1] == {
        "meter_switch": False,
        "solar_power_w": 1800,
        "house_consumption_w": 0,
        "tesla_power_w": 0,
        "total_consumption_w": 0,
        "tesla_state": "disconnected",
        "meter_fault": 0,
    }


def test_property_get_returns_requested_values_only():
    class Client:
        property_set_topic = "tylink/device123/thing/property/set"
        property_get_topic = "tylink/device123/thing/property/get"

        def __init__(self):
            self.responses = []

        def respond_property_get(self, msg_id, properties):
            self.responses.append((msg_id, properties))

    settings = SimpleNamespace(energy_source="mock", tuya_average_samples=1)
    bridge = TuyaEnergyMeterBridge(settings=settings, controller=None, meter_enabled=False)
    client = Client()
    message = MqttMessage(
        client.property_get_topic,
        json.dumps({"msgId": "get-1", "data": ["meter_switch", "tesla_state"]}).encode(),
    )

    bridge.handle_property_get(client, message)

    assert client.responses == [
        ("get-1", {"meter_switch": False, "tesla_state": "disconnected"})
    ]


def test_property_get_also_reports_full_current_values():
    class Client:
        property_get_topic = "tylink/device123/thing/property/get"

        def __init__(self):
            self.responses = []
            self.reports = []

        def respond_property_get(self, msg_id, properties):
            self.responses.append((msg_id, properties))

        def report_properties(self, properties):
            self.reports.append(properties)

    settings = SimpleNamespace(energy_source="mock", tuya_average_samples=1)
    bridge = TuyaEnergyMeterBridge(settings=settings, controller=None, meter_enabled=True)
    client = Client()
    message = MqttMessage(
        client.property_get_topic,
        json.dumps({"msgId": "get-1", "data": ["meter_switch"]}).encode(),
    )

    bridge.handle_property_get(client, message)

    assert client.responses == [("get-1", {"meter_switch": True})]
    assert client.reports[-1]["meter_switch"] is True
    assert "tesla_state" in client.reports[-1]


def test_properties_are_averaged_over_configured_samples():
    class Grid:
        def __init__(self):
            self.measurements = [
                GridMeasurement(total_power_w=0, solar_power_w=1000),
                GridMeasurement(total_power_w=0, solar_power_w=3000),
            ]

        def read(self):
            return self.measurements.pop(0)

    class Vehicle:
        def __init__(self):
            self.states = [
                ChargeState("Charging", 6, 16, 0, 3, 230, charger_power_kw=1.0),
                ChargeState("Charging", 6, 16, 0, 3, 230, charger_power_kw=3.0),
            ]

        def get_charge_state(self):
            return self.states.pop(0)

    settings = SimpleNamespace(energy_source="mock", tuya_average_samples=2)
    controller = SimpleNamespace(grid=Grid(), vehicle=Vehicle())
    bridge = TuyaEnergyMeterBridge(settings=settings, controller=controller)

    first = bridge.properties()
    second = bridge.properties()

    assert first["solar_power_w"] == 1000
    assert first["tesla_power_w"] == 1000
    assert first["total_consumption_w"] == 1000
    assert second["solar_power_w"] == 2000
    assert second["tesla_power_w"] == 2000
    assert second["total_consumption_w"] == 2000


def test_tuya_uses_latest_sqlite_measurement_without_live_polling(tmp_path):
    class Grid:
        def read(self):
            raise AssertionError("Tuya deve usare SQLite quando ci sono misure salvate")

    class Vehicle:
        def get_charge_state(self):
            raise AssertionError("Tuya non deve leggere Tesla per un refresh da app")

    database_file = tmp_path / "energy.sqlite3"
    database = EnergyDatabase(str(database_file))
    database.add_measurement(
        {
            "observed_at": "2026-06-25T15:00:00+02:00",
            "solar_power_w": 1000,
            "vimar_power_w": 500,
            "tesla_power_w": 4000,
            "total_consumption_w": 4500,
            "import_power_w": 0,
            "export_power_w": 500,
            "controller_enabled": True,
        },
        [],
    )
    database.add_measurement(
        {
            "observed_at": "2026-06-25T15:05:00+02:00",
            "solar_power_w": 2500,
            "vimar_power_w": 700,
            "tesla_power_w": 4500,
            "total_consumption_w": 5200,
            "import_power_w": 0,
            "export_power_w": 1800,
            "controller_enabled": False,
        },
        [],
    )

    settings = SimpleNamespace(
        energy_source="mock",
        energy_database_file=str(database_file),
        tuya_average_samples=3,
        tuya_report_tesla=False,
    )
    controller = SimpleNamespace(grid=Grid(), vehicle=Vehicle())
    bridge = TuyaEnergyMeterBridge(settings=settings, controller=controller, meter_enabled=False)

    properties = bridge.properties()

    assert properties["meter_switch"] is False
    assert properties["solar_power_w"] == 2500
    assert properties["house_consumption_w"] == 700
    assert properties["tesla_power_w"] == 4500
    assert properties["total_consumption_w"] == 5200
    assert properties["tesla_state"] == "charging"
    assert properties["meter_fault"] == 0


def test_tuya_sqlite_wall_connector_standby_is_idle(tmp_path):
    database_file = tmp_path / "energy.sqlite3"
    database = EnergyDatabase(str(database_file))
    database.add_measurement(
        {
            "observed_at": "2026-07-01T22:12:11+02:00",
            "solar_power_w": 0,
            "vimar_power_w": 712,
            "tesla_power_w": 105,
            "total_consumption_w": 1059,
            "import_power_w": 1059,
            "export_power_w": 0,
            "tesla_current_a": 0.4,
            "controller_enabled": True,
            "action": "outside-window",
            "reason": "Fuori dalla finestra solare 05:24-21:04",
        },
        [],
    )

    settings = SimpleNamespace(
        energy_source="mock",
        energy_database_file=str(database_file),
        tuya_average_samples=1,
        tuya_report_tesla=False,
    )
    bridge = TuyaEnergyMeterBridge(settings=settings, controller=None)

    properties = bridge.properties()

    assert properties["house_consumption_w"] == 954
    assert properties["tesla_power_w"] == 105
    assert properties["total_consumption_w"] == 1059
    assert properties["tesla_state"] == "idle"


def test_tuya_can_report_meter_values_without_reading_tesla():
    class Grid:
        def read(self):
            return GridMeasurement(total_power_w=0, solar_power_w=4200)

    class Vehicle:
        def get_charge_state(self):
            raise AssertionError("Tesla non deve essere letta quando TUYA_REPORT_TESLA=false")

    settings = SimpleNamespace(
        energy_source="mock",
        tuya_average_samples=1,
        tuya_report_tesla=False,
    )
    controller = SimpleNamespace(grid=Grid(), vehicle=Vehicle())
    bridge = TuyaEnergyMeterBridge(settings=settings, controller=controller)

    properties = bridge.properties()

    assert properties["solar_power_w"] == 4200
    assert properties["tesla_power_w"] == 0
    assert properties["total_consumption_w"] == 0
    assert properties["tesla_state"] == "disconnected"
    assert properties["meter_fault"] == 0
