"""Seal Gmail and Etsy token files when DLM_TOKEN_KEY is available.

The key comes from the environment, or from a file in the home directory on this PC.
It is not stored in the data folder. New files use AES-GCM. Older files sealed with
the previous construction can still be opened, then rewritten in the new form.
Without a key, token files stay as normal JSON. Railway refuses to start in that case.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC_V1 = b"DLMSEC1\n"
MAGIC_V2 = b"DLMSEC2\n"
_AAD = b"dlm-token"
_HOME_KEY = Path.home() / ".dlm-token-key"


class SecretKeyError(RuntimeError):
    """An encrypted token file cannot be opened with the current key."""


def token_key() -> bytes | None:
    raw = str(os.environ.get("DLM_TOKEN_KEY", "") or "").strip()
    if not raw and _HOME_KEY.is_file():
        try:
            raw = _HOME_KEY.read_text(encoding="utf-8").strip()
        except OSError:
            raw = ""
    if not raw:
        return None
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except Exception:
        decoded = b""
    if len(decoded) >= 32:
        return decoded[:32]
    return hashlib.sha256(raw.encode("utf-8")).digest()


def require_token_key(*, hosted: bool) -> None:
    """Railway must have a key. This PC may run without one."""
    if hosted and token_key() is None:
        raise SecretKeyError(
            "DLM_TOKEN_KEY is required on Railway so mailbox and Etsy tokens stay encrypted."
        )


def is_sealed(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with path.open("rb") as handle:
            prefix = handle.read(len(MAGIC_V2))
    except OSError:
        return False
    return prefix.startswith(MAGIC_V1) or prefix.startswith(MAGIC_V2)


def read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    raw = path.read_bytes()
    if raw.startswith(MAGIC_V1) or raw.startswith(MAGIC_V2):
        key = token_key()
        if not key:
            raise SecretKeyError(f"Set DLM_TOKEN_KEY before reading {path.name}.")
        text = _open(raw, key).decode("utf-8")
    else:
        text = raw.decode("utf-8")
    payload = json.loads(text)
    return payload if isinstance(payload, dict) else {}


def write_json(path: Path, payload: dict) -> None:
    if is_sealed(path) and not token_key():
        raise SecretKeyError(f"Set DLM_TOKEN_KEY before writing {path.name}.")
    body = json.dumps(payload, indent=2).encode("utf-8")
    key = token_key()
    data = _seal_v2(body, key) if key else body
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def seal_plaintext_files(paths: list[Path]) -> list[str]:
    """Encrypt token files that are still plain JSON, and upgrade older seals."""
    if not token_key():
        return []
    changed: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if raw.startswith(MAGIC_V2):
            continue
        try:
            payload = read_json(path)
        except (OSError, json.JSONDecodeError, SecretKeyError):
            continue
        if not payload:
            continue
        write_json(path, payload)
        changed.append(path.name)
    return changed


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    output = bytearray()
    counter = 0
    while len(output) < length:
        block = hmac.new(key, b"enc" + nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        output.extend(block)
        counter += 1
    return bytes(output[:length])


def _seal_v1(plaintext: bytes, key: bytes) -> bytes:
    """Previous construction. Kept so existing files can be read and upgraded."""
    nonce = secrets.token_bytes(16)
    cipher = bytes(left ^ right for left, right in zip(plaintext, _keystream(key, nonce, len(plaintext))))
    mac = hmac.new(key, b"mac" + nonce + cipher, hashlib.sha256).digest()
    return MAGIC_V1 + nonce + mac + cipher


def _open_v1(blob: bytes, key: bytes) -> bytes:
    body = blob[len(MAGIC_V1):]
    if len(body) < 48:
        raise SecretKeyError("Encrypted token file is incomplete.")
    nonce, mac, cipher = body[:16], body[16:48], body[48:]
    expected = hmac.new(key, b"mac" + nonce + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise SecretKeyError("Encrypted token file does not match DLM_TOKEN_KEY.")
    return bytes(left ^ right for left, right in zip(cipher, _keystream(key, nonce, len(cipher))))


def _seal_v2(plaintext: bytes, key: bytes) -> bytes:
    nonce = secrets.token_bytes(12)
    cipher = AESGCM(key).encrypt(nonce, plaintext, _AAD)
    return MAGIC_V2 + nonce + cipher


def _open_v2(blob: bytes, key: bytes) -> bytes:
    body = blob[len(MAGIC_V2):]
    if len(body) < 12 + 16:
        raise SecretKeyError("Encrypted token file is incomplete.")
    nonce, cipher = body[:12], body[12:]
    try:
        return AESGCM(key).decrypt(nonce, cipher, _AAD)
    except Exception as exc:
        raise SecretKeyError("Encrypted token file does not match DLM_TOKEN_KEY.") from exc


def _open(blob: bytes, key: bytes) -> bytes:
    if blob.startswith(MAGIC_V2):
        return _open_v2(blob, key)
    if blob.startswith(MAGIC_V1):
        return _open_v1(blob, key)
    raise SecretKeyError("Encrypted token file is incomplete.")
