from __future__ import annotations

import html
import json
import logging
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from .solar import SolarEdgeAccessError
from .smtp_mailer import send_mail as send_smtp_mail
from .wordpress_mailer import send_mail as send_wordpress_mail

LOG = logging.getLogger("tesla_energy_controller.diagnostics")


def diagnostic_payload(exc: Exception) -> dict[str, Any]:
    if hasattr(exc, "to_report_dict"):
        payload = exc.to_report_dict()
    else:
        payload = {
            "component": "application",
            "message": str(exc),
            "exception_type": exc.__class__.__name__,
        }
    payload.setdefault("exception_type", exc.__class__.__name__)
    payload.setdefault("occurred_at", datetime.now(timezone.utc).astimezone().isoformat())
    return payload


# Email impaginata in HTML "safe" (inline style): l'endpoint WordPress accetta
# "plain text or safe HTML" e wp_kses conserva solo tag e proprietà CSS basilari,
# quindi niente <style>, niente classi, solo stili inline su tag standard.
_EMAIL_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_MONO_FONT = "'SF Mono',Menlo,Consolas,'Liberation Mono',monospace"


def _html_value(value: Any) -> str:
    """Escape per email mantenendo gli a-capo come <br>."""
    return html.escape(str(value)).replace("\n", "<br>")


def _kv_table(rows: list[tuple[str, Any]]) -> str:
    cells = "".join(
        "<tr>"
        '<td style="padding:9px 14px;background:#f1f5fb;border:1px solid #e2e8f2;'
        'font-weight:600;color:#1f4e8c;white-space:nowrap;vertical-align:top;">'
        f"{html.escape(str(label))}</td>"
        '<td style="padding:9px 14px;border:1px solid #e2e8f2;color:#1c2533;'
        f'vertical-align:top;">{_html_value(value)}</td>'
        "</tr>"
        for label, value in rows
    )
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;width:100%;font-size:14px;line-height:1.5;'
        f'font-family:{_EMAIL_FONT};">{cells}</table>'
    )


def _section(title: str, inner_html: str) -> str:
    return (
        '<div style="margin-top:20px;">'
        '<div style="font-size:12px;font-weight:700;text-transform:uppercase;'
        'letter-spacing:0.5px;color:#5a6b85;margin-bottom:8px;">'
        f"{html.escape(title)}</div>{inner_html}</div>"
    )


def _code_block(text: str) -> str:
    return (
        '<pre style="margin:0;background:#0f1b2d;color:#d6e2f5;padding:14px 16px;'
        f"border-radius:6px;font-family:{_MONO_FONT};font-size:12px;line-height:1.5;"
        f'white-space:pre-wrap;">{html.escape(text)}</pre>'
    )


def _email_shell(subtitle: str, content_html: str) -> str:
    return (
        f'<div style="margin:0;padding:24px 12px;background:#eef2f8;font-family:{_EMAIL_FONT};">'
        '<table role="presentation" align="center" cellpadding="0" cellspacing="0" '
        'style="width:600px;max-width:100%;margin:0 auto;background:#ffffff;'
        'border:1px solid #e2e8f2;border-radius:10px;border-collapse:separate;">'
        '<tr><td style="background:#1f4e8c;padding:20px 24px;border-radius:10px 10px 0 0;">'
        '<div style="font-size:18px;font-weight:700;color:#ffffff;">'
        "⚡ Tesla Energy Controller</div>"
        '<div style="font-size:13px;color:#bcd0ee;margin-top:3px;">'
        f"{html.escape(subtitle)}</div></td></tr>"
        f'<tr><td style="padding:22px 24px;">{content_html}</td></tr>'
        '<tr><td style="padding:14px 24px;border-top:1px solid #eef2f8;color:#8893a8;'
        'font-size:12px;border-radius:0 0 10px 10px;">'
        "Messaggio automatico del servizio Tesla Energy Controller.</td></tr>"
        "</table></div>"
    )


def render_error_report(exc: Exception, context: dict[str, Any] | None = None) -> str:
    """Corpo HTML impaginato del report errore inviato via email."""
    payload = diagnostic_payload(exc)
    rows: list[tuple[str, Any]] = [
        ("Quando", payload.get("occurred_at")),
        ("Componente", payload.get("component")),
        ("Fase", payload.get("phase", "-")),
        ("Tipo eccezione", payload.get("exception_type")),
        ("Messaggio", payload.get("message")),
    ]
    for key, label in (
        ("endpoint", "Endpoint"),
        ("status_code", "Codice HTTP"),
        ("response_excerpt", "Risposta"),
    ):
        if payload.get(key) not in {None, ""}:
            rows.append((label, payload[key]))

    sections = [_kv_table(rows)]

    hints = payload.get("hints") or []
    if hints:
        items = "".join(
            f'<li style="margin-bottom:5px;">{_html_value(hint)}</li>' for hint in hints
        )
        sections.append(
            _section(
                "Suggerimenti",
                '<ul style="margin:0;padding-left:20px;font-size:14px;line-height:1.5;'
                f'color:#1c2533;font-family:{_EMAIL_FONT};">{items}</ul>',
            )
        )

    if context:
        sections.append(
            _section(
                "Contesto",
                _code_block(json.dumps(context, ensure_ascii=False, indent=2, sort_keys=True)),
            )
        )

    traceback_text = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    ).strip()
    sections.append(_section("Traceback", _code_block(traceback_text)))

    return _email_shell("Report errore", "".join(sections))


