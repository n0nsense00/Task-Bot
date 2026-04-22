"""Basic command handlers: ``/start`` and ``/help``."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from utils.auth import authorized_only

logger = logging.getLogger(__name__)

_START_MESSAGE = (
    "Hi! I'm your personal Task-Bot — I help you keep track of university "
    "tasks, lectures, tutorials, and deadlines through the semester.\n\n"
    "Commands coming online:\n"
    "  /today     - what's on today\n"
    "  /week      - this week's lectures + next week's tutorials\n"
    "  /semester  - big deadlines (midterms, finals)\n"
    "  /add       - add a new task\n"
    "  /done      - mark a task done\n"
    "  /delete    - delete a task\n"
    "  /help      - show this list\n\n"
    "Most of these are still being wired up. Stay tuned."
)

_HELP_MESSAGE = "Help is still a work-in-progress. For now, try /start."


@authorized_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/start``: greet the owner and outline planned commands."""
    message = update.effective_message
    if message is None:
        return
    try:
        await message.reply_text(_START_MESSAGE)
    except Exception:
        logger.exception("Failed to send /start response")


@authorized_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/help``: placeholder, to be expanded in later phases."""
    message = update.effective_message
    if message is None:
        return
    try:
        await message.reply_text(_HELP_MESSAGE)
    except Exception:
        logger.exception("Failed to send /help response")
