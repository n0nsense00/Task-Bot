"""Inline-keyboard calendar widget for date picking.

Telegram bots have no native date picker — inline-keyboard buttons are the
only option. This module renders a month-view calendar with prev/next-month
navigation and a row of shortcut buttons (Today / Tomorrow / +7 days /
+30 days). Tapping a day finalises the selection.

Module name is ``calendar_widget`` (not ``calendar``) to avoid shadowing
Python's stdlib ``calendar`` module — we *use* the stdlib calendar inside
to compute the month grid.

Callback-data patterns produced::

    cal:select:YYYY-MM-DD   — user picked this day, conversation should advance
    cal:nav:YYYY-MM         — navigate to year-month (re-render, no selection)
    cal:short:today         — quick-pick: today
    cal:short:tomorrow      — quick-pick: tomorrow
    cal:short:+7d           — quick-pick: today + 7 days
    cal:short:+30d          — quick-pick: today + 30 days
    cal:noop                — placeholder for non-interactive cells
    cal:cancel              — user cancelled the picker

Decode incoming callbacks via :func:`parse_calendar_callback`. Resolve
shortcut keys to actual dates via :func:`shortcut_to_date`.
"""
from __future__ import annotations

import calendar as _calendar
from datetime import date, timedelta
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

CB_PREFIX: str = "cal"
NOOP_DATA: str = f"{CB_PREFIX}:noop"


def build_calendar_keyboard(year: int, month: int) -> InlineKeyboardMarkup:
    """Build an inline keyboard rendering ``year``-``month`` as a calendar.

    Layout:
      Row 0: ◀ prev | Month YYYY (no-op) | next ▶
      Row 1-6: 7 day buttons each (Monday-first). Days from adjacent months
        included so the grid stays rectangular; tapping any day selects it.
      Row 7: Today / Tomorrow / +7d / +30d shortcuts
      Row 8: ❌ Cancel
    """
    prev_year, prev_month = (year, month - 1) if month > 1 else (year - 1, 12)
    next_year, next_month = (year, month + 1) if month < 12 else (year + 1, 1)
    month_name = _calendar.month_name[month]

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                "◀",
                callback_data=f"{CB_PREFIX}:nav:{prev_year:04d}-{prev_month:02d}",
            ),
            InlineKeyboardButton(
                f"{month_name} {year}", callback_data=NOOP_DATA
            ),
            InlineKeyboardButton(
                "▶",
                callback_data=f"{CB_PREFIX}:nav:{next_year:04d}-{next_month:02d}",
            ),
        ]
    ]

    # Use a local Calendar instance (not the module-level setfirstweekday,
    # which would mutate stdlib global state) so weeks start on Monday.
    cal = _calendar.Calendar(firstweekday=_calendar.MONDAY)
    for week in cal.monthdatescalendar(year, month):
        row: list[InlineKeyboardButton] = []
        for day in week:
            row.append(
                InlineKeyboardButton(
                    str(day.day),
                    callback_data=f"{CB_PREFIX}:select:{day.isoformat()}",
                )
            )
        rows.append(row)

    rows.append(
        [
            InlineKeyboardButton(
                "Today", callback_data=f"{CB_PREFIX}:short:today"
            ),
            InlineKeyboardButton(
                "Tmrw", callback_data=f"{CB_PREFIX}:short:tomorrow"
            ),
            InlineKeyboardButton(
                "+7d", callback_data=f"{CB_PREFIX}:short:+7d"
            ),
            InlineKeyboardButton(
                "+30d", callback_data=f"{CB_PREFIX}:short:+30d"
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "❌ Cancel", callback_data=f"{CB_PREFIX}:cancel"
            )
        ]
    )

    return InlineKeyboardMarkup(rows)


def calendar_header_text() -> str:
    """Day-name header line shown in the message body above the keyboard.

    Telegram inline keyboards don't support non-clickable buttons, so the
    weekday header lives in the message text rather than the keyboard.
    """
    return "Mo  Tu  We  Th  Fr  Sa  Su"


def parse_calendar_callback(data: str) -> tuple[str, Optional[str]]:
    """Decode a calendar callback string into ``(action, payload)``.

    Returned actions:
      - ``select`` — payload is an ISO date string ``YYYY-MM-DD``
      - ``nav`` — payload is ``YYYY-MM``
      - ``short`` — payload is a shortcut key (``today``, ``tomorrow``, etc.)
      - ``cancel`` — no payload
      - ``noop`` — no payload (no-op cells)
      - ``unknown`` — the data didn't match anything we produce
    """
    if not data.startswith(f"{CB_PREFIX}:"):
        return ("unknown", None)
    parts = data.split(":", 2)
    if len(parts) < 2:
        return ("unknown", None)
    action = parts[1]
    payload = parts[2] if len(parts) >= 3 else None
    if action in ("select", "nav", "short", "cancel", "noop"):
        return (action, payload)
    return ("unknown", None)


def shortcut_to_date(key: str) -> Optional[date]:
    """Resolve a shortcut key (``today`` / ``tomorrow`` / ``+7d`` / ``+30d``)."""
    today = date.today()
    if key == "today":
        return today
    if key == "tomorrow":
        return today + timedelta(days=1)
    if key == "+7d":
        return today + timedelta(days=7)
    if key == "+30d":
        return today + timedelta(days=30)
    return None


def parse_iso_date(payload: str) -> Optional[date]:
    """Parse a ``YYYY-MM-DD`` string into a ``date``, or ``None`` on failure."""
    try:
        return date.fromisoformat(payload)
    except (TypeError, ValueError):
        return None


def parse_year_month(payload: str) -> Optional[tuple[int, int]]:
    """Parse a ``YYYY-MM`` string into ``(year, month)``, or ``None``."""
    try:
        year_str, month_str = payload.split("-", 1)
        return (int(year_str), int(month_str))
    except (TypeError, ValueError):
        return None