def render_event_report(kind: str, message: str, details: dict[str, Any] | None = None) -> str:
    """Corpo HTML impaginato di una notifica evento inviata via email."""
    occurred_at = datetime.now(timezone.utc).astimezone().isoformat()
    rows: list[tuple[str, Any]] = [
        ("Quando", occurred_at),
        ("Evento", kind),
        ("Messaggio", message),
    ]
    sections = [_kv_table(rows)]
    if details:
        detail_rows = [
            (
                key,
                value
                if isinstance(value, (str, int, float, bool))
                else json.dumps(value, ensure_ascii=False),
            )
            for key, value in details.items()
        ]
        sections.append(_section("Dettagli", _kv_table(detail_rows)))
    return _email_shell("Notifica evento", "".join(sections))


@dataclass
class EmailReportResult:
    attempted: bool
    sent: bool
    message: str


class MailSender(Protocol):
    recipients: tuple[str, ...]
    backend_name: str

    def missing(self) -> tuple[str, ...]: ...

    def send(self, subject: str, message: str) -> None: ...


class WordPressSender:
    backend_name = "WordPress"

    def __init__(
        self,
        *,
        recipients: tuple[str, ...],
        api_url: str | None,
        api_key: str | None,
        api_user: str | None,
        sender_name: str,
        reply_to: str,
        timeout_seconds: int,
    ) -> None:
        self.recipients = recipients
        self.api_url = api_url
        self.api_key = api_key
        self.api_user = api_user
        self.sender_name = sender_name
        self.reply_to = reply_to
        self.timeout_seconds = timeout_seconds

    def missing(self) -> tuple[str, ...]:
        missing = []
        if not self.recipients:
            missing.append("NOTIFY_RECIPIENTS")
        if not self.api_url:
            missing.append("NOTIFY_API_URL")
        if not self.api_key:
            missing.append("NOTIFY_API_KEY")
        if not self.api_user:
            missing.append("NOTIFY_API_USER")
        return tuple(missing)

    def send(self, subject: str, message: str) -> None:
        missing = self.missing()
        if missing:
            raise RuntimeError("config WordPress mail incompleta: " + ", ".join(missing))
        assert self.api_url
        assert self.api_key
        assert self.api_user
        failures = [
            recipient
            for recipient in self.recipients
            if not send_wordpress_mail(
                endpoint=self.api_url,
                api_key=self.api_key,
                basic_auth=self.api_user,
                to=recipient,
                subject=subject,
                message=message,
                sender_name=self.sender_name,
                reply_to=self.reply_to,
                timeout=self.timeout_seconds,
            )
        ]
        if failures:
            raise RuntimeError("WordPress mail fallita per " + ", ".join(failures))

    @classmethod
    def from_settings(cls, settings) -> "WordPressSender":
        return cls(
            recipients=settings.notify_recipients,
            api_url=settings.notify_api_url,
            api_key=settings.notify_api_key,
            api_user=settings.notify_api_user,
            sender_name=settings.notify_sender_name,
            reply_to=settings.notify_reply_to,
            timeout_seconds=settings.notify_timeout_seconds,
        )


class SMTPSender:
    backend_name = "SMTP"

    def __init__(
        self,
        *,
        recipients: tuple[str, ...],
        host: str | None,
        port: int,
        username: str | None,
        password: str | None,
        sender_name: str,
        from_address: str | None,
        reply_to: str,
        timeout_seconds: int,
        starttls: bool,
        ssl: bool,
    ) -> None:
        self.recipients = recipients
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sender_name = sender_name
        self.from_address = from_address
        self.reply_to = reply_to
        self.timeout_seconds = timeout_seconds
        self.starttls = starttls
        self.ssl = ssl

    def missing(self) -> tuple[str, ...]:
        missing = []
        if not self.recipients:
            missing.append("NOTIFY_RECIPIENTS")
        if not self.host:
            missing.append("SMTP_HOST")
        if not self.from_address:
            missing.append("SMTP_FROM")
        if self.username and not self.password:
            missing.append("SMTP_PASSWORD")
        if self.password and not self.username:
            missing.append("SMTP_USERNAME")
        return tuple(missing)

    def send(self, subject: str, message: str) -> None:
        missing = self.missing()
        if missing:
            raise RuntimeError("config SMTP mail incompleta: " + ", ".join(missing))
        assert self.host
        assert self.from_address
        send_smtp_mail(
            host=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            sender_name=self.sender_name,
            from_address=self.from_address,
            reply_to=self.reply_to,
            recipients=self.recipients,
            subject=subject,
            message=message,
            timeout=self.timeout_seconds,
            starttls=self.starttls,
            ssl=self.ssl,
        )

    @classmethod
    def from_settings(cls, settings) -> "SMTPSender":
        return cls(
            recipients=settings.notify_recipients,
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            sender_name=settings.notify_sender_name,
            from_address=settings.smtp_from,
            reply_to=settings.notify_reply_to,
            timeout_seconds=settings.notify_timeout_seconds,
            starttls=settings.smtp_starttls,
            ssl=settings.smtp_ssl,
        )


