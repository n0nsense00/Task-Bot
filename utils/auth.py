"""Authorization decorators for Task-Bot.

The bot is restricted to a single group chat (``ALLOWED_CHAT_ID``). Every
update from any other chat — DMs, other groups, channels — is silently
dropped. We deliberately do not reply, so outsiders cannot confirm the bot
exists or that they hit a guarded handler.

Two decorators:

- ``authorized_only``: the default gate. Any member of the allowed group can
  call the wrapped handler.
- ``admin_only``: stricter gate for destructive commands. Requires both the
  allowed chat AND the bot owner (``MY_TELEGRAM_ID``).
"""
from __future__ import annotations

import logging
from functools import wraps
from typing import Any, Awaitable, Callable

from telegram import Update
from telegram.ext import ContextTypes

from config import ALLOWED_CHAT_ID, MY_TELEGRAM_ID
from utils.kill_switch import is_killed

logger = logging.getLogger(__name__)

HandlerFunc = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]]


def authorized_only(func: HandlerFunc) -> HandlerFunc:
    """Restrict a handler to the allowed group chat.

    Wraps an async PTB handler. Updates are silently dropped if either:
    (a) the effective chat is not ``ALLOWED_CHAT_ID`` — logged at WARNING
        with chat id/type so a fresh deployment can read the group id from
        its own logs; or
    (b) the kill switch is engaged (see :mod:`utils.kill_switch`) — logged
        at DEBUG only, since this is an expected owner-driven silence.

    Admin-only handlers use ``admin_only`` instead, which deliberately
    bypasses the kill switch so the owner can still ``/revive``.
    """

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        chat = update.effective_chat
        if chat is None or chat.id != ALLOWED_CHAT_ID:
            user = update.effective_user
            logger.warning(
                "Unauthorized chat: chat_id=%s chat_type=%s user_id=%s username=%s",
                getattr(chat, "id", None),
                getattr(chat, "type", None),
                getattr(user, "id", None),
                getattr(user, "username", None),
            )
            return None
        if is_killed():
            user = update.effective_user
            logger.debug(
                "Playing dead — pretending not to hear %s (id=%s)",
                getattr(user, "username", None),
                getattr(user, "id", None),
            )
            return None
        return await func(update, context)

    return wrapper


def admin_only(func: HandlerFunc) -> HandlerFunc:
    """Restrict a handler to the bot owner inside the allowed group chat.

    Both conditions must hold: the update must come from ``ALLOWED_CHAT_ID``
    AND from ``MY_TELEGRAM_ID``. Use for destructive commands that even
    trusted group members shouldn't be able to invoke (e.g. ``/clear``).
    Failures are logged at WARNING and silently dropped.
    """

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        chat = update.effective_chat
        user = update.effective_user
        if (
            chat is None
            or chat.id != ALLOWED_CHAT_ID
            or user is None
            or user.id != MY_TELEGRAM_ID
        ):
            logger.warning(
                "Admin-only refused: chat_id=%s user_id=%s username=%s",
                getattr(chat, "id", None),
                getattr(user, "id", None),
                getattr(user, "username", None),
            )
            return None
        return await func(update, context)

    return wrapper
