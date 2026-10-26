"""Japan & Bali itinerary, served at /trip by the ema app.

app.py registers this package in three places: handle_get, handle_post,
and password_file() when token files are sealed. Every /trip path is
handled here. Other paths return None and Lost Miniatures continues.
"""

from trip.auth import (
    COOKIE_NAME,
    RESET_RECIPIENT,
    ResetError,
    _TRIP_WORD_MIN,
    _deliver_reset_email,
    _read_password_state,
    _saved_word,
    _word_matches,
    _write_password_state,
    gate_token,
    reset_password,
    unlocked,
)
from trip.routes import handle_get, handle_post
from trip.storage import PAGE, PASSWORD_FILE_NAME, adopt_snapshot, password_file

adopt_snapshot()

__all__ = [
    "COOKIE_NAME",
    "PAGE",
    "PASSWORD_FILE_NAME",
    "RESET_RECIPIENT",
    "ResetError",
    "_TRIP_WORD_MIN",
    "_deliver_reset_email",
    "_read_password_state",
    "_saved_word",
    "_word_matches",
    "_write_password_state",
    "gate_token",
    "handle_get",
    "handle_post",
    "password_file",
    "reset_password",
    "unlocked",
]
