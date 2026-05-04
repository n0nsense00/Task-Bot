"""Basic command handlers: ``/start`` and ``/help``.

Both produce a sectioned command list with a rotated daily tip in the footer.
``/help`` adds a section describing the inline-keyboard buttons available on
``/today`` so the user discovers them without having to tap around blindly.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from utils.auth import authorized_only
from utils.errors import safe
from utils.format import DIVIDER, todays_tip

logger = logging.getLogger(__name__)


def _start_message() -> str:
    """Build the /start welcome message with sectioned command list + tip."""
    sections = [
        "☀️ <b>Welcome to Task-Bot</b>",
        "<i>Your personal university tasks tracker.</i>",
        "",
        DIVIDER,
        "",
        "<b>📅 View</b>",
        "• /today — tasks due today",
        "• /week — this week's lectures + next week's tutorials",
        "• /semester — midterms &amp; finals",
        "",
        "<b>✏️ Manage</b>",
        "• /add — create a new task (interactive)",
        "• /done &lt;id&gt; — mark a task complete",
        "• /delete &lt;id&gt; — delete a task",
        "• /brief — today's morning summary on demand",
        "",
        DIVIDER,
        "",
        todays_tip(),
    ]
    return "\n".join(sections)


def _help_message() -> str:
    """Build the /help reference message — more detail than /start."""
    sections = [
        "📖 <b>Task-Bot Commands</b>",
        "",
        DIVIDER,
        "",
        "<b>📅 View</b>",
        "• /today — tasks due today, with quick-action buttons",
        "• /week — this week's lectures + next week's tutorials",
        "• /semester — every midterm and final, grouped by urgency",
        "",
        "<b>✏️ Manage</b>",
        "• /add — start the 6-step task creation flow",
        "• /done &lt;id&gt; — mark a task done; e.g. <code>/done 24</code>",
        "• /delete &lt;id&gt; — delete a task (asks confirmation); "
        "e.g. <code>/delete 24</code>",
        "• /cancel — abort an /add or /edit flow",
        "• /brief — send today's morning summary now",
        "",
        "<b>🎮 Inline Buttons</b>",
        "On /today, tap:",
        "• ✅ to mark a task complete",
        "• 📝 to edit any field",
        "• 🗑️ to delete (with confirmation)",
        "",
        DIVIDER,
        "",
        todays_tip(),
    ]
    return "\n".join(sections)


@authorized_only
@safe
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/start``: greet the owner with a sectioned command list."""
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(_start_message(), parse_mode=ParseMode.HTML)


@authorized_only
@safe
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/help``: reference message with examples and inline-button cheat sheet."""
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(_help_message(), parse_mode=ParseMode.HTML)
