from __future__ import annotations

import base64
import enum
import json
import os
import random
import sys
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from .models import ChargeState


class VehicleClient(Protocol):
    def get_charge_state(self) -> ChargeState: ...

    def set_charging_amps(self, amps: int) -> None: ...

    def start_charging(self) -> None: ...

    def stop_charging(self) -> None: ...


@dataclass(frozen=True)
class WallConnectorVitals:
    vehicle_connected: bool
    contactor_closed: bool
    grid_v: float
    vehicle_current_a: float
    phase_currents_a: tuple[float, ...] = ()
    power_w: float = 0.0
    evse_state: int | None = None


class WallConnectorClient:
    """Lettura locale Tesla Wall Connector Gen 3."""

    def __init__(
        self,
        host: str,
        *,
        timeout_seconds: float = 3.0,
        phases: int = 3,
        min_current_a: float = 0.3,
        minimum_interval_seconds: float = 15.0,
    ) -> None:
        base = host.strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            base = f"http://{base}"
        self.base_url = base
        self.timeout_seconds = timeout_seconds
        self.phases = phases
        self.min_current_a = min_current_a
        self.minimum_interval_seconds = minimum_interval_seconds
        self._last_read_monotonic = 0.0
        self._last_vitals: WallConnectorVitals | None = None

    @classmethod
    def parse_vitals(
        cls,
        payload: dict,
        *,
        phases: int = 3,
        min_current_a: float = 0.3,
    ) -> WallConnectorVitals:
        connected = bool(payload.get("vehicle_connected"))
        contactor_closed = bool(payload.get("contactor_closed"))
        grid_v = max(float(payload.get("grid_v") or 0.0), 0.0)
        vehicle_current_a = max(float(payload.get("vehicle_current_a") or 0.0), 0.0)
        phase_currents = tuple(
            max(float(payload.get(key) or 0.0), 0.0)
            for key in ("currentA_a", "currentB_a", "currentC_a")
        )
        active_phases = sum(current > min_current_a for current in phase_currents)
        phase_count = active_phases or max(min(phases, 3), 1)
        power_w = 0.0
        if (connected or contactor_closed) and vehicle_current_a > min_current_a and grid_v > 0:
            power_w = grid_v * vehicle_current_a * phase_count
        evse_state = payload.get("evse_state")
        return WallConnectorVitals(
            vehicle_connected=connected,
            contactor_closed=contactor_closed,
            grid_v=grid_v,
            vehicle_current_a=vehicle_current_a,
            phase_currents_a=phase_currents,
            power_w=power_w,
            evse_state=int(evse_state) if evse_state is not None else None,
        )

    def read_vitals(self) -> WallConnectorVitals:
        now = time.monotonic()
        if (
            self._last_vitals is not None
            and now - self._last_read_monotonic < self.minimum_interval_seconds
        ):
            return self._last_vitals
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.get(f"{self.base_url}/api/1/vitals")
            response.raise_for_status()
            vitals = self.parse_vitals(
                response.json(),
                phases=self.phases,
                min_current_a=self.min_current_a,
            )
        self._last_read_monotonic = now
        self._last_vitals = vitals
        return vitals


class MockTeslaClient:
    def __init__(self, current_a: int, phases: int = 3, voltage_v: float = 230.0) -> None:
        self.state = ChargeState(
            charging_state="Charging",
            current_request_a=current_a,
            current_request_max_a=32,
            actual_current_a=float(current_a),
            phases=phases,
            voltage_v=voltage_v,
            charger_power_kw=current_a * phases * voltage_v / 1000,
        )
        self.commands: list[int | str] = []

    def get_charge_state(self) -> ChargeState:
        return self.state

    def set_charging_amps(self, amps: int) -> None:
        self.commands.append(amps)

    def start_charging(self) -> None:
        self.commands.append("start")

    def stop_charging(self) -> None:
        self.commands.append("stop")


class BLEErrorCategory(str, enum.Enum):
    OUT_OF_RANGE = "out_of_range"
    BLE_STACK = "ble_stack"
    AUTH = "auth"
    CLOCK = "clock"
    CAR_STATE = "car_state"
    UNKNOWN = "unknown"


