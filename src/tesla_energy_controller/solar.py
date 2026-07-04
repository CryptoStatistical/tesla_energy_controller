from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import os
import re
import secrets
import time
from dataclasses import replace
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx

from .models import GridMeasurement

LOG = logging.getLogger("tesla_energy_controller.solar")


class SolarEdgeAccessError(RuntimeError):
    """Errore SolarEdge con diagnostica sicura per log, dashboard e report mail."""

    def __init__(
        self,
        message: str,
        *,
        phase: str,
        endpoint: str | None = None,
        status_code: int | None = None,
        response_excerpt: str | None = None,
        hints: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.phase = phase
        self.endpoint = _safe_url(endpoint)
        self.status_code = status_code
        self.response_excerpt = response_excerpt
        self.hints = hints

    def to_report_dict(self) -> dict:
        payload = {
            "component": "solaredge",
            "phase": self.phase,
            "message": str(self),
            "endpoint": self.endpoint,
            "status_code": self.status_code,
            "hints": list(self.hints),
        }
        if self.response_excerpt:
            payload["response_excerpt"] = self.response_excerpt
        return {key: value for key, value in payload.items() if value is not None and value != ""}


def _safe_url(url: object) -> str | None:
    if not url:
        return None
    parsed = urlparse(str(url))
    if not parsed.scheme or not parsed.netloc:
        return str(url).split("?", 1)[0]
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _response_excerpt(response: httpx.Response, limit: int = 700) -> str:
    text = re.sub(r"\s+", " ", response.text or "").strip()
    return text[:limit]


def _http_error_message(exc: httpx.HTTPError) -> str:
    return f"{exc.__class__.__name__}: {exc}"


class GridSource(Protocol):
    def read(self) -> GridMeasurement: ...


class MockGridSource:
    def __init__(self, power_w: float, solar_power_w: float | None = None) -> None:
        self.power_w = power_w
        self.solar_power_w = solar_power_w

    def read(self) -> GridMeasurement:
        return GridMeasurement(
            total_power_w=self.power_w,
            solar_power_w=self.solar_power_w,
            source="mock",
        )


class SolarEdgeCloudSource:
    """SolarEdge Monitoring API; una misura nuova al massimo ogni intervallo configurato."""

    def __init__(self, api_key: str, site_id: int, minimum_interval_seconds: int = 300) -> None:
        from solaredge import MonitoringClient

        self._client = MonitoringClient(api_key=api_key)
        self._site_id = site_id
        self._minimum_interval = minimum_interval_seconds
        self._last_read_monotonic: float | None = None
        self._last_measurement: GridMeasurement | None = None

    def read(self) -> GridMeasurement:
        now = time.monotonic()
        if (
            self._last_read_monotonic is not None
            and now - self._last_read_monotonic < self._minimum_interval
            and self._last_measurement is not None
        ):
            previous = self._last_measurement
            return GridMeasurement(
                total_power_w=previous.total_power_w,
                solar_power_w=previous.solar_power_w,
                observed_at=previous.observed_at,
                fresh=False,
                source=previous.source,
            )

        payload = self._client.get_current_power_flow(site_id=self._site_id)
        measurement = self.parse_power_flow(payload)
        self._last_read_monotonic = now
        self._last_measurement = measurement
        return measurement

    @staticmethod
    def parse_power_flow(payload: dict) -> GridMeasurement:
        flow = payload.get("siteCurrentPowerFlow", payload)
        grid = flow.get("GRID") or flow.get("grid")
        if not isinstance(grid, dict) or "currentPower" not in grid:
            raise ValueError("Risposta SolarEdge senza potenza GRID")

        direction: int | None = None
        for connection in flow.get("connections", []):
            origin = str(connection.get("from", "")).upper()
            destination = str(connection.get("to", "")).upper()
            if origin == "GRID" and destination != "GRID":
                direction = 1
                break
            if destination == "GRID" and origin != "GRID":
                direction = -1
                break
        power = float(grid["currentPower"])
        if power != 0 and direction is None:
            raise ValueError("SolarEdge non indica la direzione del flusso GRID")
        unit = str(flow.get("unit", "kW")).casefold()
        multiplier = {"w": 1.0, "kw": 1000.0, "mw": 1_000_000.0}.get(unit)
        if multiplier is None:
            raise ValueError(f"Unità SolarEdge non supportata: {unit}")
        solar = flow.get("PV") or flow.get("pv") or flow.get("solarProduction")
        solar_power_w = None
        if isinstance(solar, dict) and "currentPower" in solar:
            solar_power_w = float(solar["currentPower"]) * multiplier
        return GridMeasurement(
            total_power_w=power * multiplier * (direction or 1),
            solar_power_w=solar_power_w,
            observed_at=datetime.now(timezone.utc),
            source="solaredge-cloud",
        )


class _FirstPostFormParser(HTMLParser):
    """Estrae il primo form POST e i suoi campi nascosti dalla pagina Cognito."""

    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None
        self.hidden: dict[str, str] = {}
        self.input_names: set[str] = set()
        self._inside = False
        self._done = False

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        if tag == "form" and not self._done and not self._inside:
            if attributes.get("method", "get").casefold() == "post":
                self._inside = True
                self.action = attributes.get("action")
            return
        if tag == "input" and self._inside:
            name = attributes.get("name")
            if name:
                self.input_names.add(name)
                if attributes.get("type", "text").casefold() == "hidden":
                    self.hidden[name] = attributes.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._inside:
            self._inside = False
            self._done = True


class SolarEdgeWebSource:
    """Client dell'interfaccia web privata SolarEdge (Cognito + sessione Monitoring)."""

    CLIENT_ID = "ugfnsujd3384sshcjehaphlh3"
    COGNITO_ORIGIN = "https://login.solaredge.com"
    MONITORING_ORIGIN = "https://monitoring.solaredge.com"
    REDIRECT_URI = f"{MONITORING_ORIGIN}/mfe/auth/callback"
    _CSRF_RE = re.compile(r'name="csrf"\s+value="([^"]+)"')

    # L'integrazione è non ufficiale: ogni richiesta deve presentarsi come Chrome
    # reale (UA completo + client hints) per non essere filtrata dal portale.
    # Valori allineati a una sessione browser reale catturata (HAR) su macOS.
    _CHROME_MAJOR = "149"
    _CHROME_VERSION = f"{_CHROME_MAJOR}.0.0.0"
    _BROWSER_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{_CHROME_VERSION} Safari/537.36"
        ),
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        # Ordine e brand GREASE come inviati da Chrome 149 reale.
        "sec-ch-ua": (
            f'"Google Chrome";v="{_CHROME_MAJOR}", '
            f'"Chromium";v="{_CHROME_MAJOR}", '
            '"Not)A;Brand";v="24"'
        ),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    }
    # XHR ricorrente del cruscotto power-flow: stessa origine, contesto fetch.
    _POWER_FLOW_HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{MONITORING_ORIGIN}/",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }

    def __init__(
        self,
        username: str,
        password: str,
        site_id: int,
        client: httpx.Client | None = None,
        session_file: str | None = None,
        minimum_interval_seconds: int = 300,
    ) -> None:
        self.username = username
        self.password = password
        self.site_id = site_id
        self.client = client or httpx.Client(
            timeout=25,
            follow_redirects=False,
            headers=dict(self._BROWSER_HEADERS),
        )
        self._authenticated = False
        self._token: dict | None = None
        self._last_update: str | None = None
        self._session_file = session_file
        # Throttle: una sola richiesta al portale per intervallo; le chiamate più
        # frequenti (es. report Tuya ogni ~30s) riusano l'ultima misura.
        self._minimum_interval = minimum_interval_seconds
        self._last_read_monotonic: float | None = None
        self._last_measurement: GridMeasurement | None = None
        self._load_session()

    @staticmethod
    def _pkce() -> tuple[str, str]:
        verifier = secrets.token_urlsafe(64)
        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        return verifier, challenge

    @classmethod
    def _authorization_url(cls, challenge: str) -> str:
        query = urlencode(
            {
                # Il portale reale usa "it" (non "it_IT"); l'ordine dei parametri
                # replica quello osservato nel browser.
                "lang": "it",
                "response_type": "code",
                "client_id": cls.CLIENT_ID,
                "scope": "email openid",
                "redirect_uri": cls.REDIRECT_URI,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
            }
        )
        return f"{cls.COGNITO_ORIGIN}/login?{query}"

    @staticmethod
    def _form(html: str) -> _FirstPostFormParser:
        parser = _FirstPostFormParser()
        parser.feed(html)
        if not parser.action:
            raise SolarEdgeAccessError(
                "Form di login SolarEdge non riconosciuto",
                phase="login-form",
                hints=(
                    "Possibile modifica del portale SolarEdge.",
                    "Verificare manualmente il login da browser.",
                ),
            )
        return parser

    def _authorization_code(self, response: httpx.Response) -> str:
        current = response
        for _ in range(12):
            location = current.headers.get("location")
            if location:
                target = urljoin(str(current.url), location)
                query = parse_qs(urlparse(target).query)
                if urlparse(target).path == "/mfe/auth/callback" and query.get("code"):
                    return query["code"][0]
                current = self.client.get(target)
                continue
            if current.status_code == 200:
                form = self._form(current.text)
                if {"code", "verification_code"} & form.input_names:
                    raise SolarEdgeAccessError(
                        "L'account SolarEdge richiede MFA: completare prima "
                        "l'onboarding interattivo",
                        phase="mfa-required",
                        endpoint=str(current.url),
                        status_code=current.status_code,
                        hints=(
                            "Accedere al portale SolarEdge da browser e completare MFA/verifica.",
                            "Se SolarEdge richiede sempre MFA, valutare API ufficiale o Modbus locale.",
                        ),
                    )
                if "password" in form.input_names:
                    raise SolarEdgeAccessError(
                        "Login SolarEdge rifiutato: verificare le credenziali",
                        phase="credentials-rejected",
                        endpoint=str(current.url),
                        status_code=current.status_code,
                        response_excerpt=_response_excerpt(current),
                        hints=(
                            "Verificare SOLAREDGE_USERNAME_FILE e SOLAREDGE_PASSWORD_FILE.",
                            "Provare login manuale su monitoring.solaredge.com.",
                        ),
                    )
            try:
                current.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise SolarEdgeAccessError(
                    f"SolarEdge ha restituito HTTP {current.status_code} durante il login",
                    phase="authorization-redirect",
                    endpoint=str(current.url),
                    status_code=current.status_code,
                    response_excerpt=_response_excerpt(current),
                    hints=("Verificare connettività Internet e disponibilità del portale SolarEdge.",),
                ) from exc
            raise SolarEdgeAccessError(
                "Flusso di login SolarEdge inatteso",
                phase="authorization-flow",
                endpoint=str(current.url),
                status_code=current.status_code,
                response_excerpt=_response_excerpt(current),
                hints=("Possibile modifica del portale SolarEdge.",),
            )
        raise SolarEdgeAccessError(
            "Troppi redirect durante il login SolarEdge",
            phase="authorization-redirects",
            hints=("Verificare che il portale SolarEdge non stia cambiando flusso di login.",),
        )

    def login(self) -> None:
        verifier, challenge = self._pkce()
        authorization_url = self._authorization_url(challenge)
        try:
            page = self.client.get(
                authorization_url,
                headers={
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;q=0.9,"
                        "image/avif,image/webp,image/apng,*/*;q=0.8"
                    ),
                    # Nel browser reale la pagina di login è raggiunta da
                    # monitoring.solaredge.com (stesso dominio registrabile).
                    "Referer": f"{self.MONITORING_ORIGIN}/",
                    "Sec-Fetch-Site": "same-site",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-User": "?1",
                    "Sec-Fetch-Dest": "document",
                    "Upgrade-Insecure-Requests": "1",
                },
            )
        except httpx.HTTPError as exc:
            raise SolarEdgeAccessError(
                "Impossibile aprire la pagina di login SolarEdge",
                phase="authorization-page",
                endpoint=authorization_url,
                response_excerpt=_http_error_message(exc),
                hints=(
                    "Verificare Internet/DNS dalla macchina o Raspberry.",
                    "Verificare eventuali firewall/proxy.",
                ),
            ) from exc
        if page.status_code >= 400:
            raise SolarEdgeAccessError(
                f"SolarEdge login page HTTP {page.status_code}",
                phase="authorization-page",
                endpoint=str(page.url),
                status_code=page.status_code,
                response_excerpt=_response_excerpt(page),
                hints=("Verificare disponibilità del portale SolarEdge.",),
            )
        match = self._CSRF_RE.search(page.text)
        if not match:
            raise SolarEdgeAccessError(
                "Token CSRF SolarEdge non trovato",
                phase="csrf",
                endpoint=str(page.url),
                status_code=page.status_code,
                response_excerpt=_response_excerpt(page),
                hints=(
                    "Possibile modifica HTML del portale SolarEdge.",
                    "Verificare manualmente il login da browser.",
                ),
            )
        csrf = html.unescape(match.group(1))
        login_endpoint = f"{authorization_url}&_data=routes%2Flogin"
        try:
            response = self.client.post(
                login_endpoint,
                data={
                    # Il form Remix richiede ogni valore come stringa JSON.
                    "username": json.dumps(self.username),
                    "password": json.dumps(self.password),
                    "csrf": json.dumps(csrf),
                    "cognitoAsfData": json.dumps(""),
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                    "Origin": self.COGNITO_ORIGIN,
                    "Referer": str(page.url),
                    "Accept": "application/json, text/plain, */*",
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Dest": "empty",
                },
            )
        except httpx.HTTPError as exc:
            raise SolarEdgeAccessError(
                "Invio credenziali SolarEdge non riuscito",
                phase="credentials-submit",
                endpoint=login_endpoint,
                response_excerpt=_http_error_message(exc),
                hints=("Verificare connettività e raggiungibilità di login.solaredge.com.",),
            ) from exc
        if response.status_code >= 400:
            raise SolarEdgeAccessError(
                f"SolarEdge credentials submit HTTP {response.status_code}",
                phase="credentials-submit",
                endpoint=str(response.url),
                status_code=response.status_code,
                response_excerpt=_response_excerpt(response),
                hints=("Verificare credenziali SolarEdge e stato account.",),
            )
        redirect = response.headers.get("x-remix-redirect", "")
        code = parse_qs(urlparse(redirect).query).get("code", [None])[0]
        if not code:
            raise SolarEdgeAccessError(
                "Login SolarEdge rifiutato o verifica aggiuntiva richiesta",
                phase="authorization-code",
                endpoint=str(response.url),
                status_code=response.status_code,
                response_excerpt=_response_excerpt(response),
                hints=(
                    "Verificare username/password.",
                    "Controllare se SolarEdge richiede MFA, cambio password o consenso da browser.",
                ),
            )
        token_endpoint = f"{self.COGNITO_ORIGIN}/oauth2/token"
        try:
            token_response = self.client.post(
                token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "client_id": self.CLIENT_ID,
                    "redirect_uri": self.REDIRECT_URI,
                    "code": code,
                    "code_verifier": verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise SolarEdgeAccessError(
                "Scambio token SolarEdge non riuscito",
                phase="token-exchange",
                endpoint=token_endpoint,
                response_excerpt=_http_error_message(exc),
                hints=("Verificare disponibilità di login.solaredge.com.",),
            ) from exc
        if token_response.status_code >= 400:
            raise SolarEdgeAccessError(
                f"Token SolarEdge HTTP {token_response.status_code}",
                phase="token-exchange",
                endpoint=str(token_response.url),
                status_code=token_response.status_code,
                response_excerpt=_response_excerpt(token_response),
                hints=("Il codice OAuth potrebbe essere scaduto o il flusso SolarEdge cambiato.",),
            )
        try:
            token = token_response.json()
        except json.JSONDecodeError as exc:
            raise SolarEdgeAccessError(
                "Risposta token SolarEdge non JSON",
                phase="token-json",
                endpoint=str(token_response.url),
                status_code=token_response.status_code,
                response_excerpt=_response_excerpt(token_response),
                hints=("Possibile modifica o errore temporaneo del servizio SolarEdge.",),
            ) from exc
        self._open_monitoring_session(token)
        self._token = token
        self._save_session()

    def _open_monitoring_session(self, token: dict) -> None:
        """Scambia il token OAuth con il cookie di sessione Monitoring."""
        session_endpoint = f"{self.MONITORING_ORIGIN}/services/auth/token"
        try:
            session_response = self.client.post(
                session_endpoint,
                json=token,
                headers={
                    "Origin": self.MONITORING_ORIGIN,
                    "Referer": self.REDIRECT_URI,
                    "Accept": "application/json, text/plain, */*",
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Dest": "empty",
                },
            )
        except httpx.HTTPError as exc:
            raise SolarEdgeAccessError(
                "Creazione sessione Monitoring SolarEdge non riuscita",
                phase="monitoring-session",
                endpoint=session_endpoint,
                response_excerpt=_http_error_message(exc),
                hints=("Verificare raggiungibilità di monitoring.solaredge.com.",),
            ) from exc
        if session_response.status_code >= 400:
            raise SolarEdgeAccessError(
                f"Sessione Monitoring SolarEdge HTTP {session_response.status_code}",
                phase="monitoring-session",
                endpoint=str(session_response.url),
                status_code=session_response.status_code,
                response_excerpt=_response_excerpt(session_response),
                hints=("Verificare permessi dell'utente SolarEdge sul sito configurato.",),
            )
        self._authenticated = True

    def _load_session(self) -> None:
        """Carica un token SolarEdge persistito per evitare il login completo al riavvio."""
        if not self._session_file:
            return
        try:
            with open(self._session_file, encoding="utf-8") as handle:
                token = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(token, dict) and token.get("refresh_token"):
            self._token = token

    def _save_session(self) -> None:
        """Persiste il token corrente (scrittura atomica, permessi 0600). Best-effort."""
        if not self._session_file or not self._token:
            return
        path = Path(self._session_file)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(self._token), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError:
            # La persistenza è solo un'ottimizzazione: un errore non blocca il controllo.
            pass

    def _refresh(self) -> bool:
        """Rinnova la sessione riusando il refresh_token, senza reinviare le credenziali.

        Tenere viva la sessione con il flusso minimo (oauth2/token +
        services/auth/token) riduce le richieste verso il portale e l'esposizione
        ai controlli anti-bot. Se il refresh non è possibile si ritorna False e il
        chiamante ripiega sul login completo.
        """
        refresh_token = (self._token or {}).get("refresh_token")
        if not refresh_token:
            return False
        try:
            response = self.client.post(
                f"{self.COGNITO_ORIGIN}/oauth2/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": self.CLIENT_ID,
                    "refresh_token": refresh_token,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError:
            return False
        if response.status_code >= 400:
            return False
        try:
            refreshed = response.json()
        except json.JSONDecodeError:
            return False
        if "access_token" not in refreshed:
            return False
        # Cognito non rinvia il refresh_token sul grant di refresh: si conserva.
        refreshed.setdefault("refresh_token", refresh_token)
        try:
            self._open_monitoring_session(refreshed)
        except SolarEdgeAccessError:
            return False
        self._token = refreshed
        self._save_session()
        return True

    @staticmethod
    def parse_power_flow(payload: dict, previous_update: str | None = None) -> GridMeasurement:
        grid = payload.get("grid")
        solar = payload.get("solarProduction")
        grid_power_w = 0.0
        if isinstance(grid, dict) and "currentPower" in grid:
            status = str(grid.get("status", "")).casefold()
            if status == "import":
                sign = 1
            elif status == "export":
                sign = -1
            elif status in {"idle", "grid-outage"} and float(grid["currentPower"]) == 0:
                sign = 1
            else:
                raise ValueError(f"Direzione GRID SolarEdge non riconosciuta: {status}")
            grid_power_w = float(grid["currentPower"]) * 1000 * sign
        solar_power_w = None
        if isinstance(solar, dict) and "currentPower" in solar:
            solar_power_w = float(solar["currentPower"]) * 1000
        if solar_power_w is None and not isinstance(grid, dict):
            raise ValueError("Dashboard SolarEdge senza produzione FV né potenza di rete")
        update = str(payload.get("lastUpdateTime") or "")
        return GridMeasurement(
            # Il widget power-flow espone currentPower in kW.
            total_power_w=grid_power_w,
            solar_power_w=solar_power_w,
            observed_at=datetime.now(timezone.utc),
            fresh=not update or update != previous_update,
            source="solaredge-web",
        )

    def read(self) -> GridMeasurement:
        now = time.monotonic()
        if (
            self._last_read_monotonic is not None
            and now - self._last_read_monotonic < self._minimum_interval
            and self._last_measurement is not None
        ):
            previous = self._last_measurement
            return GridMeasurement(
                total_power_w=previous.total_power_w,
                solar_power_w=previous.solar_power_w,
                observed_at=previous.observed_at,
                fresh=False,
                source=previous.source,
            )
        if not self._authenticated:
            # Riusa la sessione persistita (refresh leggero) prima del login completo.
            if not (self._token and self._refresh()):
                self.login()
        url = f"{self.MONITORING_ORIGIN}/services/dashboard/power-flow/v2/sites/{self.site_id}"
        try:
            response = self.client.get(url, headers=self._POWER_FLOW_HEADERS)
        except httpx.HTTPError as exc:
            raise SolarEdgeAccessError(
                "Lettura power-flow SolarEdge non riuscita",
                phase="power-flow",
                endpoint=url,
                response_excerpt=_http_error_message(exc),
                hints=("Verificare Internet e disponibilità di monitoring.solaredge.com.",),
            ) from exc
        if response.status_code in {401, 403}:
            self._authenticated = False
            if not self._refresh():
                self.login()
            try:
                response = self.client.get(url, headers=self._POWER_FLOW_HEADERS)
            except httpx.HTTPError as exc:
                raise SolarEdgeAccessError(
                    "Lettura power-flow SolarEdge non riuscita dopo nuovo login",
                    phase="power-flow-retry",
                    endpoint=url,
                    response_excerpt=_http_error_message(exc),
                    hints=("Verificare sessione/permessi SolarEdge.",),
                ) from exc
        if response.status_code >= 400:
            raise SolarEdgeAccessError(
                f"Power-flow SolarEdge HTTP {response.status_code}",
                phase="power-flow",
                endpoint=str(response.url),
                status_code=response.status_code,
                response_excerpt=_response_excerpt(response),
                hints=(
                    "Verificare SOLAREDGE_SITE_ID.",
                    "Verificare che l'utente abbia accesso al sito nel portale SolarEdge.",
                ),
            )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise SolarEdgeAccessError(
                "Risposta power-flow SolarEdge non JSON",
                phase="power-flow-json",
                endpoint=str(response.url),
                status_code=response.status_code,
                response_excerpt=_response_excerpt(response),
                hints=("Possibile modifica API privata SolarEdge o errore temporaneo.",),
            ) from exc
        measurement = self.parse_power_flow(payload, self._last_update)
        update = str(payload.get("lastUpdateTime") or "")
        if update:
            self._last_update = update
        self._last_read_monotonic = now
        self._last_measurement = measurement
        return measurement


class SolarEdgeModbusSource:
    """Legge SolarEdge via SunSpec Modbus TCP.

    Lo StorEdge espone la produzione FV nel modello inverter 101-104. Se è
    presente anche il modello meter 201-204 lo usiamo per import/export; in caso
    contrario evitiamo fallback web e lasciamo la rete come non disponibile.
    """

    # Nel modello meter: DID, length, correnti (5 + SF), tensioni, frequenza,
    # potenze totale/A/B/C + SF. Gli offset sono relativi al DID del meter.
    INVERTER_READ_COUNT = 33
    METER_MODEL_OFFSET = 67
    READ_COUNT = 23

    def __init__(
        self,
        host: str,
        port: int,
        unit: int,
        meter_base: int,
        expected_phases: int,
        power_sign: int,
        inverter_base: int = 40069,
    ) -> None:
        from pymodbus.client import ModbusTcpClient

        self._client_factory = lambda: ModbusTcpClient(host=host, port=port, timeout=5)
        self._client = self._client_factory()
        self._host = host
        self._port = port
        self._unit = unit
        self._inverter_model_address = inverter_base
        self._model_address = meter_base + self.METER_MODEL_OFFSET
        self._expected_phases = expected_phases
        self._power_sign = power_sign
        self._retry_attempts = 3
        self._retry_delay_seconds = 1.0

    @staticmethod
    def _signed(value: int) -> int:
        return value - 0x10000 if value & 0x8000 else value

    @classmethod
    def decode_inverter_registers(cls, registers: list[int]) -> GridMeasurement:
        if len(registers) < 16:
            raise ValueError("Blocco Modbus inverter SolarEdge incompleto")
        model_id = registers[0]
        if model_id not in {101, 102, 103, 104}:
            raise ValueError(f"Modello inverter SunSpec non supportato: {model_id}")
        power_raw = cls._signed(registers[14])
        power_sf = cls._signed(registers[15])
        if power_raw == -32768 or power_sf == -32768:
            raise ValueError("Potenza inverter SolarEdge non disponibile")
        return GridMeasurement(
            total_power_w=0.0,
            solar_power_w=max(power_raw * (10.0**power_sf), 0.0),
            observed_at=datetime.now(timezone.utc),
            source="solaredge-modbus",
        )

    @classmethod
    def decode_meter_registers(
        cls, registers: list[int], expected_phases: int, power_sign: int = 1
    ) -> GridMeasurement:
        if len(registers) < cls.READ_COUNT:
            raise ValueError("Blocco Modbus SolarEdge incompleto")
        model_id = registers[0]
        actual_phases = 3 if model_id in {203, 204} else 1 if model_id == 201 else 2
        if actual_phases != expected_phases:
            raise ValueError(
                f"Il contatore SunSpec è modello {model_id} ({actual_phases} fasi), "
                f"ma EXPECTED_PHASES={expected_phases}"
            )
        current_sf = cls._signed(registers[6])
        power_sf = cls._signed(registers[22])
        if current_sf == -32768 or power_sf == -32768:
            raise ValueError("Scale factor Modbus SolarEdge non disponibile")
        current_scale = 10.0**current_sf
        power_scale = 10.0**power_sf * power_sign
        phase_count = 3 if actual_phases == 3 else 1
        currents = tuple(cls._signed(registers[3 + i]) * current_scale for i in range(phase_count))
        powers = tuple(cls._signed(registers[19 + i]) * power_scale for i in range(phase_count))
        total = cls._signed(registers[18]) * power_scale
        return GridMeasurement(
            total_power_w=total,
            import_power_w=max(total, 0.0),
            export_power_w=max(-total, 0.0),
            phase_power_w=powers,
            phase_current_a=currents,
            observed_at=datetime.now(timezone.utc),
            source="solaredge-modbus",
        )

    def _read(self, address: int, count: int):
        def read_once():
            if not self._client.connect():
                raise ConnectionError("Connessione Modbus SolarEdge non riuscita")
            try:
                return self._client.read_holding_registers(
                    address=address, count=count, device_id=self._unit
                )
            except TypeError:  # compatibilità pymodbus 3.7/3.8
                return self._client.read_holding_registers(
                    address=address, count=count, slave=self._unit
                )

        last_exc: Exception | None = None
        attempts = max(int(getattr(self, "_retry_attempts", 2) or 2), 1)
        for attempt in range(attempts):
            try:
                return read_once()
            except Exception as exc:
                last_exc = exc
                self._client.close()
                factory = getattr(self, "_client_factory", None)
                if factory is not None:
                    self._client = factory()
                if attempt < attempts - 1:
                    delay = float(getattr(self, "_retry_delay_seconds", 0.0) or 0.0)
                    if delay > 0:
                        time.sleep(delay)
                    continue

        assert last_exc is not None
        if isinstance(last_exc, ConnectionError) and "Connessione Modbus SolarEdge" in str(
            last_exc
        ):
            raise SolarEdgeAccessError(
                "Connessione Modbus SolarEdge non riuscita",
                phase="modbus-connect",
                endpoint=f"tcp://{self._host}:{self._port}",
                response_excerpt=f"{type(last_exc).__name__}: {last_exc}",
                hints=(
                    "Verificare rotta Raspberry verso StorEdge/Mikrotik.",
                    "Verificare che Modbus TCP sia attivo sull'inverter SolarEdge.",
                ),
            ) from last_exc
        raise SolarEdgeAccessError(
            "Lettura Modbus SolarEdge non riuscita",
            phase=f"modbus-read-{address}",
            endpoint=f"tcp://{self._host}:{self._port}",
            response_excerpt=f"{type(last_exc).__name__}: {last_exc}",
            hints=(
                "Verificare stabilità rete tra Raspberry, Mikrotik e StorEdge.",
                "Se l'errore persiste, riaprire/riavviare Modbus TCP sull'inverter.",
            ),
        ) from last_exc

    def read(self) -> GridMeasurement:
        inverter_response = self._read(self._inverter_model_address, self.INVERTER_READ_COUNT)
        if inverter_response.isError():
            raise SolarEdgeAccessError(
                "Errore Modbus SolarEdge inverter",
                phase="modbus-inverter",
                endpoint=f"tcp://{self._host}:{self._port}",
                response_excerpt=str(inverter_response),
                hints=("Verificare unit ID e registri SunSpec SolarEdge.",),
            )
        inverter = self.decode_inverter_registers(list(inverter_response.registers))

        try:
            meter_response = self._read(self._model_address, self.READ_COUNT)
            if meter_response.isError():
                raise ConnectionError(f"Errore Modbus SolarEdge meter: {meter_response}")
            meter = self.decode_meter_registers(
                list(meter_response.registers), self._expected_phases, self._power_sign
            )
        except Exception as exc:
            LOG.debug("meter SolarEdge Modbus non disponibile, uso solo inverter FV: %s", exc)
            return inverter

        return replace(
            inverter,
            total_power_w=meter.total_power_w,
            import_power_w=meter.import_power_w,
            export_power_w=meter.export_power_w,
            phase_power_w=meter.phase_power_w,
            phase_current_a=meter.phase_current_a,
        )


class AlfaModbusSource:
    """Legge il contatore ALFA by Sinapsi via Modbus TCP.

    Registri da configurazione Home Assistant ALFA pubblica:
    2 import W, 12 export W, 921 produzione FV W, 5/15/924 energie totali Wh.
    Altri registri non invasivi: 9/19 medie quartorarie, 203 tempo distacco,
    30/32/34 e 54/56/58 energie F1/F2/F3 giorno precedente, 780/782 diagnostica evento.
    """

    IMPORT_POWER_W = 2
    EXPORT_POWER_W = 12
    SOLAR_POWER_W = 921
    QUARTER_HOUR_IMPORT_POWER_W = 9
    QUARTER_HOUR_EXPORT_POWER_W = 19
    IMPORTED_ENERGY_WH = 5
    EXPORTED_ENERGY_WH = 15
    PRODUCED_ENERGY_WH = 924
    POWER_LIMIT_REMAINING_SECONDS = 203
    IMPORTED_ENERGY_TARIFF_WH = (30, 32, 34)
    EXPORTED_ENERGY_TARIFF_WH = (54, 56, 58)
    CURRENT_TARIFF = 780
    EVENT_TIMESTAMP_RAW = 782

    def __init__(self, host: str, port: int, unit: int, timeout_seconds: float = 5.0) -> None:
        from pymodbus.client import ModbusTcpClient

        self._client = ModbusTcpClient(host=host, port=port, timeout=timeout_seconds)
        self._unit = unit

    @staticmethod
    def _u32(registers: list[int]) -> int:
        if len(registers) != 2:
            raise ValueError("Valore uint32 ALFA incompleto")
        return (registers[0] << 16) | registers[1]

    @staticmethod
    def _optional_u16(value: int | None) -> int | None:
        if value is None or value == 0xFFFF:
            return None
        return value

    @staticmethod
    def _optional_u32(value: int | None) -> int | None:
        if value is None or value in {0xFFFFFFFF, 0xFFFF0000}:
            return None
        return value

    @classmethod
    def _tariff_energies(cls, values: tuple[list[int], ...] | None) -> tuple[float, ...]:
        if values is None:
            return ()
        return tuple(float(cls._u32(value)) for value in values)

    @classmethod
    def decode_registers(
        cls,
        *,
        import_power_w: int,
        export_power_w: int,
        solar_power_w: int,
        quarter_hour_import_power_w: int | None = None,
        quarter_hour_export_power_w: int | None = None,
        power_limit_remaining_seconds: int | None = None,
        current_tariff: int | None = None,
        event_timestamp_raw: int | None = None,
        imported_energy: list[int] | None = None,
        exported_energy: list[int] | None = None,
        produced_energy: list[int] | None = None,
        imported_energy_by_tariff: tuple[list[int], ...] | None = None,
        exported_energy_by_tariff: tuple[list[int], ...] | None = None,
    ) -> GridMeasurement:
        imported = float(max(import_power_w, 0))
        exported = float(max(export_power_w, 0))
        solar = float(max(solar_power_w, 0))
        return GridMeasurement(
            total_power_w=imported - exported,
            solar_power_w=solar,
            import_power_w=imported,
            export_power_w=exported,
            imported_energy_wh=(
                float(cls._u32(imported_energy)) if imported_energy is not None else None
            ),
            exported_energy_wh=(
                float(cls._u32(exported_energy)) if exported_energy is not None else None
            ),
            produced_energy_wh=(
                float(cls._u32(produced_energy)) if produced_energy is not None else None
            ),
            quarter_hour_import_power_w=(
                float(max(quarter_hour_import_power_w, 0))
                if quarter_hour_import_power_w is not None
                else None
            ),
            quarter_hour_export_power_w=(
                float(max(quarter_hour_export_power_w, 0))
                if quarter_hour_export_power_w is not None
                else None
            ),
            alfa_power_limit_remaining_seconds=(
                float(cls._optional_u16(power_limit_remaining_seconds))
                if cls._optional_u16(power_limit_remaining_seconds) is not None
                else None
            ),
            alfa_current_tariff=cls._optional_u16(current_tariff),
            alfa_event_timestamp_raw=cls._optional_u32(event_timestamp_raw),
            imported_energy_by_tariff_wh=cls._tariff_energies(imported_energy_by_tariff),
            exported_energy_by_tariff_wh=cls._tariff_energies(exported_energy_by_tariff),
            observed_at=datetime.now(timezone.utc),
            source="alfa-modbus",
        )

    def _read(self, address: int, count: int):
        def read_once():
            if not self._client.connect():
                raise ConnectionError("Connessione Modbus ALFA non riuscita")
            try:
                return self._client.read_holding_registers(
                    address=address, count=count, device_id=self._unit
                )
            except TypeError:  # compatibilità pymodbus 3.7/3.8
                return self._client.read_holding_registers(
                    address=address, count=count, slave=self._unit
                )

        try:
            return read_once()
        except Exception:
            self._client.close()
            return read_once()

    def _read_registers(self, address: int, count: int) -> list[int]:
        response = self._read(address, count)
        if response.isError():
            raise ConnectionError(f"Errore Modbus ALFA address={address}: {response}")
        return list(response.registers)

    def _read_registers_optional(self, address: int, count: int) -> list[int] | None:
        try:
            return self._read_registers(address, count)
        except ConnectionError:
            return None

    def _read_one_optional(self, address: int) -> int | None:
        registers = self._read_registers_optional(address, 1)
        return registers[0] if registers else None

    def _read_u32_optional(self, address: int) -> list[int] | None:
        return self._read_registers_optional(address, 2)

    def _read_u32_value_optional(self, address: int) -> int | None:
        registers = self._read_u32_optional(address)
        return self._u32(registers) if registers is not None else None

    def read(self) -> GridMeasurement:
        import_power = self._read_registers(self.IMPORT_POWER_W, 1)[0]
        export_power = self._read_registers(self.EXPORT_POWER_W, 1)[0]
        solar_power = self._read_registers(self.SOLAR_POWER_W, 1)[0]
        return self.decode_registers(
            import_power_w=import_power,
            export_power_w=export_power,
            solar_power_w=solar_power,
            quarter_hour_import_power_w=self._read_one_optional(
                self.QUARTER_HOUR_IMPORT_POWER_W
            ),
            quarter_hour_export_power_w=self._read_one_optional(
                self.QUARTER_HOUR_EXPORT_POWER_W
            ),
            power_limit_remaining_seconds=self._read_one_optional(
                self.POWER_LIMIT_REMAINING_SECONDS
            ),
            current_tariff=self._read_one_optional(self.CURRENT_TARIFF),
            event_timestamp_raw=self._read_u32_value_optional(self.EVENT_TIMESTAMP_RAW),
            imported_energy=self._read_registers(self.IMPORTED_ENERGY_WH, 2),
            exported_energy=self._read_registers(self.EXPORTED_ENERGY_WH, 2),
            produced_energy=self._read_registers(self.PRODUCED_ENERGY_WH, 2),
            imported_energy_by_tariff=tuple(
                registers
                for address in self.IMPORTED_ENERGY_TARIFF_WH
                if (registers := self._read_u32_optional(address)) is not None
            ),
            exported_energy_by_tariff=tuple(
                registers
                for address in self.EXPORTED_ENERGY_TARIFF_WH
                if (registers := self._read_u32_optional(address)) is not None
            ),
        )
