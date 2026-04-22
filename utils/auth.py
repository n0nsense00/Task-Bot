"""Authorization decorator for Task-Bot.

Only the bot owner (identified by ``MY_TELEGRAM_ID`` in ``.env``) may interact
with the bot. Any other user's updates are silently dropped — we deliberately
do not reply, so unauthorized parties cannot confirm the bot exists.
"""
from __future__ import annotations

import logging
from functools import wraps
from typing import Any, Awaitable, Callable

from telegram import Update
from telegram.ext import ContextTypes

from config import MY_TELEGRAM_ID

logger = logging.getLogger(__name__)

HandlerFunc = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]]


def authorized_only(func: HandlerFunc) -> HandlerFunc:
    """Restrict a handler to the bot owner.

    Wraps an async PTB handler. If the incoming update's effective user is not
    ``MY_TELEGRAM_ID``, the call is logged at WARNING and silently dropped.
    """

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        user = update.effective_user
        if user is None or user.id != MY_TELEGRAM_ID:
            logger.warning(
                "Unauthorized access attempt: user_id=%s username=%s",
                getattr(user, "id", None),
                getattr(user, "username", None),
            )
            return None
        return await func(update, context)

    return wrapper
