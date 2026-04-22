"""Configuration loading for Task-Bot.

Reads secrets from `.env` via python-dotenv and exposes them as module-level
constants. Fails fast with a clear error at import time if anything required
is missing or malformed, so misconfiguration is caught on startup rather than
at first use.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    """Return env var ``name`` or raise ``RuntimeError`` if unset/empty."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            "Add it to your .env file (see .env.example)."
        )
    return value


def _require_int(name: str) -> int:
    """Return env var ``name`` cast to int, or raise if unset/non-numeric."""
    raw = _require(name)
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"Environment variable {name} must be a valid integer, got: {raw!r}"
        ) from exc


TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
MY_TELEGRAM_ID: int = _require_int("MY_TELEGRAM_ID")

CMD_START: str = "start"
CMD_HELP: str = "help"
