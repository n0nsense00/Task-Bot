"""Tests for per-chat /add and /edit state owned by one Telegram user."""
from __future__ import annotations

import asyncio
import os
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-not-a-real-bot")
os.environ.setdefault("MY_TELEGRAM_ID", "424242")

import utils.auth as auth_mod  # noqa: E402
from database.models import Task  # noqa: E402
from handlers import add_task as add_mod  # noqa: E402
from handlers import edit_task as edit_mod  # noqa: E402
from telegram.ext import ConversationHandler  # noqa: E402
from utils.conversation_state import (  # noqa: E402
    begin_chat_flow,
    finish_chat_flow,
    get_chat_value,
)


class ConversationIsolationTests(unittest.TestCase):
    """Drive handlers with one shared user_data dict across two chats."""

    GROUP_ID = -1002222222222

    def setUp(self) -> None:
        self.owner_id = auth_mod.MY_TELEGRAM_ID
        self.user_data: dict = {}
        self.context = SimpleNamespace(
            user_data=self.user_data,
            application=Mock(),
        )
        self.auth_patch = patch.object(
            auth_mod, "ALLOWED_CHAT_ID", self.GROUP_ID
        )
        self.auth_patch.start()
        self.addCleanup(self.auth_patch.stop)
        self.dashboard_patch = patch(
            "handlers.transient.get_deadline_dashboard_message_id",
            return_value=None,
        )
        self.dashboard_patch.start()
        self.addCleanup(self.dashboard_patch.stop)

    def make_message_update(self, chat_id: int) -> tuple[Mock, Mock]:
        message = Mock()
        message.reply_text = AsyncMock()
        update = Mock()
        update.effective_message = message
        update.effective_chat = Mock(id=chat_id)
        update.effective_user = Mock(id=self.owner_id)
        update.callback_query = None
        return update, message

    def make_edit_update(
        self, chat_id: int, task_id: int
    ) -> tuple[Mock, Mock]:
        query = Mock()
        query.data = f"edit:{task_id}"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message = Mock(message_id=100 + task_id)

        update = Mock()
        update.effective_message = query.message
        update.effective_chat = Mock(id=chat_id)
        update.effective_user = Mock(id=self.owner_id)
        update.callback_query = query
        return update, query

    @staticmethod
    def task(task_id: int, chat_id: int) -> Task:
        return Task(
            id=task_id,
            chat_id=chat_id,
            title=f"Task {task_id}",
            task_type="assignment",
            due_date=date(2026, 10, 1),
        )

    def test_add_drafts_are_isolated_and_cancel_clears_only_one_chat(self) -> None:
        dm, _dm_message = self.make_message_update(self.owner_id)
        group, _group_message = self.make_message_update(self.GROUP_ID)

        self.assertEqual(
            asyncio.run(add_mod.add_entry(dm, self.context)), add_mod.TYPE
        )
        add_mod._draft(dm, self.context)["title"] = "Personal deadline"

        self.assertEqual(
            asyncio.run(add_mod.add_entry(group, self.context)), add_mod.TYPE
        )
        add_mod._draft(group, self.context)["title"] = "Group deadline"

        self.assertEqual(
            add_mod._draft(dm, self.context)["title"], "Personal deadline"
        )
        self.assertEqual(
            add_mod._draft(group, self.context)["title"], "Group deadline"
        )

        self.assertEqual(
            asyncio.run(add_mod.add_cancel(dm, self.context)),
            ConversationHandler.END,
        )
        self.assertIsNone(
            get_chat_value(dm, self.context, add_mod._USER_DATA_KEY)
        )
        self.assertEqual(
            get_chat_value(group, self.context, add_mod._USER_DATA_KEY),
            {"title": "Group deadline"},
        )
        self.assertEqual(begin_chat_flow(group, self.context, "edit"), "add")
        self.assertIsNone(begin_chat_flow(dm, self.context, "edit"))
        finish_chat_flow(dm, self.context, "edit")

    def test_edit_targets_are_isolated_and_cancel_clears_only_one_chat(self) -> None:
        dm, _dm_query = self.make_edit_update(self.owner_id, 11)
        group, _group_query = self.make_edit_update(self.GROUP_ID, 22)

        def lookup(task_id: int, chat_id: int) -> Task:
            return self.task(task_id, chat_id)

        with patch.object(edit_mod, "get_task", side_effect=lookup):
            self.assertEqual(
                asyncio.run(edit_mod.edit_entry(dm, self.context)),
                edit_mod.EDIT_PICK_FIELD,
            )
            self.assertEqual(
                asyncio.run(edit_mod.edit_entry(group, self.context)),
                edit_mod.EDIT_PICK_FIELD,
            )

        self.assertEqual(edit_mod._draft_task_id(dm, self.context), 11)
        self.assertEqual(edit_mod._draft_task_id(group, self.context), 22)

        dm_cancel, _message = self.make_message_update(self.owner_id)
        self.assertEqual(
            asyncio.run(edit_mod.edit_cancel_command(dm_cancel, self.context)),
            ConversationHandler.END,
        )
        self.assertIsNone(edit_mod._draft_task_id(dm, self.context))
        self.assertEqual(edit_mod._draft_task_id(group, self.context), 22)
        self.assertEqual(begin_chat_flow(group, self.context, "add"), "edit")

    def test_finishing_add_clears_only_the_completed_chats_state(self) -> None:
        dm, _dm_message = self.make_message_update(self.owner_id)
        group, _group_message = self.make_message_update(self.GROUP_ID)
        asyncio.run(add_mod.add_entry(dm, self.context))
        asyncio.run(add_mod.add_entry(group, self.context))

        add_mod._draft(dm, self.context).update(
            title="Personal",
            task_type="assignment",
            module_code="SC2002",
            due_date=date(2026, 10, 1),
        )
        add_mod._draft(group, self.context)["title"] = "Group in progress"

        with (
            patch.object(add_mod, "add_task", return_value=99),
            patch.object(
                add_mod, "refresh_deadline_dashboard", new_callable=AsyncMock
            ),
        ):
            result = asyncio.run(add_mod._finalize_add(dm, self.context))

        self.assertEqual(result, ConversationHandler.END)
        self.assertIsNone(
            get_chat_value(dm, self.context, add_mod._USER_DATA_KEY)
        )
        self.assertEqual(
            get_chat_value(group, self.context, add_mod._USER_DATA_KEY),
            {"title": "Group in progress"},
        )
        self.assertEqual(begin_chat_flow(group, self.context, "edit"), "add")
        self.assertIsNone(begin_chat_flow(dm, self.context, "edit"))
        finish_chat_flow(dm, self.context, "edit")

    def test_active_add_blocks_edit_in_same_chat_without_losing_draft(self) -> None:
        add_update, _message = self.make_message_update(self.owner_id)
        self.assertEqual(
            asyncio.run(add_mod.add_entry(add_update, self.context)),
            add_mod.TYPE,
        )
        add_mod._draft(add_update, self.context)["title"] = "Keep me"

        edit_update, query = self.make_edit_update(self.owner_id, 31)
        with patch.object(
            edit_mod,
            "get_task",
            return_value=self.task(31, self.owner_id),
        ):
            result = asyncio.run(edit_mod.edit_entry(edit_update, self.context))

        self.assertEqual(result, ConversationHandler.END)
        self.assertEqual(
            get_chat_value(add_update, self.context, add_mod._USER_DATA_KEY),
            {"title": "Keep me"},
        )
        self.assertIsNone(edit_mod._draft_task_id(edit_update, self.context))
        query.edit_message_text.assert_not_awaited()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        self.assertIn("/add", query.answer.await_args.args[0])

    def test_active_edit_blocks_add_in_same_chat_without_losing_target(self) -> None:
        edit_update, _query = self.make_edit_update(self.owner_id, 41)
        with patch.object(
            edit_mod,
            "get_task",
            return_value=self.task(41, self.owner_id),
        ):
            self.assertEqual(
                asyncio.run(edit_mod.edit_entry(edit_update, self.context)),
                edit_mod.EDIT_PICK_FIELD,
            )

        add_update, message = self.make_message_update(self.owner_id)
        result = asyncio.run(add_mod.add_entry(add_update, self.context))

        self.assertEqual(result, ConversationHandler.END)
        self.assertEqual(edit_mod._draft_task_id(edit_update, self.context), 41)
        self.assertIsNone(
            get_chat_value(add_update, self.context, add_mod._USER_DATA_KEY)
        )
        self.assertIn("/edit", message.reply_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
