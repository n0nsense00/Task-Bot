"""Helpers for views that must never replace the persistent dashboard."""
from __future__ import annotations

from telegram import CallbackQuery, InlineKeyboardMarkup

from database.db import get_deadline_dashboard_message_id


async def show_transient_view(
    query: CallbackQuery,
    chat_id: int,
    text: str,
    *,
    parse_mode: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Show a manager view without ever editing the registered dashboard.

    The dashboard's Manage button is the entry point into a separate message.
    Once callbacks originate from that separate message, the same helper edits
    it in place.  The boolean result is ``True`` only when a new transient
    message was sent.

    Checking the persisted message id at every transition also protects a
    dashboard that still carries an older Edit/Delete button after a deploy.
    """
    message = query.message
    message_id = message.message_id if message is not None else None
    is_dashboard = (
        message_id is not None
        and get_deadline_dashboard_message_id(chat_id) == message_id
    )

    kwargs = {
        "parse_mode": parse_mode,
        "reply_markup": reply_markup,
    }
    if is_dashboard and message is not None:
        await message.reply_text(text, **kwargs)
        return True

    await query.edit_message_text(text, **kwargs)
    return False
