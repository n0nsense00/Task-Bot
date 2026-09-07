"""Regression tests for Telegram payload and user-input limits."""
from __future__ import annotations

import inspect
import os
import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0000000000:test-token-not-real")
os.environ.setdefault("MY_TELEGRAM_ID", "424242")

from telegram.error import BadRequest  # noqa: E402

from database.models import Module, Task  # noqa: E402
from handlers import add_task, callbacks, edit_task, tasks  # noqa: E402
import scheduler  # noqa: E402
from seed import seed_modules, seed_tasks  # noqa: E402
from utils import format as task_format  # noqa: E402
from utils.calendar_widget import build_calendar_keyboard  # noqa: E402
from utils.limits import (  # noqa: E402
    MODULE_CODE_MAX_BYTES,
    MODULE_CODE_MAX_LENGTH,
    MODULE_NAME_MAX_LENGTH,
    NOTES_MAX_LENGTH,
    TELEGRAM_CALLBACK_ANSWER_MAX_UNITS,
    TELEGRAM_CALLBACK_DATA_MAX_BYTES,
    TELEGRAM_MESSAGE_MAX_UNITS,
    TITLE_MAX_LENGTH,
    telegram_text_units,
)
from utils.timepicker import (  # noqa: E402
    build_hour_keyboard,
    build_minute_keyboard,
    build_time_preset_keyboard,
)


TODAY = date(2026, 9, 7)
CHAT_ID = 424242


def _task(index: int, *, long: bool = False) -> Task:
    title = (
        f"Deadline {index:02d} " + "x" * 100
        if long
        else f"Semester deadline number {index:02d}"
    )
    return Task(
        id=index,
        chat_id=CHAT_ID,
        title=title,
        task_type="assignment",
        module_code="SC2002",
        due_date=TODAY + timedelta(days=index),
        notes=("n" * NOTES_MAX_LENGTH) if long else None,
    )


def _original(handler):
    """Return the undecorated async handler for focused unit tests."""
    return inspect.unwrap(handler)


class RenderBudgetTests(unittest.TestCase):
    def test_forty_deadline_dashboard_fits_and_routes_overflow_to_manager(self):
        deadlines = [_task(index) for index in range(1, 41)]
        with patch.object(tasks, "get_semester_deadlines", return_value=deadlines):
            text, keyboard = tasks.render_deadlines(CHAT_ID, TODAY)
            picker_text, picker = tasks.render_deadline_picker(CHAT_ID, 0, TODAY)

        self.assertLessEqual(
            telegram_text_units(text), TELEGRAM_MESSAGE_MAX_UNITS
        )
        self.assertIn("more — tap Manage deadlines to view all", text)
        self.assertIsNotNone(keyboard)
        self.assertIn("40 pending", picker_text)
        self.assertIn("Page 1 of 7", picker_text)
        self.assertEqual(len(picker.inline_keyboard[0:6]), 6)

    def test_large_brief_fits_and_reports_omitted_deadlines(self):
        deadlines = [_task(index, long=True) for index in range(1, 41)]
        for task in deadlines:
            task.due_date = TODAY
        with (
            patch.object(scheduler, "today_local", return_value=TODAY),
            patch.object(
                scheduler, "get_semester_deadlines", return_value=deadlines
            ),
        ):
            text = scheduler.build_morning_brief(CHAT_ID)

        self.assertLessEqual(
            telegram_text_units(text), TELEGRAM_MESSAGE_MAX_UNITS
        )
        self.assertIn("more — use /deadlines", text)

    def test_legacy_oversized_card_is_bounded(self):
        task = _task(1, long=True)
        task.title = "t" * 10_000
        task.module_code = "m" * 10_000
        task.notes = "n" * 10_000

        text = task_format.format_task_card(task)

        self.assertLessEqual(
            telegram_text_units(text), TELEGRAM_MESSAGE_MAX_UNITS
        )
        self.assertIn("…", text)


