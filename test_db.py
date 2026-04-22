"""One-shot sanity check for the database layer.

Run from the project root with the venv active:

    python test_db.py

Inserts three sample tasks into the real DB, exercises every query function,
prints the results, then deletes the sample rows — so a clean run leaves
``data/tasks.db`` in the exact state it started in (aside from the
auto-incrementing id counter).
"""
from __future__ import annotations

from datetime import date

from database.db import (
    add_task,
    delete_task,
    get_all_pending,
    get_semester_deadlines,
    get_task,
    get_tasks_for_date,
    get_tasks_for_week,
    init_db,
    mark_complete,
    update_task,
)
from database.models import (
    TASK_TYPE_ASSIGNMENT,
    TASK_TYPE_LECTURE,
    TASK_TYPE_MIDTERM,
    Task,
)


def _print_tasks(label: str, tasks: list[Task]) -> None:
    """Print a labeled list of tasks, one per line."""
    print(f"\n{label}  ({len(tasks)} result{'s' if len(tasks) != 1 else ''})")
    if not tasks:
        print("  (empty)")
        return
    for t in tasks:
        print(
            f"  id={t.id}  type={t.task_type:<10}  "
            f"module={t.module_code or '-':<8}  "
            f"due={t.due_date}  week={t.week_number}  "
            f"done={t.completed}  title={t.title!r}"
        )


def main() -> None:
    """Run the sanity check end-to-end."""
    print("=== init_db() ===")
    init_db()
    print("OK")

    # Using dates in the near future so they'd plausibly show up in real queries.
    lecture = Task(
        title="[TEST] CS2040 Lecture 4",
        task_type=TASK_TYPE_LECTURE,
        due_date=date(2026, 4, 23),
        module_code="CS2040",
        week_number=3,
    )
    assignment = Task(
        title="[TEST] CS2040 Assignment 2",
        task_type=TASK_TYPE_ASSIGNMENT,
        due_date=date(2026, 4, 30),
        module_code="CS2040",
        week_number=4,
        notes="Covers chapters 5-6",
    )
    midterm = Task(
        title="[TEST] CS2040 Midterm",
        task_type=TASK_TYPE_MIDTERM,
        due_date=date(2026, 5, 15),
        module_code="CS2040",
        week_number=7,
    )

    inserted_ids: list[int] = []
    try:
        print("\n=== add_task() x3 ===")
        lecture_id = add_task(lecture)
        assignment_id = add_task(assignment)
        midterm_id = add_task(midterm)
        inserted_ids = [lecture_id, assignment_id, midterm_id]
        print(f"Inserted ids: {inserted_ids}")

        print("\n=== get_task() ===")
        fetched = get_task(lecture_id)
        print(f"Round-tripped lecture: {fetched}")
        assert fetched is not None
        assert fetched.title == lecture.title
        assert fetched.due_date == lecture.due_date
        assert fetched.created_at is not None, "created_at should be set by DB"

        _print_tasks(
            "=== get_tasks_for_date(2026-04-23) ===",
            get_tasks_for_date(date(2026, 4, 23)),
        )

        _print_tasks(
            "=== get_tasks_for_week(3) ===",
            get_tasks_for_week(3),
        )

        _print_tasks(
            "=== get_tasks_for_week(4, ['assignment']) ===",
            get_tasks_for_week(4, [TASK_TYPE_ASSIGNMENT]),
        )

        _print_tasks(
            "=== get_tasks_for_week(4, ['lecture']) (filter should exclude) ===",
            get_tasks_for_week(4, [TASK_TYPE_LECTURE]),
        )

        _print_tasks(
            "=== get_semester_deadlines() ===",
            get_semester_deadlines(),
        )

        _print_tasks(
            "=== get_all_pending() (before mark_complete) ===",
            [t for t in get_all_pending() if t.id in inserted_ids],
        )

        print("\n=== mark_complete(lecture_id) ===")
        print(f"mark_complete returned: {mark_complete(lecture_id)}")

        _print_tasks(
            "=== get_all_pending() (after mark_complete — lecture should be gone) ===",
            [t for t in get_all_pending() if t.id in inserted_ids],
        )

        print("\n=== update_task() ===")
        assignment_row = get_task(assignment_id)
        assert assignment_row is not None
        assignment_row.title = "[TEST] CS2040 Assignment 2 (RENAMED)"
        assignment_row.notes = "Updated notes"
        updated = update_task(assignment_row)
        print(f"update_task returned: {updated}")
        reloaded = get_task(assignment_id)
        print(f"Reloaded: title={reloaded.title!r}  notes={reloaded.notes!r}")

    finally:
        print("\n=== cleanup: delete_task() x3 ===")
        for tid in inserted_ids:
            removed = delete_task(tid)
            print(f"  delete_task({tid}) -> {removed}")

        remaining = [t for t in get_all_pending() if t.id in inserted_ids]
        print(f"Remaining test rows after cleanup: {len(remaining)} (expected 0)")

    print("\nDone. Data layer looks healthy.")


if __name__ == "__main__":
    main()
