"""Timezone-aware ``today`` helper.

The EC2 host runs in UTC (the instance default). Calling
:func:`datetime.date.today` returns the UTC day, which can lag the user's
local calendar by up to several hours (8 hours for ``Asia/Singapore``). For
features whose correctness depends on "is this date in the past?" — the
auto-cleanup of expired midterms/finals in particular — we anchor against
:data:`config.TIMEZONE` instead.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from config import TIMEZONE


def today_local(at: datetime | None = None) -> date:
    """Return today's date in the configured local timezone.

    Falls back to ``Asia/Singapore`` if ``TIMEZONE`` is malformed — same
    fallback strategy used by :mod:`scheduler`, so the two stay aligned.

    ``at`` is an optional timezone-aware instant used by deterministic tests
    and callers that have already captured the current time.  Supplying a
    naive ``datetime`` is rejected because interpreting it as either UTC or
    local time would silently reintroduce the boundary bug this helper avoids.
    """
    try:
        tz = ZoneInfo(TIMEZONE)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("Asia/Singapore")
    if at is None:
        return datetime.now(tz).date()
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("today_local(at=...) requires a timezone-aware datetime")
    return at.astimezone(tz).date()
