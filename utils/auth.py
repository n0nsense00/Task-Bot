"""Authorization decorators for Task-Bot.

Access model:
- The bot owner can use it in their private bot chat and the configured group.
- Any other user can only use the bot inside the allowed group chat
  (``ALLOWED_CHAT_ID``). DMs, other groups, channels — all silently dropped.
- The kill switch (``utils.kill_switch``) silences non-owner users only;
  the owner always retains control so they can ``/revive``.

Two decorators:

- ``authorized_only``: the default gate. Lets through (a) any message from
  the allowed group, OR (b) the owner's private bot chat. Honors the
  kill switch for non-owners.
- ``admin_only``: stricter gate for destructive commands. Owner only, and
  only in the owner's private bot chat or configured group.
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
    """Restrict a handler to the owner's DM or the one allowed group.

    Wraps an async PTB handler. The update passes if either:
    (a) it comes from the owner's private bot chat; or
    (b) it comes from the allowed group chat (``ALLOWED_CHAT_ID``).

    Updates failing both checks are logged at WARNING (with chat/user ids
    so a fresh deployment can discover the group id from logs) and dropped.

    The kill switch (see :mod:`utils.kill_switch`) silences non-owner users
    only. The owner can still use ``/revive`` from their DM or allowed group.
    """

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        chat = update.effective_chat
        user = update.effective_user
        is_owner = user is not None and user.id == MY_TELEGRAM_ID
        is_owner_private_chat = (
            is_owner and chat is not None and chat.id == MY_TELEGRAM_ID
        )
        in_allowed_chat = chat is not None and chat.id == ALLOWED_CHAT_ID
        if not (is_owner_private_chat or in_allowed_chat):
            logger.warning(
                "Unauthorized: chat_id=%s chat_type=%s user_id=%s username=%s",
                getattr(chat, "id", None),
                getattr(chat, "type", None),
                getattr(user, "id", None),
                getattr(user, "username", None),
            )
            return None
        if is_killed() and not is_owner:
            logger.debug(
                "Playing dead — pretending not to hear %s (id=%s)",
                getattr(user, "username", None),
                getattr(user, "id", None),
            )
            return None
        return await func(update, context)

    return wrapper


def admin_only(func: HandlerFunc) -> HandlerFunc:
    """Restrict a handler to the owner in their DM or allowed group.

    The update must come from ``MY_TELEGRAM_ID`` and from either the owner's
    private bot chat or the configured group. Use for destructive
    commands that even trusted group members shouldn't invoke (e.g.
    ``/clear``, ``/kill``, ``/revive``). Bypasses the kill switch by
    design so the owner always retains control. Failures are logged at
    WARNING and silently dropped.
    """

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        chat = update.effective_chat
        user = update.effective_user
        is_owner = user is not None and user.id == MY_TELEGRAM_ID
        in_supported_chat = chat is not None and chat.id in (
            MY_TELEGRAM_ID,
            ALLOWED_CHAT_ID,
        )
        if not (is_owner and in_supported_chat):
            logger.warning(
                "Admin-only refused: chat_id=%s user_id=%s username=%s",
                getattr(chat, "id", None),
                getattr(user, "id", None),
                getattr(user, "username", None),
            )
            return None
        return await func(update, context)

    return wrapper
