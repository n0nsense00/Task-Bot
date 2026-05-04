"""Inline-keyboard time picker for the optional ``due_time`` field.

Hybrid design — most uni deadlines fall on predictable times (9am, noon,
5pm, 11:59pm), so the first view is preset buttons. "Custom…" opens an
hour picker (24 buttons in a 4×6 grid), then a minute picker (5-minute
granularity, 12 buttons). Any final time is emitted as ``time:set:HH:MM``
so the calling conversation handler only needs to watch for two callback
prefixes (``time:`` and ``timehr:``).

Callback patterns produced::

    time:set:HH:MM   — final time selected (preset or custom-finalized)
    time:skip        — "All day" (no specific time stored)
    time:custom      — open the hour picker
    time:cancel      — abort
    timehr:HH        — hour picked, conversation should render minute picker

Decode via :func:`parse_time_callback`.
"""
from __future__ import annotations

from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

CB_TIME: str = "time"
CB_TIMEHR: str = "timehr"

PRESETS: tuple[tuple[str, str], ...] = (
    ("All day", f"{CB_TIME}:skip"),
    ("09:00", f"{CB_TIME}:set:09:00"),
    ("12:00", f"{CB_TIME}:set:12:00"),
    ("17:00", f"{CB_TIME}:set:17:00"),
    ("23:59", f"{CB_TIME}:set:23:59"),
    ("Custom…", f"{CB_TIME}:custom"),
)


def build_time_preset_keyboard() -> InlineKeyboardMarkup:
    """Initial picker view — preset times + Custom + Cancel."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for label, data in PRESETS:
        row.append(InlineKeyboardButton(label, callback_data=data))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [InlineKeyboardButton("❌ Cancel", callback_data=f"{CB_TIME}:cancel")]
    )
    return InlineKeyboardMarkup(rows)


def build_hour_keyboard() -> InlineKeyboardMarkup:
    """Hour picker — 24 buttons (00-23) in a 4×6 grid plus Cancel."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for hour in range(24):
        row.append(
            InlineKeyboardButton(
                f"{hour:02d}", callback_data=f"{CB_TIMEHR}:{hour:02d}"
            )
        )
        if len(row) == 6:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [InlineKeyboardButton("❌ Cancel", callback_data=f"{CB_TIME}:cancel")]
    )
    return InlineKeyboardMarkup(rows)


def build_minute_keyboard(hour: int) -> InlineKeyboardMarkup:
    """Minute picker — 12 buttons at 5-min granularity (00..55).

    Each button emits ``time:set:HH:MM`` directly, so finalization is a single
    state transition in the calling conversation — no hand-off needed.
    """
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for minute in range(0, 60, 5):
        row.append(
            InlineKeyboardButton(
                f"{minute:02d}",
                callback_data=f"{CB_TIME}:set:{hour:02d}:{minute:02d}",
            )
        )
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [InlineKeyboardButton("❌ Cancel", callback_data=f"{CB_TIME}:cancel")]
    )
    return InlineKeyboardMarkup(rows)


def parse_time_callback(data: str) -> tuple[str, Optional[str]]:
    """Decode a time-picker callback into ``(action, payload)``.

    Actions:
      - ``set``    — payload is the final ``HH:MM`` string
      - ``skip``   — user picked "All day"; no time should be stored
      - ``custom`` — user wants the hour picker; conversation re-renders
      - ``cancel`` — user aborted
      - ``hour``   — payload is the picked hour ``HH``; render minute picker
      - ``unknown`` — fallback for malformed callbacks
    """
    if data.startswith(f"{CB_TIMEHR}:"):
        parts = data.split(":", 1)
        if len(parts) == 2:
            return ("hour", parts[1])
        return ("unknown", None)
    if data.startswith(f"{CB_TIME}:"):
        rest = data[len(CB_TIME) + 1 :]
        if rest in ("skip", "custom", "cancel"):
            return (rest, None)
        if rest.startswith("set:"):
            return ("set", rest[4:])  # "HH:MM"
    return ("unknown", None)