def mail_sender_from_settings(settings) -> MailSender:
    if settings.notify_backend == "smtp":
        return SMTPSender.from_settings(settings)
    return WordPressSender.from_settings(settings)


class ErrorReporter:
    def __init__(
        self,
        *,
        enabled: bool,
        solar_only: bool,
        cooldown_seconds: int,
        sender: MailSender,
    ) -> None:
        self.enabled = enabled
        self.solar_only = solar_only
        self.cooldown_seconds = cooldown_seconds
        self.sender = sender
        self._last_sent: dict[str, float] = {}

    @classmethod
    def from_settings(cls, settings) -> "ErrorReporter":
        return cls(
            enabled=settings.error_report_email_enabled,
            solar_only=settings.error_report_email_on_solaredge_failure,
            cooldown_seconds=settings.error_report_email_cooldown_seconds,
            sender=mail_sender_from_settings(settings),
        )

    def notify(
        self,
        exc: Exception,
        *,
        context: dict[str, Any] | None = None,
        force: bool = False,
    ) -> EmailReportResult:
        if not self.enabled and not force:
            return EmailReportResult(False, False, "report mail disabilitato")
        if self.solar_only and not isinstance(exc, SolarEdgeAccessError) and not force:
            return EmailReportResult(False, False, "errore non SolarEdge: mail non inviata")
        missing = self.sender.missing()
        if missing:
            return EmailReportResult(
                False,
                False,
                f"config {self.sender.backend_name} mail incompleta: {', '.join(missing)}",
            )
        fingerprint = self._fingerprint(exc)
        now = time.monotonic()
        if not force and now - self._last_sent.get(fingerprint, 0) < self.cooldown_seconds:
            return EmailReportResult(False, False, "mail non inviata: cooldown attivo")
        try:
            self._send(exc, context=context)
        except Exception as mail_exc:  # pragma: no cover - dipende dal servizio mail esterno
            LOG.exception("invio report mail fallito")
            return EmailReportResult(True, False, f"invio report fallito: {mail_exc}")
        self._last_sent[fingerprint] = now
        return EmailReportResult(True, True, "report mail inviato")

    def send_test(self) -> EmailReportResult:
        exc = RuntimeError("Test report manuale richiesto da tesla-energy-controller")
        return self.notify(exc, context={"command": "report-test"}, force=True)

    def _fingerprint(self, exc: Exception) -> str:
        payload = diagnostic_payload(exc)
        return "|".join(
            str(payload.get(key, ""))
            for key in ("component", "phase", "endpoint", "status_code", "exception_type")
        )

    def _send(self, exc: Exception, *, context: dict[str, Any] | None = None) -> None:
        payload = diagnostic_payload(exc)
        component = payload.get("component", "application")
        phase = payload.get("phase", "unknown")
        self.sender.send(
            f"[tesla-energy-controller] Errore {component}/{phase}",
            render_error_report(exc, context=context),
        )


class EventReporter:
    def __init__(
        self,
        *,
        enabled: bool,
        cooldown_seconds: int,
        sender: MailSender,
    ) -> None:
        self.enabled = enabled
        self.cooldown_seconds = cooldown_seconds
        self.sender = sender
        self._last_sent: dict[str, float] = {}

    @classmethod
    def from_settings(cls, settings) -> "EventReporter":
        return cls(
            enabled=settings.event_email_enabled,
            cooldown_seconds=settings.event_email_cooldown_seconds,
            sender=mail_sender_from_settings(settings),
        )

    def notify(self, kind: str, subject: str, message: str) -> EmailReportResult:
        if not self.enabled:
            return EmailReportResult(False, False, "event mail disabilitata")
        missing = self.sender.missing()
        if missing:
            return EmailReportResult(
                False,
                False,
                f"config {self.sender.backend_name} mail incompleta: {', '.join(missing)}",
            )
        now = time.monotonic()
        if now - self._last_sent.get(kind, 0) < self.cooldown_seconds:
            return EmailReportResult(False, False, "mail evento non inviata: cooldown attivo")
        try:
            self._send(subject, message)
        except Exception as exc:  # pragma: no cover - dipende dal servizio mail esterno
            LOG.exception("invio mail evento fallito")
            return EmailReportResult(True, False, f"invio evento fallito: {exc}")
        self._last_sent[kind] = now
        return EmailReportResult(True, True, "mail evento inviata")

    def _send(self, subject: str, message: str) -> None:
        self.sender.send(subject, message)
