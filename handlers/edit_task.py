"""Multi-step edit conversation entered from the 📝 button on /today.

Conversation state machine::

    edit:N (callback)
      └─ EDIT_PICK_FIELD: show field-picker keyboard
          ├─ editf:title:N  → EDIT_TITLE  (text)        → save → END
          ├─ editf:type:N   → EDIT_TYPE   (callback)    → save → END
          ├─ editf:module:N → EDIT_MODULE (text)        → save → END
          ├─ editf:due:N    → EDIT_DUE    (text)        → save → END
          ├─ editf:week:N   → EDIT_WEEK   (text)        → save → END
          ├─ editf:notes:N  → EDIT_NOTES  (text)        → save → END
          └─ editcancel:N   → END

The target task id is stashed in ``context.user_data`` after the entry
callback, then read by each state handler so they know which row to mutate.
Cleared on END so future conversations start clean.
"""
from __future__ import annotations

import logging
from datetime import date

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import CMD_CANCEL
from database.db import get_task, update_task
from database.models import TASK_TYPES, Task
from utils.auth import authorized_only
from utils.errors import safe
from utils.format import (
    CB_EDIT,
    CB_EDIT_CANCEL,
    CB_EDIT_FIELD,
    CB_EDIT_TYPE_VALUE,
    build_edit_field_keyboard,
    build_edit_type_keyboard,
    esc,
    format_task_card,
    module_prefix,
)

logger = logging.getLogger(__name__)

# Conversation states — high integers to avoid clash with /add (which uses 0..5).
EDIT_PICK_FIELD = 200
EDIT_TITLE = 201
EDIT_TYPE = 202
EDIT_MODULE = 203
EDIT_DUE = 204
EDIT_WEEK = 205
EDIT_NOTES = 206

_CLEAR_KEYWORD: str = "clear"
_USER_DATA_TASK_ID: str = "edit_task_id"


