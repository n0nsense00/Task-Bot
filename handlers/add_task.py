"""Multi-step /add conversation for creating a new task.

Walks the user through six prompts — title, type, module code, due date,
week number, notes — then persists the resulting ``Task``. ``/cancel`` at
any step aborts without writing to the DB.

The conversation stores the in-progress draft in ``context.user_data`` under
``_USER_DATA_KEY`` so a mid-flow crash can't corrupt anything durable.
"""
from __future__ import annotations

import html
import logging
from datetime import date
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import CMD_ADD, CMD_CANCEL
from database.db import add_task
from database.models import TASK_TYPES, Task
from utils.auth import authorized_only
from utils.errors import safe
from utils.format import format_task_card

logger = logging.getLogger(__name__)

# Conversation states.
TITLE, TASK_TYPE, MODULE_CODE, DUE_DATE, WEEK_NUMBER, NOTES = range(6)

_SKIP_KEYWORD: str = "skip"
_ADD_TYPE_CB_PREFIX: str = "addtype"
_USER_DATA_KEY: str = "new_task_draft"


def _esc(text: str | None) -> str:
    """HTML-escape a user-supplied string; map ``None`` to the empty string."""
    return html.escape(text) if text else ""


def _type_keyboard() -> InlineKeyboardMarkup:
    """Build a 2-wide inline keyboard covering every valid task type."""
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for t in TASK_TYPES:
        row.append(
            InlineKeyboardButton(
                t.capitalize(), callback_data=f"{_ADD_TYPE_CB_PREFIX}:{t}"
            )
        )
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


