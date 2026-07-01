from tesla_energy_controller.config import Settings
from tesla_energy_controller.diagnostics import SMTPSender, mail_sender_from_settings
from tesla_energy_controller.smtp_mailer import send_mail


class FakeSMTP:
    instance = None

    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.credentials = None
        self.message = None
        type(self).instance = self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def starttls(self):
        self.started_tls = True

    def login(self, username, password):
        self.credentials = (username, password)

    def send_message(self, message):
        self.message = message


def test_smtp_mailer_sends_html_with_starttls(monkeypatch):
    monkeypatch.setattr("tesla_energy_controller.smtp_mailer.smtplib.SMTP", FakeSMTP)

    send_mail(
        host="smtp.example.com",
        port=587,
        username="user@example.com",
        password="secret",
        sender_name="Energy Controller",
        from_address="sender@example.com",
        reply_to="reply@example.com",
        recipients=("one@example.com", "two@example.com"),
        subject="Test",
        message="<p>Corpo HTML</p>",
        starttls=True,
    )

    client = FakeSMTP.instance
    assert client.started_tls is True
    assert client.credentials == ("user@example.com", "secret")
    assert client.message["From"] == "Energy Controller <sender@example.com>"
    assert client.message["To"] == "one@example.com, two@example.com"
    assert client.message.get_body(preferencelist=("html",)).get_content().strip() == (
        "<p>Corpo HTML</p>"
    )


def test_settings_select_smtp_backend(monkeypatch):
    monkeypatch.setenv("NOTIFY_BACKEND", "smtp")
    monkeypatch.setenv("EVENT_EMAIL_ENABLED", "true")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "sender@example.com")
    monkeypatch.setenv("NOTIFY_RECIPIENTS", "recipient@example.com")

    sender = mail_sender_from_settings(Settings.from_env())

    assert isinstance(sender, SMTPSender)
    assert sender.missing() == ()
