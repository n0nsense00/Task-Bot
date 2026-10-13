"""Regressions for real user inputs and failures after deadline commits."""
from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import date, datetime, timezone
import inspect
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:dummy-token-for-tests")
os.environ.setdefault("MY_TELEGRAM_ID", "424242")
os.environ.setdefault("ALLOWED_CHAT_ID", "-1009876543210")

from telegram import MessageEntity
from telegram.constants import ChatType, MessageEntityType
from telegram.error import BadRequest, TimedOut
from telegram.error import RetryAfter
from telegram.ext import ConversationHandler, ExtBot

from config import MY_TELEGRAM_ID
from database import db
from database.models import Module, Task
from handlers import add_task, basic, edit_task, tasks, transient
from utils.conversation_state import begin_chat_flow, get_chat_value, set_chat_value
from utils.tracking_bot import TrackingBot, append_tracked_message, set_skip_tracking
from utils import clock, calendar_widget
import scheduler


class RuntimeAuditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="taskbot-runtime-audit-")
        self.old_path = db.DB_PATH
        db.DB_PATH = Path(self.temp.name) / "tasks.db"
        db.init_db()
        tasks._dashboard_locks.clear()
        self.chat_id = MY_TELEGRAM_ID
        self.chat = SimpleNamespace(id=self.chat_id, type=ChatType.PRIVATE)
        self.bot = SimpleNamespace(
            id=777, username="TaskBot", _tracked_messages=[],
            edit_message_text=AsyncMock(),
        )
        self.context = SimpleNamespace(
            bot=self.bot, application=SimpleNamespace(bot=self.bot, bot_data={}), user_data={},
        )

    async def asyncTearDown(self):
        db.DB_PATH = self.old_path
        tasks._dashboard_locks.clear()
        self.temp.cleanup()

    def update(self, text=None, data=None):
        message = SimpleNamespace(
            message_id=101, chat_id=self.chat_id, chat=self.chat, text=text,
            date=datetime.now(timezone.utc), entities=[], reply_to_message=None,
            reply_text=AsyncMock(),
        )
        query = None if data is None else SimpleNamespace(
            data=data, message=message, answer=AsyncMock(),
            edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock(),
        )
        return SimpleNamespace(
            effective_message=message, effective_chat=self.chat,
            effective_user=SimpleNamespace(id=MY_TELEGRAM_ID),
            callback_query=query, message=message if data is None else None,
        )

    def saved_task(self, title="Original deadline", task_type="project"):
        item = Task(title, task_type, date(2026, 11, 15), chat_id=self.chat_id,
                    module_code="SC2002")
        task_id = db.add_task(item)
        return db.get_task(task_id, self.chat_id)

    def begin_edit(self, update, item):
        begin_chat_flow(update, self.context, "edit")
        set_chat_value(update, self.context, edit_task._USER_DATA_TASK_ID, item.id)

    async def test_private_media_and_whitespace_are_tracked_without_crashing(self):
        for i, text in enumerate((None, "", "   ")):
            update = self.update(text=text)
            update.effective_message.message_id = 101 + i
            await basic.track_incoming_message(update, self.context)
        self.assertEqual([row[0] for row in db.list_tracked_messages(self.chat_id)],
                         [101, 102, 103])

    async def test_group_mentions_after_emoji_use_telegram_entity_offsets(self):
        update = self.update(text="\U0001f600 @TaskBot hello")
        update.effective_message.chat.type = ChatType.SUPERGROUP
        update.effective_message.entities = [
            MessageEntity(MessageEntityType.MENTION, offset=3, length=8)
        ]
        self.assertTrue(basic._is_bot_related_message(
            update.effective_message, self.bot.id, self.bot.username
        ))

    async def test_custom_module_code_is_escaped_in_add_prompt(self):
        update = self.update(text="B&<C>")
        state = await inspect.unwrap(add_task.add_module_text)(update, self.context)
        self.assertEqual(state, add_task.TITLE)
        self.assertIn("<code>B&amp;&lt;C&gt;</code>",
                      update.effective_message.reply_text.await_args.args[0])

    async def test_seeded_module_code_is_escaped_in_add_prompt(self):
        db.add_module(Module("B&<C>"))
        update = self.update(data="mod:select:B&<C>")
        state = await inspect.unwrap(add_task.add_module_picked)(update, self.context)
        self.assertEqual(state, add_task.TITLE)
        self.assertIn("<code>B&amp;&lt;C&gt;</code>",
                      update.callback_query.edit_message_text.await_args.args[0])

    async def test_saved_add_finishes_and_refreshes_when_confirmation_fails(self):
        update = self.update(text="skip")
        begin_chat_flow(update, self.context, "add")
        add_task._draft(update, self.context).update(
            title="Saved deadline", task_type="quiz", module_code="SC2001",
            due_date=date(2026, 11, 4),
        )
        update.effective_message.reply_text.side_effect = TimedOut("lost reply")
        with patch.object(add_task, "refresh_deadline_dashboard", new_callable=AsyncMock) as refresh:
            state = await add_task._finalize_add(update, self.context)
        self.assertEqual(state, ConversationHandler.END)
        self.assertEqual(db.count_tasks(self.chat_id), 1)
        refresh.assert_awaited_once_with(self.context.application, self.chat_id)
        self.assertIsNone(get_chat_value(update, self.context, add_task._USER_DATA_KEY))
        self.assertIsNone(begin_chat_flow(update, self.context, "edit"))

    async def test_saved_callback_add_finishes_when_confirmation_message_is_gone(self):
        update = self.update(data="notes:skip")
        begin_chat_flow(update, self.context, "add")
        add_task._draft(update, self.context).update(
            title="Saved deadline", task_type="quiz", module_code="SC2001",
            due_date=date(2026, 11, 4),
        )
        update.callback_query.edit_message_text.side_effect = BadRequest("Message to edit not found")
        with patch.object(add_task, "refresh_deadline_dashboard", new_callable=AsyncMock) as refresh:
            state = await add_task._finalize_add(update, self.context, via_query=update.callback_query)
        self.assertEqual(state, ConversationHandler.END)
        self.assertEqual(db.count_tasks(self.chat_id), 1)
        refresh.assert_awaited_once()

    async def test_saved_edit_cleans_up_and_refreshes_despite_failed_confirmation(self):
        for callback in (False, True):
            with self.subTest(callback=callback):
                item = self.saved_task()
                update = self.update(data="editf:title:1" if callback else None)
                self.begin_edit(update, item)
                item.title = "Saved edit"
                update.effective_message.reply_text.side_effect = TimedOut("lost reply")
                with (
                    patch.object(edit_task, "show_transient_view", new=AsyncMock(side_effect=TimedOut("lost edit"))),
                    patch.object(edit_task, "refresh_deadline_dashboard", new_callable=AsyncMock) as refresh,
                ):
                    finish = edit_task._finish_edit_callback if callback else edit_task._finish_edit_text
                    state = await finish(update, self.context, item)
                self.assertEqual(state, ConversationHandler.END)
                self.assertEqual(db.get_task(item.id, self.chat_id).title, "Saved edit")
                self.assertIsNone(edit_task._draft_task_id(update, self.context))
                self.assertIsNone(begin_chat_flow(update, self.context, "add"))
                # Release the test's next-flow claim for the next subcase.
                add_task._clear_add_state(update, self.context)
                refresh.assert_awaited_once_with(self.context.application, self.chat_id)

    async def test_done_command_refreshes_despite_failed_confirmation(self):
        item = self.saved_task()
        update = self.update(text=f"/done {item.id}")
        self.context.args = [str(item.id)]
        update.effective_message.reply_text.side_effect = TimedOut("lost reply")
        with patch.object(tasks, "refresh_deadline_dashboard", new_callable=AsyncMock) as refresh:
            await inspect.unwrap(tasks.done_task_cmd)(update, self.context)
        self.assertTrue(db.get_task(item.id, self.chat_id).completed)
        refresh.assert_awaited_once_with(self.context.application, self.chat_id)

    async def test_stale_field_button_does_not_switch_edit_to_another_deadline(self):
        current = self.saved_task("Current")
        other = self.saved_task("Other")
        update = self.update(data=f"editf:title:{other.id}")
        self.begin_edit(update, current)
        state = await inspect.unwrap(edit_task.edit_field_picked)(update, self.context)
        self.assertEqual(state, edit_task.EDIT_PICK_FIELD)
        self.assertEqual(edit_task._draft_task_id(update, self.context), current.id)
        self.assertTrue(update.callback_query.answer.await_args.kwargs["show_alert"])

    async def test_stale_type_button_does_not_modify_another_deadline(self):
        current = self.saved_task("Current")
        other = self.saved_task("Other")
        update = self.update(data=f"edittype:quiz:{other.id}")
        self.begin_edit(update, current)
        state = await inspect.unwrap(edit_task.edit_type)(update, self.context)
        self.assertEqual(state, edit_task.EDIT_TYPE)
        self.assertEqual(db.get_task(other.id, self.chat_id).task_type, "project")
        self.assertEqual(edit_task._draft_task_id(update, self.context), current.id)

    async def test_reopening_same_manager_view_is_a_successful_noop(self):
        update = self.update(data="manage:0")
        update.callback_query.edit_message_text.side_effect = BadRequest("Message is not modified")
        created = await transient.show_transient_view(update.callback_query, self.chat_id, "same view")
        self.assertFalse(created)

    async def test_failed_entry_releases_its_flow_claim(self):
        for flow in ("add", "edit"):
            with self.subTest(flow=flow):
                item = self.saved_task()
                update = self.update(data=f"edit:{item.id}" if flow == "edit" else None)
                update.effective_message.reply_text.side_effect = TimedOut("lost prompt")
                if update.callback_query is not None:
                    update.callback_query.answer.side_effect = TimedOut("lost answer")
                entry = add_task.add_entry if flow == "add" else edit_task.edit_entry
                with self.assertRaises(TimedOut):
                    await inspect.unwrap(entry)(update, self.context)
                opposite = "edit" if flow == "add" else "add"
                self.assertIsNone(begin_chat_flow(update, self.context, opposite))
                add_task._clear_add_state(update, self.context)
                edit_task._abort(update, self.context)

    async def test_cancel_finishes_even_if_its_confirmation_fails(self):
        for flow in ("add", "edit"):
            with self.subTest(flow=flow):
                update = self.update(text="/cancel")
                begin_chat_flow(update, self.context, flow)
                update.effective_message.reply_text.side_effect = TimedOut("lost cancellation")
                cancel = add_task.add_cancel if flow == "add" else edit_task.edit_cancel_command
                state = await inspect.unwrap(cancel)(update, self.context)
                self.assertEqual(state, ConversationHandler.END)
                opposite = "edit" if flow == "add" else "add"
                self.assertIsNone(begin_chat_flow(update, self.context, opposite))
                add_task._clear_add_state(update, self.context)
                edit_task._abort(update, self.context)

    async def test_stale_cancel_button_does_not_abort_current_edit(self):
        current = self.saved_task("Current")
        other = self.saved_task("Other")
        update = self.update(data=f"editcancel:{other.id}")
        self.begin_edit(update, current)
        state = await inspect.unwrap(edit_task.edit_cancel_callback)(update, self.context)
        self.assertIsNone(state)
        self.assertEqual(edit_task._draft_task_id(update, self.context), current.id)

    async def test_paused_housekeeping_does_not_hide_another_tasks_send(self):
        bot = TrackingBot(token="123456:dummy-token-for-tests")
        paused = asyncio.Event()
        resume = asyncio.Event()

        async def send(*args, **kwargs):
            return SimpleNamespace(chat_id=self.chat_id, message_id=201 if kwargs["text"] == "normal" else 202,
                                   date=datetime.now(timezone.utc))

        async def housekeeping():
            set_skip_tracking(bot, True)
            try:
                paused.set()
                await resume.wait()
                await bot.send_message(chat_id=self.chat_id, text="confirmation")
            finally:
                set_skip_tracking(bot, False)

        with patch.object(ExtBot, "send_message", new=AsyncMock(side_effect=send)):
            action = asyncio.create_task(housekeeping())
            await paused.wait()
            try:
                await bot.send_message(chat_id=self.chat_id, text="normal")
            finally:
                resume.set()
                await action
        self.assertEqual([row[0] for row in db.list_tracked_messages(self.chat_id)], [201])

    async def test_tracking_cache_is_bounded_without_losing_persistent_records(self):
        db.save_tracked_message(self.chat_id, 1)
        self.bot._tracked_messages = [(self.chat_id, i) for i in range(1, 1006)]
        append_tracked_message(self.bot, self.chat_id, 2000)
        append_tracked_message(self.bot, self.chat_id, 2000)
        self.assertLessEqual(len(self.bot._tracked_messages), 1000)
        self.assertEqual(self.bot._tracked_messages.count((self.chat_id, 2000)), 1)
        self.assertEqual([row[0] for row in db.list_tracked_messages(self.chat_id)], [1, 2000])

    async def test_clear_stops_after_rate_limit_and_preserves_queued_ids(self):
        for message_id in (11, 12, 13):
            db.save_tracked_message(self.chat_id, message_id)
        self.bot.delete_message = AsyncMock(side_effect=RetryAfter(30))
        self.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=555))
        self.context.application = None
        update = self.update(text="/clear")
        await inspect.unwrap(basic.clear)(update, self.context)
        self.bot.delete_message.assert_awaited_once()
        self.assertEqual([row[0] for row in db.list_tracked_messages(self.chat_id)], [11, 12, 13, 101])

    async def test_invalid_timezone_keys_fall_back_to_singapore(self):
        instant = datetime(2026, 10, 13, 18, 0, tzinfo=timezone.utc)
        for key in ("", "/Asia/Singapore"):
            with self.subTest(key=key):
                with patch.object(clock, "TIMEZONE", key), patch.object(scheduler, "TIMEZONE", key):
                    self.assertEqual(clock.today_local(instant), date(2026, 10, 14))
                    self.assertEqual(scheduler._resolve_timezone().key, "Asia/Singapore")

    async def test_calendar_parser_rejects_out_of_range_navigation(self):
        for payload in ("2026-00", "2026-13", "0000-10", "10000-10"):
            with self.subTest(payload=payload):
                self.assertIsNone(calendar_widget.parse_year_month(payload))

    async def test_calendar_extreme_years_have_valid_days_and_disabled_boundary_navigation(self):
        for year, month, boundary_index in ((1, 1, 0), (9999, 12, 2)):
            with self.subTest(year=year, month=month):
                keyboard = calendar_widget.build_calendar_keyboard(year, month)
                self.assertEqual(keyboard.inline_keyboard[0][boundary_index].callback_data, "cal:noop")
                for row in keyboard.inline_keyboard:
                    for button in row:
                        if button.callback_data.startswith("cal:select:"):
                            self.assertIsNotNone(calendar_widget.parse_iso_date(button.callback_data.split(":", 2)[2]))

    async def test_missing_task_is_not_reported_as_a_successful_edit(self):
        for callback in (False, True):
            with self.subTest(callback=callback):
                item = self.saved_task()
                update = self.update(data=f"edit:{item.id}" if callback else None)
                self.begin_edit(update, item)
                db.delete_task(item.id, self.chat_id)
                finish = edit_task._finish_edit_callback if callback else edit_task._finish_edit_text
                state = await finish(update, self.context, item)
                self.assertEqual(state, ConversationHandler.END)
                output = (update.callback_query.edit_message_text.await_args.args[0] if callback
                          else update.effective_message.reply_text.await_args.args[0])
                self.assertIn("no longer exists", output)
                self.assertNotIn("<b>Updated</b>", output)

    async def test_simultaneous_catchup_and_cron_send_only_one_morning_brief(self):
        self.bot.send_message = AsyncMock()
        stamp = Path(self.temp.name) / "last_brief.txt"
        with patch.object(scheduler, "_LAST_BRIEF_FILE", stamp):
            await asyncio.gather(
                scheduler.send_morning_brief(self.context.application),
                scheduler.send_morning_brief(self.context.application),
            )
            self.bot.send_message.assert_awaited_once()
            self.assertEqual(stamp.read_text(), clock.today_local().isoformat())

    async def test_clear_confirmation_failure_does_not_lose_completed_cleanup(self):
        db.save_tracked_message(self.chat_id, 11)
        self.bot.delete_message = AsyncMock()
        self.bot.send_message = AsyncMock(side_effect=TimedOut("lost confirmation"))
        self.context.application = None
        await inspect.unwrap(basic.clear)(self.update(text="/clear"), self.context)
        self.assertEqual(db.list_tracked_messages(self.chat_id), [])

    async def test_bulk_tracking_cleanup_respects_sqlite_variable_limits(self):
        for message_id in range(1, 11):
            db.save_tracked_message(self.chat_id, message_id)
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 4)
            with patch.object(db, "_get_conn", return_value=conn):
                count = db.delete_tracked_messages(self.chat_id, list(range(1, 11)))
        self.assertEqual(count, 10)
        self.assertEqual(db.list_tracked_messages(self.chat_id), [])

    async def test_failed_task_batch_does_not_mutate_input_metadata(self):
        first = Task("First", "quiz", date(2026, 11, 4))
        invalid = Task("Invalid", "not-a-task-type", date(2026, 11, 4))
        with self.assertRaises(sqlite3.IntegrityError):
            db.add_tasks([first, invalid])
        self.assertIsNone(first.chat_id)
        self.assertEqual(db.count_tasks(), 0)


class MigrationFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="taskbot-migration-audit-")
        self.old_path = db.DB_PATH
        db.DB_PATH = Path(self.temp.name) / "legacy.db"
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            conn.executescript("""CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                task_type TEXT NOT NULL CHECK(task_type IN ('lecture','tutorial','personal')),
                module_code TEXT, due_date TEXT NOT NULL, due_time TEXT, notes TEXT,
                completed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );""")
            conn.execute("INSERT INTO tasks VALUES (1, ?, 'Keep me', 'lecture', 'SC2001', '2026-11-01', NULL, NULL, 0, '2026-10-01')", (MY_TELEGRAM_ID,))
            conn.execute("INSERT INTO tasks VALUES (100, ?, 'Deleted task', 'lecture', 'SC2001', '2026-11-01', NULL, NULL, 0, '2026-10-01')", (MY_TELEGRAM_ID,))
            conn.execute("DELETE FROM tasks WHERE id=100")
            conn.commit()

    def tearDown(self):
        db.DB_PATH = self.old_path
        self.temp.cleanup()

    def test_failed_migration_preserves_original_table_and_rows(self):
        original = db._migrate_schema
        def reject_copy(conn):
            def authorizer(operation, table, column, schema, trigger):
                if operation == sqlite3.SQLITE_INSERT and table == "tasks":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            conn.set_authorizer(authorizer)
            try:
                original(conn)
            finally:
                conn.set_authorizer(None)
        with patch.object(db, "_migrate_schema", side_effect=reject_copy):
            with self.assertRaises(sqlite3.DatabaseError):
                db.init_db()
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            self.assertEqual(conn.execute("SELECT id,title FROM tasks").fetchall(), [(1, "Keep me")])
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='tasks_legacy'").fetchone())

    def test_migration_does_not_reuse_previously_deleted_deadline_ids(self):
        db.init_db()
        new_id = db.add_task(Task("New quiz", "quiz", date(2026, 11, 4), chat_id=MY_TELEGRAM_ID))
        self.assertGreater(new_id, 100)


if __name__ == "__main__":
    unittest.main()