def _draft(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    """Return (creating if needed) the in-progress draft dict for this user."""
    draft = context.user_data.get(_USER_DATA_KEY)
    if draft is None:
        draft = {}
        context.user_data[_USER_DATA_KEY] = draft
    return draft


@authorized_only
@safe
async def add_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Begin the /add flow: clear any stale draft and ask for the title."""
    message = update.effective_message
    if message is None:
        return ConversationHandler.END
    context.user_data[_USER_DATA_KEY] = {}
    await message.reply_text(
        "What's the task? (send its title, or /cancel to abort)"
    )
    return TITLE


@authorized_only
@safe
async def add_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Capture the title; prompt for task type via inline keyboard."""
    message = update.effective_message
    if message is None or message.text is None:
        return TITLE
    title = message.text.strip()
    if not title:
        await message.reply_text("Title can't be empty. Try again, or /cancel.")
        return TITLE
    _draft(context)["title"] = title
    await message.reply_text("What type of task?", reply_markup=_type_keyboard())
    return TASK_TYPE


@authorized_only
@safe
async def add_task_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Capture the task-type selection; prompt for module code."""
    query = update.callback_query
    if query is None or query.data is None:
        return TASK_TYPE
    await query.answer()

    parts = query.data.split(":", 1)
    if (
        len(parts) != 2
        or parts[0] != _ADD_TYPE_CB_PREFIX
        or parts[1] not in TASK_TYPES
    ):
        await query.edit_message_text("Invalid selection. Aborting /add.")
        context.user_data.pop(_USER_DATA_KEY, None)
        return ConversationHandler.END

    chosen = parts[1]
    _draft(context)["task_type"] = chosen
    await query.edit_message_text(f"Type: {chosen}")
    # Send a fresh prompt message rather than editing the keyboard message,
    # so the conversation reads chronologically in the chat.
    if query.message is not None:
        await query.message.reply_text("Module code? (e.g. CS2040, or 'skip')")
    return MODULE_CODE


@authorized_only
@safe
async def add_module_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Capture the module code (or ``skip``); prompt for due date."""
    message = update.effective_message
    if message is None or message.text is None:
        return MODULE_CODE
    raw = message.text.strip()
    _draft(context)["module_code"] = (
        None if raw.lower() == _SKIP_KEYWORD else raw
    )
    await message.reply_text("Due date? (YYYY-MM-DD)")
    return DUE_DATE


@authorized_only
@safe
async def add_due_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validate a YYYY-MM-DD date; prompt for week number or re-ask on failure."""
    message = update.effective_message
    if message is None or message.text is None:
        return DUE_DATE
    raw = message.text.strip()
    try:
        due = date.fromisoformat(raw)
    except ValueError:
        await message.reply_text(
            "That didn't parse as YYYY-MM-DD. Try again, or /cancel."
        )
        return DUE_DATE
    _draft(context)["due_date"] = due
    await message.reply_text("Week number? (1-13, or 'skip')")
    return WEEK_NUMBER


@authorized_only
@safe
async def add_week_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validate the week number (1-13 or ``skip``); prompt for notes."""
    message = update.effective_message
    if message is None or message.text is None:
        return WEEK_NUMBER
    raw = message.text.strip()
    if raw.lower() == _SKIP_KEYWORD:
        _draft(context)["week_number"] = None
    else:
        try:
            week_num = int(raw)
        except ValueError:
            await message.reply_text(
                "That's not a number. Try 1-13, or 'skip', or /cancel."
            )
            return WEEK_NUMBER
        if not 1 <= week_num <= 13:
            await message.reply_text(
                "Week must be between 1 and 13. Try again, 'skip', or /cancel."
            )
            return WEEK_NUMBER
        _draft(context)["week_number"] = week_num
    await message.reply_text("Any notes? (text, or 'skip')")
    return NOTES


@authorized_only
@safe
async def add_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Capture notes, persist the task, reply with a summary, end the conversation."""
    message = update.effective_message
    if message is None or message.text is None:
        return NOTES
    raw = message.text.strip()
    draft = _draft(context)
    draft["notes"] = None if raw.lower() == _SKIP_KEYWORD else raw

    required = ("title", "task_type", "due_date")
    if any(k not in draft for k in required):
        await message.reply_text(
            "Draft is missing required fields — aborting. Please /add again."
        )
        context.user_data.pop(_USER_DATA_KEY, None)
        return ConversationHandler.END

    try:
        task = Task(
            title=draft["title"],
            task_type=draft["task_type"],
            due_date=draft["due_date"],
            module_code=draft.get("module_code"),
            week_number=draft.get("week_number"),
            notes=draft.get("notes"),
        )
        new_id = add_task(task)
    except Exception:
        logger.exception("Failed to save new task")
        await message.reply_text(
            "Couldn't save that task — check the logs. Nothing was stored."
        )
        context.user_data.pop(_USER_DATA_KEY, None)
        return ConversationHandler.END

    context.user_data.pop(_USER_DATA_KEY, None)

    # Stamp the persisted id back onto the dataclass so format_task_card
    # renders ``#N`` rather than ``#None``.
    task.id = new_id
    summary = "✅ <b>Added</b>\n\n" + format_task_card(task)
    await message.reply_text(summary, parse_mode=ParseMode.HTML)
    return ConversationHandler.END


@authorized_only
@safe
async def add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Abort the /add flow, clear the draft, confirm to the user."""
    message = update.effective_message
    context.user_data.pop(_USER_DATA_KEY, None)
    if message is not None:
        await message.reply_text("Cancelled. Nothing was added.")
    return ConversationHandler.END


def build_add_conversation() -> ConversationHandler:
    """Construct the ``ConversationHandler`` wiring the /add flow end-to-end."""
    return ConversationHandler(
        entry_points=[CommandHandler(CMD_ADD, add_entry)],
        states={
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_title)],
            TASK_TYPE: [
                CallbackQueryHandler(
                    add_task_type, pattern=f"^{_ADD_TYPE_CB_PREFIX}:"
                )
            ],
            MODULE_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_module_code)
            ],
            DUE_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_due_date)
            ],
            WEEK_NUMBER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_week_number)
            ],
            NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_notes)],
        },
        fallbacks=[CommandHandler(CMD_CANCEL, add_cancel)],
        name="add_task_conversation",
        persistent=False,
    )
