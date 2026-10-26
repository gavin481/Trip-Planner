"""Standalone Japan & Bali service for its own Railway service.

Start it from the repository root:

    python app/trip/server.py

It serves the same /trip page, API, photos, and booking PDFs as the ema app,
but in its own process with its own volume. Lost Miniatures is not imported,
so deploying this service never restarts or redeploys ema.

Data stays where the trip package already looks for it: DLM_DATA_DIR (on the
trip service this is the trip volume, for example /data). Nothing here moves,
rewrites, or deletes trip_itinerary.json, trip_files/, trip_photos/,
trip_password.json, trip_gate_secret, or the Gmail token files.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# Tells the trip package it is the service itself, so the ema-only "moved"
# redirect (TRIP_REDIRECT_URL) can never point this service back at itself.
os.environ["TRIP_STANDALONE"] = "1"

import trip  # noqa: E402  (needs APP_DIR on sys.path first)
from trip import storage  # noqa: E402

LOGGER = logging.getLogger("dlm.server")
MAX_BODY = 256 * 1024
HTML_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'; object-src 'none'"
)


class TripHandler(BaseHTTPRequestHandler):
    server_version = "Trip/1.0"
    sys_version = ""

    # ---- The small handler surface app/trip/routes.py relies on ----

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _send_bytes(
        self,
        body: bytes,
        *,
        status: int,
        content_type: str,
        cache_control: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        headers = dict(extra_headers or {})
        headers["Cache-Control"] = cache_control
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Referrer-Policy", "same-origin")
        headers.setdefault("X-Frame-Options", "DENY")
        kind = content_type.split(";", 1)[0].strip().lower()
        if kind == "text/html":
            headers.setdefault("Content-Security-Policy", HTML_CSP)
        payload = body
        accepts = str(self.headers.get("Accept-Encoding", "") or "").lower()
        if (kind.startswith("text/") or kind == "application/json") and len(body) > 1024 and "gzip" in accepts:
            payload = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _send_json(self, payload, status: int = 200, extra_headers: dict[str, str] | None = None) -> None:
        self._send_bytes(
            json.dumps(payload).encode("utf-8"),
            status=status,
            content_type="application/json; charset=utf-8",
            cache_control="no-store",
            extra_headers=extra_headers,
        )

    def _serve_file(self, file_path: Path, content_type: str) -> None:
        if not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
            return
        self._send_bytes(file_path.read_bytes(), status=HTTPStatus.OK, content_type=content_type, cache_control="no-cache")

    def _cookie_secure(self) -> bool:
        if storage.on_railway():
            return True
        host = str(self.headers.get("Host", "") or "").split(":", 1)[0].strip().lower()
        if host in {"127.0.0.1", "localhost", "::1"}:
            return False
        forwarded = str(self.headers.get("X-Forwarded-Proto", "") or "").split(",")[0].strip().lower()
        return forwarded == "https"

    # ---- Routing ----

    def _redirect(self, location: str) -> None:
        self._send_bytes(b"", status=HTTPStatus.FOUND, content_type="text/plain; charset=utf-8",
                         cache_control="no-store", extra_headers={"Location": location})

    def _common(self, path: str) -> bool:
        if path in {"", "/", "/trip/"}:
            self._redirect("/trip")
            return True
        if path in {"/health", "/trip/health"}:
            self._send_json({"ok": True, "service": "trip"})
            return True
        if path == "/favicon.ico":
            self._send_bytes(b"", status=HTTPStatus.NO_CONTENT, content_type="image/x-icon", cache_control="max-age=86400")
            return True
        return False

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if self._common(path):
            return
        try:
            status = trip.handle_get(self, path)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            LOGGER.exception("Trip GET failed for %s", path)
            self._send_json({"ok": False, "error": "Something went wrong."}, status=500)
            return
        if status is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            self._send_json({"ok": False, "error": "Too large."}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        try:
            status = trip.handle_post(self, path)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            LOGGER.exception("Trip POST failed for %s", path)
            self._send_json({"ok": False, "error": "Something went wrong."}, status=500)
            return
        if status is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    def log_error(self, format: str, *args) -> None:  # noqa: A002
        LOGGER.error("HTTP %s - %s", self.address_string(), format % args)


def _port() -> int:
    for key in ("PORT", "TRIP_PORT"):
        raw = str(os.environ.get(key, "") or "").strip()
        if raw.isdigit():
            return int(raw)
    return 8011


def _report_data() -> None:
    """Log what is on the volume. Read-only: nothing is created, moved, or removed."""
    folder = storage.data_dir()
    LOGGER.info("Trip data folder: %s", folder)
    for name in (storage.STORE_FILE_NAME, storage.PASSWORD_FILE_NAME, storage.GATE_SECRET_NAME,
                 "gmail_oauth_token.json", "gmail_oauth_client.json"):
        LOGGER.info("  %-24s %s", name, "present" if (folder / name).is_file() else "missing")
    for name in (storage.FILES_DIR_NAME, storage.PHOTOS_DIR_NAME):
        path = folder / name
        count = sum(1 for _ in path.iterdir()) if path.is_dir() else 0
        LOGGER.info("  %-24s %s files", name + "/", count)
    if not (folder / storage.STORE_FILE_NAME).is_file():
        LOGGER.warning("No trip_itinerary.json on this volume yet. The page shows the built-in seed until the "
                       "ema volume files are copied here (see docs/RAILWAY.md, Trip service).")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if storage.on_railway() and not os.environ.get("DLM_DATA_DIR"):
        LOGGER.warning("DLM_DATA_DIR is not set. Set it to the trip volume mount (for example /data).")
    _report_data()
    host = str(os.environ.get("TRIP_HOST", "") or "0.0.0.0").strip() or "0.0.0.0"
    port = _port()
    server = ThreadingHTTPServer((host, port), TripHandler)
    server.daemon_threads = True
    print(f"Trip service running at http://127.0.0.1:{port}/trip", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
