"""Data models for Task-Bot.

A single ``Task`` dataclass represents every assessed deadline the bot tracks,
distinguished by the ``task_type`` field.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

TASK_TYPE_QUIZ: str = "quiz"
TASK_TYPE_LAB: str = "lab"
TASK_TYPE_ASSIGNMENT: str = "assignment"
TASK_TYPE_PROJECT: str = "project"
TASK_TYPE_MIDTERM: str = "midterm"
TASK_TYPE_FINAL: str = "final"
TASK_TYPE_OTHER: str = "other"

TASK_TYPES: tuple[str, ...] = (
    TASK_TYPE_QUIZ,
    TASK_TYPE_LAB,
    TASK_TYPE_ASSIGNMENT,
    TASK_TYPE_PROJECT,
    TASK_TYPE_MIDTERM,
    TASK_TYPE_FINAL,
    TASK_TYPE_OTHER,
)


@dataclass
class Task:
    """A single item tracked by the bot.

    Required fields (``title``, ``task_type``, ``due_date``) come first so
    ``Task(...)`` calls stay ergonomic. ``id`` is ``None`` until the row is
    inserted; ``created_at`` is set by the DB layer on insert and is ``None``
    on freshly-constructed, not-yet-persisted instances.

    ``chat_id`` is Telegram's conversation id and scopes the row to one DM or
    group. ``due_time`` is an optional ``HH:MM`` string (24-hour). Stored as text in
    SQLite to keep the schema migration trivial and avoid timezone confusion
    when combined with ``due_date``. ``None`` means "all day, no specific time".
    """

    title: str
    task_type: str
    due_date: date
    chat_id: Optional[int] = None
    module_code: Optional[str] = None
    week_number: Optional[int] = None
    notes: Optional[str] = None
    completed: bool = False
    id: Optional[int] = None
    created_at: Optional[datetime] = None
    due_time: Optional[str] = None


@dataclass
class Module:
    """A module the user is taking this semester.

    Used to populate the dropdown in /add and /edit when picking a module
    code. ``name`` is optional; if provided, the picker shows
    ``CODE · Name`` instead of the bare code for friendlier scanning.
    Seeded from ``seed/seed_modules.csv`` via :mod:`seed.seed_modules`.
    """

    code: str
    name: Optional[str] = None
