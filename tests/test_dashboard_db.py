"""Tests for the SQLite persistence layer behind the persistent /deadlines dashboard.

Covers the ``deadline_dashboards`` table added in :mod:`database.db` and proves
the pre-existing ``tasks`` CRUD path is undisturbed by it.

Isolation
---------
``database.db`` resolves ``DB_PATH`` **at import time** from the
``TASK_BOT_DB_PATH`` environment variable, so the variable is pointed at a
throwaway file *before* the first import below, and re-pointed at a fresh
per-test temp directory (followed by ``importlib.reload``) in ``setUp``.
Every test asserts the module is actually looking at its temp file and not at
the project's real ``data/tasks.db``, so a regression in the isolation shows up
as a failure rather than as silent damage to real data.

Standard library only: ``unittest`` + ``sqlite3``. No Telegram object is
constructed and no network call is possible from this module.
"""
from __future__ import annotations

import importlib
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

# --------------------------------------------------------------------------
# Point the DB at a scratch file BEFORE database.db is imported for the first
# time. database.db calls load_dotenv(), which never overrides an existing
# environment variable, so this assignment wins over any .env on the machine.
# --------------------------------------------------------------------------
_IMPORT_GUARD_DIR: str = tempfile.mkdtemp(prefix="taskbot-dashboard-import-")
os.environ["TASK_BOT_DB_PATH"] = str(Path(_IMPORT_GUARD_DIR) / "import-guard.db")
# add_tasks() falls back to get_default_chat_id() when a Task carries no
# chat_id; these tests always pass one explicitly, but keep the env sane.
os.environ.setdefault("MY_TELEGRAM_ID", "424242")

import database.db as db  # noqa: E402  (import must follow the env setup above)
from database.models import Task  # noqa: E402

REAL_DB_PATH: Path = Path(__file__).resolve().parent.parent / "data" / "tasks.db"

OWNER_CHAT_ID: int = 424242
GROUP_CHAT_ID: int = -1001234567890


def tearDownModule() -> None:
    """Remove the pre-import scratch directory once the module is done."""
    shutil.rmtree(_IMPORT_GUARD_DIR, ignore_errors=True)


class DashboardDBTestCase(unittest.TestCase):
    """Base case: a fresh, initialised, temp-file database per test."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="taskbot-dashboard-db-")
        self.addCleanup(shutil.rmtree, self._tmpdir, True)

        self.db_path = Path(self._tmpdir) / "tasks.db"
        previous = os.environ.get("TASK_BOT_DB_PATH")
        os.environ["TASK_BOT_DB_PATH"] = str(self.db_path)
        self.addCleanup(self._restore_env, previous)

        # DB_PATH is a module constant, so the module must be re-executed for
        # the new path to take effect.
        importlib.reload(db)

        # Prove the isolation actually happened before touching anything.
        self.assertEqual(
            db.DB_PATH,
            self.db_path.resolve(),
            "database.db is not pointing at this test's temp file",
        )
        self.assertNotEqual(
            db.DB_PATH,
            REAL_DB_PATH,
            "database.db is pointing at the real project database",
        )
        self.assertTrue(
            str(db.DB_PATH).startswith(str(Path(self._tmpdir).resolve())),
            f"DB_PATH {db.DB_PATH} escaped the temp directory {self._tmpdir}",
        )

        db.init_db()
        self.assertTrue(self.db_path.exists(), "init_db() did not create the file")

    @staticmethod
    def _restore_env(previous: str | None) -> None:
        if previous is None:
            os.environ.pop("TASK_BOT_DB_PATH", None)
        else:
            os.environ["TASK_BOT_DB_PATH"] = previous

    # -- raw SQL helpers (deliberately bypass the module under test) --------

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """Run ``sql`` against the temp DB on an independent connection."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def scalar(self, sql: str, params: tuple = ()) -> object:
        return self.query(sql, params)[0][0]

    def dashboard_row_count(self, chat_id: int | None = None) -> int:
        if chat_id is None:
            return int(self.scalar("SELECT COUNT(*) FROM deadline_dashboards"))
        return int(
            self.scalar(
                "SELECT COUNT(*) FROM deadline_dashboards WHERE chat_id = ?",
                (chat_id,),
            )
        )

    @staticmethod
    def make_task(**overrides) -> Task:
        fields = {
            "title": "Data Structures Assignment 2",
            "task_type": "assignment",
            "due_date": date(2026, 9, 30),
            "chat_id": OWNER_CHAT_ID,
            "module_code": "CS2040",
            "notes": "Submit on LumiNUS",
            "due_time": "23:59",
        }
        fields.update(overrides)
        return Task(**fields)


