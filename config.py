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


def _optional_int(name: str, default: int) -> int:
    """Return env var ``name`` cast to int, or ``default`` if unset/empty.

    Raises ``RuntimeError`` if set but not a valid integer — silent default
    on a typo would be worse than failing fast.
    """
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"Environment variable {name} must be a valid integer, got: {raw!r}"
        ) from exc


TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
MY_TELEGRAM_ID: int = _require_int("MY_TELEGRAM_ID")
# Numeric Telegram chat ID of the group the bot is restricted to. Updates from
# any other chat (DMs, other groups, channels) are dropped at the auth
# decorator. Group IDs are negative — supergroups start with -100...
#
# Optional: if unset (or set to 0), only the owner can use the bot. The owner
# can send /start in a target group and read its chat id from the startup logs,
# then set ALLOWED_CHAT_ID and restart.
ALLOWED_CHAT_ID: int = _optional_int("ALLOWED_CHAT_ID", 0)

CMD_START: str = "start"
CMD_HELP: str = "help"
CMD_DEADLINES: str = "deadlines"
CMD_ADD: str = "add"
CMD_DONE: str = "done"
CMD_DELETE: str = "delete"
CMD_CANCEL: str = "cancel"
CMD_BRIEF: str = "brief"
CMD_CLEAR: str = "clear"
CMD_KILL: str = "kill"
CMD_REVIVE: str = "revive"

# ----------------------------------------------------------------------------
# Scheduler
# ----------------------------------------------------------------------------
# IANA timezone name used for the daily morning brief. Override via .env if
# you're not in Singapore; leave unset to use the default.
TIMEZONE: str = os.getenv("TIMEZONE", "Asia/Singapore")

# Daily morning brief time, in TIMEZONE. Change and restart the bot to shift
# the delivery hour — handy for testing (e.g. set BRIEF_HOUR to your current
# hour plus one minute in BRIEF_MINUTE to see the cron fire in real time).
BRIEF_HOUR: int = 8
BRIEF_MINUTE: int = 0

# Master switch for the daily 08:00 morning brief.
# - When False: the scheduled cron is not installed AND the startup catch-up
#   is skipped — the bot stays completely silent at the scheduled hour.
# - When True (or env var MORNING_BRIEF_ENABLED=true / 1 / yes): the daily
#   push fires as designed.
# - The /brief slash command works in BOTH cases — it's an on-demand
#   manual trigger that bypasses this flag, so you can still pull a brief
#   any time you want one.
MORNING_BRIEF_ENABLED: bool = os.getenv(
    "MORNING_BRIEF_ENABLED", "false"
).strip().lower() in ("true", "1", "yes", "on")
