from __future__ import annotations

import hashlib
import hmac
import json
import logging
import socket
import ssl
import struct
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from .config import ConfigurationError, Settings
from .controller import EnergyController
from .energy import reconcile_energy_flows
from .live_status import read_status_cache
from .storage import EnergyDatabase

LOG = logging.getLogger("tesla_energy_controller.tuya")

FAULT_BITS = {
    "solar_error": 1,
    "vimar_error": 2,
    "tesla_error": 4,
    "controller_disabled": 8,
}


@dataclass(frozen=True)
class TuyaLinkConfig:
    host: str
    port: int
    device_id: str
    device_secret: str
    keepalive_seconds: int = 60

    @classmethod
    def from_settings(cls, settings: Settings) -> "TuyaLinkConfig":
        if not settings.tuya_device_id or not settings.tuya_device_secret:
            raise ConfigurationError(
                "Per TuyaLink servono TUYA_DEVICE_ID e TUYA_DEVICE_SECRET(_FILE)"
            )
        return cls(
            host=settings.tuya_mqtt_host,
            port=settings.tuya_mqtt_port,
            device_id=settings.tuya_device_id,
            device_secret=settings.tuya_device_secret,
            keepalive_seconds=settings.tuya_keepalive_seconds,
        )


@dataclass(frozen=True)
class TuyaAuth:
    client_id: str
    username: str
    password: str


@dataclass(frozen=True)
class MqttMessage:
    topic: str
    payload: bytes


