from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import ssl
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from websocket import create_connection


class VimarError(RuntimeError):
    pass


@dataclass(frozen=True)
class VimarCredentials:
    username: str
    useruid: str
    password: str
    device_uid: str | None = None
    plant_name: str | None = None
    paired_at: str | None = None


@dataclass(frozen=True)
class VimarEnergyPoint:
    idsf: int
    name: str
    sftype: str
    sstype: str
    power_w: float | None
    production_w: float | None
    exchange_w: float | None


@dataclass(frozen=True)
class VimarGateway:
    host: str
    port: int
    device_uid: str | None
    model: str | None
    protocol_version: str | None
    software_version: str | None


def local_ip_for(host: str) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((host, 9))
        return sock.getsockname()[0]


def discover_gateways(timeout_seconds: float = 4.0) -> list[VimarGateway]:
    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

    service_type = "_vimar-devctrl._tcp.local."
    found: list[VimarGateway] = []

    class Listener(ServiceListener):
        def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            info = zc.get_service_info(type_, name, timeout=int(timeout_seconds * 1000))
            if info is None:
                return
            properties = {
                key.decode("utf-8", errors="replace").casefold(): value.decode(
                    "utf-8", errors="replace"
                )
                for key, value in info.properties.items()
            }
            addresses = [socket.inet_ntoa(address) for address in info.addresses]
            if not addresses:
                return
            found.append(
                VimarGateway(
                    host=addresses[0],
                    port=info.port,
                    device_uid=properties.get("deviceuid"),
                    model=properties.get("model"),
                    protocol_version=properties.get("protocolversion"),
                    software_version=properties.get("softwareversion"),
                )
            )

        def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            self.add_service(zc, type_, name)

        def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            return

    zeroconf = Zeroconf()
    try:
        ServiceBrowser(zeroconf, service_type, Listener())
        import time

        time.sleep(timeout_seconds)
    finally:
        zeroconf.close()
    dedup: dict[tuple[str, int], VimarGateway] = {}
    for gateway in found:
        dedup[(gateway.host, gateway.port)] = gateway
    return list(dedup.values())


