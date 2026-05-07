"""SQLite data layer for Task-Bot.

Schema lives in a single ``tasks`` table. Dates are stored as ISO strings
(``YYYY-MM-DD``) and converted to ``datetime.date`` on read; booleans are
stored as ``0``/``1`` integers. All queries go through ``_get_conn``, a
context manager that commits on clean exit, rolls back on exception, and
always closes the connection.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator, Optional

from database.models import Module, Task

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    pass
else:
    load_dotenv()

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
_DB_PATH_ENV: str = "TASK_BOT_DB_PATH"
DB_PATH: Path = (
    Path(os.environ[_DB_PATH_ENV]).expanduser().resolve()
    if os.getenv(_DB_PATH_ENV)
    else PROJECT_ROOT / "data" / "tasks.db"
)

_SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL,
    task_type   TEXT    NOT NULL CHECK(task_type IN
                    ('lecture','tutorial','assignment','midterm','final','personal')),
    module_code TEXT,
    due_date    TEXT    NOT NULL,
    due_time    TEXT,
    week_number INTEGER,
    notes       TEXT,
    completed   INTEGER NOT NULL DEFAULT 0 CHECK(completed IN (0, 1)),
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_due_date    ON tasks(due_date);
CREATE INDEX IF NOT EXISTS idx_tasks_week_number ON tasks(week_number);
CREATE INDEX IF NOT EXISTS idx_tasks_type        ON tasks(task_type);

CREATE TABLE IF NOT EXISTS modules (
    code TEXT PRIMARY KEY,
    name TEXT
);
"""

_TASK_INSERT_SQL: str = """
INSERT INTO tasks
    (title, task_type, module_code, due_date, due_time,
     week_number, notes, completed, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply additive schema changes that ``CREATE TABLE IF NOT EXISTS`` can't.

    SQLite's ``IF NOT EXISTS`` only creates tables — it doesn't add columns to
    existing tables. So when a deployed bot already has a ``tasks`` table from
    an earlier schema, we need ``ALTER TABLE ADD COLUMN`` to bring it up to
    date. Idempotent: re-running is a no-op.
    """
    cur = conn.execute("PRAGMA table_info(tasks)")
    columns = {row[1] for row in cur.fetchall()}
    if "due_time" not in columns:
        logger.info("Migrating: adding due_time column to tasks")
        conn.execute("ALTER TABLE tasks ADD COLUMN due_time TEXT")


@contextmanager
def _get_conn() -> Iterator[sqlite3.Connection]:
    """Yield a configured SQLite connection.

    Row access is named (``sqlite3.Row``). Commits on clean exit, rolls back
    on exception, and always closes the underlying connection.
    """
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _row_to_task(row: sqlite3.Row) -> Task:
    """Convert a ``sqlite3.Row`` from the ``tasks`` table into a ``Task``."""
    # ``due_time`` may be absent on rows from a pre-migration schema even
    # after the ALTER TABLE ran (rows persisted before the alter return
    # NULL for the new column, which sqlite3.Row exposes as None — handle
    # the missing-key case defensively for safety regardless).
    try:
        due_time = row["due_time"]
    except (IndexError, KeyError):
        due_time = None
    return Task(
        id=row["id"],
        title=row["title"],
        task_type=row["task_type"],
        module_code=row["module_code"],
        due_date=date.fromisoformat(row["due_date"]),
        week_number=row["week_number"],
        notes=row["notes"],
        completed=bool(row["completed"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        due_time=due_time,
    )


def _row_to_module(row: sqlite3.Row) -> Module:
    """Convert a ``sqlite3.Row`` from the ``modules`` table into a ``Module``."""
    return Module(code=row["code"], name=row["name"])


def init_db() -> None:
    """Create the ``data/`` directory and required tables, then run migrations.

    Idempotent — safe to call on every bot startup. ``CREATE TABLE IF NOT
    EXISTS`` handles the new-DB case; :func:`_migrate_schema` covers the
    case where an older ``tasks`` schema exists from before ``due_time``
    was introduced.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _get_conn() as conn:
        conn.executescript(_SCHEMA_SQL)
        _migrate_schema(conn)
    logger.info("Database initialized at %s", DB_PATH)


def add_task(task: Task) -> int:
    """Insert ``task`` and return the new row id.

    Ignores ``task.id`` and ``task.created_at`` on input — the DB assigns both.
    """
    return add_tasks([task])[0]


def add_tasks(tasks: list[Task]) -> list[int]:
    """Insert ``tasks`` in one transaction and return their new row ids.

    Used by seed/stress paths where opening one SQLite connection per row is
    unnecessarily slow. ``add_task`` remains the simple single-row wrapper used
    by interactive Telegram handlers.
    """
    if not tasks:
        return []
    created_at = datetime.now().isoformat(timespec="seconds")
    ids: list[int] = []
    with _get_conn() as conn:
        for task in tasks:
            cur = conn.execute(
                _TASK_INSERT_SQL,
                (
                    task.title,
                    task.task_type,
                    task.module_code,
                    task.due_date.isoformat(),
                    task.due_time,
                    task.week_number,
                    task.notes,
                    1 if task.completed else 0,
                    created_at,
                ),
            )
            ids.append(int(cur.lastrowid))
    return ids


def get_task(task_id: int) -> Optional[Task]:
    """Return the ``Task`` with id ``task_id``, or ``None`` if not found."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    return _row_to_task(row) if row else None


def update_task(task: Task) -> bool:
    """Overwrite the row identified by ``task.id``. Returns ``True`` on hit.

    Raises ``ValueError`` if ``task.id`` is ``None`` — updates require a
    persisted row. ``created_at`` is not modified.
    """
    if task.id is None:
        raise ValueError("update_task requires task.id to be set")
    with _get_conn() as conn:
        cur = conn.execute(
            """UPDATE tasks
                 SET title = ?, task_type = ?, module_code = ?, due_date = ?,
                     due_time = ?, week_number = ?, notes = ?, completed = ?
               WHERE id = ?""",
            (
                task.title,
                task.task_type,
                task.module_code,
                task.due_date.isoformat(),
                task.due_time,
                task.week_number,
                task.notes,
                1 if task.completed else 0,
                task.id,
            ),
        )
        return cur.rowcount > 0


def delete_task(task_id: int) -> bool:
    """Delete the row with id ``task_id``. Returns ``True`` if a row was removed."""
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cur.rowcount > 0


def mark_complete(task_id: int) -> bool:
    """Mark the row with id ``task_id`` as completed. Returns ``True`` on hit."""
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE tasks SET completed = 1 WHERE id = ?", (task_id,)
        )
        return cur.rowcount > 0


def get_tasks_for_date(target_date: date) -> list[Task]:
    """Return all tasks whose ``due_date`` is exactly ``target_date``."""
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM tasks
               WHERE due_date = ?
               ORDER BY task_type, due_time IS NULL, due_time, title""",
            (target_date.isoformat(),),
        ).fetchall()
    return [_row_to_task(r) for r in rows]


def get_tasks_for_week(
    week_num: int, task_types: Optional[list[str]] = None
) -> list[Task]:
    """Return tasks in ``week_num``, optionally filtered by ``task_types``.

    When ``task_types`` is ``None`` or empty, no type filter is applied.
    Results are sorted by ``due_date``, then type, then title.
    """
    query = "SELECT * FROM tasks WHERE week_number = ?"
    params: list = [week_num]
    if task_types:
        placeholders = ",".join("?" * len(task_types))
        query += f" AND task_type IN ({placeholders})"
        params.extend(task_types)
    query += " ORDER BY due_date, due_time IS NULL, due_time, task_type, title"
    with _get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_task(r) for r in rows]


def get_semester_deadlines() -> list[Task]:
    """Return midterms and finals across the whole semester, sorted by date."""
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM tasks
               WHERE task_type IN ('midterm', 'final')
               ORDER BY due_date, due_time IS NULL, due_time, title""",
        ).fetchall()
    return [_row_to_task(r) for r in rows]


