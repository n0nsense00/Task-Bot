"""Bot subclass and helpers for tracking messages deletable by /clear.

PTB's ``ExtBot`` is immutable after construction, so assigning
``application.bot.send_message = ...`` is not supported. ``TrackingBot``
overrides ``send_message`` at the class level instead.
"""
from __future__ import annotations

from typing import Any

from telegram import Message
from telegram.ext import ExtBot

TrackedMessage = tuple[int, int]


class TrackingBot(ExtBot):
    """ExtBot variant that records successful ``send_message`` calls."""

    __slots__ = ("_skip_tracking", "_tracked_messages")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialise tracking storage before handlers use the bot."""
        super().__init__(*args, **kwargs)
        self._tracked_messages: list[TrackedMessage] = []
        self._skip_tracking: bool = False

    async def send_message(self, *args: Any, **kwargs: Any) -> Message:
        """Send a message and remember its id unless tracking is paused."""
        message = await super().send_message(*args, **kwargs)
        if message is not None and not self._skip_tracking:
            self._tracked_messages.append((message.chat_id, message.message_id))
        return message


def append_tracked_message(bot: object, chat_id: int, message_id: int) -> None:
    """Track an incoming user message if ``bot`` supports tracking."""
    tracked = getattr(bot, "_tracked_messages", None)
    if isinstance(tracked, list):
        tracked.append((chat_id, message_id))


def get_tracked_messages(bot: object) -> list[TrackedMessage]:
    """Return tracked message ids for ``bot``."""
    tracked = getattr(bot, "_tracked_messages", None)
    if not isinstance(tracked, list):
        return []
    return list(tracked)


def replace_tracked_messages(
    bot: object, messages: list[TrackedMessage]
) -> None:
    """Replace tracked message ids if ``bot`` supports tracking."""
    tracked = getattr(bot, "_tracked_messages", None)
    if isinstance(tracked, list):
        tracked[:] = messages


def set_skip_tracking(bot: object, skip: bool) -> None:
    """Pause/resume tracking for housekeeping sends."""
    if isinstance(bot, TrackingBot):
        bot._skip_tracking = skip
