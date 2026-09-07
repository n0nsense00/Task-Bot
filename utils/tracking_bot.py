"""Bot subclass and helpers for tracking messages deletable by /clear.

PTB's ``ExtBot`` is immutable after construction, so assigning
``application.bot.send_message = ...`` is not supported. ``TrackingBot``
overrides ``send_message`` at the class level instead. SQLite is the source of
truth; the in-memory list is retained only for compatibility with existing
callers and test doubles.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from telegram import Message
from telegram.ext import ExtBot

from database import db as db_store

TrackedMessage = tuple[int, int]
logger = logging.getLogger(__name__)


class TrackingBot(ExtBot):
    """ExtBot variant that records successful ``send_message`` calls."""

    __slots__ = ("_skip_tracking", "_tracked_messages")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialise tracking storage before handlers use the bot."""
        super().__init__(*args, **kwargs)
        self._tracked_messages: list[TrackedMessage] = []
        self._skip_tracking: bool = False

    async def send_message(self, *args: Any, **kwargs: Any) -> Message:
        """Send a message and persist its id unless tracking is paused."""
        message = await super().send_message(*args, **kwargs)
        if message is not None and not self._skip_tracking:
            created_at = getattr(message, "date", None)
            append_tracked_message(
                self,
                message.chat_id,
                message.message_id,
                created_at=created_at if isinstance(created_at, datetime) else None,
            )
        return message


def append_tracked_message(
    bot: object,
    chat_id: int,
    message_id: int,
    *,
    created_at: datetime | None = None,
) -> None:
    """Persist a message and mirror it in the compatibility memory cache."""
    tracked = getattr(bot, "_tracked_messages", None)
    if isinstance(tracked, list):
        tracked.append((chat_id, message_id))
    try:
        db_store.save_tracked_message(chat_id, message_id, created_at)
    except Exception:
        # The Telegram operation has already succeeded. Do not report a false
        # send failure (which could make a handler retry and post a duplicate),
        # but make persistence problems visible to operators.
        logger.exception(
            "Failed to persist tracked message %s in chat %s",
            message_id,
            chat_id,
        )


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
