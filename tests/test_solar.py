import pytest
import httpx

from tesla_energy_controller.solar import (
    AlfaModbusSource,
    SolarEdgeAccessError,
    SolarEdgeCloudSource,
    SolarEdgeModbusSource,
    SolarEdgeWebSource,
)


def test_cloud_import_direction():
    measurement = SolarEdgeCloudSource.parse_power_flow(
        {
            "siteCurrentPowerFlow": {
                "unit": "kW",
                "GRID": {"currentPower": 1.25},
                "connections": [{"from": "GRID", "to": "Load"}],
            }
        }
    )
    assert measurement.total_power_w == 1250


def test_cloud_export_direction():
    measurement = SolarEdgeCloudSource.parse_power_flow(
        {
            "siteCurrentPowerFlow": {
                "unit": "kW",
                "GRID": {"currentPower": 2},
                "connections": [{"from": "PV", "to": "Grid"}],
            }
        }
    )
    assert measurement.total_power_w == -2000


def test_modbus_decodes_three_phase_meter():
    registers = [0] * 23
    registers[0] = 203
    registers[3:6] = [101, 102, 103]
    registers[6] = 0xFFFF  # -1
    registers[18:22] = [3000, 1000, 900, 1100]
    registers[22] = 0
    measurement = SolarEdgeModbusSource.decode_meter_registers(registers, 3)
    assert measurement.total_power_w == 3000
    assert measurement.import_power_w == 3000
    assert measurement.export_power_w == 0
    assert measurement.phase_power_w == (1000, 900, 1100)
    assert measurement.phase_current_a == pytest.approx((10.1, 10.2, 10.3))


def test_modbus_decodes_three_phase_inverter_power():
    registers = [0] * SolarEdgeModbusSource.INVERTER_READ_COUNT
    registers[0] = 103
    registers[14] = 5218
    registers[15] = 0
    measurement = SolarEdgeModbusSource.decode_inverter_registers(registers)
    assert measurement.source == "solaredge-modbus"
    assert measurement.solar_power_w == 5218
    assert measurement.total_power_w == 0


def test_modbus_connect_failure_is_solaredge_diagnostic():
    class Client:
        closed = False

        @staticmethod
        def connect():
            return False

        def close(self):
            self.closed = True

    client = Client()
    source = SolarEdgeModbusSource.__new__(SolarEdgeModbusSource)
    source._client = client
    source._host = "192.168.2.126"
    source._port = 1502
    source._unit = 1

    with pytest.raises(SolarEdgeAccessError) as error:
        source._read(40069, SolarEdgeModbusSource.INVERTER_READ_COUNT)

    report = error.value.to_report_dict()
    assert report["component"] == "solaredge"
    assert report["phase"] == "modbus-connect"
    assert report["endpoint"] == "tcp://192.168.2.126:1502"
    assert client.closed is True


def test_modbus_read_failure_closes_socket():
    class Client:
        closed = False

        @staticmethod
        def connect():
            return True

        @staticmethod
        def read_holding_registers(**_kwargs):
            raise RuntimeError("socket closed")

        def close(self):
            self.closed = True

    client = Client()
    source = SolarEdgeModbusSource.__new__(SolarEdgeModbusSource)
    source._client = client
    source._host = "192.168.2.126"
    source._port = 1502
    source._unit = 1

    with pytest.raises(SolarEdgeAccessError) as error:
        source._read(40069, SolarEdgeModbusSource.INVERTER_READ_COUNT)

    report = error.value.to_report_dict()
    assert report["phase"] == "modbus-read-40069"
    assert "RuntimeError: socket closed" in report["response_excerpt"]
    assert client.closed is True


def test_modbus_read_retries_after_closed_socket():
    class Response:
        registers = [103] + [0] * (SolarEdgeModbusSource.INVERTER_READ_COUNT - 1)

        @staticmethod
        def isError():
            return False

    class Client:
        def __init__(self):
            self.reads = 0
            self.closed = 0

        @staticmethod
        def connect():
            return True

        def read_holding_registers(self, **_kwargs):
            self.reads += 1
            if self.reads == 1:
                raise RuntimeError("socket closed during startup")
            return Response()

        def close(self):
            self.closed += 1

    client = Client()
    source = SolarEdgeModbusSource.__new__(SolarEdgeModbusSource)
    source._client = client
    source._host = "192.168.2.126"
    source._port = 1502
    source._unit = 1

    response = source._read(40069, SolarEdgeModbusSource.INVERTER_READ_COUNT)

    assert response.registers[0] == 103
    assert client.reads == 2
    assert client.closed == 1


