"""Bulk-load tasks into the Task-Bot SQLite database from a CSV file.

Usage (from the project root with the venv active):

    python seed/seed_tasks.py                  # append to existing data
    python seed/seed_tasks.py --replace        # replace owner's personal tasks (prompts y/n)
    python seed/seed_tasks.py --file path.csv  # load from a different CSV

The CSV must have one of these header rows:

    title,task_type,module_code,due_date,week_number,notes
    title,task_type,module_code,due_date,due_time,week_number,notes

Lines whose first non-whitespace character is ``#`` are comments and skipped;
so are blank lines. The header row must be the first non-comment line.

Every row is validated BEFORE anything is written. If one row fails, the
script reports the source line number and reason and exits without writing
to the database — partial imports are impossible.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# Make the project root importable when invoked as ``python seed/seed_tasks.py``.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from database.db import (  # noqa: E402  (sys.path hack above is intentional)
    add_tasks,
    count_tasks,
    delete_all_tasks,
    get_default_chat_id,
    init_db,
)
from database.models import (  # noqa: E402
    TASK_TYPES,
    Task,
)

BASE_COLUMNS: tuple[str, ...] = (
    "title",
    "task_type",
    "module_code",
    "due_date",
    "week_number",
    "notes",
)
TIME_COLUMNS: tuple[str, ...] = (
    "title",
    "task_type",
    "module_code",
    "due_date",
    "due_time",
    "week_number",
    "notes",
)
ACCEPTED_COLUMNS: tuple[tuple[str, ...], ...] = (BASE_COLUMNS, TIME_COLUMNS)
DEFAULT_CSV_PATH: Path = _PROJECT_ROOT / "seed" / "seed_data.csv"

_PLURAL_LABEL: dict[str, str] = {
    "quiz": "quizzes",
    "lab": "labs",
    "assignment": "assignments",
    "project": "projects",
    "midterm": "midterms",
    "final": "finals",
    "other": "other deadlines",
}


@dataclass
class _RawRow:
    """A parsed CSV row plus the 1-indexed line number in the source file."""

    line_no: int
    data: dict[str, str]


def _read_rows(path: Path) -> list[_RawRow]:
    """Read ``path``, skip comments and blanks, return parsed rows with line numbers.

    ``utf-8-sig`` encoding strips any BOM that Excel may add on save, so the
    header check doesn't fail mysteriously on ``\\ufefftitle``. Raises
    ``FileNotFoundError`` or ``ValueError`` with a clear message if the file
    is missing, empty, or has the wrong header.
    """
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        raw_lines = f.readlines()

    kept: list[tuple[int, str]] = []
    for i, line in enumerate(raw_lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        kept.append((i, line))

    if not kept:
        raise ValueError(
            f"{path} contains no data rows (only comments and blanks)."
        )

    header_line = kept[0][1]
    data_entries = kept[1:]

    reader = csv.DictReader([header_line] + [ln for _, ln in data_entries])
    fieldnames = tuple(reader.fieldnames or ())
    if fieldnames not in ACCEPTED_COLUMNS:
        raise ValueError(
            "CSV header must be one of:\n  "
            + "\n  ".join(",".join(columns) for columns in ACCEPTED_COLUMNS)
            + f"\n  got: {reader.fieldnames}"
        )

    rows: list[_RawRow] = []
    for (line_no, _), parsed in zip(data_entries, reader):
        rows.append(_RawRow(line_no=line_no, data=parsed))
    return rows


def _validate_row(raw: _RawRow) -> Task:
    """Convert a raw CSV row into a ``Task`` or raise ``ValueError`` with a reason.

    Validation rules:
      - title: non-empty
      - task_type: one of ``TASK_TYPES``
      - module_code: required
      - due_date: parseable as ``YYYY-MM-DD``
      - due_time: blank, or ``HH:MM`` 24-hour time
      - week_number: blank, or integer 1..13
      - notes: any string, or blank
    """
    data = raw.data

    title = (data.get("title") or "").strip()
    if not title:
        raise ValueError("title is required")

    task_type = (data.get("task_type") or "").strip().lower()
    if task_type not in TASK_TYPES:
        raise ValueError(
            f"task_type {task_type!r} not in allowed set: "
            f"{', '.join(TASK_TYPES)}"
        )

    module_code = (data.get("module_code") or "").strip() or None
    if module_code is None:
        raise ValueError("module_code is required")

    due_raw = (data.get("due_date") or "").strip()
    if not due_raw:
        raise ValueError("due_date is required")
    try:
        due = date.fromisoformat(due_raw)
    except ValueError:
        raise ValueError(
            f"due_date {due_raw!r} is not valid YYYY-MM-DD"
        ) from None

    due_time = (data.get("due_time") or "").strip() or None
    if due_time is not None:
        try:
            hour_str, minute_str = due_time.split(":", 1)
            hour = int(hour_str)
            minute = int(minute_str)
        except ValueError:
            raise ValueError(
                f"due_time {due_time!r} is not valid HH:MM"
            ) from None
        if not (
            len(hour_str) == 2
            and len(minute_str) == 2
            and 0 <= hour <= 23
            and 0 <= minute <= 59
        ):
            raise ValueError(
                f"due_time {due_time!r} is not valid HH:MM"
            )

    week_raw = (data.get("week_number") or "").strip()
    week_number: int | None
    if week_raw:
        try:
            week_number = int(week_raw)
        except ValueError:
            raise ValueError(
                f"week_number {week_raw!r} is not an integer"
            ) from None
        if not 1 <= week_number <= 13:
            raise ValueError(
                f"week_number {week_number} must be between 1 and 13"
            )
    else:
        week_number = None

    notes = (data.get("notes") or "").strip() or None

    return Task(
        title=title,
        task_type=task_type,
        module_code=module_code,
        due_date=due,
        due_time=due_time,
        week_number=week_number,
        notes=notes,
    )


def _confirm_replace(existing_count: int) -> bool:
    """Prompt the user for y/n confirmation of ``--replace``."""
    prompt = (
        f"--replace will DELETE all {existing_count} existing tasks and "
        "then insert the new ones. Are you sure? (y/n): "
    )
    response = input(prompt).strip().lower()
    return response in ("y", "yes")


def _summary(tasks: list[Task]) -> str:
    """Return a one-line summary of ``tasks`` by type for console output."""
    by_type = Counter(t.task_type for t in tasks)
    parts = [
        f"{by_type[t]} {_PLURAL_LABEL.get(t, t)}"
        for t in TASK_TYPES
        if by_type[t] > 0
    ]
    return f"Imported {len(tasks)} tasks ({', '.join(parts)})"


def main(argv: list[str] | None = None) -> int:
    """Parse args, validate CSV, optionally replace, insert, print a summary."""
    parser = argparse.ArgumentParser(
        description="Bulk-load tasks from a CSV into the Task-Bot SQLite DB.",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Delete the owner's personal tasks before importing (prompts for confirmation).",
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=DEFAULT_CSV_PATH,
        help=f"CSV to import (default: {DEFAULT_CSV_PATH}).",
    )
    args = parser.parse_args(argv)

    try:
        raw_rows = _read_rows(args.file)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if not raw_rows:
        print("ERROR: CSV has no data rows to import.", file=sys.stderr)
        return 1

    # Validate EVERYTHING first; abort on the first failure. This runs before
    # any DB mutation so --replace cannot destroy data when the CSV is broken.
    tasks: list[Task] = []
    for raw in raw_rows:
        try:
            tasks.append(_validate_row(raw))
        except ValueError as exc:
            print(
                f"ERROR: line {raw.line_no}: {exc}\n  row: {raw.data}",
                file=sys.stderr,
            )
            print("Aborting — no rows inserted.", file=sys.stderr)
            return 1

    init_db()
    target_chat_id = get_default_chat_id()
    for task in tasks:
        task.chat_id = target_chat_id

    if args.replace:
        existing = count_tasks(target_chat_id)
        if not _confirm_replace(existing):
            print("Aborted. No changes made.")
            return 1
        deleted = delete_all_tasks(target_chat_id)
        print(f"Deleted {deleted} existing tasks.")

    add_tasks(tasks)

    print(_summary(tasks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
