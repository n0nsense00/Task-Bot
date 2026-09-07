"""Regression tests for restart-safe message tracking and ``/clear``."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test-token")
os.environ.setdefault("MY_TELEGRAM_ID", "1624017309")
os.environ.setdefault("ALLOWED_CHAT_ID", "-1002977322927")

from telegram.constants import ChatType  # noqa: E402
from telegram.error import BadRequest, TimedOut  # noqa: E402
from telegram.ext import ExtBot  # noqa: E402

from database import db  # noqa: E402
from handlers import basic  # noqa: E402
from utils.tracking_bot import (  # noqa: E402
    TrackingBot,
    append_tracked_message,
)


OWNER_CHAT_ID = 1_624_017_309


class _TrackingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._original_db_path = db.DB_PATH
        self._tmpdir = tempfile.TemporaryDirectory(prefix="taskbot-tracking-")
        db.DB_PATH = (Path(self._tmpdir.name) / "tracking.db").resolve()
        db.init_db()

    def tearDown(self) -> None:
        db.DB_PATH = self._original_db_path
        self._tmpdir.cleanup()

    @staticmethod
    def _run(coro):
        return asyncio.run(coro)

    @staticmethod
    def _message(message_id: int, *, created_at: datetime | None = None):
        return SimpleNamespace(
            chat_id=OWNER_CHAT_ID,
            message_id=message_id,
            date=created_at or datetime.now(timezone.utc),
        )

    def _clear_context(self, delete_side_effect=None):
        bot = SimpleNamespace(_tracked_messages=[])
        bot.id = 777
        bot.username = "TaskBot"
        bot.delete_message = AsyncMock(side_effect=delete_side_effect)
        bot.send_message = AsyncMock(return_value=self._message(9999))
        return SimpleNamespace(bot=bot, application=None)

    def _clear_update(self, message_id: int = 9000):
        chat = SimpleNamespace(id=OWNER_CHAT_ID, type=ChatType.PRIVATE)
        message = SimpleNamespace(
            message_id=message_id,
            date=datetime.now(timezone.utc),
            text="/clear",
            chat=chat,
        )
        return SimpleNamespace(
            effective_chat=chat,
            effective_message=message,
            effective_user=SimpleNamespace(id=OWNER_CHAT_ID),
            message=message,
        )

    def _run_clear(self, update, context) -> None:
        # Exercise the handler body; authorization is covered independently.
        handler_body = basic.clear.__wrapped__.__wrapped__
        self._run(handler_body(update, context))


class TrackedMessageDatabaseTests(_TrackingTestCase):
    def test_init_db_creates_tracking_table_and_index(self) -> None:
        conn = sqlite3.connect(db.DB_PATH)
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
                )
            }
        finally:
            conn.close()
        self.assertIn("tracked_messages", names)
        self.assertIn("idx_tracked_messages_chat_created", names)

    def test_init_db_migrates_existing_database_without_losing_rows(self) -> None:
        db.save_deadline_dashboard(OWNER_CHAT_ID, 55)
        conn = sqlite3.connect(db.DB_PATH)
        try:
            conn.execute("DROP TABLE tracked_messages")
            conn.commit()
        finally:
            conn.close()

        db.init_db()

        self.assertEqual(db.get_deadline_dashboard_message_id(OWNER_CHAT_ID), 55)
        self.assertEqual(db.list_tracked_messages(OWNER_CHAT_ID), [])

    def test_save_is_idempotent_and_keeps_earliest_timestamp(self) -> None:
        earlier = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
        later = earlier + timedelta(hours=1)
        db.save_tracked_message(OWNER_CHAT_ID, 10, later)
        db.save_tracked_message(OWNER_CHAT_ID, 10, earlier)
        db.save_tracked_message(OWNER_CHAT_ID, 10, later)

        self.assertEqual(db.list_tracked_messages(OWNER_CHAT_ID), [(10, earlier)])

    def test_rows_are_isolated_by_chat(self) -> None:
        other_chat = -100_123
        db.save_tracked_message(OWNER_CHAT_ID, 10)
        db.save_tracked_message(other_chat, 20)

        self.assertEqual(
            [message_id for message_id, _ in db.list_tracked_messages(OWNER_CHAT_ID)],
            [10],
        )
        self.assertEqual(
            [message_id for message_id, _ in db.list_tracked_messages(other_chat)],
            [20],
        )

    def test_rows_survive_a_fresh_bot_instance(self) -> None:
        first_process_bot = SimpleNamespace(_tracked_messages=[])
        append_tracked_message(first_process_bot, OWNER_CHAT_ID, 10)

        restarted_bot = SimpleNamespace(_tracked_messages=[])
        self.assertEqual(restarted_bot._tracked_messages, [])
        self.assertEqual(
            [message_id for message_id, _ in db.list_tracked_messages(OWNER_CHAT_ID)],
            [10],
        )


class TrackingBotTests(_TrackingTestCase):
    def test_successful_send_message_is_persisted_with_telegram_timestamp(self) -> None:
        sent_at = datetime(2026, 9, 7, 2, 30, tzinfo=timezone.utc)
        sent = self._message(321, created_at=sent_at)
        bot = TrackingBot(token="123456:test-token")

        with patch.object(ExtBot, "send_message", new=AsyncMock(return_value=sent)):
            result = self._run(bot.send_message(chat_id=OWNER_CHAT_ID, text="hello"))

        self.assertIs(result, sent)
        self.assertEqual(db.list_tracked_messages(OWNER_CHAT_ID), [(321, sent_at)])

    def test_failed_send_message_is_not_persisted(self) -> None:
        bot = TrackingBot(token="123456:test-token")

        with patch.object(
            ExtBot,
            "send_message",
            new=AsyncMock(side_effect=TimedOut("send failed")),
        ):
            with self.assertRaises(TimedOut):
                self._run(bot.send_message(chat_id=OWNER_CHAT_ID, text="hello"))

        self.assertEqual(db.list_tracked_messages(OWNER_CHAT_ID), [])

    def test_paused_send_message_is_not_persisted(self) -> None:
        sent = self._message(321)
        bot = TrackingBot(token="123456:test-token")
        bot._skip_tracking = True

        with patch.object(ExtBot, "send_message", new=AsyncMock(return_value=sent)):
            self._run(bot.send_message(chat_id=OWNER_CHAT_ID, text="housekeeping"))

        self.assertEqual(db.list_tracked_messages(OWNER_CHAT_ID), [])


class ClearPersistenceTests(_TrackingTestCase):
    def test_clear_uses_database_after_restart_and_includes_dashboard(self) -> None:
        db.save_tracked_message(OWNER_CHAT_ID, 10)
        db.save_tracked_message(OWNER_CHAT_ID, 20)
        db.save_deadline_dashboard(OWNER_CHAT_ID, 30)  # legacy/untracked dashboard
        context = self._clear_context()

        self._run_clear(self._clear_update(), context)

        deleted_ids = {
            call.kwargs["message_id"]
            for call in context.bot.delete_message.await_args_list
        }
        self.assertEqual(deleted_ids, {10, 20, 30, 9000})
        self.assertEqual(db.list_tracked_messages(OWNER_CHAT_ID), [])
        self.assertIsNone(db.get_deadline_dashboard_message_id(OWNER_CHAT_ID))

    def test_clear_skips_and_prunes_messages_outside_48_hour_window(self) -> None:
        old = datetime.now(timezone.utc) - timedelta(hours=49)
        db.save_tracked_message(OWNER_CHAT_ID, 10, old)
        db.save_deadline_dashboard(OWNER_CHAT_ID, 10)
        context = self._clear_context()

        self._run_clear(self._clear_update(), context)

        deleted_ids = {
            call.kwargs["message_id"]
            for call in context.bot.delete_message.await_args_list
        }
        self.assertNotIn(10, deleted_ids)
        self.assertEqual(db.list_tracked_messages(OWNER_CHAT_ID), [])
        # It is too old to delete but can still be edited, so retain the
        # persistent-dashboard registration.
        self.assertEqual(db.get_deadline_dashboard_message_id(OWNER_CHAT_ID), 10)

    def test_transient_failure_remains_for_next_clear(self) -> None:
        db.save_tracked_message(OWNER_CHAT_ID, 10)

        async def delete(*, chat_id: int, message_id: int):
            if message_id == 10:
                raise TimedOut("temporary timeout")
            return True

        context = self._clear_context(delete)
        self._run_clear(self._clear_update(), context)

        self.assertEqual(
            [message_id for message_id, _ in db.list_tracked_messages(OWNER_CHAT_ID)],
            [10],
        )
        confirmation = context.bot.send_message.await_args.kwargs["text"]
        self.assertIn("will be retried next time", confirmation)

    def test_permanently_gone_message_is_forgotten_and_dashboard_dropped(self) -> None:
        db.save_tracked_message(OWNER_CHAT_ID, 10)
        db.save_deadline_dashboard(OWNER_CHAT_ID, 10)

        async def delete(*, chat_id: int, message_id: int):
            if message_id == 10:
                raise BadRequest("Message to delete not found")
            return True

        context = self._clear_context(delete)
        self._run_clear(self._clear_update(), context)

        self.assertEqual(db.list_tracked_messages(OWNER_CHAT_ID), [])
        self.assertIsNone(db.get_deadline_dashboard_message_id(OWNER_CHAT_ID))

    def test_clear_does_not_touch_another_chats_rows(self) -> None:
        other_chat = -100_123
        db.save_tracked_message(OWNER_CHAT_ID, 10)
        db.save_tracked_message(other_chat, 20)
        context = self._clear_context()

        self._run_clear(self._clear_update(), context)

        self.assertEqual(
            [message_id for message_id, _ in db.list_tracked_messages(other_chat)],
            [20],
        )

    def test_group_one_tracker_does_not_reinsert_clear_command(self) -> None:
        context = self._clear_context()
        update = self._clear_update()

        self._run(basic.track_incoming_message(update, context))

        self.assertEqual(db.list_tracked_messages(OWNER_CHAT_ID), [])

    def test_group_one_tracker_persists_incoming_message_timestamp(self) -> None:
        context = self._clear_context()
        sent_at = datetime(2026, 9, 7, 1, 0, tzinfo=timezone.utc)
        chat = SimpleNamespace(id=OWNER_CHAT_ID, type=ChatType.PRIVATE)
        message = SimpleNamespace(
            message_id=44,
            date=sent_at,
            text="hello",
            chat=chat,
            reply_to_message=None,
            entities=None,
        )
        update = SimpleNamespace(effective_message=message, effective_chat=chat)

        self._run(basic.track_incoming_message(update, context))

        self.assertEqual(db.list_tracked_messages(OWNER_CHAT_ID), [(44, sent_at)])


if __name__ == "__main__":
    unittest.main()
