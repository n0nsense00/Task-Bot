"""Timezone-boundary tests for user-facing and scheduled calendar dates."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

# ``config`` validates these at import time.  Keep tests independent of a
# developer's .env and guarantee no real Telegram credentials are needed.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-not-a-real-bot")
os.environ.setdefault("MY_TELEGRAM_ID", "424242")

import handlers.add_task as add_task  # noqa: E402
import handlers.edit_task as edit_task  # noqa: E402
import scheduler  # noqa: E402
import utils.calendar_widget as calendar_widget  # noqa: E402
import utils.clock as clock  # noqa: E402
from database.models import Task  # noqa: E402
from utils.format import (  # noqa: E402
    TIPS,
    format_task_card,
    morning_greeting,
    todays_tip,
)


UTC_DAY = date(2026, 9, 7)
LOCAL_DAY = date(2026, 9, 8)
CROSS_MIDNIGHT_INSTANT = datetime(2026, 9, 7, 16, 30, tzinfo=timezone.utc)


def _original_handler(decorated):
    """Unwrap ``@authorized_only`` and ``@safe`` for focused handler tests."""
    return decorated.__wrapped__.__wrapped__


class LocalClockTests(unittest.TestCase):
    def test_utc_instant_after_singapore_midnight_returns_local_day(self) -> None:
        with patch.object(clock, "TIMEZONE", "Asia/Singapore"):
            self.assertEqual(clock.today_local(CROSS_MIDNIGHT_INSTANT), LOCAL_DAY)
        self.assertEqual(CROSS_MIDNIGHT_INSTANT.date(), UTC_DAY)

    def test_injected_instant_must_be_timezone_aware(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            clock.today_local(datetime(2026, 9, 8, 0, 30))


class CalendarBoundaryTests(unittest.TestCase):
    def test_today_and_tomorrow_shortcuts_use_local_day(self) -> None:
        with patch.object(calendar_widget, "today_local", return_value=LOCAL_DAY):
            self.assertEqual(calendar_widget.shortcut_to_date("today"), LOCAL_DAY)
            self.assertEqual(
                calendar_widget.shortcut_to_date("tomorrow"),
                date(2026, 9, 9),
            )

    def test_add_calendar_opens_on_local_month(self) -> None:
        message = Mock()
        message.text = "Project deadline"
        message.reply_text = AsyncMock()
        update = Mock(effective_message=message)
        context = Mock(user_data={})
        keyboard = Mock()

        with (
            patch.object(add_task, "today_local", return_value=LOCAL_DAY),
            patch.object(
                add_task, "build_calendar_keyboard", return_value=keyboard
            ) as build,
        ):
            state = asyncio.run(
                _original_handler(add_task.add_title)(update, context)
            )

        self.assertEqual(state, add_task.DATE)
        build.assert_called_once_with(2026, 9)
        self.assertIs(message.reply_text.await_args.kwargs["reply_markup"], keyboard)

    def test_edit_calendar_opens_on_local_month(self) -> None:
        task = Task(
            id=7,
            title="Midterm",
            task_type="midterm",
            due_date=date(2026, 10, 1),
            chat_id=424242,
        )
        query = Mock()
        query.data = "editf:due:7"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update = Mock(callback_query=query)
        update.effective_chat = Mock(id=424242)
        context = Mock(user_data={})
        keyboard = Mock()

        with (
            patch.object(edit_task, "get_task", return_value=task),
            patch.object(edit_task, "today_local", return_value=LOCAL_DAY),
            patch.object(
                edit_task, "build_calendar_keyboard", return_value=keyboard
            ) as build,
        ):
            state = asyncio.run(
                _original_handler(edit_task.edit_field_picked)(update, context)
            )

        self.assertEqual(state, edit_task.EDIT_DUE)
        build.assert_called_once_with(2026, 9)
        self.assertIs(
            query.edit_message_text.await_args.kwargs["reply_markup"], keyboard
        )


class UserFacingDateTests(unittest.TestCase):
    def test_task_card_relative_date_uses_local_clock(self) -> None:
        task = Task(
            id=3,
            title="Lab",
            task_type="lab",
            due_date=LOCAL_DAY,
            chat_id=424242,
        )
        with patch("utils.format.today_local", return_value=LOCAL_DAY) as local_day:
            card = format_task_card(task)

        local_day.assert_called_once_with()
        self.assertIn("today", card)
        self.assertNotIn("tomorrow", card)

    def test_greeting_and_tip_use_local_clock(self) -> None:
        with patch("utils.format.today_local", return_value=LOCAL_DAY) as local_day:
            greeting = morning_greeting()
            tip = todays_tip()

        self.assertEqual(local_day.call_count, 2)
        self.assertIn("Tuesday, 08 Sep 2026", greeting)
        self.assertEqual(tip, TIPS[LOCAL_DAY.toordinal() % len(TIPS)])


class BriefBookkeepingTests(unittest.TestCase):
    def test_sent_file_is_compared_with_local_day(self) -> None:
        with tempfile.TemporaryDirectory(prefix="taskbot-brief-date-") as tmp:
            sent_file = Path(tmp) / "last_brief.txt"
            with patch.object(scheduler, "_LAST_BRIEF_FILE", sent_file):
                scheduler._record_brief_sent(LOCAL_DAY)
                self.assertTrue(scheduler._brief_already_sent_today(LOCAL_DAY))
                self.assertFalse(scheduler._brief_already_sent_today(UTC_DAY))

    def test_send_records_same_local_day_used_to_build_brief(self) -> None:
        application = Mock()
        application.bot.send_message = AsyncMock()

        with (
            patch.object(scheduler, "today_local", return_value=LOCAL_DAY),
            patch.object(
                scheduler, "build_morning_brief", return_value="brief"
            ) as build,
            patch.object(scheduler, "_record_brief_sent") as record,
        ):
            asyncio.run(scheduler.send_morning_brief(application))

        build.assert_called_once_with(scheduler.MY_TELEGRAM_ID, today=LOCAL_DAY)
        record.assert_called_once_with(LOCAL_DAY)
        application.bot.send_message.assert_awaited_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
