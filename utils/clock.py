"""Timezone-aware ``today`` helper.

Railway containers run in UTC. Calling :func:`datetime.date.today` returns
the UTC day, which can lag the user's local calendar by up to several hours
(8 hours for ``Asia/Singapore``). For features whose correctness depends on
"is this date in the past?" — the auto-cleanup of expired midterms/finals
in particular — we anchor against :data:`config.TIMEZONE` instead.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from config import TIMEZONE


def today_local() -> date:
    """Return today's date in the configured local timezone.

    Falls back to ``Asia/Singapore`` if ``TIMEZONE`` is malformed — same
    fallback strategy used by :mod:`scheduler`, so the two stay aligned.
    """
    try:
        tz = ZoneInfo(TIMEZONE)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("Asia/Singapore")
    return datetime.now(tz).date()
