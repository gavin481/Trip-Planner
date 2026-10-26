"""Trip files on the ema volume.

Names start with trip_ so the itinerary, password, photos, and booking PDFs
stay next to the rest of the data without sharing filenames with it.
A deploy does not replace them. .railwayignore already leaves data/ on the volume.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PAGE = PACKAGE_DIR / "templates" / "trip.html"
PUBLISH_DIR = PACKAGE_DIR / "publish"
PUBLISH_STORE = PUBLISH_DIR / "trip_itinerary.json"
PUBLISH_FILES = PUBLISH_DIR / "files"
PUBLISH_STAMP = PUBLISH_DIR / "stamp.txt"

PASSWORD_FILE_NAME = "trip_password.json"
STORE_FILE_NAME = "trip_itinerary.json"
FILES_DIR_NAME = "trip_files"
PHOTOS_DIR_NAME = "trip_photos"
GATE_SECRET_NAME = "trip_gate_secret"


def data_dir() -> Path:
    raw = str(os.environ.get("DLM_DATA_DIR", "") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (PACKAGE_DIR.parents[1] / "data").resolve()


def store_path() -> Path:
    return data_dir() / STORE_FILE_NAME


def password_file() -> Path:
    return data_dir() / PASSWORD_FILE_NAME


def gate_secret_path() -> Path:
    return data_dir() / GATE_SECRET_NAME


def files_dir() -> Path:
    return data_dir() / FILES_DIR_NAME


def photos_dir() -> Path:
    return data_dir() / PHOTOS_DIR_NAME


def booking_pdf(item_id: str) -> Path:
    return files_dir() / f"{item_id}.pdf"


def on_railway() -> bool:
    return bool(os.environ.get("RAILWAY_PROJECT_ID") or os.environ.get("RAILWAY_ENVIRONMENT"))


def read_store_payload() -> dict:
    path = store_path()
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def publish_snapshot() -> str:
    """Copy this PC's itinerary and booking files into the folder a deploy uploads.

    A deploy sends app files only, so without this the hosted page would show the
    seed bookings with no edits and no attachments.
    """
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if PUBLISH_FILES.is_dir():
        shutil.rmtree(PUBLISH_FILES)
    PUBLISH_FILES.mkdir(parents=True, exist_ok=True)
    store = store_path()
    PUBLISH_STORE.write_text(
        store.read_text(encoding="utf-8") if store.is_file() else "{}",
        encoding="utf-8",
    )
    source = files_dir()
    if source.is_dir():
        for pdf in sorted(source.glob("*.pdf")):
            shutil.copy2(pdf, PUBLISH_FILES / pdf.name)
    PUBLISH_STAMP.write_text(stamp, encoding="utf-8")
    return stamp


def published_stamp() -> str:
    if not PUBLISH_STAMP.is_file():
        return ""
    try:
        return PUBLISH_STAMP.read_text(encoding="utf-8").strip()[:40]
    except OSError:
        return ""


def adopt_snapshot() -> None:
    """Leave each hosted environment's itinerary on its own volume.

    A code deploy used to copy this PC's trip snapshot onto Railway. DEV and
    PROD now keep the trip files already stored on their volumes.
    """
    return
