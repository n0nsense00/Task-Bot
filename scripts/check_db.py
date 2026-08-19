"""One-shot database sanity check using an isolated temporary SQLite file.

Run from the project root:

    python scripts/check_db.py

The script never touches ``data/tasks.db``. It patches ``database.db.DB_PATH``
to a temporary file, exercises CRUD/query/module helpers, and exits non-zero if
an assertion fails.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from database import db  # noqa: E402
from database.models import (  # noqa: E402
    TASK_TYPE_ASSIGNMENT,
    TASK_TYPE_MIDTERM,
    TASK_TYPE_QUIZ,
    Module,
    Task,
)


def _print_result(label: str, value: object) -> None:
    """Print a compact labelled result for manual runs."""
    print(f"{label}: {value}")


def run_check() -> None:
    """Exercise the database layer against a throwaway DB file."""
    personal_chat_id = 111_111
    group_chat_id = -100_222_222
    with TemporaryDirectory(prefix="taskbot-check-") as tmp_dir:
        db.DB_PATH = Path(tmp_dir) / "tasks.db"
        db.init_db()

        db.add_module(Module(code="CS2040", name="Data Structures"))
        db.add_module(Module(code="MH2100", name="Calculus III"))
        assert db.count_modules() == 2
        assert db.get_module("CS2040") is not None

        today = date.today()
        tomorrow = today + timedelta(days=1)
        yesterday = today - timedelta(days=1)

        early_assignment = Task(
            title="Assignment early slot",
            task_type=TASK_TYPE_ASSIGNMENT,
            due_date=today,
            due_time="09:00",
            module_code="CS2040",
            week_number=1,
            chat_id=personal_chat_id,
        )
        late_assignment = Task(
            title="Assignment late slot",
            task_type=TASK_TYPE_ASSIGNMENT,
            due_date=today,
            due_time="23:59",
            module_code="CS2040",
            week_number=1,
            notes="Submit before midnight.",
            chat_id=personal_chat_id,
        )
        quiz = Task(
            title="Quiz tomorrow",
            task_type=TASK_TYPE_QUIZ,
            due_date=tomorrow,
            module_code="MH2100",
            week_number=1,
            chat_id=personal_chat_id,
        )
        old_midterm = Task(
            title="Past midterm",
            task_type=TASK_TYPE_MIDTERM,
            due_date=yesterday,
            due_time="18:30",
            module_code="CS2040",
            week_number=1,
            chat_id=personal_chat_id,
        )
        group_quiz = Task(
            title="Group-only quiz",
            task_type=TASK_TYPE_QUIZ,
            due_date=tomorrow,
            module_code="CS2040",
            chat_id=group_chat_id,
        )

        early_id, late_id = db.add_tasks([early_assignment, late_assignment])
        quiz_id = db.add_task(quiz)
        old_midterm_id = db.add_task(old_midterm)
        group_quiz_id = db.add_task(group_quiz)
        assert db.count_tasks() == 5
        assert db.count_tasks(personal_chat_id) == 4
        assert db.count_tasks(group_chat_id) == 1

        due_today = db.get_tasks_for_date(today, personal_chat_id)
        assert [t.id for t in due_today] == [early_id, late_id]
        assert db.get_tasks_for_date(today, group_chat_id) == []

        fetched = db.get_task(late_id, personal_chat_id)
        assert fetched is not None
        assert fetched.due_time == "23:59"
        assert db.get_task(late_id, group_chat_id) is None
        fetched.notes = "Updated notes"
        assert db.update_task(fetched)
        assert db.get_task(late_id, personal_chat_id).notes == "Updated notes"  # type: ignore[union-attr]

        assert not db.mark_complete(early_id, group_chat_id)
        assert db.mark_complete(early_id, personal_chat_id)
        pending_ids = {t.id for t in db.get_all_pending(personal_chat_id)}
        assert early_id not in pending_ids
        assert late_id in pending_ids

        deadline_ids = {
            t.id for t in db.get_semester_deadlines(personal_chat_id)
        }
        assert late_id in deadline_ids
        assert quiz_id in deadline_ids
        assert early_id not in deadline_ids
        assert {
            t.id for t in db.get_semester_deadlines(group_chat_id)
        } == {group_quiz_id}

        removed_deadlines = db.cleanup_past_deadlines(today, personal_chat_id)
        assert removed_deadlines == 1
        assert db.get_task(old_midterm_id, personal_chat_id) is None

        assert not db.delete_task(group_quiz_id, personal_chat_id)
        assert db.delete_task(group_quiz_id, group_chat_id)
        assert db.delete_task(quiz_id, personal_chat_id)
        assert db.delete_task(late_id, personal_chat_id)
        assert db.delete_task(early_id, personal_chat_id)
        assert db.count_tasks() == 0

        _print_result("temporary_db", db.DB_PATH)
        _print_result("modules_checked", db.count_modules())
        _print_result("status", "ok")


def main() -> int:
    """CLI entry point."""
    run_check()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