def test_modbus_read_uses_inverter_even_without_meter():
    class Response:
        def __init__(self, registers):
            self.registers = registers

        @staticmethod
        def isError():
            return False

    registers = [0] * SolarEdgeModbusSource.INVERTER_READ_COUNT
    registers[0] = 103
    registers[14] = 5184
    registers[15] = 0
    source = SolarEdgeModbusSource.__new__(SolarEdgeModbusSource)
    source._inverter_model_address = 40069
    source._model_address = 40188
    source._expected_phases = 3
    source._power_sign = 1

    def read(address, count):
        assert count in {SolarEdgeModbusSource.INVERTER_READ_COUNT, SolarEdgeModbusSource.READ_COUNT}
        if address == 40069:
            return Response(registers)
        raise ConnectionError("meter assente")

    source._read = read
    measurement = source.read()
    assert measurement.solar_power_w == 5184
    assert measurement.import_power_w is None
    assert measurement.export_power_w is None


def test_modbus_read_can_skip_meter_to_keep_inverter_session():
    class Response:
        def __init__(self, registers):
            self.registers = registers

        @staticmethod
        def isError():
            return False

    registers = [0] * SolarEdgeModbusSource.INVERTER_READ_COUNT
    registers[0] = 103
    registers[14] = 4200
    registers[15] = 0
    source = SolarEdgeModbusSource.__new__(SolarEdgeModbusSource)
    source._inverter_model_address = 40069
    source._model_address = 40188
    source._read_meter = False
    reads = []

    def read(address, _count):
        reads.append(address)
        if address != 40069:
            raise AssertionError("il meter SolarEdge non deve essere letto")
        return Response(registers)

    source._read = read
    measurement = source.read()

    assert measurement.solar_power_w == 4200
    assert reads == [40069]


def test_modbus_read_combines_inverter_and_meter():
    class Response:
        def __init__(self, registers):
            self.registers = registers

        @staticmethod
        def isError():
            return False

    inverter = [0] * SolarEdgeModbusSource.INVERTER_READ_COUNT
    inverter[0] = 103
    inverter[14] = 5184
    inverter[15] = 0
    meter = [0] * SolarEdgeModbusSource.READ_COUNT
    meter[0] = 203
    meter[3:6] = [10, 11, 12]
    meter[6] = 0
    meter[18:22] = [1200, 300, 400, 500]
    meter[22] = 0
    source = SolarEdgeModbusSource.__new__(SolarEdgeModbusSource)
    source._inverter_model_address = 40069
    source._model_address = 40188
    source._expected_phases = 3
    source._power_sign = 1

    def read(address, _count):
        return Response(inverter if address == 40069 else meter)

    source._read = read
    measurement = source.read()
    assert measurement.solar_power_w == 5184
    assert measurement.total_power_w == 1200
    assert measurement.import_power_w == 1200
    assert measurement.export_power_w == 0
    assert measurement.phase_power_w == (300, 400, 500)


def test_modbus_rejects_single_phase_meter():
    registers = [0] * 23
    registers[0] = 201
    with pytest.raises(ValueError, match="EXPECTED_PHASES=3"):
        SolarEdgeModbusSource.decode_meter_registers(registers, 3)


