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
    TASK_TYPE_LECTURE,
    TASK_TYPE_MIDTERM,
    Module,
    Task,
)


def _print_result(label: str, value: object) -> None:
    """Print a compact labelled result for manual runs."""
    print(f"{label}: {value}")


def run_check() -> None:
    """Exercise the database layer against a throwaway DB file."""
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
        )
        late_assignment = Task(
            title="Assignment late slot",
            task_type=TASK_TYPE_ASSIGNMENT,
            due_date=today,
            due_time="23:59",
            module_code="CS2040",
            week_number=1,
            notes="Submit before midnight.",
        )
        lecture = Task(
            title="Lecture tomorrow",
            task_type=TASK_TYPE_LECTURE,
            due_date=tomorrow,
            module_code="MH2100",
            week_number=1,
        )
        old_midterm = Task(
            title="Past midterm",
            task_type=TASK_TYPE_MIDTERM,
            due_date=yesterday,
            due_time="18:30",
            module_code="CS2040",
            week_number=1,
        )

        early_id, late_id = db.add_tasks([early_assignment, late_assignment])
        lecture_id = db.add_task(lecture)
        old_midterm_id = db.add_task(old_midterm)
        assert db.count_tasks() == 4

        due_today = db.get_tasks_for_date(today)
        assert [t.id for t in due_today] == [early_id, late_id]

        fetched = db.get_task(late_id)
        assert fetched is not None
        assert fetched.due_time == "23:59"
        fetched.notes = "Updated notes"
        assert db.update_task(fetched)
        assert db.get_task(late_id).notes == "Updated notes"  # type: ignore[union-attr]

        assert db.mark_complete(early_id)
        pending_ids = {t.id for t in db.get_all_pending()}
        assert early_id not in pending_ids
        assert late_id in pending_ids

        removed_deadlines = db.cleanup_past_deadlines(today)
        assert removed_deadlines == 1
        assert db.get_task(old_midterm_id) is None

        assert db.delete_task(lecture_id)
        assert db.delete_task(late_id)
        assert db.delete_task(early_id)
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
