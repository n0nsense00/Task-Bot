"""Error-handling decorator and generic reply constants.

Pair with ``@authorized_only`` on every handler to guarantee (a) only the
owner's updates ever reach the body, and (b) no exception from the body
ever leaks a traceback to Telegram.
"""
from __future__ import annotations

import logging
from functools import wraps
from typing import Any, Awaitable, Callable

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

HandlerFunc = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]]

GENERIC_ERROR_MESSAGE: str = "Something went wrong. Check the logs."


def safe(func: HandlerFunc) -> HandlerFunc:
    """Wrap a handler so exceptions log a traceback and send a generic reply.

    Failures while sending the fallback reply are themselves logged rather
    than re-raised, so a broken network connection can't turn into a crash.
    Returns ``None`` on exception — for conversation state handlers, PTB
    interprets that as "stay in the current state", so the user can retry.
    """

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        try:
            return await func(update, context)
        except Exception:
            logger.exception("Handler %s failed", func.__name__)
            try:
                message = update.effective_message if update else None
                if message is not None:
                    await message.reply_text(GENERIC_ERROR_MESSAGE)
            except Exception:
                logger.exception("Failed to send error reply")
            return None

    return wrapper
