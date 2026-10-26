"""Send the trip password reset through the Gmail account already used by EMA.

Tokens live on the ema volume (gmail_oauth_token.json and gmail_oauth_client.json),
sealed with DLM_TOKEN_KEY when that key is set. Client id and secret can also
come from DLM_GMAIL_CLIENT_ID and DLM_GMAIL_CLIENT_SECRET.
"""

from __future__ import annotations

import base64
import json
import os
import time
from email.message import EmailMessage
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import secret_box
from trip.storage import data_dir as _data_dir

OWNER = "gavin@lostminiatures.com"


def _client() -> dict:
    path = _data_dir() / "gmail_oauth_client.json"
    config = {}
    if path.is_file():
        try:
            config = secret_box.read_json(path)
        except (OSError, json.JSONDecodeError, secret_box.SecretKeyError):
            config = {}
    client_id = str(os.environ.get("DLM_GMAIL_CLIENT_ID", "") or config.get("client_id", "")).strip()
    client_secret = str(os.environ.get("DLM_GMAIL_CLIENT_SECRET", "") or config.get("client_secret", "")).strip()
    return {"client_id": client_id, "client_secret": client_secret}


def _tokens() -> dict:
    path = _data_dir() / "gmail_oauth_token.json"
    if not path.is_file():
        return {}
    return secret_box.read_json(path)


def _write_tokens(tokens: dict) -> dict:
    path = _data_dir() / "gmail_oauth_token.json"
    current = {}
    if path.is_file():
        try:
            current = secret_box.read_json(path)
        except (OSError, json.JSONDecodeError, secret_box.SecretKeyError):
            current = {}
    current.update({key: value for key, value in tokens.items() if value is not None})
    if "expires_in" in current:
        current["expires_at"] = int(time.time()) + int(current.get("expires_in", 3600)) - 60
    secret_box.write_json(path, current)
    return current


def _access_token() -> str:
    tokens = _tokens()
    token = str(tokens.get("access_token", "") or "")
    if token and int(tokens.get("expires_at", 0) or 0) > int(time.time()):
        return token
    refresh = str(tokens.get("refresh_token", "") or "")
    config = _client()
    if not refresh or not config["client_id"] or not config["client_secret"]:
        raise ValueError("Gmail is not connected")
    request = Request(
        "https://oauth2.googleapis.com/token",
        data=urlencode({
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        }).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urlopen(request, timeout=30) as response:
        refreshed = json.loads(response.read().decode("utf-8"))
    return str(_write_tokens(refreshed).get("access_token", ""))


def send_message(
    recipients: list[str],
    subject: str,
    html_body: str,
    text_body: str = "",
    *,
    internal: bool = False,
) -> dict:
    """Send one internal message. The only allowed recipient is the trip owner."""
    if list(recipients) != [OWNER]:
        raise ValueError("Trip mail can only go to the trip owner.")
    message = EmailMessage()
    message["To"] = OWNER
    message["Subject"] = subject
    if internal:
        message["X-DLM-Internal"] = "1"
    message.set_content(text_body or "Open this message in an HTML mail client.")
    message.add_alternative(html_body, subtype="html")
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
    body = json.dumps({"raw": raw}).encode("utf-8")
    request = Request(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {_access_token()}",
            "Content-Type": "application/json",
        },
    )
    with urlopen(request, timeout=30) as response:
        raw_body = response.read()
        return json.loads(raw_body.decode("utf-8")) if raw_body else {}