# ===========================================================================
# 1. Table creation
# ===========================================================================
class TestSchemaCreation(DashboardDBTestCase):
    """init_db() creates deadline_dashboards on a brand-new database."""

    def test_table_exists_after_init_db(self) -> None:
        rows = self.query(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'deadline_dashboards'"
        )
        self.assertEqual(
            [r["name"] for r in rows],
            ["deadline_dashboards"],
            "init_db() did not create the deadline_dashboards table",
        )

    def test_table_has_expected_columns(self) -> None:
        info = self.query("PRAGMA table_info(deadline_dashboards)")
        columns = {row["name"]: row for row in info}
        self.assertEqual(set(columns), {"chat_id", "message_id", "updated_at"})
        # chat_id is the primary key: one dashboard per chat, enforced by SQLite.
        self.assertEqual(columns["chat_id"]["pk"], 1)
        self.assertEqual(columns["message_id"]["notnull"], 1)
        self.assertEqual(columns["updated_at"]["notnull"], 1)

    def test_fresh_table_is_empty(self) -> None:
        self.assertEqual(self.dashboard_row_count(), 0)
        self.assertEqual(db.list_deadline_dashboards(), [])
        self.assertIsNone(db.get_deadline_dashboard_message_id(OWNER_CHAT_ID))

    def test_tasks_table_still_created_alongside(self) -> None:
        names = {
            r["name"]
            for r in self.query(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertIn("tasks", names)
        self.assertIn("modules", names)
        self.assertIn("deadline_dashboards", names)


# ===========================================================================
# 2 & 3. Save / read-back and upsert semantics
# ===========================================================================
class TestSaveAndRead(DashboardDBTestCase):

    def test_save_then_read_returns_same_message_id(self) -> None:
        db.save_deadline_dashboard(OWNER_CHAT_ID, 555)
        self.assertEqual(db.get_deadline_dashboard_message_id(OWNER_CHAT_ID), 555)

    def test_read_returns_an_int(self) -> None:
        db.save_deadline_dashboard(OWNER_CHAT_ID, 555)
        message_id = db.get_deadline_dashboard_message_id(OWNER_CHAT_ID)
        self.assertIsInstance(message_id, int)

    def test_unknown_chat_reads_back_none(self) -> None:
        db.save_deadline_dashboard(OWNER_CHAT_ID, 555)
        self.assertIsNone(db.get_deadline_dashboard_message_id(GROUP_CHAT_ID))

    def test_negative_group_chat_id_round_trips(self) -> None:
        # Telegram supergroup ids are large negatives; make sure nothing coerces
        # them to unsigned or to text.
        db.save_deadline_dashboard(GROUP_CHAT_ID, 98765)
        self.assertEqual(db.get_deadline_dashboard_message_id(GROUP_CHAT_ID), 98765)

    def test_save_records_an_iso_timestamp(self) -> None:
        db.save_deadline_dashboard(OWNER_CHAT_ID, 555)
        updated_at = self.scalar(
            "SELECT updated_at FROM deadline_dashboards WHERE chat_id = ?",
            (OWNER_CHAT_ID,),
        )
        self.assertIsInstance(updated_at, str)
        # Must parse: the column is documented as an ISO-8601 string.
        datetime.fromisoformat(str(updated_at))

    def test_resaving_same_chat_replaces_message_id(self) -> None:
        db.save_deadline_dashboard(OWNER_CHAT_ID, 555)
        db.save_deadline_dashboard(OWNER_CHAT_ID, 777)

        self.assertEqual(db.get_deadline_dashboard_message_id(OWNER_CHAT_ID), 777)
        self.assertEqual(
            self.dashboard_row_count(OWNER_CHAT_ID),
            1,
            "re-registering a dashboard must upsert, not accumulate rows",
        )
        self.assertEqual(self.dashboard_row_count(), 1)
        self.assertEqual(db.list_deadline_dashboards(), [(OWNER_CHAT_ID, 777)])

    def test_many_resaves_keep_exactly_one_row(self) -> None:
        for message_id in range(100, 110):
            db.save_deadline_dashboard(GROUP_CHAT_ID, message_id)
        self.assertEqual(self.dashboard_row_count(), 1)
        self.assertEqual(db.get_deadline_dashboard_message_id(GROUP_CHAT_ID), 109)

    def test_resaving_same_message_id_is_a_noop(self) -> None:
        db.save_deadline_dashboard(OWNER_CHAT_ID, 555)
        db.save_deadline_dashboard(OWNER_CHAT_ID, 555)
        self.assertEqual(self.dashboard_row_count(), 1)
        self.assertEqual(db.get_deadline_dashboard_message_id(OWNER_CHAT_ID), 555)


# ===========================================================================
# 4 & 5. Multiple chats
# ===========================================================================
class TestMultipleChats(DashboardDBTestCase):

    def setUp(self) -> None:
        super().setUp()
        db.save_deadline_dashboard(OWNER_CHAT_ID, 11)
        db.save_deadline_dashboard(GROUP_CHAT_ID, 22)

    def test_each_chat_keeps_its_own_record(self) -> None:
        self.assertEqual(db.get_deadline_dashboard_message_id(OWNER_CHAT_ID), 11)
        self.assertEqual(db.get_deadline_dashboard_message_id(GROUP_CHAT_ID), 22)
        self.assertEqual(self.dashboard_row_count(), 2)

    def test_updating_one_chat_leaves_the_other_alone(self) -> None:
        db.save_deadline_dashboard(OWNER_CHAT_ID, 33)
        self.assertEqual(db.get_deadline_dashboard_message_id(OWNER_CHAT_ID), 33)
        self.assertEqual(db.get_deadline_dashboard_message_id(GROUP_CHAT_ID), 22)
        self.assertEqual(self.dashboard_row_count(), 2)

    def test_list_returns_both_chats(self) -> None:
        listed = db.list_deadline_dashboards()
        self.assertEqual(len(listed), 2)
        self.assertEqual(set(listed), {(OWNER_CHAT_ID, 11), (GROUP_CHAT_ID, 22)})

    def test_list_is_ordered_by_chat_id(self) -> None:
        listed = db.list_deadline_dashboards()
        self.assertEqual(listed, sorted(listed))
        # Group ids are negative, so the group sorts first.
        self.assertEqual(listed[0], (GROUP_CHAT_ID, 22))

    def test_list_entries_are_int_pairs(self) -> None:
        for chat_id, message_id in db.list_deadline_dashboards():
            self.assertIsInstance(chat_id, int)
            self.assertIsInstance(message_id, int)


# ===========================================================================
# 6. Deletion
# ===========================================================================
class TestDeletion(DashboardDBTestCase):

    def setUp(self) -> None:
        super().setUp()
        db.save_deadline_dashboard(OWNER_CHAT_ID, 11)
        db.save_deadline_dashboard(GROUP_CHAT_ID, 22)

    def test_delete_returns_true_and_removes_only_that_chat(self) -> None:
        self.assertTrue(db.delete_deadline_dashboard(OWNER_CHAT_ID))

        self.assertIsNone(db.get_deadline_dashboard_message_id(OWNER_CHAT_ID))
        self.assertEqual(db.get_deadline_dashboard_message_id(GROUP_CHAT_ID), 22)
        self.assertEqual(db.list_deadline_dashboards(), [(GROUP_CHAT_ID, 22)])
        self.assertEqual(self.dashboard_row_count(), 1)

    def test_deleting_twice_reports_false_the_second_time(self) -> None:
        self.assertTrue(db.delete_deadline_dashboard(OWNER_CHAT_ID))
        # Second call removed nothing, so it must report False.
        self.assertFalse(db.delete_deadline_dashboard(OWNER_CHAT_ID))
        self.assertEqual(self.dashboard_row_count(), 1)

    def test_delete_unknown_chat_returns_false_and_changes_nothing(self) -> None:
        self.assertFalse(db.delete_deadline_dashboard(999_999_999))
        self.assertEqual(self.dashboard_row_count(), 2)
        self.assertEqual(db.get_deadline_dashboard_message_id(OWNER_CHAT_ID), 11)
        self.assertEqual(db.get_deadline_dashboard_message_id(GROUP_CHAT_ID), 22)

    def test_delete_on_empty_table_returns_false(self) -> None:
        self.assertTrue(db.delete_deadline_dashboard(OWNER_CHAT_ID))
        self.assertTrue(db.delete_deadline_dashboard(GROUP_CHAT_ID))
        self.assertEqual(db.list_deadline_dashboards(), [])
        self.assertFalse(db.delete_deadline_dashboard(OWNER_CHAT_ID))

    def test_chat_can_re_register_after_deletion(self) -> None:
        db.delete_deadline_dashboard(OWNER_CHAT_ID)
        db.save_deadline_dashboard(OWNER_CHAT_ID, 44)
        self.assertEqual(db.get_deadline_dashboard_message_id(OWNER_CHAT_ID), 44)
        self.assertEqual(self.dashboard_row_count(), 2)


# ===========================================================================
# 7. Existing task CRUD is undisturbed
# ===========================================================================
class TestTaskCrudStillWorks(DashboardDBTestCase):

    def setUp(self) -> None:
        super().setUp()
        # A live dashboard registration is present throughout, so any coupling
        # between the two tables would surface here.
        db.save_deadline_dashboard(OWNER_CHAT_ID, 11)

    def test_add_and_get_task(self) -> None:
        task_id = db.add_task(self.make_task())
        self.assertIsInstance(task_id, int)

        stored = db.get_task(task_id, OWNER_CHAT_ID)
        self.assertIsNotNone(stored)
        assert stored is not None  # narrowing for readers
        self.assertEqual(stored.id, task_id)
        self.assertEqual(stored.chat_id, OWNER_CHAT_ID)
        self.assertEqual(stored.title, "Data Structures Assignment 2")
        self.assertEqual(stored.task_type, "assignment")
        self.assertEqual(stored.module_code, "CS2040")
        self.assertEqual(stored.due_date, date(2026, 9, 30))
        self.assertEqual(stored.due_time, "23:59")
        self.assertFalse(stored.completed)
        self.assertIsInstance(stored.created_at, datetime)

    def test_get_task_is_chat_scoped(self) -> None:
        task_id = db.add_task(self.make_task())
        self.assertIsNone(db.get_task(task_id, GROUP_CHAT_ID))

    def test_update_task(self) -> None:
        task_id = db.add_task(self.make_task())
        stored = db.get_task(task_id, OWNER_CHAT_ID)
        assert stored is not None

        stored.title = "Data Structures Assignment 2 (extended)"
        stored.due_date = date(2026, 10, 7)
        stored.due_time = None
        stored.completed = True
        self.assertTrue(db.update_task(stored))

        reloaded = db.get_task(task_id, OWNER_CHAT_ID)
        assert reloaded is not None
        self.assertEqual(reloaded.title, "Data Structures Assignment 2 (extended)")
        self.assertEqual(reloaded.due_date, date(2026, 10, 7))
        self.assertIsNone(reloaded.due_time)
        self.assertTrue(reloaded.completed)

    def test_mark_complete_and_pending_queries(self) -> None:
        pending_id = db.add_task(self.make_task(title="Lab 3", task_type="lab"))
        db.add_task(self.make_task(title="Quiz 1", task_type="quiz"))

        self.assertEqual(len(db.get_all_pending(OWNER_CHAT_ID)), 2)
        self.assertEqual(len(db.get_semester_deadlines(OWNER_CHAT_ID)), 2)

        self.assertTrue(db.mark_complete(pending_id, OWNER_CHAT_ID))
        remaining = db.get_all_pending(OWNER_CHAT_ID)
        self.assertEqual([t.title for t in remaining], ["Quiz 1"])

    def test_delete_task(self) -> None:
        task_id = db.add_task(self.make_task())
        self.assertTrue(db.delete_task(task_id, OWNER_CHAT_ID))
        self.assertIsNone(db.get_task(task_id, OWNER_CHAT_ID))
        self.assertFalse(db.delete_task(task_id, OWNER_CHAT_ID))

    def test_count_tasks_global_and_per_chat(self) -> None:
        db.add_task(self.make_task())
        db.add_task(self.make_task(title="Group project", chat_id=GROUP_CHAT_ID))

        self.assertEqual(db.count_tasks(), 2)
        self.assertEqual(db.count_tasks(OWNER_CHAT_ID), 1)
        self.assertEqual(db.count_tasks(GROUP_CHAT_ID), 1)
        self.assertEqual(db.count_tasks(999_999_999), 0)

    def test_task_mutations_do_not_touch_dashboard_rows(self) -> None:
        task_id = db.add_task(self.make_task())
        db.mark_complete(task_id, OWNER_CHAT_ID)
        db.delete_task(task_id, OWNER_CHAT_ID)
        db.delete_all_tasks(OWNER_CHAT_ID)

        self.assertEqual(db.count_tasks(OWNER_CHAT_ID), 0)
        self.assertEqual(db.get_deadline_dashboard_message_id(OWNER_CHAT_ID), 11)

    def test_dashboard_mutations_do_not_touch_tasks(self) -> None:
        task_id = db.add_task(self.make_task())

        db.save_deadline_dashboard(OWNER_CHAT_ID, 22)
        db.delete_deadline_dashboard(OWNER_CHAT_ID)

        self.assertEqual(db.count_tasks(OWNER_CHAT_ID), 1)
        self.assertIsNotNone(db.get_task(task_id, OWNER_CHAT_ID))


# ===========================================================================
# 8. init_db() idempotency
# ===========================================================================
class TestInitDbIdempotency(DashboardDBTestCase):

    def test_second_init_db_preserves_dashboards_and_tasks(self) -> None:
        db.save_deadline_dashboard(OWNER_CHAT_ID, 11)
        db.save_deadline_dashboard(GROUP_CHAT_ID, 22)
        task_id = db.add_task(self.make_task())

        db.init_db()  # simulates a bot restart against an existing database

        self.assertEqual(db.get_deadline_dashboard_message_id(OWNER_CHAT_ID), 11)
        self.assertEqual(db.get_deadline_dashboard_message_id(GROUP_CHAT_ID), 22)
        self.assertEqual(
            set(db.list_deadline_dashboards()),
            {(OWNER_CHAT_ID, 11), (GROUP_CHAT_ID, 22)},
        )
        self.assertEqual(self.dashboard_row_count(), 2)

        stored = db.get_task(task_id, OWNER_CHAT_ID)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.title, "Data Structures Assignment 2")
        self.assertEqual(db.count_tasks(), 1)

    def test_repeated_init_db_keeps_a_single_table_definition(self) -> None:
        db.save_deadline_dashboard(OWNER_CHAT_ID, 11)
        db.init_db()
        db.init_db()
        db.init_db()

        tables = self.query(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'deadline_dashboards'"
        )
        self.assertEqual(len(tables), 1)
        self.assertEqual(db.get_deadline_dashboard_message_id(OWNER_CHAT_ID), 11)
        self.assertEqual(self.dashboard_row_count(), 1)

    def test_registration_survives_a_simulated_process_restart(self) -> None:
        db.save_deadline_dashboard(GROUP_CHAT_ID, 4242)

        # Reload the module (new connections, same file) exactly as a restarted
        # process would, then re-run startup init.
        importlib.reload(db)
        self.assertEqual(db.DB_PATH, self.db_path.resolve())
        db.init_db()

        self.assertEqual(db.list_deadline_dashboards(), [(GROUP_CHAT_ID, 4242)])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