class CallbackTransportTests(unittest.IsolatedAsyncioTestCase):
    def _update(self, task_id: int, answer_error: Exception):
        query = Mock()
        query.data = f"done:{task_id}"
        query.message = SimpleNamespace(message_id=9001)
        query.answer = AsyncMock(side_effect=answer_error)
        query.edit_message_text = AsyncMock()
        update = SimpleNamespace(
            callback_query=query,
            effective_chat=SimpleNamespace(id=CHAT_ID),
            effective_message=query.message,
        )
        context = SimpleNamespace(application=Mock())
        return update, context, query

    async def test_long_title_completion_refreshes_when_toast_has_expired(self):
        task = _task(7)
        task.title = "x" * 10_000
        update, context, query = self._update(
            task.id, BadRequest("Query is too old")
        )

        with (
            patch.object(callbacks, "get_task", return_value=task),
            patch.object(callbacks, "mark_complete", return_value=True),
            patch.object(
                callbacks, "is_tracked_deadline_dashboard", return_value=True
            ),
            patch.object(
                callbacks, "refresh_deadline_dashboard", new_callable=AsyncMock
            ) as refresh,
        ):
            await _original(callbacks.done_callback)(update, context)

        answer = query.answer.await_args.args[0]
        self.assertLessEqual(
            telegram_text_units(answer), TELEGRAM_CALLBACK_ANSWER_MAX_UNITS
        )
        self.assertNotIn(task.title, answer)
        refresh.assert_awaited_once_with(context.application, CHAT_ID)

    async def test_long_title_deletion_refreshes_when_toast_has_expired(self):
        task = _task(8)
        task.title = "x" * 10_000
        update, context, query = self._update(
            task.id, BadRequest("Query is too old")
        )
        query.data = f"del:yes:{task.id}"

        with (
            patch.object(callbacks, "get_task", return_value=task),
            patch.object(callbacks, "delete_task", return_value=True),
            patch.object(
                callbacks, "is_tracked_deadline_dashboard", return_value=True
            ),
            patch.object(
                callbacks, "refresh_deadline_dashboard", new_callable=AsyncMock
            ) as refresh,
        ):
            await _original(callbacks.delete_confirm_callback)(update, context)

        answer = query.answer.await_args.args[0]
        self.assertLessEqual(
            telegram_text_units(answer), TELEGRAM_CALLBACK_ANSWER_MAX_UNITS
        )
        self.assertNotIn(task.title, answer)
        refresh.assert_awaited_once_with(context.application, CHAT_ID)

    def test_all_generated_callback_data_fits_sixty_four_utf8_bytes(self):
        task = _task(1)
        task.id = 9223372036854775807
        keyboards = [
            task_format.build_deadline_dashboard_keyboard(),
            task_format.build_deadline_picker_keyboard([task], 999999, 1000000),
            task_format.build_deadline_action_keyboard(task.id, 999999),
            task_format.build_delete_confirmation_keyboard(task.id),
            task_format.build_edit_field_keyboard(task.id),
            task_format.build_edit_type_keyboard(task.id),
            task_format.build_notes_keyboard(include_clear=True),
            task_format.build_module_keyboard(
                [Module(code="A" * MODULE_CODE_MAX_LENGTH, name="A module")]
            ),
            add_task._type_keyboard(),
            build_calendar_keyboard(2099, 12),
            build_time_preset_keyboard(),
            build_hour_keyboard(),
            build_minute_keyboard(23),
        ]

        for keyboard in keyboards:
            for row in keyboard.inline_keyboard:
                for button in row:
                    if button.callback_data is not None:
                        self.assertLessEqual(
                            len(button.callback_data.encode("utf-8")),
                            TELEGRAM_CALLBACK_DATA_MAX_BYTES,
                            button.callback_data,
                        )


class InputLimitTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _text_update(value: str):
        message = SimpleNamespace(text=value, reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_chat=None)
        context = SimpleNamespace(user_data={})
        return update, context, message

    async def test_add_rejects_oversized_title_module_and_notes(self):
        cases = (
            (add_task.add_title, "x" * (TITLE_MAX_LENGTH + 1), add_task.TITLE),
            (
                add_task.add_module_text,
                "x" * (MODULE_CODE_MAX_LENGTH + 1),
                add_task.MODULE_TEXT,
            ),
            (add_task.add_notes, "x" * (NOTES_MAX_LENGTH + 1), add_task.NOTES),
        )
        for handler, value, expected_state in cases:
            with self.subTest(handler=handler.__name__):
                update, context, message = self._text_update(value)
                result = await _original(handler)(update, context)
                self.assertEqual(result, expected_state)
                message.reply_text.assert_awaited_once()

    async def test_edit_rejects_oversized_title_module_and_notes(self):
        cases = (
            (
                edit_task.edit_title,
                "x" * (TITLE_MAX_LENGTH + 1),
                edit_task.EDIT_TITLE,
            ),
            (
                edit_task.edit_module_text,
                "x" * (MODULE_CODE_MAX_LENGTH + 1),
                edit_task.EDIT_MODULE_TEXT,
            ),
            (
                edit_task.edit_notes,
                "x" * (NOTES_MAX_LENGTH + 1),
                edit_task.EDIT_NOTES,
            ),
        )
        for handler, value, expected_state in cases:
            with self.subTest(handler=handler.__name__):
                update, context, message = self._text_update(value)
                result = await _original(handler)(update, context)
                self.assertEqual(result, expected_state)
                message.reply_text.assert_awaited_once()

    def test_seed_paths_reject_oversized_fields(self):
        task_data = {
            "title": "x" * (TITLE_MAX_LENGTH + 1),
            "task_type": "quiz",
            "module_code": "SC2002",
            "due_date": "2026-09-08",
            "due_time": "",
            "notes": "",
        }
        with self.assertRaisesRegex(ValueError, "title must be"):
            seed_tasks._validate_row(seed_tasks._RawRow(2, task_data))

        module_data = {
            "code": "SC2002",
            "name": "x" * (MODULE_NAME_MAX_LENGTH + 1),
        }
        with self.assertRaisesRegex(ValueError, "name must be"):
            seed_modules._validate_row(seed_modules._RawRow(2, module_data))

        multibyte_data = {
            **task_data,
            "title": "Valid title",
            "module_code": "界" * MODULE_CODE_MAX_LENGTH,
        }
        self.assertGreater(
            len(multibyte_data["module_code"].encode("utf-8")),
            MODULE_CODE_MAX_BYTES,
        )
        with self.assertRaisesRegex(ValueError, "multi-byte"):
            seed_tasks._validate_row(seed_tasks._RawRow(2, multibyte_data))


class RefreshOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_deadlines_reports_transient_failure_without_false_success(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(id=CHAT_ID),
        )
        context = SimpleNamespace(application=Mock())

        with (
            patch.object(
                tasks, "get_deadline_dashboard_message_id", return_value=99
            ),
            patch.object(
                tasks,
                "refresh_deadline_dashboard",
                new=AsyncMock(
                    return_value=tasks.DashboardRefreshResult.TRANSIENT_FAILURE
                ),
            ),
            patch.object(
                tasks, "send_and_register_dashboard", new_callable=AsyncMock
            ) as send_new,
        ):
            await _original(tasks.deadlines)(update, context)

        message.reply_text.assert_awaited_once_with(
            tasks._DASHBOARD_REFRESH_FAILED_MESSAGE
        )
        self.assertNotEqual(
            message.reply_text.await_args.args[0], tasks._DASHBOARD_REFRESHED_MESSAGE
        )
        send_new.assert_not_awaited()


if __name__ == "__main__":
    unittest.main(verbosity=2)
