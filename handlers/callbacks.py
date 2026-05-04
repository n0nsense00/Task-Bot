"""Inline-keyboard callback handlers for the /today task buttons.

The 📝 Edit flow is intentionally handled elsewhere — it's a multi-step
``ConversationHandler`` that lives in :mod:`handlers.edit_task`. This module
only contains the single-shot Done / Delete-request / Delete-confirm flows
because they're stateless and don't fit the conversation model.

Callback-data formats handled here:
    ``done:N``       — mark task #N complete, re-render /today in place
    ``del:N``        — entry point: show Yes/No confirmation in place
    ``del:yes:N``    — confirmed: delete task #N, show "Deleted" card
    ``del:no:N``     — cancelled: show "Cancelled" message

The ``del:N`` (one colon) and ``del:yes:N`` / ``del:no:N`` (two colons) are
disambiguated by colon count — see :func:`delete_request_callback` and
:func:`delete_confirm_callback`.
"""
from __future__ import annotations

import logging
from datetime import date

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from database.db import (
    delete_task,
    get_task,
    get_tasks_for_date,
    mark_complete,
)
from utils.auth import authorized_only
from utils.errors import safe
from utils.format import (
    CB_DELETE,
    CB_DONE,
    DIVIDER,
    build_delete_confirmation_keyboard,
    build_task_keyboard,
    format_grouped_today,
    format_task_card,
    morning_greeting,
    todays_tip,
)

logger = logging.getLogger(__name__)

_NO_TASKS_TODAY_MESSAGE: str = "🎉 Nothing scheduled for today. Enjoy!"


def _render_today_message() -> tuple[str, InlineKeyboardMarkup | None]:
    """Rebuild the /today text + keyboard from current DB state.

    Called after Done/Delete mutations so the re-rendered message reflects
    the live row set. Mirrors the layout used by the /today slash handler
    in :mod:`handlers.tasks` — keep them in sync if either side changes.
    """
    today = date.today()
    tasks = get_tasks_for_date(today)
    if not tasks:
        return (_NO_TASKS_TODAY_MESSAGE, None)

    lines: list[str] = [morning_greeting(), "", DIVIDER]
    lines.extend(format_grouped_today(tasks, today))
    lines.append("")
    lines.append(DIVIDER)
    lines.append("")
    plural = "s" if len(tasks) != 1 else ""
    lines.append(f"<i>{len(tasks)} task{plural} today</i>")
    lines.append("")
    lines.append(todays_tip())
    return ("\n".join(lines), build_task_keyboard(tasks))


@authorized_only
@safe
async def done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``done:N``: mark task complete and re-render /today in place.

    A toast confirmation appears at the top of Telegram via ``query.answer``;
    the message body is then edited to the refreshed task list.
    """
    query = update.callback_query
    if query is None or query.data is None:
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

    task = get_task(task_id)
    if task is None:
        await query.answer(f"Task #{task_id} not found", show_alert=True)
        return

    mark_complete(task_id)
    await query.answer(f"Marked done: {task.title}")

    text, keyboard = _render_today_message()
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
    except Exception:
        # Editing fails for messages older than 48h, or if the message has
        # since been deleted. Don't crash — the toast already confirmed.
        logger.exception("Failed to edit /today after Done tap")


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
    if query is None or query.data is None:
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

    task = get_task(task_id)
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
    deliberately do **not** auto-render /today, because the user may have
    invoked /delete in isolation and a sudden /today rebuild would be
    surprising.
    """
    query = update.callback_query
    if query is None or query.data is None:
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

    task = get_task(task_id)
    if task is None:
        await query.answer("Already deleted")
        await query.edit_message_text(
            f"Task #{task_id} was already gone."
        )
        return

    delete_task(task_id)
    await query.answer(f"Deleted: {task.title}")
    await query.edit_message_text(
        "🗑️ <b>Deleted</b>\n\n" + format_task_card(task),
        parse_mode=ParseMode.HTML,
    )