class TeslaBLEError(RuntimeError):
    def __init__(self, message: str, category: BLEErrorCategory, *, retryable: bool) -> None:
        super().__init__(message)
        self.category = category
        self.retryable = retryable

    def to_report_dict(self) -> dict:
        hints = {
            BLEErrorCategory.OUT_OF_RANGE: (
                "Avvicinare il Raspberry all'auto o ridurre ostacoli tra antenna e Tesla.",
                "Verificare che la Tesla sia sveglia/in carica e che il VIN sia corretto.",
            ),
            BLEErrorCategory.BLE_STACK: (
                "Controllare BlueZ, permessi/capability del binario tesla-control e adapter BLE.",
                "Se il reset manuale bluetoothctl power off/on funziona, valutare la recovery automatica.",
            ),
            BLEErrorCategory.AUTH: (
                "Ripetere pairing BLE con la chiave NFC e ruolo charging_manager.",
                "Verificare che TESLA_BLE_KEY_FILE punti alla chiave privata associata all'auto.",
            ),
            BLEErrorCategory.CLOCK: (
                "Attendere la sincronizzazione NTP del Raspberry prima di inviare comandi.",
            ),
            BLEErrorCategory.CAR_STATE: (
                "Il controller non sveglia l'auto: avviare manualmente la ricarica prima del controllo.",
            ),
            BLEErrorCategory.UNKNOWN: (
                "Eseguire tesla-control manualmente sul Raspberry per leggere l'errore completo.",
            ),
        }
        return {
            "component": "tesla_ble",
            "phase": self.category.value,
            "message": str(self),
            "retryable": self.retryable,
            "hints": hints.get(self.category, ()),
        }


