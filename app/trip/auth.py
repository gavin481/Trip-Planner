"""Shared trip password, session cookie, and reset email."""

from __future__ import annotations

import hashlib
import hmac
import html as html_lib
import json
import logging
import secrets
import threading
import time
from datetime import datetime

import secret_box
from trip.storage import gate_secret_path, password_file

COOKIE_NAME = "dlm_trip"
COOKIE_MAX_AGE = 180 * 24 * 60 * 60
# The hosted password, matched without regard to case. A Forgot password reset
# replaces it until that reset file is removed.
_LOCAL_TRIP_WORD = "sakura"
_TRIP_WORD_MIN = 8
# Passwords saved before this generation are cleared on startup. That covers an
# older reset file and leaves TRIP_PASSWORD unused, so Sakura opens DEV and PROD.
PASSWORD_GENERATION = 1
# Fixed mailbox. The reset request cannot choose a different address.
RESET_RECIPIENT = "gavin@lostminiatures.com"
_RESET_LIMIT = 3
_RESET_WINDOW_SECONDS = 60 * 60
_RESET_GAP_SECONDS = 60
LOGGER = logging.getLogger("dlm.server")
_LOCK = threading.Lock()
_FAIL_TIMES: list[float] = []
_FAIL_GUARD = threading.Lock()


def _read_password_state() -> dict:
    try:
        payload = secret_box.read_json(password_file())
    except (OSError, json.JSONDecodeError):
        payload = {}
    word = str(payload.get("word") or "").strip().lower()
    attempts = []
    for item in payload.get("attempts") or []:
        try:
            attempts.append(int(item))
        except (TypeError, ValueError):
            continue
    try:
        generation = int(payload.get("generation") or 0)
    except (TypeError, ValueError):
        generation = 0
    return {"word": word, "attempts": attempts, "generation": generation}

def _write_password_state(state: dict) -> None:
    path = password_file()
    secret_box.write_json(path, {
        "word": str(state.get("word") or ""),
        "attempts": [int(item) for item in (state.get("attempts") or [])],
        "generation": PASSWORD_GENERATION,
    })
    try:
        path.chmod(0o600)
    except OSError:
        pass

def retire_legacy_password() -> None:
    """Remove a trip password saved before Sakura was the hosted word.

    The app runs this at startup on DEV and on PROD. A reset after this change
    stores generation 1 and is kept. An older trip_password.json is deleted.
    TRIP_PASSWORD is not read, so an old Railway variable cannot block Sakura.
    """
    path = password_file()
    if not path.is_file():
        return
    try:
        payload = secret_box.read_json(path)
    except secret_box.SecretKeyError:
        LOGGER.error("Trip password file could not be opened")
        return
    except (OSError, json.JSONDecodeError):
        payload = None
    generation = 0
    if isinstance(payload, dict):
        try:
            generation = int(payload.get("generation") or 0)
        except (TypeError, ValueError):
            generation = 0
    if generation == PASSWORD_GENERATION:
        return
    try:
        path.unlink()
    except OSError:
        LOGGER.exception("Old trip password file could not be removed")

def _saved_word() -> str:
    """Password written by a reset after Sakura became the hosted word."""
    if not password_file().is_file():
        return ""
    try:
        state = _read_password_state()
    except secret_box.SecretKeyError:
        LOGGER.error("Trip password file could not be opened")
        return ""
    if state.get("generation") != PASSWORD_GENERATION:
        return ""
    word = state["word"]
    if len(word) < _TRIP_WORD_MIN or word == _LOCAL_TRIP_WORD:
        return ""
    return word

def _active_word() -> str:
    saved = _saved_word()
    if saved:
        return saved
    return _LOCAL_TRIP_WORD

def _password_accepted() -> bool:
    """Sakura always opens the page. A later reset must still be a full word."""
    word = _active_word()
    if word == _LOCAL_TRIP_WORD:
        return True
    return len(word) >= _TRIP_WORD_MIN

def _pepper() -> bytes:
    path = gate_secret_path()
    try:
        if path.is_file():
            raw = path.read_bytes()
            if len(raw) >= 32:
                return raw[:32]
    except OSError:
        raw = b""
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = secrets.token_bytes(32)
    path.write_bytes(fresh)
    return fresh

def _word_matches(supplied: str) -> bool:
    if not _password_accepted():
        return False
    given = hashlib.sha256(supplied.strip().lower().encode("utf-8")).digest()
    expected = hashlib.sha256(_active_word().encode("utf-8")).digest()
    return bool(supplied.strip()) and hmac.compare_digest(given, expected)