def test_alfa_modbus_decodes_meter_snapshot():
    measurement = AlfaModbusSource.decode_registers(
        import_power_w=300,
        export_power_w=1200,
        solar_power_w=5200,
        quarter_hour_import_power_w=250,
        quarter_hour_export_power_w=900,
        power_limit_remaining_seconds=120,
        current_tariff=2,
        event_timestamp_raw=2401011200,
        imported_energy=[0, 12345],
        exported_energy=[0, 6789],
        produced_energy=[1, 10],
        imported_energy_by_tariff=([0, 100], [0, 200], [0, 300]),
        exported_energy_by_tariff=([0, 10], [0, 20], [0, 30]),
    )
    assert measurement.source == "alfa-modbus"
    assert measurement.import_power_w == 300
    assert measurement.export_power_w == 1200
    assert measurement.solar_power_w == 5200
    assert measurement.total_power_w == -900
    assert measurement.total_consumption_w is None
    assert measurement.imported_energy_wh == 12345
    assert measurement.exported_energy_wh == 6789
    assert measurement.produced_energy_wh == 65546
    assert measurement.quarter_hour_import_power_w == 250
    assert measurement.quarter_hour_export_power_w == 900
    assert measurement.alfa_power_limit_remaining_seconds == 120
    assert measurement.alfa_current_tariff == 2
    assert measurement.alfa_event_timestamp_raw == 2401011200
    assert measurement.imported_energy_by_tariff_wh == (100, 200, 300)
    assert measurement.exported_energy_by_tariff_wh == (10, 20, 30)


def test_alfa_modbus_ignores_diagnostic_sentinel_values():
    measurement = AlfaModbusSource.decode_registers(
        import_power_w=300,
        export_power_w=0,
        solar_power_w=0,
        power_limit_remaining_seconds=65535,
        current_tariff=65535,
        event_timestamp_raw=0xFFFF0000,
    )

    assert measurement.alfa_power_limit_remaining_seconds is None
    assert measurement.alfa_current_tariff is None
    assert measurement.alfa_event_timestamp_raw is None


def test_alfa_modbus_retries_after_closed_socket():
    class Response:
        registers = [1234]

        @staticmethod
        def isError():
            return False

    class Client:
        def __init__(self):
            self.reads = 0
            self.closed = 0

        @staticmethod
        def connect():
            return True

        def close(self):
            self.closed += 1

        def read_holding_registers(self, *, address, count, device_id):
            self.reads += 1
            if self.reads == 1:
                raise ConnectionError("socket closed")
            assert address == 2
            assert count == 1
            assert device_id == 1
            return Response()

    source = AlfaModbusSource.__new__(AlfaModbusSource)
    source._client = Client()
    source._unit = 1

    assert source._read_registers(2, 1) == [1234]
    assert source._client.closed == 1


def test_web_dashboard_import_and_export():
    imported = SolarEdgeWebSource.parse_power_flow(
        {"grid": {"status": "import", "currentPower": 1.2}, "lastUpdateTime": "a"}
    )
    exported = SolarEdgeWebSource.parse_power_flow(
        {"grid": {"status": "export", "currentPower": 2.5}, "lastUpdateTime": "b"}
    )
    assert imported.total_power_w == 1200
    assert exported.total_power_w == -2500


def test_web_dashboard_does_not_reuse_same_sample():
    measurement = SolarEdgeWebSource.parse_power_flow(
        {"grid": {"status": "import", "currentPower": 0.5}, "lastUpdateTime": "same"},
        previous_update="same",
    )
    assert not measurement.fresh


def test_web_dashboard_supports_solar_only_site():
    measurement = SolarEdgeWebSource.parse_power_flow(
        {
            "solarProduction": {
                "currentPower": 4.56,
                "isActive": True,
                "isProducing": True,
            },
            "lastUpdateTime": "now",
        }
    )
    assert measurement.solar_power_w == 4560
    assert measurement.total_power_w == 0


def test_web_login_missing_csrf_has_structured_debug():
    class Client:
        def get(self, url, headers=None):
            return httpx.Response(
                200,
                text="<html><body>changed-login</body></html>",
                request=httpx.Request("GET", url),
            )

    source = SolarEdgeWebSource("user@example.test", "secret", 123, client=Client())
    with pytest.raises(SolarEdgeAccessError) as error:
        source.login()

    report = error.value.to_report_dict()
    assert report["phase"] == "csrf"
    assert report["endpoint"] == "https://login.solaredge.com/login"
    assert report["status_code"] == 200
    assert "changed-login" in report["response_excerpt"]
    assert "secret" not in report["response_excerpt"]


