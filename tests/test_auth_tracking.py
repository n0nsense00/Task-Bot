"""Authorization-scope tests for incoming-message tracking.

The catch-all tracker runs outside handler decorators, so it must apply the
same chat boundary itself.  These tests use mocks only and never contact
Telegram.
"""
from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:dummy-token-for-tests")
os.environ.setdefault("MY_TELEGRAM_ID", "424242")
os.environ.setdefault("ALLOWED_CHAT_ID", "-1009876543210")

import config  # noqa: E402
from handlers.basic import track_incoming_message  # noqa: E402
from utils.auth import is_supported_chat  # noqa: E402


class IncomingMessageTrackingAuthorizationTests(unittest.TestCase):
    """Only the owner's DM and configured group may enter tracking state."""

    def setUp(self) -> None:
        self.bot = Mock()
        self.bot.id = 999000
        self.bot.username = "TaskBot"
        self.bot._tracked_messages = []
        self.context = Mock()
        self.context.bot = self.bot

    @staticmethod
    def _update(
        *,
        chat_id: int,
        user_id: int,
        chat_type: str,
        message_id: int,
        text: str,
    ) -> Mock:
        chat = Mock()
        chat.id = chat_id
        chat.type = chat_type

        user = Mock()
        user.id = user_id

        message = Mock()
        message.message_id = message_id
        message.chat = chat
        message.text = text
        message.entities = []
        message.reply_to_message = None

        update = Mock()
        update.effective_chat = chat
        update.effective_user = user
        update.effective_message = message
        return update

    def _track(self, update: Mock) -> None:
        asyncio.run(track_incoming_message(update, self.context))

    def test_outsider_dm_spam_is_ignored(self) -> None:
        outsider_id = 700001
        for message_id in range(1, 101):
            update = self._update(
                chat_id=outsider_id,
                user_id=outsider_id,
                chat_type="private",
                message_id=message_id,
                text="spam",
            )
            self.assertFalse(is_supported_chat(update))
            self._track(update)

        self.assertEqual(self.bot._tracked_messages, [])

    def test_other_group_is_ignored(self) -> None:
        update = self._update(
            chat_id=-100111222333,
            user_id=700002,
            chat_type="supergroup",
            message_id=201,
            text="/deadlines",
        )

        self.assertFalse(is_supported_chat(update))
        self._track(update)

        self.assertEqual(self.bot._tracked_messages, [])

    def test_channel_post_is_ignored(self) -> None:
        update = self._update(
            chat_id=-100444555666,
            user_id=777000,
            chat_type="channel",
            message_id=202,
            text="/deadlines",
        )

        self.assertFalse(is_supported_chat(update))
        self._track(update)

        self.assertEqual(self.bot._tracked_messages, [])

    def test_owner_dm_is_tracked(self) -> None:
        update = self._update(
            chat_id=config.MY_TELEGRAM_ID,
            user_id=config.MY_TELEGRAM_ID,
            chat_type="private",
            message_id=301,
            text="deadline details",
        )

        self.assertTrue(is_supported_chat(update))
        self._track(update)

        self.assertEqual(
            self.bot._tracked_messages,
            [(config.MY_TELEGRAM_ID, 301)],
        )

    def test_allowed_group_member_command_is_tracked(self) -> None:
        update = self._update(
            chat_id=config.ALLOWED_CHAT_ID,
            user_id=700003,
            chat_type="supergroup",
            message_id=401,
            text="/deadlines",
        )

        self.assertTrue(is_supported_chat(update))
        self._track(update)

        self.assertEqual(
            self.bot._tracked_messages,
            [(config.ALLOWED_CHAT_ID, 401)],
        )

    def test_kill_switch_does_not_change_tracking_scope(self) -> None:
        update = self._update(
            chat_id=config.ALLOWED_CHAT_ID,
            user_id=700004,
            chat_type="supergroup",
            message_id=501,
            text="/brief",
        )

        # The scope predicate and tracker must not consult the kill switch.
        # Handler response suppression remains authorized_only's concern.
        with patch(
            "utils.auth.is_killed",
            side_effect=AssertionError("tracking consulted the kill switch"),
        ):
            self.assertTrue(is_supported_chat(update))
            self._track(update)

        self.assertEqual(
            self.bot._tracked_messages,
            [(config.ALLOWED_CHAT_ID, 501)],
        )


if __name__ == "__main__":
    unittest.main()
