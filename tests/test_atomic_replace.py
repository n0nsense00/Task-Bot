"""Regression tests for atomic seed replacement operations."""
from __future__ import annotations

import importlib
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

_IMPORT_GUARD_DIR = tempfile.mkdtemp(prefix="taskbot-atomic-import-")
os.environ["TASK_BOT_DB_PATH"] = str(Path(_IMPORT_GUARD_DIR) / "guard.db")
os.environ.setdefault("MY_TELEGRAM_ID", "424242")

import database.db as db  # noqa: E402
from database.models import Module, Task  # noqa: E402

OWNER_CHAT_ID = 424242
GROUP_CHAT_ID = -1001234567890


def tearDownModule() -> None:
    shutil.rmtree(_IMPORT_GUARD_DIR, ignore_errors=True)


class AtomicReplaceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="taskbot-atomic-replace-")
        self.addCleanup(shutil.rmtree, self._tmpdir, True)
        self.db_path = Path(self._tmpdir) / "tasks.db"
        previous = os.environ.get("TASK_BOT_DB_PATH")
        os.environ["TASK_BOT_DB_PATH"] = str(self.db_path)
        self.addCleanup(self._restore_env, previous)
        importlib.reload(db)
        db.init_db()

    @staticmethod
    def _restore_env(previous: str | None) -> None:
        if previous is None:
            os.environ.pop("TASK_BOT_DB_PATH", None)
        else:
            os.environ["TASK_BOT_DB_PATH"] = previous

    @staticmethod
    def task(
        title: str,
        *,
        chat_id: int = OWNER_CHAT_ID,
        task_type: str = "assignment",
    ) -> Task:
        return Task(
            title=title,
            task_type=task_type,
            due_date=date(2026, 10, 1),
            chat_id=chat_id,
            module_code="SC2002",
        )

    def module_rows(self) -> list[tuple[str, str | None]]:
        conn = sqlite3.connect(str(self.db_path))
        try:
            return conn.execute(
                "SELECT code, name FROM modules ORDER BY code"
            ).fetchall()
        finally:
            conn.close()

    def test_replace_tasks_replaces_only_target_chat(self) -> None:
        db.add_task(self.task("Old personal"))
        db.add_task(self.task("Group task", chat_id=GROUP_CHAT_ID))
        replacements = [
            self.task("New quiz", chat_id=GROUP_CHAT_ID, task_type="quiz"),
            self.task("New final", chat_id=GROUP_CHAT_ID, task_type="final"),
        ]

        ids = db.replace_tasks(OWNER_CHAT_ID, replacements)

        self.assertEqual(len(ids), 2)
        self.assertEqual(
            [task.title for task in db.get_all_pending(OWNER_CHAT_ID)],
            ["New final", "New quiz"],
        )
        self.assertEqual(
            [task.title for task in db.get_all_pending(GROUP_CHAT_ID)],
            ["Group task"],
        )
        self.assertEqual(
            [task.chat_id for task in replacements], [OWNER_CHAT_ID] * 2
        )

    def test_replace_tasks_rolls_back_delete_and_partial_inserts(self) -> None:
        old_id = db.add_task(self.task("Keep me"))
        invalid_batch = [
            self.task("Would be inserted"),
            self.task("Invalid type", task_type="not-a-real-type"),
        ]

        with self.assertRaises(sqlite3.IntegrityError):
            db.replace_tasks(OWNER_CHAT_ID, invalid_batch)

        stored = db.get_task(old_id, OWNER_CHAT_ID)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.title, "Keep me")
        self.assertEqual(db.count_tasks(OWNER_CHAT_ID), 1)

    def test_replace_modules_replaces_complete_catalogue(self) -> None:
        db.add_module(Module("OLD1000", "Old module"))

        inserted = db.replace_modules(
            [Module("SC2001", "Algorithms"), Module("SC2002", "OOP")]
        )

        self.assertEqual(inserted, 2)
        self.assertEqual(
            self.module_rows(),
            [("SC2001", "Algorithms"), ("SC2002", "OOP")],
        )

    def test_replace_modules_rolls_back_delete_and_partial_inserts(self) -> None:
        db.add_module(Module("OLD1000", "Keep me"))
        invalid_batch = [
            Module("SC2001", "Would be inserted"),
            # sqlite3 cannot bind a plain object, so this deterministically
            # fails after the first replacement row has been inserted.
            Module(object(), "Invalid code"),  # type: ignore[arg-type]
        ]

        with self.assertRaises(sqlite3.ProgrammingError):
            db.replace_modules(invalid_batch)

        self.assertEqual(self.module_rows(), [("OLD1000", "Keep me")])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