def test_web_default_headers_look_like_real_chrome():
    source = SolarEdgeWebSource("user@example.test", "secret", 123)
    headers = source.client.headers
    user_agent = headers["user-agent"]
    # Un UA Chrome reale contiene sempre il token motore e la versione completa.
    assert "(KHTML, like Gecko)" in user_agent
    assert "Chrome/149.0.0.0" in user_agent
    assert user_agent.endswith("Safari/537.36")
    # Client hints come catturati da Chrome 149 reale (HAR): brand prima
    # "Google Chrome", poi "Chromium", quindi il brand GREASE.
    assert headers["sec-ch-ua"] == (
        '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"'
    )
    assert headers["sec-ch-ua-mobile"] == "?0"
    assert headers["sec-ch-ua-platform"] == '"macOS"'
    assert headers["accept-language"].startswith("it-IT")


def test_web_authorization_url_matches_real_portal():
    url = SolarEdgeWebSource._authorization_url("CHAL")
    # Parametri e ordine osservati nel browser reale (HAR).
    assert url.startswith("https://login.solaredge.com/login?lang=it&response_type=code")
    assert "client_id=ugfnsujd3384sshcjehaphlh3" in url
    assert "scope=email+openid" in url
    assert "code_challenge_method=S256" in url
    assert url.endswith("code_challenge=CHAL")


class _RefreshClient:
    """Client fittizio che traccia le chiamate del flusso di refresh."""

    def __init__(self, token_status: int = 200, session_status: int = 200) -> None:
        self.calls: list[tuple] = []
        self.token_status = token_status
        self.session_status = session_status

    def post(self, url, data=None, json=None, headers=None):
        self.calls.append((url, data, json))
        if url.endswith("/oauth2/token"):
            body = {"access_token": "new"} if self.token_status < 400 else {}
            return httpx.Response(
                self.token_status, json=body, request=httpx.Request("POST", url)
            )
        if url.endswith("/services/auth/token"):
            return httpx.Response(
                self.session_status, json={}, request=httpx.Request("POST", url)
            )
        raise AssertionError(f"URL inatteso: {url}")


def test_web_refresh_reuses_token_without_resubmitting_credentials():
    client = _RefreshClient()
    source = SolarEdgeWebSource("u", "p", 1, client=client)
    source._token = {"access_token": "old", "refresh_token": "r0"}

    assert source._refresh() is True
    assert source._authenticated

    token_call = next(c for c in client.calls if c[0].endswith("/oauth2/token"))
    assert token_call[1]["grant_type"] == "refresh_token"
    assert token_call[1]["refresh_token"] == "r0"
    # Il refresh_token viene conservato per il rinnovo successivo.
    assert source._token["refresh_token"] == "r0"


def test_web_refresh_falls_back_without_stored_token():
    source = SolarEdgeWebSource("u", "p", 1, client=_RefreshClient())
    assert source._refresh() is False


def test_web_refresh_falls_back_on_http_error():
    source = SolarEdgeWebSource("u", "p", 1, client=_RefreshClient(token_status=400))
    source._token = {"refresh_token": "r0"}
    assert source._refresh() is False


class _CountingPowerFlowClient:
    """Client fittizio che conta le GET al power-flow e restituisce un payload valido."""

    def __init__(self) -> None:
        self.get_calls = 0

    def get(self, url, headers=None):
        self.get_calls += 1
        return httpx.Response(
            200,
            json={
                "grid": {"status": "import", "currentPower": 1.0},
                "solarProduction": {"currentPower": 2.0, "isActive": True, "isProducing": True},
                "lastUpdateTime": f"t{self.get_calls}",
            },
            request=httpx.Request("GET", url),
        )


def test_web_read_throttles_portal_requests():
    client = _CountingPowerFlowClient()
    source = SolarEdgeWebSource("u", "p", 1, client=client, minimum_interval_seconds=300)
    source._authenticated = True

    first = source.read()
    assert client.get_calls == 1
    assert first.fresh is True
    assert first.solar_power_w == 2000

    # Entro l'intervallo: misura riusata, nessuna nuova richiesta al portale.
    second = source.read()
    assert client.get_calls == 1
    assert second.fresh is False
    assert second.solar_power_w == first.solar_power_w

    # Intervallo scaduto: nuova richiesta al portale.
    source._last_read_monotonic -= 301
    source.read()
    assert client.get_calls == 2
