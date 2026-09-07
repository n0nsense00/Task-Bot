"""Regression tests for the display-only persistent deadline dashboard."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0000000000:test-token-not-real")
os.environ.setdefault("MY_TELEGRAM_ID", "424242")
os.environ.setdefault("ALLOWED_CHAT_ID", "-100424242")

import config  # noqa: E402
import database.db as db  # noqa: E402
import handlers.callbacks as callbacks  # noqa: E402
import handlers.edit_task as edit_task  # noqa: E402
from database.models import Task  # noqa: E402
from telegram.ext import ConversationHandler  # noqa: E402
from utils.clock import today_local  # noqa: E402

_TEMP_DIR = tempfile.mkdtemp(prefix="taskbot-transient-dashboard-")
_TEMP_DB = (Path(_TEMP_DIR) / "transient-dashboard.db").resolve()


def tearDownModule() -> None:
    shutil.rmtree(_TEMP_DIR, ignore_errors=True)


class TransientDashboardTests(unittest.IsolatedAsyncioTestCase):
    """Drive callback handlers without contacting Telegram."""

    async def asyncSetUp(self) -> None:
        db.DB_PATH = _TEMP_DB
        db.init_db()
        db.delete_all_tasks(config.MY_TELEGRAM_ID)
        db.delete_deadline_dashboard(config.MY_TELEGRAM_ID)
        self.chat_id = config.MY_TELEGRAM_ID
        self.dashboard_id = 700
        self.manager_id = 701
        self.task_id = db.add_task(
            Task(
                id=None,
                chat_id=self.chat_id,
                title="Transient dashboard regression",
                task_type="assignment",
                due_date=today_local() + timedelta(days=14),
            )
        )
        db.save_deadline_dashboard(self.chat_id, self.dashboard_id)

    def make_message(self, message_id: int) -> Mock:
        message = Mock()
        message.message_id = message_id
        message.chat_id = self.chat_id
        message.reply_text = AsyncMock()
        return message

    def make_update(
        self, data: str, message_id: int, *, user_id: int | None = None
    ) -> tuple[Mock, Mock]:
        message = self.make_message(message_id)
        query = Mock()
        query.data = data
        query.message = message
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()

        update = Mock()
        update.callback_query = query
        update.effective_message = message
        update.effective_chat = Mock(id=self.chat_id)
        update.effective_user = Mock(
            id=config.MY_TELEGRAM_ID if user_id is None else user_id
        )
        return update, query

    def make_context(self) -> Mock:
        context = Mock()
        context.user_data = {}
        context.application = Mock()
        context.application.bot.edit_message_text = AsyncMock()
        return context

    async def test_manage_and_cancel_never_edit_registered_dashboard(self) -> None:
        """Reproduce the old cancellation bug across the complete UI path."""
        context = self.make_context()
        manager_message = self.make_message(self.manager_id)

        manage_update, dashboard_query = self.make_update(
            "manage:0", self.dashboard_id
        )
        dashboard_query.message.reply_text.return_value = manager_message
        await callbacks.manage_deadlines_callback(manage_update, context)

        dashboard_query.edit_message_text.assert_not_awaited()
        dashboard_query.message.reply_text.assert_awaited_once()
        self.assertEqual(
            db.get_deadline_dashboard_message_id(self.chat_id),
            self.dashboard_id,
        )

        edit_update, edit_query = self.make_update(
            f"edit:{self.task_id}", self.manager_id
        )
        state = await edit_task.edit_entry(edit_update, context)
        self.assertEqual(state, edit_task.EDIT_PICK_FIELD)
        edit_query.edit_message_text.assert_awaited_once()
        self.assertIn("Editing task", edit_query.edit_message_text.await_args.args[0])

        cancel_update, cancel_query = self.make_update(
            f"editcancel:{self.task_id}", self.manager_id
        )
        state = await edit_task.edit_cancel_callback(cancel_update, context)

        self.assertEqual(state, ConversationHandler.END)
        cancel_query.edit_message_text.assert_awaited_once_with(
            "Edit cancelled. No changes made."
        )
        self.assertEqual(
            db.get_deadline_dashboard_message_id(self.chat_id),
            self.dashboard_id,
            "cancel must leave the registration pointing at the dashboard",
        )
        context.application.bot.edit_message_text.assert_not_awaited()

    async def test_stale_edit_button_on_dashboard_opens_transient_view(self) -> None:
        """A pre-deploy dashboard button cannot replace the live dashboard."""
        context = self.make_context()
        update, query = self.make_update(
            f"edit:{self.task_id}", self.dashboard_id
        )

        state = await edit_task.edit_entry(update, context)

        self.assertEqual(state, edit_task.EDIT_PICK_FIELD)
        query.edit_message_text.assert_not_awaited()
        query.message.reply_text.assert_awaited_once()
        self.assertIn(
            "Editing task", query.message.reply_text.await_args.args[0]
        )
        self.assertEqual(
            db.get_deadline_dashboard_message_id(self.chat_id),
            self.dashboard_id,
        )

    async def test_group_members_each_open_their_own_manager_message(self) -> None:
        """Concurrent group entry taps do not compete for the dashboard body."""
        group_id = config.ALLOWED_CHAT_ID
        self.chat_id = group_id
        db.add_task(
            Task(
                id=None,
                chat_id=group_id,
                title="Shared group deadline",
                task_type="assignment",
                due_date=today_local() + timedelta(days=7),
            )
        )
        db.save_deadline_dashboard(group_id, self.dashboard_id)
        context = self.make_context()

        update_a, query_a = self.make_update(
            "manage:0", self.dashboard_id, user_id=111
        )
        update_b, query_b = self.make_update(
            "manage:0", self.dashboard_id, user_id=222
        )
        await callbacks.manage_deadlines_callback(update_a, context)
        await callbacks.manage_deadlines_callback(update_b, context)

        query_a.message.reply_text.assert_awaited_once()
        query_b.message.reply_text.assert_awaited_once()
        query_a.edit_message_text.assert_not_awaited()
        query_b.edit_message_text.assert_not_awaited()
        self.assertEqual(
            db.get_deadline_dashboard_message_id(group_id), self.dashboard_id
        )

    async def test_success_updates_transient_card_and_refreshes_dashboard(self) -> None:
        """A completed edit writes to the two messages through separate paths."""
        context = self.make_context()
        context.user_data["edit_task_id"] = self.task_id
        update, query = self.make_update(
            f"edittype:quiz:{self.task_id}", self.manager_id
        )

        state = await edit_task.edit_type(update, context)

        self.assertEqual(state, ConversationHandler.END)
        self.assertEqual(db.get_task(self.task_id, self.chat_id).task_type, "quiz")
        query.edit_message_text.assert_awaited_once()
        self.assertIn("Updated", query.edit_message_text.await_args.args[0])
        context.application.bot.edit_message_text.assert_awaited_once()
        refresh_kwargs = context.application.bot.edit_message_text.await_args.kwargs
        self.assertEqual(refresh_kwargs["message_id"], self.dashboard_id)
        self.assertEqual(refresh_kwargs["chat_id"], self.chat_id)
        self.assertEqual(
            db.get_deadline_dashboard_message_id(self.chat_id),
            self.dashboard_id,
        )

    async def test_delete_confirmation_from_dashboard_is_transient(self) -> None:
        """Stale Delete buttons receive the same display-only protection."""
        context = self.make_context()
        update, query = self.make_update(
            f"del:{self.task_id}", self.dashboard_id
        )

        await callbacks.delete_request_callback(update, context)

        query.edit_message_text.assert_not_awaited()
        query.message.reply_text.assert_awaited_once()
        self.assertIn(
            "Delete this task?", query.message.reply_text.await_args.args[0]
        )
        self.assertEqual(
            db.get_deadline_dashboard_message_id(self.chat_id),
            self.dashboard_id,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