def _draft_task_id(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Return the task id stashed in user_data, or ``None`` if missing/invalid."""
    raw = context.user_data.get(_USER_DATA_TASK_ID)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _save_and_summarize(task: Task) -> str:
    """Persist ``task`` and build the post-update HTML success message."""
    update_task(task)
    return "✅ <b>Updated</b>\n\n" + format_task_card(task)


@authorized_only
@safe
async def edit_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry: parse 'edit:N' callback, store task id, show field picker."""
    query = update.callback_query
    if query is None or query.data is None:
        return ConversationHandler.END
    await query.answer()

    parts = query.data.split(":", 1)
    if len(parts) != 2 or parts[0] != CB_EDIT:
        return ConversationHandler.END

    try:
        task_id = int(parts[1])
    except ValueError:
        await query.edit_message_text("Invalid task id.")
        return ConversationHandler.END

    task = get_task(task_id)
    if task is None:
        await query.edit_message_text(f"Task #{task_id} not found.")
        return ConversationHandler.END

    context.user_data[_USER_DATA_TASK_ID] = task_id

    body = (
        f"✏️ <b>Editing task</b> <code>#{task_id}</code>\n\n"
        + format_task_card(task)
        + "\n\nWhich field?"
    )
    await query.edit_message_text(
        body,
        parse_mode=ParseMode.HTML,
        reply_markup=build_edit_field_keyboard(task_id),
    )
    return EDIT_PICK_FIELD


@authorized_only
@safe
async def edit_field_picked(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """EDIT_PICK_FIELD: receive 'editf:<field>:N', prompt for the new value."""
    query = update.callback_query
    if query is None or query.data is None:
        return EDIT_PICK_FIELD
    await query.answer()

    parts = query.data.split(":", 2)
    if len(parts) != 3 or parts[0] != CB_EDIT_FIELD:
        return EDIT_PICK_FIELD

    field = parts[1]
    try:
        task_id = int(parts[2])
    except ValueError:
        await query.edit_message_text("Invalid task id.")
        context.user_data.pop(_USER_DATA_TASK_ID, None)
        return ConversationHandler.END

    context.user_data[_USER_DATA_TASK_ID] = task_id
    task = get_task(task_id)
    if task is None:
        await query.edit_message_text(f"Task #{task_id} no longer exists.")
        context.user_data.pop(_USER_DATA_TASK_ID, None)
        return ConversationHandler.END

    header = (
        f"✏️ <b>Editing</b> <code>#{task_id}</code>: "
        f"{module_prefix(task)}{esc(task.title)}\n"
    )

    if field == "title":
        await query.edit_message_text(
            header + "\nSend the new title, or /cancel.",
            parse_mode=ParseMode.HTML,
        )
        return EDIT_TITLE
    if field == "type":
        await query.edit_message_text(
            header + "\nPick the new type:",
            parse_mode=ParseMode.HTML,
            reply_markup=build_edit_type_keyboard(task_id),
        )
        return EDIT_TYPE
    if field == "module":
        await query.edit_message_text(
            header
            + "\nSend the new module code (e.g. CS2040), "
            "'clear' to remove it, or /cancel.",
            parse_mode=ParseMode.HTML,
        )
        return EDIT_MODULE
    if field == "due":
        await query.edit_message_text(
            header + "\nSend the new due date as YYYY-MM-DD, or /cancel.",
            parse_mode=ParseMode.HTML,
        )
        return EDIT_DUE
    if field == "week":
        await query.edit_message_text(
            header
            + "\nSend the new week number (1-13), "
            "'clear' to remove it, or /cancel.",
            parse_mode=ParseMode.HTML,
        )
        return EDIT_WEEK
    if field == "notes":
        await query.edit_message_text(
            header
            + "\nSend the new notes, 'clear' to remove them, or /cancel.",
            parse_mode=ParseMode.HTML,
        )
        return EDIT_NOTES

    await query.edit_message_text("Unknown field. Edit cancelled.")
    context.user_data.pop(_USER_DATA_TASK_ID, None)
    return ConversationHandler.END


@authorized_only
@safe
async def edit_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """EDIT_TITLE: receive new title text, validate, save."""
    message = update.effective_message
    if message is None or message.text is None:
        return EDIT_TITLE
    new_title = message.text.strip()
    if not new_title:
        await message.reply_text(
            "Title can't be empty. Send a non-empty title, or /cancel."
        )
        return EDIT_TITLE

    task_id = _draft_task_id(context)
    task = get_task(task_id) if task_id is not None else None
    if task is None:
        await message.reply_text("Task no longer exists. Edit cancelled.")
        context.user_data.pop(_USER_DATA_TASK_ID, None)
        return ConversationHandler.END

    task.title = new_title
    await message.reply_text(_save_and_summarize(task), parse_mode=ParseMode.HTML)
    context.user_data.pop(_USER_DATA_TASK_ID, None)
    return ConversationHandler.END


@authorized_only
@safe
async def edit_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """EDIT_TYPE: receive type selection from the inline keyboard."""
    query = update.callback_query
    if query is None or query.data is None:
        return EDIT_TYPE
    await query.answer()

    parts = query.data.split(":", 2)
    if len(parts) != 3 or parts[0] != CB_EDIT_TYPE_VALUE:
        await query.edit_message_text("Invalid selection. Edit cancelled.")
        context.user_data.pop(_USER_DATA_TASK_ID, None)
        return ConversationHandler.END

    new_type = parts[1]
    if new_type not in TASK_TYPES:
        await query.edit_message_text("Invalid type. Edit cancelled.")
        context.user_data.pop(_USER_DATA_TASK_ID, None)
        return ConversationHandler.END

    try:
        task_id = int(parts[2])
    except ValueError:
        await query.edit_message_text("Invalid task id. Edit cancelled.")
        context.user_data.pop(_USER_DATA_TASK_ID, None)
        return ConversationHandler.END

    task = get_task(task_id)
    if task is None:
        await query.edit_message_text("Task no longer exists. Edit cancelled.")
        context.user_data.pop(_USER_DATA_TASK_ID, None)
        return ConversationHandler.END

    task.task_type = new_type
    await query.edit_message_text(
        _save_and_summarize(task), parse_mode=ParseMode.HTML
    )
    context.user_data.pop(_USER_DATA_TASK_ID, None)
    return ConversationHandler.END


@authorized_only
@safe
async def edit_module(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """EDIT_MODULE: receive new module code, or 'clear' to unset."""
    message = update.effective_message
    if message is None or message.text is None:
        return EDIT_MODULE
    raw = message.text.strip()

    task_id = _draft_task_id(context)
    task = get_task(task_id) if task_id is not None else None
    if task is None:
        await message.reply_text("Task no longer exists. Edit cancelled.")
        context.user_data.pop(_USER_DATA_TASK_ID, None)
        return ConversationHandler.END

    task.module_code = None if raw.lower() == _CLEAR_KEYWORD else raw
    await message.reply_text(_save_and_summarize(task), parse_mode=ParseMode.HTML)
    context.user_data.pop(_USER_DATA_TASK_ID, None)
    return ConversationHandler.END


@authorized_only
@safe
async def edit_due(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """EDIT_DUE: receive YYYY-MM-DD, validate, save."""
    message = update.effective_message
    if message is None or message.text is None:
        return EDIT_DUE
    raw = message.text.strip()
    try:
        new_due = date.fromisoformat(raw)
    except ValueError:
        await message.reply_text(
            "That didn't parse as YYYY-MM-DD. Try again, or /cancel."
        )
        return EDIT_DUE

    task_id = _draft_task_id(context)
    task = get_task(task_id) if task_id is not None else None
    if task is None:
        await message.reply_text("Task no longer exists. Edit cancelled.")
        context.user_data.pop(_USER_DATA_TASK_ID, None)
        return ConversationHandler.END

    task.due_date = new_due
    await message.reply_text(_save_and_summarize(task), parse_mode=ParseMode.HTML)
    context.user_data.pop(_USER_DATA_TASK_ID, None)
    return ConversationHandler.END


@authorized_only
@safe
async def edit_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """EDIT_WEEK: receive int 1-13, 'clear' to unset, save."""
    message = update.effective_message
    if message is None or message.text is None:
        return EDIT_WEEK
    raw = message.text.strip()

    task_id = _draft_task_id(context)
    task = get_task(task_id) if task_id is not None else None
    if task is None:
        await message.reply_text("Task no longer exists. Edit cancelled.")
        context.user_data.pop(_USER_DATA_TASK_ID, None)
        return ConversationHandler.END

    if raw.lower() == _CLEAR_KEYWORD:
        task.week_number = None
    else:
        try:
            week_num = int(raw)
        except ValueError:
            await message.reply_text(
                "That's not a number. Try 1-13, 'clear', or /cancel."
            )
            return EDIT_WEEK
        if not 1 <= week_num <= 13:
            await message.reply_text(
                "Week must be between 1 and 13. Try again, 'clear', or /cancel."
            )
            return EDIT_WEEK
        task.week_number = week_num

    await message.reply_text(_save_and_summarize(task), parse_mode=ParseMode.HTML)
    context.user_data.pop(_USER_DATA_TASK_ID, None)
    return ConversationHandler.END


@authorized_only
@safe
async def edit_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """EDIT_NOTES: receive new notes, or 'clear' to unset."""
    message = update.effective_message
    if message is None or message.text is None:
        return EDIT_NOTES
    raw = message.text.strip()

    task_id = _draft_task_id(context)
    task = get_task(task_id) if task_id is not None else None
    if task is None:
        await message.reply_text("Task no longer exists. Edit cancelled.")
        context.user_data.pop(_USER_DATA_TASK_ID, None)
        return ConversationHandler.END

    task.notes = None if raw.lower() == _CLEAR_KEYWORD else raw
    await message.reply_text(_save_and_summarize(task), parse_mode=ParseMode.HTML)
    context.user_data.pop(_USER_DATA_TASK_ID, None)
    return ConversationHandler.END


@authorized_only
@safe
async def edit_cancel_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle 'editcancel:N' tap: abort edit, edit message to confirmation."""
    query = update.callback_query
    if query is None:
        return ConversationHandler.END
    await query.answer("Cancelled")
    context.user_data.pop(_USER_DATA_TASK_ID, None)
    try:
        await query.edit_message_text("Edit cancelled. No changes made.")
    except Exception:
        logger.exception("Failed to edit message after edit cancel")
    return ConversationHandler.END


@authorized_only
@safe
async def edit_cancel_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle /cancel during the edit flow: abort, send confirmation reply."""
    message = update.effective_message
    context.user_data.pop(_USER_DATA_TASK_ID, None)
    if message is not None:
        await message.reply_text("Edit cancelled. No changes made.")
    return ConversationHandler.END


def build_edit_conversation() -> ConversationHandler:
    """Construct the ConversationHandler wiring the edit flow end-to-end."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(edit_entry, pattern=r"^edit:\d+$"),
        ],
        states={
            EDIT_PICK_FIELD: [
                CallbackQueryHandler(
                    edit_field_picked, pattern=r"^editf:[a-z]+:\d+$"
                ),
                CallbackQueryHandler(
                    edit_cancel_callback, pattern=r"^editcancel:\d+$"
                ),
            ],
            EDIT_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_title),
            ],
            EDIT_TYPE: [
                CallbackQueryHandler(
                    edit_type, pattern=r"^edittype:[a-z]+:\d+$"
                ),
                CallbackQueryHandler(
                    edit_cancel_callback, pattern=r"^editcancel:\d+$"
                ),
            ],
            EDIT_MODULE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_module),
            ],
            EDIT_DUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_due),
            ],
            EDIT_WEEK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_week),
            ],
            EDIT_NOTES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_notes),
            ],
        },
        fallbacks=[
            CommandHandler(CMD_CANCEL, edit_cancel_command),
        ],
        name="edit_task_conversation",
        persistent=False,
    )