def build_tuya_auth(config: TuyaLinkConfig, timestamp: int | None = None) -> TuyaAuth:
    stamp = str(timestamp if timestamp is not None else int(time.time()))
    secure_mode = "1"
    access_type = "1"
    client_id = f"tuyalink_{config.device_id}"
    username = (
        f"{config.device_id}|signMethod=hmacSha256,timestamp={stamp},"
        f"secureMode={secure_mode},accessType={access_type}"
    )
    content = (
        f"deviceId={config.device_id},timestamp={stamp},"
        f"secureMode={secure_mode},accessType={access_type}"
    )
    password = hmac.new(
        config.device_secret.encode("utf-8"),
        content.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return TuyaAuth(client_id, username, password)


def build_property_report(
    properties: dict[str, Any],
    *,
    msg_id: str | None = None,
    time_ms: int | None = None,
    ack: bool = False,
) -> dict[str, Any]:
    stamp = time_ms if time_ms is not None else int(time.time() * 1000)
    payload: dict[str, Any] = {
        "msgId": msg_id or uuid.uuid4().hex,
        "time": stamp,
        "data": {
            identifier: {"value": value, "time": stamp}
            for identifier, value in properties.items()
        },
    }
    if ack:
        payload["sys"] = {"ack": 1}
    return payload


def build_property_response(
    properties: dict[str, Any],
    *,
    msg_id: str,
    time_ms: int | None = None,
    code: int = 0,
) -> dict[str, Any]:
    stamp = time_ms if time_ms is not None else int(time.time() * 1000)
    return {
        "msgId": msg_id,
        "time": stamp,
        "code": code,
        "data": {
            identifier: {"value": value, "time": stamp}
            for identifier, value in properties.items()
        },
    }


def encode_faults(names: set[str]) -> int:
    value = 0
    for name in names:
        value |= FAULT_BITS.get(name, 0)
    return value


def _mqtt_string(value: str | bytes) -> bytes:
    data = value if isinstance(value, bytes) else value.encode("utf-8")
    return struct.pack("!H", len(data)) + data


def _remaining_length(value: int) -> bytes:
    output = bytearray()
    while True:
        encoded = value % 128
        value //= 128
        if value:
            encoded |= 128
        output.append(encoded)
        if not value:
            return bytes(output)


def _packet(packet_type: int, payload: bytes = b"") -> bytes:
    return bytes([packet_type]) + _remaining_length(len(payload)) + payload


class TuyaLinkMqttClient:
    def __init__(self, config: TuyaLinkConfig) -> None:
        self.config = config
        self._sock: ssl.SSLSocket | None = None
        self._packet_id = 0
        self._last_sent = 0.0

    @property
    def property_set_topic(self) -> str:
        return f"tylink/{self.config.device_id}/thing/property/set"

    @property
    def property_set_response_topic(self) -> str:
        return f"tylink/{self.config.device_id}/thing/property/set_response"

    @property
    def property_get_topic(self) -> str:
        return f"tylink/{self.config.device_id}/thing/property/get"

    @property
    def property_get_response_topic(self) -> str:
        return f"tylink/{self.config.device_id}/thing/property/get_response"

    @property
    def property_report_topic(self) -> str:
        return f"tylink/{self.config.device_id}/thing/property/report"

    @property
    def property_report_response_topic(self) -> str:
        return f"tylink/{self.config.device_id}/thing/property/report_response"

    def connect(self) -> None:
        auth = build_tuya_auth(self.config)
        raw = socket.create_connection((self.config.host, self.config.port), timeout=15)
        context = ssl.create_default_context()
        sock = context.wrap_socket(raw, server_hostname=self.config.host)
        sock.settimeout(15)
        self._sock = sock
        variable_header = (
            _mqtt_string("MQTT")
            + bytes([4])
            + bytes([0xC2])
            + struct.pack("!H", self.config.keepalive_seconds)
        )
        payload = (
            _mqtt_string(auth.client_id)
            + _mqtt_string(auth.username)
            + _mqtt_string(auth.password)
        )
        self._send(_packet(0x10, variable_header + payload))
        packet_type, body = self._read_packet()
        if packet_type != 0x20 or len(body) < 2:
            raise RuntimeError(f"Risposta MQTT inattesa type=0x{packet_type:02x}")
        if body[1] != 0:
            raise RuntimeError(f"Connessione TuyaLink rifiutata, CONNACK={body[1]}")
        sock.settimeout(1)
        LOG.info("tuya_connected host=%s port=%s", self.config.host, self.config.port)

    def disconnect(self) -> None:
        sock = self._sock
        if sock is None:
            return
        try:
            self._send(_packet(0xE0))
        except OSError:
            pass
        try:
            sock.close()
        finally:
            self._sock = None

    def subscribe_property_set(self) -> None:
        self.subscribe_topics([self.property_set_topic])

    def subscribe_control_topics(self) -> None:
        self.subscribe_topics(
            [
                self.property_set_topic,
                self.property_get_topic,
                self.property_report_response_topic,
            ]
        )

    def subscribe_topics(self, topics: list[str]) -> None:
        sock = self._require_socket()
        previous_timeout = sock.gettimeout()
        packet_id = self._next_packet_id()
        payload = struct.pack("!H", packet_id)
        for topic in topics:
            payload += _mqtt_string(topic) + b"\x00"
        sock.settimeout(15)
        try:
            self._send(_packet(0x82, payload))
            while True:
                packet_type, body = self._read_packet()
                message_type = packet_type >> 4
                if message_type == 9 and len(body) >= 3 and body[:2] == struct.pack("!H", packet_id):
                    return_codes = body[2:]
                    if any(code == 0x80 for code in return_codes):
                        raise RuntimeError("TuyaLink ha rifiutato una subscription")
                    return
                if message_type == 3:
                    LOG.debug("messaggio Tuya ignorato durante subscribe")
        finally:
            sock.settimeout(previous_timeout)

    def publish_json(self, topic: str, payload: dict[str, Any], *, qos: int = 0) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if qos == 0:
            self._send(_packet(0x30, _mqtt_string(topic) + body))
            return
        if qos == 1:
            packet_id = self._next_packet_id()
            self._send(_packet(0x32, _mqtt_string(topic) + struct.pack("!H", packet_id) + body))
            return
        raise ValueError("TuyaLink supporta solo QoS 0 o 1")

    def report_properties(self, properties: dict[str, Any]) -> None:
        self.publish_json(
            self.property_report_topic,
            build_property_report(properties, ack=True),
            qos=1,
        )

    def respond_property_set(self, msg_id: str, code: int = 0) -> None:
        self.publish_json(
            self.property_set_response_topic,
            {"msgId": msg_id, "time": int(time.time() * 1000), "code": code},
        )

    def respond_property_get(self, msg_id: str, properties: dict[str, Any]) -> None:
        self.publish_json(
            self.property_get_response_topic,
            build_property_response(properties, msg_id=msg_id),
        )

    def loop_once(self, timeout: float = 1.0) -> MqttMessage | None:
        sock = self._require_socket()
        sock.settimeout(timeout)
        try:
            packet_type, body = self._read_packet()
        except (socket.timeout, TimeoutError):
            return None
        message_type = packet_type >> 4
        if message_type == 3:
            topic_length = struct.unpack("!H", body[:2])[0]
            topic_start = 2
            topic_end = topic_start + topic_length
            topic = body[topic_start:topic_end].decode("utf-8")
            payload_start = topic_end
            qos = (packet_type >> 1) & 0x03
            if qos == 1:
                packet_id = struct.unpack("!H", body[payload_start : payload_start + 2])[0]
                payload_start += 2
                self._send(_packet(0x40, struct.pack("!H", packet_id)))
            elif qos > 1:
                raise RuntimeError(f"QoS MQTT non supportato: {qos}")
            return MqttMessage(topic, body[payload_start:])
        if message_type == 13:
            return None
        if message_type in {4, 9}:
            return None
        LOG.debug("pacchetto MQTT ignorato type=%s", message_type)
        return None

    def ping_if_needed(self) -> None:
        elapsed = time.monotonic() - self._last_sent
        interval = max(10, self.config.keepalive_seconds // 2)
        if elapsed >= interval:
            self._send(_packet(0xC0))

    def _next_packet_id(self) -> int:
        self._packet_id = self._packet_id % 65535 + 1
        return self._packet_id

    def _send(self, data: bytes) -> None:
        self._require_socket().sendall(data)
        self._last_sent = time.monotonic()

    def _read_packet(self) -> tuple[int, bytes]:
        first = self._read_exact(1)
        multiplier = 1
        length = 0
        while True:
            digit = self._read_exact(1)[0]
            length += (digit & 127) * multiplier
            if digit & 128 == 0:
                break
            multiplier *= 128
            if multiplier > 128 * 128 * 128:
                raise RuntimeError("Remaining length MQTT non valido")
        return first[0], self._read_exact(length)

    def _read_exact(self, length: int) -> bytes:
        sock = self._require_socket()
        chunks = bytearray()
        while len(chunks) < length:
            chunk = sock.recv(length - len(chunks))
            if not chunk:
                raise RuntimeError("Connessione MQTT chiusa dal broker")
            chunks.extend(chunk)
        return bytes(chunks)

    def _require_socket(self) -> ssl.SSLSocket:
        if self._sock is None:
            raise RuntimeError("Client TuyaLink non connesso")
        return self._sock


class TuyaEnergyMeterBridge:
    def __init__(
        self,
        settings: Settings,
        controller: EnergyController,
        *,
        on_switch: Callable[[bool], None] | None = None,
        meter_enabled: bool = True,
    ) -> None:
        self.settings = settings
        self.controller = controller
        self.on_switch = on_switch
        self.meter_enabled = meter_enabled
        database_file = getattr(settings, "energy_database_file", None) if settings else None
        self.database = EnergyDatabase(database_file) if database_file else None
        sample_count = getattr(settings, "tuya_average_samples", 1) if settings else 1
        self._samples: deque[dict[str, Any]] = deque(maxlen=max(1, int(sample_count)))

    def properties(self) -> dict[str, Any]:
        status_properties = self._read_status_properties()
        if status_properties is not None:
            self._samples.clear()
            return status_properties

        database_properties = self._read_database_properties()
        if database_properties is not None:
            self._samples.clear()
            return database_properties

        latest = self._read_properties()
        self._samples.append(latest)
        return self._average_properties(latest)

    def _read_status_properties(self) -> dict[str, Any] | None:
        if self.settings is None:
            return None
        max_age = max(90, int(getattr(self.settings, "tuya_report_interval_seconds", 30)) * 3)
        status = read_status_cache(self.settings, max_age_seconds=max_age)
        if status is None:
            return None

        solar_power_w = max(self._number(status.get("solar_power_w")) or 0.0, 0.0)
        tesla_power_w = max(self._number(status.get("tesla_power_w")) or 0.0, 0.0)
        total_consumption_w = max(
            self._number(status.get("total_consumption_w")) or 0.0,
            0.0,
        )
        house_power_w = self._number(status.get("house_power_w"))
        wall_disconnected = (
            status.get("wall_connector_vehicle_connected") is False
            and status.get("wall_connector_contactor_closed") is False
        )
        if wall_disconnected:
            tesla_power_w = 0.0
            if house_power_w is not None:
                total_consumption_w = max(house_power_w, 0.0)
        if house_power_w is None:
            house_power_w = max(total_consumption_w - tesla_power_w, 0.0)
        total_consumption_w = max(total_consumption_w, house_power_w + tesla_power_w)
        tesla_state = self._tuya_cached_tesla_state(status, tesla_power_w)

        return {
            "meter_switch": self.meter_enabled,
            "solar_power_w": round(solar_power_w),
            "house_consumption_w": round(max(house_power_w, 0.0)),
            "tesla_power_w": round(tesla_power_w),
            "total_consumption_w": round(total_consumption_w),
            "tesla_state": tesla_state,
            "meter_fault": 0,
        }

    def _read_database_properties(self) -> dict[str, Any] | None:
        if self.database is None:
            return None
        try:
            rows = self.database.latest_measurements(1)
        except Exception:
            LOG.exception("lettura SQLite per Tuya fallita")
            return None
        if not rows:
            return None

        latest = rows[0]
        solar_power_w = max(float(latest.get("solar_power_w") or 0.0), 0.0)
        tesla_power_w = max(float(latest.get("tesla_power_w") or 0.0), 0.0)
        house_consumption_w = max(
            float(latest.get("total_consumption_w") or 0.0) - tesla_power_w,
            0.0,
        )
        tesla_state = self._tuya_cached_tesla_state(latest, tesla_power_w)

        return {
            "meter_switch": self.meter_enabled,
            "solar_power_w": round(solar_power_w),
            "house_consumption_w": round(house_consumption_w),
            "tesla_power_w": round(tesla_power_w),
            "total_consumption_w": round(house_consumption_w + tesla_power_w),
            "tesla_state": tesla_state,
            "meter_fault": 0,
        }

    def _read_properties(self) -> dict[str, Any]:
        faults: set[str] = set()
        solar_power_w = 0.0
        appliances_power_w = 0.0
        tesla_power_w = 0.0
        tesla_state = "idle"
        measurement = None

        if self.controller is not None:
            try:
                measurement = self.controller.grid.read()
                solar_power_w = float(measurement.solar_power_w or 0.0)
            except Exception:
                LOG.exception("lettura SolarEdge per Tuya fallita")
                faults.add("solar_error")

        if self.settings and self.settings.energy_source != "mock":
            try:
                from .vimar import read_energy_points_from_settings

                points = read_energy_points_from_settings(self.settings)
                appliances_power_w = sum(
                    max(float(point.power_w or 0.0), 0.0)
                    for point in points
                    if point.power_w is not None
                )
            except Exception:
                LOG.exception("lettura Vimar per Tuya fallita")
                faults.add("vimar_error")

        if self.controller is not None and getattr(self.settings, "tuya_report_tesla", True):
            try:
                car = self.controller.vehicle.get_charge_state()
                tesla_state = self._tuya_tesla_state(car.charging_state)
                if car.is_charging:
                    tesla_power_w = car.charging_power_w
            except Exception:
                LOG.exception("lettura Tesla per Tuya fallita")
                faults.add("tesla_error")
                tesla_state = "error"
        else:
            tesla_state = "disconnected"

        breakdown = reconcile_energy_flows(
            solar_power_w=solar_power_w,
            appliances_power_w=appliances_power_w,
            tesla_power_w=tesla_power_w,
            import_power_w=measurement.import_power_w if measurement is not None else None,
            export_power_w=measurement.export_power_w if measurement is not None else None,
        )
        house_consumption_w = max(breakdown.house_power_w, 0.0)
        return {
            "meter_switch": self.meter_enabled,
            "solar_power_w": round(solar_power_w),
            "house_consumption_w": round(house_consumption_w),
            "tesla_power_w": round(tesla_power_w),
            "total_consumption_w": round(house_consumption_w + tesla_power_w),
            "tesla_state": tesla_state,
            "meter_fault": encode_faults(faults),
        }

    def _average_properties(self, latest: dict[str, Any]) -> dict[str, Any]:
        numeric_fields = (
            "solar_power_w",
            "house_consumption_w",
            "tesla_power_w",
            "total_consumption_w",
        )
        averaged = dict(latest)
        for field in numeric_fields:
            values = [float(sample[field]) for sample in self._samples if field in sample]
            averaged[field] = round(sum(values) / len(values)) if values else 0
        averaged["meter_switch"] = self.meter_enabled
        return averaged

    def handle_property_set(self, client: TuyaLinkMqttClient, message: MqttMessage) -> None:
        if message.topic != client.property_set_topic:
            return
        try:
            payload = json.loads(message.payload.decode("utf-8"))
            data = payload.get("data") or {}
            msg_id = str(payload.get("msgId") or uuid.uuid4().hex)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            LOG.warning("property/set Tuya non valido: %s", exc)
            return

        if "meter_switch" in data:
            raw_enabled = data["meter_switch"]
            if isinstance(raw_enabled, dict) and "value" in raw_enabled:
                raw_enabled = raw_enabled["value"]
            if isinstance(raw_enabled, str):
                self.meter_enabled = raw_enabled.strip().casefold() in {"1", "true", "on", "yes"}
            else:
                self.meter_enabled = bool(raw_enabled)
            if self.on_switch is not None:
                self.on_switch(self.meter_enabled)
            LOG.info("tuya_meter_switch enabled=%s", self.meter_enabled)
        client.respond_property_set(msg_id, 0)
        client.report_properties(self.properties())

    def handle_property_get(self, client: TuyaLinkMqttClient, message: MqttMessage) -> None:
        if message.topic != client.property_get_topic:
            return
        try:
            payload = json.loads(message.payload.decode("utf-8"))
            requested = payload.get("data") or []
            msg_id = str(payload.get("msgId") or uuid.uuid4().hex)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            LOG.warning("property/get Tuya non valido: %s", exc)
            return

        properties = self.properties()
        if isinstance(requested, list) and requested:
            properties = {
                key: value
                for key, value in properties.items()
                if key in set(str(item) for item in requested)
            }
        client.respond_property_get(msg_id, properties)
        if hasattr(client, "report_properties"):
            client.report_properties(self.properties())

    def handle_message(self, client: TuyaLinkMqttClient, message: MqttMessage) -> None:
        if message.topic == client.property_set_topic:
            self.handle_property_set(client, message)
        elif message.topic == client.property_get_topic:
            self.handle_property_get(client, message)
        elif message.topic == client.property_report_response_topic:
            self.handle_property_report_response(message)

    def handle_property_report_response(self, message: MqttMessage) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8"))
            code = int(payload.get("code", 0))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            LOG.warning("property/report_response Tuya non valido: %s", exc)
            return
        if code != 0:
            LOG.warning("tuya_property_report_response code=%s payload=%s", code, payload)

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _tuya_cached_tesla_state(cls, row: dict[str, Any], tesla_power_w: float) -> str:
        current_a = cls._number(row.get("tesla_current_a"))
        if current_a is None:
            current_a = cls._number(row.get("current_a"))
        if current_a is not None:
            if current_a >= 1.0:
                return "charging"
            return "idle" if tesla_power_w > 0 else "disconnected"
        if row.get("tesla_connected") is True and tesla_power_w == 0:
            return "idle"
        if row.get("wall_connector_vehicle_connected") is False:
            return "disconnected"
        if tesla_power_w >= 500:
            return "charging"
        if tesla_power_w > 0:
            return "idle"
        return "disconnected"

    @staticmethod
    def _tuya_tesla_state(charging_state: str) -> str:
        state = charging_state.strip().casefold()
        if state == "charging":
            return "charging"
        if state in {"disconnected", "offline", "unknown", "asleep"}:
            return "disconnected"
        if state in {"error", "fault", "failed"}:
            return "error"
        return "idle"
