"""Catch-all handlers: unknown commands and PTB-level error callback."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from utils.auth import authorized_only
from utils.errors import safe

logger = logging.getLogger(__name__)

_UNKNOWN_COMMAND_MESSAGE: str = "Unknown command. Try /help"


@authorized_only
@safe
async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Respond to unrecognized commands from the owner with a pointer to /help.

    Outsiders never reach this — ``@authorized_only`` drops their updates
    silently, preserving the "bot doesn't exist" illusion.
    """
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(_UNKNOWN_COMMAND_MESSAGE)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """PTB-level catch-all for errors that slip past per-handler ``@safe``.

    Only logs; does not reply, since ``@safe`` has typically already sent a
    user-visible fallback and we want to avoid double-replies.
    """
    logger.exception(
        "Unhandled exception during update processing",
        exc_info=context.error,
    )
