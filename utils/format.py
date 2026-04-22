"""Shared HTML formatting helpers for task display.

Centralised here so both the on-demand command handlers (``/today``, ``/week``,
``/semester``) and the scheduled morning brief render tasks identically.
Every user-supplied string must flow through :func:`esc` before interpolation
to prevent injection of HTML tags into the rendered message.
"""
from __future__ import annotations

import html
from datetime import date

from database.models import (
    TASK_TYPE_ASSIGNMENT,
    TASK_TYPE_FINAL,
    TASK_TYPE_LECTURE,
    TASK_TYPE_MIDTERM,
    TASK_TYPE_PERSONAL,
    TASK_TYPE_TUTORIAL,
    Task,
)

TYPE_DISPLAY_ORDER: tuple[str, ...] = (
    TASK_TYPE_LECTURE,
    TASK_TYPE_TUTORIAL,
    TASK_TYPE_ASSIGNMENT,
    TASK_TYPE_MIDTERM,
    TASK_TYPE_FINAL,
    TASK_TYPE_PERSONAL,
)

TYPE_DISPLAY_LABEL: dict[str, str] = {
    TASK_TYPE_LECTURE: "Lectures",
    TASK_TYPE_TUTORIAL: "Tutorials",
    TASK_TYPE_ASSIGNMENT: "Assignments",
    TASK_TYPE_MIDTERM: "Midterms",
    TASK_TYPE_FINAL: "Finals",
    TASK_TYPE_PERSONAL: "Personal",
}


def esc(text: str | None) -> str:
    """HTML-escape a user-supplied string; map ``None`` to the empty string."""
    return html.escape(text) if text else ""


def module_prefix(task: Task) -> str:
    """Return ``[MODULE] `` for tasks with a module_code, else an empty string."""
    return f"[{esc(task.module_code)}] " if task.module_code else ""


def format_task_line(task: Task) -> str:
    """Render one task as a bulleted HTML list item for grouped views."""
    return (
        f"• {module_prefix(task)}{esc(task.title)} <code>(id {task.id})</code>"
    )


def days_away_label(target: date) -> str:
    """Return a human-readable relative distance between today and ``target``."""
    delta = (target - date.today()).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    if delta > 0:
        return f"{delta} days away"
    return f"{-delta} days ago"


def format_grouped_today(tasks: list[Task], target_date: date) -> list[str]:
    """Render tasks due on ``target_date`` as HTML lines, grouped by type.

    Returns a list of lines (not a single joined string) so callers can mix
    the output into a larger message — e.g. the morning brief prepends a
    greeting and appends an upcoming-deadlines section.
    """
    grouped: dict[str, list[Task]] = {}
    for t in tasks:
        grouped.setdefault(t.task_type, []).append(t)

    lines: list[str] = [f"<b>Today — {target_date.isoformat()}</b>", ""]
    for ttype in TYPE_DISPLAY_ORDER:
        bucket = grouped.get(ttype)
        if not bucket:
            continue
        lines.append(f"<b>{TYPE_DISPLAY_LABEL[ttype]}</b>")
        lines.extend(format_task_line(t) for t in bucket)
        lines.append("")
    return lines
