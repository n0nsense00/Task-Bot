"""Data models for Task-Bot.

A single ``Task`` dataclass represents every item the bot tracks — lectures,
tutorials, assignments, exams, and personal to-dos — distinguished by the
``task_type`` field.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

TASK_TYPE_LECTURE: str = "lecture"
TASK_TYPE_TUTORIAL: str = "tutorial"
TASK_TYPE_ASSIGNMENT: str = "assignment"
TASK_TYPE_MIDTERM: str = "midterm"
TASK_TYPE_FINAL: str = "final"
TASK_TYPE_PERSONAL: str = "personal"

TASK_TYPES: tuple[str, ...] = (
    TASK_TYPE_LECTURE,
    TASK_TYPE_TUTORIAL,
    TASK_TYPE_ASSIGNMENT,
    TASK_TYPE_MIDTERM,
    TASK_TYPE_FINAL,
    TASK_TYPE_PERSONAL,
)


@dataclass
class Task:
    """A single item tracked by the bot.

    Required fields (``title``, ``task_type``, ``due_date``) come first so
    ``Task(...)`` calls stay ergonomic. ``id`` is ``None`` until the row is
    inserted; ``created_at`` is set by the DB layer on insert and is ``None``
    on freshly-constructed, not-yet-persisted instances.
    """

    title: str
    task_type: str
    due_date: date
    module_code: Optional[str] = None
    week_number: Optional[int] = None
    notes: Optional[str] = None
    completed: bool = False
    id: Optional[int] = None
    created_at: Optional[datetime] = None
