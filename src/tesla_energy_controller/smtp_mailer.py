from __future__ import annotations

import smtplib
from email.message import EmailMessage
from email.utils import formataddr


def send_mail(
    *,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
    sender_name: str,
    from_address: str,
    reply_to: str,
    recipients: tuple[str, ...],
    subject: str,
    message: str,
    timeout: int = 15,
    starttls: bool = True,
    ssl: bool = False,
) -> None:
    mail = EmailMessage()
    mail["Subject"] = subject
    mail["From"] = formataddr((sender_name, from_address))
    mail["To"] = ", ".join(recipients)
    mail["Reply-To"] = reply_to
    mail.set_content("Aprire questo messaggio con un client email che supporta HTML.")
    mail.add_alternative(message, subtype="html")

    client_class = smtplib.SMTP_SSL if ssl else smtplib.SMTP
    with client_class(host, port, timeout=timeout) as client:
        if starttls:
            client.starttls()
        if username:
            client.login(username, password or "")
        client.send_message(mail)