def get_all_pending() -> list[Task]:
    """Return every uncompleted task, earliest due-date first."""
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM tasks
               WHERE completed = 0
               ORDER BY due_date, due_time IS NULL, due_time, title""",
        ).fetchall()
    return [_row_to_task(r) for r in rows]


def count_tasks() -> int:
    """Return the total number of rows in the ``tasks`` table."""
    with _get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
    return int(row[0])


def delete_all_tasks() -> int:
    """Delete every row from ``tasks`` and return the number removed.

    Intended for the bulk-replace path in the seeder. Deliberately not wired
    to any command handler — there is no ``/reset`` in the bot.
    """
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM tasks")
        return cur.rowcount


def cleanup_past_deadlines(today: date) -> int:
    """Delete every midterm/final whose ``due_date`` is strictly before ``today``.

    Scope is intentionally narrow — only ``midterm`` and ``final`` rows are
    auto-purged, because those are the "big deadlines" the owner actively
    wants to disappear after the date passes. Lectures, tutorials,
    assignments, and personal tasks are left alone (manual ``/delete`` only).

    Called by both the daily scheduler job and lazy cleanup on /semester so
    the view always reflects what's still ahead, even if the scheduler has
    been down.
    """
    today_iso = today.isoformat()
    with _get_conn() as conn:
        cur = conn.execute(
            """DELETE FROM tasks
               WHERE task_type IN ('midterm', 'final')
                 AND due_date < ?""",
            (today_iso,),
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# Modules table — populated from seed/seed_modules.csv
# ---------------------------------------------------------------------------

def get_modules() -> list[Module]:
    """Return every module the user has seeded, sorted by code."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM modules ORDER BY code"
        ).fetchall()
    return [_row_to_module(r) for r in rows]


def get_module(code: str) -> Optional[Module]:
    """Return the module matching ``code`` (case-sensitive), or ``None``."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM modules WHERE code = ?", (code,)
        ).fetchone()
    return _row_to_module(row) if row else None


def add_module(module: Module) -> bool:
    """Insert or replace a module row. Returns ``True`` always (no failure mode)."""
    with _get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO modules (code, name) VALUES (?, ?)",
            (module.code, module.name),
        )
    return True


def count_modules() -> int:
    """Return the number of seeded modules."""
    with _get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) FROM modules").fetchone()
    return int(row[0])


def delete_all_modules() -> int:
    """Wipe the modules table. Used by the seeder's --replace path."""
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM modules")
        return cur.rowcount
