from __future__ import annotations

import base64
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def send_mail(
    *,
    endpoint: str,
    api_key: str,
    basic_auth: str,
    to: str,
    subject: str,
    message: str,
    sender_name: str,
    reply_to: str,
    timeout: int = 15,
) -> bool:
    payload = json.dumps(
        {
            "to": to,
            "subject": subject,
            "message": message,
            "sender_name": sender_name,
            "reply_to": reply_to,
        }
    ).encode("utf-8")
    auth = base64.b64encode(basic_auth.encode("utf-8")).decode("ascii")
    request = Request(endpoint, data=payload, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("X-API-Key", api_key)
    request.add_header("Authorization", f"Basic {auth}")
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.getcode() < 300
    except (HTTPError, URLError):
        return False