def system_clock_synchronized() -> bool:
    """Fail closed on Raspberry/Linux; non-Linux hosts are development environments."""
    if not sys.platform.startswith("linux"):
        return True
    marker = Path("/run/systemd/timesync/synchronized")
    try:
        if marker.exists():
            return True
        result = subprocess.run(
            ["timedatectl", "show", "--property=NTPSynchronized", "--value"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        return result.returncode == 0 and result.stdout.strip().casefold() == "yes"
    except (OSError, subprocess.SubprocessError):
        return False


class TeslaBLEClient:
    """Adapter per il tool ufficiale ``tesla-control`` via Bluetooth LE."""

    _access_lock = threading.Lock()

    def __init__(
        self,
        vin: str,
        key_file: str,
        *,
        binary: str = "tesla-control",
        cache_file: str | None = None,
        timeout_seconds: int = 35,
        connect_timeout_seconds: int = 20,
        command_timeout_seconds: int = 10,
        retries: int = 2,
        retry_backoff_seconds: float = 1.0,
        bt_adapter: str | None = None,
        require_time_sync: bool = True,
        preflight_sleep_check: bool = True,
        recovery_enabled: bool = False,
        recovery_threshold: int = 3,
        clock_checker=system_clock_synchronized,
    ) -> None:
        self.vin = vin
        self.key_file = str(Path(key_file).expanduser())
        self.binary = binary
        self.cache_file = str(Path(cache_file).expanduser()) if cache_file else None
        self.timeout_seconds = timeout_seconds
        self.connect_timeout_seconds = connect_timeout_seconds
        self.command_timeout_seconds = command_timeout_seconds
        self.retries = retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.bt_adapter = bt_adapter
        self.require_time_sync = require_time_sync
        self.preflight_sleep_check = preflight_sleep_check
        self.recovery_enabled = recovery_enabled
        self.recovery_threshold = recovery_threshold
        self.clock_checker = clock_checker
        self._consecutive_transport_failures = 0

    def _command(self, *arguments: str) -> list[str]:
        command = [
            self.binary,
            "-ble",
            "-vin",
            self.vin,
            "-key-file",
            self.key_file,
            "-connect-timeout",
            f"{self.connect_timeout_seconds}s",
            "-command-timeout",
            f"{self.command_timeout_seconds}s",
        ]
        if self.bt_adapter:
            command.extend(["-bt-adapter", self.bt_adapter])
        if self.cache_file:
            command.extend(["-session-cache", self.cache_file])
        command.extend(arguments)
        return command

    @staticmethod
    def _classify_error(detail: str) -> tuple[BLEErrorCategory, bool]:
        normalized = detail.casefold()
        patterns = (
            (
                BLEErrorCategory.AUTH,
                False,
                (
                    "unauthor",
                    "insufficient privilege",
                    "whitelist",
                    "private key",
                    "authentication failed",
                ),
            ),
            (
                BLEErrorCategory.CLOCK,
                False,
                ("clock skew", "command expired", "anti-replay", "replay", "invalid epoch"),
            ),
            (
                BLEErrorCategory.BLE_STACK,
                True,
                (
                    "org.bluez",
                    "d-bus",
                    "dbus",
                    "no default controller",
                    "adapter not",
                    "bluetooth adapter",
                ),
            ),
            (
                BLEErrorCategory.OUT_OF_RANGE,
                True,
                (
                    "failed to find ble beacon",
                    "can't scan",
                    "cannot scan",
                    "context deadline exceeded",
                    "not advertising",
                    "device not found",
                ),
            ),
            (
                BLEErrorCategory.CAR_STATE,
                False,
                (
                    "vehicle is offline",
                    "vehicle asleep",
                    "invalid vehicle state",
                    "command rejected",
                ),
            ),
        )
        for category, retryable, needles in patterns:
            if any(needle in normalized for needle in needles):
                return category, retryable
        return BLEErrorCategory.UNKNOWN, True

    def _recover_adapter(self) -> None:
        if not self.recovery_enabled:
            return
        try:
            for power in ("off", "on"):
                subprocess.run(
                    ["bluetoothctl", "power", power],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
                time.sleep(1)
        except (OSError, subprocess.SubprocessError):
            pass

    def _run_once(self, *arguments: str) -> str:
        try:
            result = subprocess.run(
                self._command(*arguments),
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise TeslaBLEError(
                f"Comando Tesla BLE non trovato: {self.binary}",
                BLEErrorCategory.UNKNOWN,
                retryable=False,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise TeslaBLEError(
                "Tesla non raggiungibile via Bluetooth entro il timeout",
                BLEErrorCategory.OUT_OF_RANGE,
                retryable=True,
            ) from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "errore sconosciuto").strip()
            category, retryable = self._classify_error(detail)
            raise TeslaBLEError(
                f"Tesla BLE [{category.value}]: {detail[:500]}", category, retryable=retryable
            ) from exc
        return result.stdout

    def _run(self, *arguments: str) -> str:
        if self.require_time_sync and not self.clock_checker():
            raise TeslaBLEError(
                "Orologio Raspberry non sincronizzato: attendo NTP prima di usare Tesla BLE",
                BLEErrorCategory.CLOCK,
                retryable=False,
            )
        with self._access_lock:
            for attempt in range(self.retries + 1):
                try:
                    output = self._run_once(*arguments)
                    self._consecutive_transport_failures = 0
                    return output
                except TeslaBLEError as exc:
                    if exc.category in {BLEErrorCategory.OUT_OF_RANGE, BLEErrorCategory.BLE_STACK}:
                        self._consecutive_transport_failures += 1
                    if (
                        self._consecutive_transport_failures >= self.recovery_threshold
                        and exc.retryable
                    ):
                        self._recover_adapter()
                        self._consecutive_transport_failures = 0
                    if not exc.retryable or attempt >= self.retries:
                        raise
                    delay = self.retry_backoff_seconds * (2**attempt)
                    time.sleep(delay + random.uniform(0, delay * 0.25))
        raise AssertionError("retry loop terminato senza risultato")

    @staticmethod
    def _charging_state(value) -> str:
        if isinstance(value, str):
            return value.removeprefix("ChargingState")
        if isinstance(value, dict):
            nested = value.get("type", value)
            if isinstance(nested, dict) and nested:
                return str(next(iter(nested))).replace("_", " ").title().replace(" ", "")
        return "Unknown"

    @classmethod
    def parse_charge_state(cls, payload: dict) -> ChargeState:
        charge = payload.get("chargeState") or payload.get("charge_state") or payload

        def value(camel: str, snake: str, default=0):
            return charge.get(camel, charge.get(snake, default))

        state = cls._charging_state(value("chargingState", "charging_state", "Unknown"))
        voltage = float(value("chargerVoltage", "charger_voltage") or 0)
        actual = float(value("chargerActualCurrent", "charger_actual_current") or 0)
        power_kw = float(value("chargerPower", "charger_power") or 0)
        phases = int(value("chargerPhases", "charger_phases") or 0)
        if phases not in {1, 3} and voltage > 0 and actual > 0 and power_kw > 0:
            estimate = power_kw * 1000 / (voltage * actual)
            phases = 3 if estimate >= 2 else 1
        return ChargeState(
            charging_state=state,
            current_request_a=int(value("chargeCurrentRequest", "charge_current_request") or 0),
            current_request_max_a=int(
                value("chargeCurrentRequestMax", "charge_current_request_max") or 0
            ),
            actual_current_a=actual,
            phases=phases,
            voltage_v=voltage,
            charger_power_kw=power_kw,
        )

    def get_charge_state(self) -> ChargeState:
        if self.preflight_sleep_check and not self.is_awake():
            return ChargeState(
                charging_state="Asleep",
                current_request_a=0,
                current_request_max_a=0,
                actual_current_a=0,
                phases=0,
                voltage_v=0,
                charger_power_kw=0,
            )
        output = self._run("state", "charge")
        try:
            return self.parse_charge_state(json.loads(output))
        except json.JSONDecodeError as exc:
            raise RuntimeError("Risposta JSON non valida da tesla-control") from exc

    def set_charging_amps(self, amps: int) -> None:
        self._run("charging-set-amps", str(amps))

    def start_charging(self) -> None:
        self._run("charging-start")

    def stop_charging(self) -> None:
        self._run("charging-stop")

    def is_awake(self) -> bool:
        output = self._run("body-controller-state")
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise TeslaBLEError(
                "Risposta JSON non valida da body-controller-state",
                BLEErrorCategory.UNKNOWN,
                retryable=False,
            ) from exc
        vehicle = payload.get("vehicleStatus", payload.get("vehicle_status", payload))
        status = str(
            vehicle.get("vehicleSleepStatus", vehicle.get("vehicle_sleep_status", "UNKNOWN"))
        ).upper()
        return status.endswith("_AWAKE") or status == "AWAKE"


class TeslaTokenStore:
    def __init__(
        self,
        token_file: str,
        client_id: str,
        token_url: str,
        initial_access_token: str | None = None,
        initial_refresh_token: str | None = None,
    ) -> None:
        self.path = Path(token_file).expanduser()
        self.client_id = client_id
        self.token_url = token_url
        self.data: dict = {}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            if initial_access_token:
                self.data["access_token"] = initial_access_token
            if initial_refresh_token:
                self.data["refresh_token"] = initial_refresh_token

    @staticmethod
    def _jwt_exp(token: str) -> int:
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            return int(json.loads(base64.urlsafe_b64decode(payload))["exp"])
        except (IndexError, KeyError, ValueError, json.JSONDecodeError):
            return 0

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)

    def access_token(self) -> str:
        token = self.data.get("access_token")
        if token and self._jwt_exp(token) > int(time.time()) + 60:
            return token
        refresh_token = self.data.get("refresh_token")
        if not refresh_token:
            raise RuntimeError("Token Tesla mancante o scaduto; eseguire scripts/tesla_oauth.py")
        response = httpx.post(
            self.token_url,
            data={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "refresh_token": refresh_token,
            },
            timeout=20,
        )
        response.raise_for_status()
        refreshed = response.json()
        if "access_token" not in refreshed or "refresh_token" not in refreshed:
            raise RuntimeError("Risposta di refresh Tesla incompleta")
        self.data.update(refreshed)
        self._save()  # il refresh token Tesla ruota: va salvato immediatamente
        return str(self.data["access_token"])


class TeslaFleetClient:
    def __init__(
        self,
        vin: str,
        base_url: str,
        token_store: TeslaTokenStore,
        ca_cert: str | None = None,
        verify_ssl: bool = True,
    ) -> None:
        self.vin = vin
        self.base_url = base_url.rstrip("/")
        verify: bool | str = ca_cert if ca_cert else verify_ssl
        self.client = httpx.Client(base_url=self.base_url, verify=verify, timeout=25)
        self.tokens = token_store

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens.access_token()}"}

    @staticmethod
    def parse_charge_state(payload: dict) -> ChargeState:
        response = payload.get("response", payload)
        charge = response.get("charge_state", response)
        required = {"charging_state", "charge_current_request"}
        missing = required - charge.keys()
        if missing:
            raise ValueError(f"Dati Tesla incompleti: {', '.join(sorted(missing))}")
        return ChargeState(
            charging_state=str(charge["charging_state"]),
            current_request_a=int(charge["charge_current_request"]),
            current_request_max_a=int(charge.get("charge_current_request_max", 0)),
            actual_current_a=float(charge.get("charger_actual_current", 0)),
            phases=int(charge.get("charger_phases") or 0),
            voltage_v=float(charge.get("charger_voltage") or 0),
            charger_power_kw=float(charge.get("charger_power") or 0),
        )

    def get_charge_state(self) -> ChargeState:
        response = self.client.get(
            f"/api/1/vehicles/{self.vin}/vehicle_data",
            params={"endpoints": "charge_state"},
            headers=self._headers(),
        )
        response.raise_for_status()
        return self.parse_charge_state(response.json())

    def set_charging_amps(self, amps: int) -> None:
        response = self.client.post(
            f"/api/1/vehicles/{self.vin}/command/set_charging_amps",
            json={"charging_amps": amps},
            headers=self._headers(),
        )
        response.raise_for_status()
        payload = response.json().get("response", response.json())
        if not payload.get("result", False):
            raise RuntimeError(f"Comando Tesla rifiutato: {payload.get('reason', 'motivo ignoto')}")

    def _charge_command(self, command: str) -> None:
        response = self.client.post(
            f"/api/1/vehicles/{self.vin}/command/{command}",
            headers=self._headers(),
        )
        response.raise_for_status()
        payload = response.json().get("response", response.json())
        if not payload.get("result", False):
            raise RuntimeError(f"Comando Tesla rifiutato: {payload.get('reason', 'motivo ignoto')}")

    def start_charging(self) -> None:
        self._charge_command("charge_start")

    def stop_charging(self) -> None:
        self._charge_command("charge_stop")
