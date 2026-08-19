"""Owner-only admin commands: ``/kill`` and ``/revive``.

The kill switch silences the bot inside the allowed group without removing
it from the chat or redeploying. State lives in ``data/killed.flag`` on the
EC2 instance's EBS-backed disk, so it survives a service or host restart with
no volume configuration at all.

Both commands use ``@admin_only`` rather than ``@authorized_only`` because
the latter respects the kill switch — using it on ``/revive`` would make
the bot unrevivable once killed. ``admin_only`` deliberately bypasses the
kill check so the owner always has emergency control.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from utils.auth import admin_only
from utils.errors import safe
from utils.kill_switch import is_killed, kill, revive

logger = logging.getLogger(__name__)


@admin_only
@safe
async def kill_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/kill``: engage the kill switch.

    Once engaged, every ``@authorized_only`` handler silently drops its
    update. Admin commands (``/clear``, ``/kill``, ``/revive``) keep working.
    """
    message = update.effective_message
    if message is None:
        return

    if is_killed():
        await message.reply_text(
            "💀 <b>Already dead, boss.</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    kill()
    await message.reply_text(
        "☠️ <b>Kill switch engaged.</b>",
        parse_mode=ParseMode.HTML,
    )


@admin_only
@safe
async def revive_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/revive``: disengage the kill switch and resume normal operation."""
    message = update.effective_message
    if message is None:
        return

    was_killed = revive()
    if not was_killed:
        await message.reply_text(
            "🟢 <b>I was never dead.</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    await message.reply_text(
        "🟢 <b>Charlie Kirk at your service.</b>",
        parse_mode=ParseMode.HTML,
    )