def _too_many_tries() -> bool:
    now = datetime.now().timestamp()
    cutoff = now - 600
    with _FAIL_GUARD:
        _FAIL_TIMES[:] = [stamp for stamp in _FAIL_TIMES if stamp >= cutoff]
        if len(_FAIL_TIMES) >= 12:
            return True
        _FAIL_TIMES.append(now)
        return False

def gate_token() -> str:
    return hmac.new(_pepper(), _active_word().encode("utf-8"), hashlib.sha256).hexdigest()

def cookie_header(*, secure: bool, clear: bool = False) -> str:
    value = "" if clear else gate_token()
    lifetime = 0 if clear else COOKIE_MAX_AGE
    parts = [
        f"{COOKIE_NAME}={value}",
        "HttpOnly",
        "Path=/trip",
        "SameSite=Lax",
        f"Max-Age={lifetime}",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)

def unlocked(cookie_header_value: str) -> bool:
    if not _password_accepted():
        return False
    expected = gate_token()
    for part in str(cookie_header_value or "").split(";"):
        name, _, value = part.strip().partition("=")
        token = value.strip()
        if name == COOKIE_NAME and len(token) == len(expected):
            return hmac.compare_digest(token, expected)
    return False

class ResetError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status

def _new_password() -> str:
    raw = secrets.token_hex(8)
    return "-".join(raw[index:index + 4] for index in range(0, len(raw), 4))

def _restore_password(word: str, attempts: list[int], *, expected: str) -> None:
    """Put the previous password back unless a newer reset already replaced it."""
    try:
        with _LOCK:
            if not password_file().is_file():
                return
            current = _read_password_state()
            if current["word"] != expected:
                return
            _write_password_state({"word": word, "attempts": attempts})
    except Exception:
        LOGGER.exception("Trip password could not be restored after a failed email")

def _deliver_reset_email(password: str) -> None:
    """Send the new password through the shared Gmail connection.

    Tokens live on the data volume (or DLM_GMAIL_CLIENT_ID and
    DLM_GMAIL_CLIENT_SECRET) and DLM_TOKEN_KEY opens them when they are sealed.
    The recipient is fixed. The request cannot choose a different address.
    """
    from trip import mail

    safe = html_lib.escape(password)
    subject = "Japan & Bali trip password"
    text = (
        "A new password was created for the Japan & Bali itinerary.\n\n"
        f"{password}\n\n"
        "Type it with the dashes. The previous password no longer works.\n"
    )
    html_body = (
        "<p>A new password was created for the Japan &amp; Bali itinerary.</p>"
        f"<p style=\"font-size:20px;font-weight:700;letter-spacing:0.5px\">{safe}</p>"
        "<p>Type it with the dashes. The previous password no longer works.</p>"
    )
    mail.send_message([RESET_RECIPIENT], subject, html_body, text, internal=True)

def reset_password(*, deliver=None, now: float | None = None) -> None:
    """Replace the shared trip password and email it to the fixed recipient.

    The new word is stored in the data directory, sealed when DLM_TOKEN_KEY is
    set, and replaces Sakura. A failed email puts the previous password back.
    `deliver` is only for tests; it cannot choose the recipient.
    """
    moment = int(time.time() if now is None else now)
    sender = deliver or _deliver_reset_email
    with _LOCK:
        try:
            state = _read_password_state() if password_file().is_file() else {"word": "", "attempts": []}
        except secret_box.SecretKeyError as exc:
            raise ResetError("The saved password could not be opened.", 500) from exc
        recent = [stamp for stamp in state["attempts"] if stamp >= moment - _RESET_WINDOW_SECONDS]
        if recent and moment - max(recent) < _RESET_GAP_SECONDS:
            raise ResetError("A reset was just sent. Wait a minute and try again.", 429)
        if len(recent) >= _RESET_LIMIT:
            raise ResetError("Too many resets. Wait a while and try again.", 429)
        previous = state["word"] if len(state["word"]) >= _TRIP_WORD_MIN else ""
        word = _new_password()
        recent.append(moment)
        try:
            _write_password_state({"word": word, "attempts": recent})
        except secret_box.SecretKeyError as exc:
            raise ResetError("The new password could not be saved.", 500) from exc
    try:
        sender(word)
    except Exception as exc:
        _restore_password(previous, recent, expected=word)
        if isinstance(exc, ResetError):
            raise
        LOGGER.exception("Trip password email failed")
        raise ResetError("The password was not changed because the email could not be sent.", 502) from exc

