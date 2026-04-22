"""Manual trigger for the scheduled morning brief.

Lets the owner request the same message the 08:00 cron job would deliver —
useful for testing without waiting until morning, or for pulling a fresh
brief mid-day after editing tasks.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from scheduler import build_morning_brief
from utils.auth import authorized_only
from utils.errors import safe

logger = logging.getLogger(__name__)


@authorized_only
@safe
async def brief(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /brief: send the morning-brief message on demand."""
    message = update.effective_message
    if message is None:
        return
    text = build_morning_brief()
    await message.reply_text(text, parse_mode=ParseMode.HTML)
    logger.info("Morning brief sent via /brief")
