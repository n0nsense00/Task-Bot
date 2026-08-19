"""Inline-keyboard callback handlers for the /deadlines action buttons.

The 📝 Edit flow is intentionally handled elsewhere — it's a multi-step
``ConversationHandler`` that lives in :mod:`handlers.edit_task`. This module
only contains the single-shot Done / Delete-request / Delete-confirm flows
because they're stateless and don't fit the conversation model.

Callback-data formats handled here:
    ``manage:N``     — show page N of the compact deadline picker
    ``manageitem:N:P`` — show task #N's actions, returning to page P
    ``managedash``   — return to the main /deadlines dashboard
    ``done:N``       — mark task #N complete, re-render /deadlines in place
    ``del:N``        — entry point: show Yes/No confirmation in place
    ``del:yes:N``    — confirmed: delete task #N, show "Deleted" card
    ``del:no:N``     — cancelled: show "Cancelled" message

The ``del:N`` (one colon) and ``del:yes:N`` / ``del:no:N`` (two colons) are
disambiguated by colon count — see :func:`delete_request_callback` and
:func:`delete_confirm_callback`.
"""
from __future__ import annotations

import logging
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from database.db import delete_task, get_task, mark_complete
from handlers.tasks import render_deadline_picker, render_deadlines
from utils.auth import authorized_only
from utils.errors import safe
from utils.format import (
    CB_DELETE,
    CB_DONE,
    CB_MANAGE,
    CB_MANAGE_DASHBOARD,
    CB_MANAGE_ITEM,
    build_deadline_action_keyboard,
    build_delete_confirmation_keyboard,
    format_task_card,
)

logger = logging.getLogger(__name__)


@authorized_only
@safe
async def manage_deadlines_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle ``manage:N`` by opening the requested deadline-picker page."""
    query = update.callback_query
    chat = update.effective_chat
    if query is None or query.data is None or chat is None:
        return

    parts = query.data.split(":", 1)
    if len(parts) != 2 or parts[0] != CB_MANAGE:
        await query.answer()
        return
    try:
        page = int(parts[1])
    except ValueError:
        await query.answer("Invalid page", show_alert=True)
        return

    await query.answer()
    text, keyboard = render_deadline_picker(chat.id, page)
    await query.edit_message_text(
        text, parse_mode=ParseMode.HTML, reply_markup=keyboard
    )


@authorized_only
@safe
async def manage_deadline_item_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle ``manageitem:N:P`` and show actions below the selected item."""
    query = update.callback_query
    chat = update.effective_chat
    if query is None or query.data is None or chat is None:
        return

    parts = query.data.split(":")
    if len(parts) != 3 or parts[0] != CB_MANAGE_ITEM:
        await query.answer()
        return
    try:
        task_id = int(parts[1])
        page = max(int(parts[2]), 0)
    except ValueError:
        await query.answer("Invalid deadline", show_alert=True)
        return

    task = get_task(task_id, chat.id)
    if task is None or task.completed:
        await query.answer("That deadline is no longer pending", show_alert=True)
        text, keyboard = render_deadline_picker(chat.id, page)
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
        return

    await query.answer()
    text = (
        "⚙️ <b>Manage Deadline</b>\n\n"
        + format_task_card(task)
        + "\n\n<i>Choose an action below.</i>"
    )
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=build_deadline_action_keyboard(task_id, page),
    )


@authorized_only
@safe
async def manage_dashboard_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle ``managedash`` by returning to the main deadline dashboard."""
    query = update.callback_query
    chat = update.effective_chat
    if query is None or query.data != CB_MANAGE_DASHBOARD or chat is None:
        return
    await query.answer()
    text, keyboard = render_deadlines(chat.id)
    await query.edit_message_text(
        text, parse_mode=ParseMode.HTML, reply_markup=keyboard
    )

@authorized_only
@safe
async def done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``done:N``: complete it and refresh /deadlines in place.

    A toast confirmation appears at the top of Telegram via ``query.answer``;
    the message body is then edited to the refreshed task list.
    """
    query = update.callback_query
    chat = update.effective_chat
    if query is None or query.data is None or chat is None:
        return

    parts = query.data.split(":", 1)
    if len(parts) != 2 or parts[0] != CB_DONE:
        await query.answer()
        return

    try:
        task_id = int(parts[1])
    except ValueError:
        await query.answer("Invalid task id", show_alert=True)
        return

    task = get_task(task_id, chat.id)
    if task is None:
        await query.answer(f"Task #{task_id} not found", show_alert=True)
        return

    mark_complete(task_id, chat.id)
    await query.answer(f"Marked done: {task.title}")

    text, keyboard = render_deadlines(chat.id)
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
    except Exception:
        # Editing fails for messages older than 48h, or if the message has
        # since been deleted. Don't crash — the toast already confirmed.
        logger.exception("Failed to refresh /deadlines after Done tap")


@authorized_only
@safe
async def delete_request_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle ``del:N`` (one colon): show Yes/No confirmation in place.

    Distinguished from ``del:yes:N`` / ``del:no:N`` (two colons, handled by
    :func:`delete_confirm_callback`) by colon count. Both share the
    ``del:`` prefix, so the dispatch pattern in ``bot.py`` is regex-based.
    """
    query = update.callback_query
    chat = update.effective_chat
    if query is None or query.data is None or chat is None:
        return

    parts = query.data.split(":")
    if len(parts) != 2 or parts[0] != CB_DELETE:
        return

    await query.answer()

    try:
        task_id = int(parts[1])
    except ValueError:
        await query.edit_message_text("Invalid task id.")
        return

    task = get_task(task_id, chat.id)
    if task is None:
        await query.edit_message_text(f"Task #{task_id} not found.")
        return

    confirmation = "🗑️ <b>Delete this task?</b>\n\n" + format_task_card(task)
    await query.edit_message_text(
        confirmation,
        parse_mode=ParseMode.HTML,
        reply_markup=build_delete_confirmation_keyboard(task_id),
    )


@authorized_only
@safe
async def delete_confirm_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle ``del:yes:N`` and ``del:no:N``: perform delete or cancel.

    Both the slash-initiated /delete flow and the button-initiated 🗑️ flow
    funnel into this handler — they construct identical Yes/No keyboards via
    :func:`utils.format.build_delete_confirmation_keyboard`. After the
    decision, the message is edited to a "Deleted" or "Cancelled" card; we
    deliberately do **not** auto-render /deadlines, because the user may have
    invoked /delete in isolation and a sudden dashboard rebuild would be
    surprising.
    """
    query = update.callback_query
    chat = update.effective_chat
    if query is None or query.data is None or chat is None:
        return

    parts = query.data.split(":")
    if len(parts) != 3 or parts[0] != CB_DELETE:
        return

    action, raw_id = parts[1], parts[2]
    try:
        task_id = int(raw_id)
    except ValueError:
        await query.answer("Invalid task id", show_alert=True)
        return

    if action == "no":
        await query.answer("Cancelled")
        await query.edit_message_text("Cancelled. No tasks were deleted.")
        return

    if action != "yes":
        return

    task = get_task(task_id, chat.id)
    if task is None:
        await query.answer("Already deleted")
        await query.edit_message_text(
            f"Task #{task_id} was already gone."
        )
        return

    delete_task(task_id, chat.id)
    await query.answer(f"Deleted: {task.title}")
    await query.edit_message_text(
        "🗑️ <b>Deleted</b>\n\n" + format_task_card(task),
        parse_mode=ParseMode.HTML,
    )
