"""Old deadline lists stay live across day changes and process restarts."""
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test-token")
os.environ.setdefault("MY_TELEGRAM_ID", "424242")

from telegram.error import BadRequest, RetryAfter, TimedOut
from database import db
from database.models import Task
from handlers import tasks, transient


class DashboardHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path_patch = patch.object(db, "DB_PATH", Path(self.directory.name) / "test.db")
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        db.init_db()
        self.application = Mock()
        self.application.bot.edit_message_text = AsyncMock()

    def add_quiz(self, chat_id, title):
        db.add_task(Task(chat_id=chat_id, title=title, task_type="quiz", due_date=date(2026, 10, 21)))

    async def test_all_lists_roll_over_after_restart_without_crossing_chats(self):
        self.add_quiz(11, "Personal quiz")
        self.add_quiz(-22, "Group quiz")
        for chat_id, message_id in [(11, 100), (11, 101), (-22, 200), (-22, 201)]:
            db.save_deadline_dashboard(chat_id, message_id)
        db.init_db()  # Startup must retain every registration, not just the latest.
        with patch.object(tasks, "today_local", return_value=date(2026, 10, 19)), patch("handlers.tasks.asyncio.sleep", new_callable=AsyncMock):
            await tasks.refresh_all_deadline_dashboards(self.application)
        calls = self.application.bot.edit_message_text.await_args_list
        self.assertEqual({(c.kwargs["chat_id"], c.kwargs["message_id"]) for c in calls}, {(11, 100), (11, 101), (-22, 200), (-22, 201)})
        self.assertEqual(len(calls), 4)
        for call in calls:
            text = call.kwargs["text"]
            self.assertIn("2 days away", text)
            self.assertIn("Personal quiz" if call.kwargs["chat_id"] == 11 else "Group quiz", text)
            self.assertNotIn("Group quiz" if call.kwargs["chat_id"] == 11 else "Personal quiz", text)
        self.application.bot.edit_message_text.reset_mock()
        with patch.object(tasks, "today_local", return_value=date(2026, 10, 20)), patch("handlers.tasks.asyncio.sleep", new_callable=AsyncMock):
            await tasks.refresh_all_deadline_dashboards(self.application)
        for call in self.application.bot.edit_message_text.await_args_list:
            self.assertIn("tomorrow", call.kwargs["text"])

    def test_single_message_schema_migrates_without_losing_existing_rows(self):
        with closing(sqlite3.connect(db.DB_PATH)) as connection, connection:
            connection.execute("DROP TABLE deadline_dashboards")
            connection.execute("CREATE TABLE deadline_dashboards (chat_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL, updated_at TEXT NOT NULL)")
            connection.execute("INSERT INTO deadline_dashboards VALUES (11,100,'2026-09-24')")
        db.init_db()
        db.save_deadline_dashboard(11, 101)
        db.save_deadline_dashboard(11, 99)  # Recover an older message later.
        db.save_deadline_dashboard(11, 100)  # Duplicate registration is harmless.
        db.init_db()
        self.assertEqual(db.list_deadline_dashboards(), [(11, 99), (11, 100), (11, 101)])
        self.assertEqual(db.get_deadline_dashboard_message_id(11), 101)

    async def test_missing_old_message_does_not_unregister_newer_lists(self):
        for message_id in [100, 101, 102]:
            db.save_deadline_dashboard(11, message_id)
        def edit(**kwargs):
            if kwargs["message_id"] == 100:
                raise BadRequest("Message to edit not found")
            if kwargs["message_id"] == 101:
                raise TimedOut("temporary outage")
            return Mock()
        self.application.bot.edit_message_text.side_effect = edit
        with patch("handlers.tasks.asyncio.sleep", new_callable=AsyncMock):
            result = await tasks.refresh_deadline_dashboard(self.application, 11)
        self.assertIs(result, tasks.DashboardRefreshResult.TRANSIENT_FAILURE)
        self.assertEqual(self.application.bot.edit_message_text.await_count, 3)
        self.assertEqual(db.list_deadline_dashboards(), [(11, 101), (11, 102)])

    def test_failed_schema_migration_rolls_back_original_registration(self):
        with closing(sqlite3.connect(db.DB_PATH)) as connection, connection:
            connection.execute("DROP TABLE deadline_dashboards")
            connection.execute("CREATE TABLE deadline_dashboards (chat_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL, updated_at TEXT NOT NULL)")
            connection.execute("INSERT INTO deadline_dashboards VALUES (11,100,'2026-09-24')")
        connect = sqlite3.connect

        def guarded_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)
            def authorize(action, table, *_):
                if action == sqlite3.SQLITE_DROP_TABLE and table == "deadline_dashboards_legacy":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            connection.set_authorizer(authorize)
            return connection

        with patch.object(db.sqlite3, "connect", side_effect=guarded_connect):
            with self.assertRaises(sqlite3.DatabaseError):
                db.init_db()
        with closing(connect(db.DB_PATH)) as connection:
            self.assertEqual(connection.execute("SELECT * FROM deadline_dashboards").fetchall(), [(11, 100, "2026-09-24")])
            keys = [(row[1], row[5]) for row in connection.execute("PRAGMA table_info(deadline_dashboards)") if row[5]]
            self.assertEqual(keys, [("chat_id", 1)])

    async def test_old_list_manage_button_preserves_the_list(self):
        db.save_deadline_dashboard(11, 100)
        db.save_deadline_dashboard(11, 101)
        query = Mock()
        query.message.message_id = 100
        query.message.reply_text = AsyncMock()
        query.edit_message_text = AsyncMock()
        self.assertTrue(await transient.show_transient_view(query, 11, "Manage deadlines"))
        query.message.reply_text.assert_awaited_once()
        query.edit_message_text.assert_not_awaited()

    async def test_rate_limit_stops_batch_and_retains_unfinished_lists(self):
        for message_id in [100, 101, 102]:
            db.save_deadline_dashboard(11, message_id)
        self.application.bot.edit_message_text.side_effect = RetryAfter(30)
        with patch("handlers.tasks.asyncio.sleep", new_callable=AsyncMock):
            result = await tasks.refresh_deadline_dashboard(self.application, 11)
        self.assertIs(result, tasks.DashboardRefreshResult.TRANSIENT_FAILURE)
        self.assertEqual(self.application.bot.edit_message_text.await_count, 1)
        self.assertEqual(len(db.list_deadline_dashboards()), 3)


if __name__ == "__main__":
    unittest.main()
