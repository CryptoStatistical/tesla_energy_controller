#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

AUTH_URL = "https://auth.tesla.com/oauth2/v3/authorize"
TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
SCOPES = "openid offline_access vehicle_device_data vehicle_cmds vehicle_charging_cmds"


def required(value: str | None, name: str) -> str:
    if value:
        return value
    raise SystemExit(f"Manca {name}")


def start(args) -> None:
    client_id = required(args.client_id, "TESLA_CLIENT_ID")
    redirect_uri = required(args.redirect_uri, "TESLA_REDIRECT_URI")
    state = secrets.token_urlsafe(32)
    state_path = Path(args.state_file)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"state": state}) + "\n", encoding="utf-8")
    os.chmod(state_path, 0o600)
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
            "prompt": "login",
            "prompt_missing_scopes": "true",
            "require_requested_scopes": "true",
            "show_keypair_step": "true",
        }
    )
    print(f"{AUTH_URL}?{query}")


def exchange(args) -> None:
    client_id = required(args.client_id, "TESLA_CLIENT_ID")
    redirect_uri = required(args.redirect_uri, "TESLA_REDIRECT_URI")
    parsed = parse_qs(urlparse(args.redirected_url).query)
    code = parsed.get("code", [None])[0]
    returned_state = parsed.get("state", [None])[0]
    expected_state = json.loads(Path(args.state_file).read_text(encoding="utf-8"))["state"]
    if not code or not secrets.compare_digest(returned_state or "", expected_state):
        raise SystemExit("Callback privo di code o state non valido")
    client_secret = args.client_secret or getpass.getpass("Tesla client secret: ")
    response = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "audience": args.audience,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
        },
        timeout=30,
    )
    response.raise_for_status()
    tokens = response.json()
    destination = Path(args.token_file)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(tokens, indent=2) + "\n", encoding="utf-8")
    os.chmod(destination, 0o600)
    print(f"Token salvati in {destination}; non incollarli in chat e non versionarli.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Onboarding OAuth Tesla Fleet API")
    parser.add_argument("command", choices=("start", "exchange"))
    parser.add_argument("redirected_url", nargs="?")
    parser.add_argument("--client-id", default=os.getenv("TESLA_CLIENT_ID"))
    parser.add_argument("--client-secret", default=os.getenv("TESLA_CLIENT_SECRET"))
    parser.add_argument("--redirect-uri", default=os.getenv("TESLA_REDIRECT_URI"))
    parser.add_argument(
        "--audience",
        default=os.getenv(
            "TESLA_AUDIENCE", "https://fleet-api.prd.eu.vn.cloud.tesla.com"
        ),
    )
    parser.add_argument("--token-file", default=".secrets/tesla_tokens.json")
    parser.add_argument("--state-file", default=".secrets/tesla_oauth_state.json")
    args = parser.parse_args()
    if args.command == "start":
        start(args)
    else:
        if not args.redirected_url:
            parser.error("exchange richiede l'URL completo di redirect")
        exchange(args)


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError as exc:
        print(f"OAuth Tesla fallito: {exc}", file=sys.stderr)
        raise SystemExit(2)