def sign_with_private_key(value: str, private_key_file: str) -> str:
    """Firma raw PKCS#1 v1.5 come richiesto dalla documentazione Vimar.

    Equivalente a:
    echo -n <VALUE> | openssl pkeyutl -sign -inkey <PRIVATE-KEY> | base64 | tr -d '\\n'
    """

    key_path = Path(private_key_file).expanduser()
    if not key_path.exists():
        raise VimarError(f"Chiave privata Vimar non trovata: {key_path}")
    try:
        signed = subprocess.run(
            ["openssl", "pkeyutl", "-sign", "-inkey", str(key_path)],
            input=value.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout
    except FileNotFoundError as exc:
        raise VimarError("openssl non trovato nel PATH") from exc
    except subprocess.CalledProcessError as exc:
        err = exc.stderr.decode("utf-8", errors="replace").strip()
        raise VimarError(f"Firma Vimar fallita: {err}") from exc
    return base64.b64encode(signed).decode("ascii")


def load_credentials(path: str) -> VimarCredentials:
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    return VimarCredentials(
        username=str(data["username"]),
        useruid=str(data["useruid"]),
        password=str(data["password"]),
        device_uid=data.get("device_uid"),
        plant_name=data.get("plant_name"),
        paired_at=data.get("paired_at"),
    )


def save_credentials(path: str, credentials: VimarCredentials) -> None:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "username": credentials.username,
        "useruid": credentials.useruid,
        "password": credentials.password,
        "device_uid": credentials.device_uid,
        "plant_name": credentials.plant_name,
        "paired_at": credentials.paired_at or datetime.now(timezone.utc).isoformat(),
    }
    fd, tmp_name = tempfile.mkstemp(prefix=target.name, dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


class VimarIPConnectorClient:
    def __init__(
        self,
        *,
        host: str,
        port: int = 20615,
        device_uid: str | None = None,
        private_key_file: str,
        ca_cert: str | None = None,
        tls_verify: bool = True,
        tls_check_hostname: bool = False,
        timeout_seconds: int = 15,
        client_name: str = "Raspberry Energy Monitor",
        protocol_version: str = "2.7",
        client_source: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.device_uid = device_uid
        self.private_key_file = private_key_file
        self.ca_cert = ca_cert
        self.tls_verify = tls_verify
        self.tls_check_hostname = tls_check_hostname
        self.timeout_seconds = timeout_seconds
        self.client_name = client_name
        self.protocol_version = protocol_version
        self.client_source = client_source or secrets.token_urlsafe(18)[:24]
        self.local_ip = local_ip_for(host)
        self._msgid = 0
        self._ws = None
        self._token = secrets.token_urlsafe(12)

    def _sslopt(self) -> dict[str, Any]:
        if not self.tls_verify:
            return {"cert_reqs": ssl.CERT_NONE, "check_hostname": False}
        options: dict[str, Any] = {
            "cert_reqs": ssl.CERT_REQUIRED,
            "check_hostname": self.tls_check_hostname,
        }
        if self.ca_cert:
            options["ca_certs"] = str(Path(self.ca_cert).expanduser())
        return options

    def _connect(self, port: int):
        url = f"wss://{self.host}:{port}"
        return create_connection(url, timeout=self.timeout_seconds, sslopt=self._sslopt())

    def _next_msgid(self) -> str:
        value = str(self._msgid)
        self._msgid += 1
        return value

    def _target(self) -> str:
        return self.device_uid or "gateway"

    def _request(self, function: str, args: list[dict], params: list[dict] | None = None) -> dict:
        if self._ws is None:
            raise VimarError("WebSocket Vimar non connesso")
        payload = {
            "type": "request",
            "function": function,
            "source": self.client_source,
            "target": self._target(),
            "token": self._token,
            "msgid": self._next_msgid(),
            "args": args,
            "params": params or [],
        }
        self._ws.send(json.dumps(payload, separators=(",", ":")))
        raw = self._ws.recv()
        try:
            response = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise VimarError(f"Risposta Vimar non JSON: {raw!r}") from exc
        error = int(response.get("error", 0))
        if error != 0:
            raise VimarError(f"Errore Vimar {function}: error={error} response={response}")
        return response

    def open_session(self) -> int:
        ws = self._connect(self.port)
        try:
            self._ws = ws
            response = self._request(
                "session",
                [
                    {
                        "communication": {
                            "communicationmode": 4,
                            "ipaddress": self.local_ip,
                            "ipport": 0,
                        }
                    }
                ],
            )
        finally:
            ws.close()
            self._ws = None
        try:
            return int(response["result"][0]["communication"]["ipport"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise VimarError(f"SESSION Vimar senza porta ATTACH: {response}") from exc

    def attach_pair(self, third_party_tag: str, setup_code: str) -> VimarCredentials:
        return self._attach(
            username=third_party_tag,
            useruid="",
            password_to_sign=setup_code,
            save_server_credentials=True,
        )

    def attach_credentials(self, credentials: VimarCredentials) -> None:
        self._attach(
            username=credentials.username,
            useruid=credentials.useruid,
            password_to_sign=credentials.password,
            save_server_credentials=False,
        )

    def _attach(
        self,
        *,
        username: str,
        useruid: str,
        password_to_sign: str,
        save_server_credentials: bool,
    ) -> VimarCredentials:
        attach_port = self.open_session()
        self._ws = self._connect(attach_port)
        signed_password = sign_with_private_key(password_to_sign, self.private_key_file)
        response = self._request(
            "attach",
            [
                {
                    "credential": {
                        "username": username,
                        "useruid": useruid,
                        "password": signed_password,
                    },
                    "clientinfo": {
                        "manufacturertag": username,
                        "clienttag": "thirdpartyapp",
                        "sfmodelversion": "1.0.0",
                        "protocolversion": self.protocol_version,
                    },
                    "communication": {"ipaddress": self.local_ip},
                }
            ],
        )
        try:
            result = response["result"][0]
            token = str(result["token"])
            server_password = str(result["password"])
            server_useruid = str(result["useruid"])
            server_username = str(result.get("username") or username)
            serverinfo = result.get("serverinfo") or {}
        except (KeyError, IndexError, TypeError) as exc:
            raise VimarError(f"ATTACH Vimar senza credenziali/token: {response}") from exc
        self._token = token
        if save_server_credentials:
            return VimarCredentials(
                username=username,
                useruid=server_useruid,
                password=server_password,
                device_uid=str(serverinfo.get("deviceuid") or self.device_uid or ""),
                plant_name=str(result.get("plantname") or ""),
                paired_at=datetime.now(timezone.utc).isoformat(),
            )
        return VimarCredentials(
            username=server_username,
            useruid=server_useruid,
            password=server_password,
            device_uid=str(serverinfo.get("deviceuid") or self.device_uid or ""),
            plant_name=str(result.get("plantname") or ""),
        )

    def close(self) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None

    def sfdiscovery(self, sfcategory: str = "Plant", with_values: bool = True) -> dict:
        return self._request(
            "sfdiscovery",
            [{"sfcategory": sfcategory}],
            [{"idambient": [], "withvalues": with_values}],
        )

    def getstatus(self, idsf: int, sfetypes: list[str] | None = None) -> dict:
        return self._request(
            "getstatus",
            [{"idsf": idsf, "sfetype": sfetypes or []}],
        )

    @staticmethod
    def extract_energy_points(discovery_response: dict) -> list[VimarEnergyPoint]:
        points: list[VimarEnergyPoint] = []
        for ambient in discovery_response.get("result", []):
            for sf in ambient.get("sf", []):
                sstype = str(sf.get("sstype", ""))
                if "Energy" not in sstype and sf.get("sftype") != "SF_Energy":
                    continue
                elements = {
                    str(element.get("sfetype")): element
                    for element in sf.get("elements", [])
                    if isinstance(element, dict)
                }
                points.append(
                    VimarEnergyPoint(
                        idsf=int(sf["idsf"]),
                        name=str(sf.get("name", "")),
                        sftype=str(sf.get("sftype", "")),
                        sstype=sstype,
                        power_w=_float_value(elements, "SFE_State_GlobalActivePowerConsumption"),
                        production_w=_float_value(elements, "SFE_State_GlobalActivePowerProduct"),
                        exchange_w=_float_value(elements, "SFE_State_GlobalActivePowerExchange"),
                    )
                )
        return points


def read_energy_points_from_settings(settings) -> list[VimarEnergyPoint]:
    if not settings.vimar_host or not Path(settings.vimar_credentials_file).expanduser().exists():
        return []
    client = VimarIPConnectorClient(
        host=settings.vimar_host,
        port=settings.vimar_port,
        device_uid=settings.vimar_device_uid,
        private_key_file=settings.vimar_private_key_file,
        ca_cert=settings.vimar_ca_cert,
        tls_verify=settings.vimar_tls_verify,
        tls_check_hostname=settings.vimar_tls_check_hostname,
        timeout_seconds=settings.vimar_timeout_seconds,
        client_name=settings.vimar_client_name,
        protocol_version=settings.vimar_protocol_version,
    )
    try:
        client.attach_credentials(load_credentials(settings.vimar_credentials_file))
        return client.extract_energy_points(client.sfdiscovery("Plant", with_values=True))
    finally:
        client.close()


def _float_value(elements: dict[str, dict], key: str) -> float | None:
    element = elements.get(key)
    if not element or not element.get("enable", True):
        return None
    value = element.get("value")
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
