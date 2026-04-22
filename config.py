"""Configuration loading for Task-Bot.

Reads secrets from `.env` via python-dotenv and exposes them as module-level
constants. Fails fast with a clear error at import time if anything required
is missing or malformed, so misconfiguration is caught on startup rather than
at first use.
"""
from __future__ import annotations

import os
from datetime import date

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
CMD_TODAY: str = "today"
CMD_WEEK: str = "week"
CMD_SEMESTER: str = "semester"
CMD_ADD: str = "add"
CMD_DONE: str = "done"
CMD_DELETE: str = "delete"
CMD_CANCEL: str = "cancel"
CMD_BRIEF: str = "brief"

# ----------------------------------------------------------------------------
# Academic calendar
# ----------------------------------------------------------------------------
# SEMESTER_START_DATE must be the Monday of YOUR semester's week 1. All /week
# queries compute the current academic week as (today - SEMESTER_START_DATE) // 7.
# Update the literal below when a new semester starts.
SEMESTER_START_DATE: date = date(2026, 1, 12)


def get_current_week() -> int:
    """Return today's academic week number (1-based).

    Returns ``0`` if today is before ``SEMESTER_START_DATE`` so callers can
    distinguish "pre-semester" from "week 1". No upper bound is enforced —
    week numbers will grow past 13 into the inter-semester break, which is
    fine because no queries depend on an upper cap.
    """
    days_elapsed = (date.today() - SEMESTER_START_DATE).days
    if days_elapsed < 0:
        return 0
    return (days_elapsed // 7) + 1


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
