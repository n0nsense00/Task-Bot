"""Multi-step edit conversation entered from the 📝 button on /today.

Conversation state machine::

    edit:N (callback)
      └─ EDIT_PICK_FIELD: show field-picker keyboard
          ├─ editf:title:N   → EDIT_TITLE       (text)               → save → END
          ├─ editf:type:N    → EDIT_TYPE        (callback)           → save → END
          ├─ editf:module:N  → EDIT_MODULE      (module picker)
          │                  └→ EDIT_MODULE_TEXT (text, "Other…")    → save → END
          ├─ editf:due:N     → EDIT_DUE         (calendar callback)  → save → END
          ├─ editf:time:N    → EDIT_TIME        (time-picker cb)     → save → END
          ├─ editf:notes:N   → EDIT_NOTES       (text)               → save → END
          └─ editcancel:N    → END

Module/Due/Time use the same inline pickers as /add. Title, Type, and Notes
use the same text+keyboard input as before.

Target task id is stashed in ``context.user_data`` so subsequent state
handlers know which row to mutate. Cleared on END / /cancel.
"""
from __future__ import annotations

import logging
from datetime import date as _date

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
from database.db import get_modules, get_task, update_task
from handlers.tasks import (
    is_tracked_deadline_dashboard,
    refresh_deadline_dashboard,
)
from database.models import TASK_TYPES, Task
from utils.auth import authorized_only
from utils.calendar_widget import (
    build_calendar_keyboard,
    calendar_header_text,
    parse_calendar_callback,
    parse_iso_date,
    parse_year_month,
    shortcut_to_date,
)
from utils.errors import safe
from utils.format import (
    CB_EDIT,
    CB_EDIT_FIELD,
    CB_EDIT_TYPE_VALUE,
    CB_MODULE,
    build_edit_field_keyboard,
    build_edit_type_keyboard,
    build_module_keyboard,
    build_notes_keyboard,
    esc,
    format_task_card,
    module_prefix,
    parse_notes_callback,
)
from utils.timepicker import (
    build_hour_keyboard,
    build_minute_keyboard,
    build_time_preset_keyboard,
    parse_time_callback,
)

logger = logging.getLogger(__name__)

# Conversation states — high integers to avoid clash with /add (which uses 0..7).
EDIT_PICK_FIELD = 200
EDIT_TITLE = 201
EDIT_TYPE = 202
EDIT_MODULE = 203
EDIT_MODULE_TEXT = 204
EDIT_DUE = 205
EDIT_TIME = 206
EDIT_NOTES = 208

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


def _updated_summary(task: Task) -> str:
    """Build the post-update HTML success card.

    Persistence deliberately lives in the ``_finish_edit_*`` helpers below, so
    every save path also refreshes the dashboard and no branch can silently
    skip it.
    """
    return "✅ <b>Updated</b>\n\n" + format_task_card(task)


def _load_task(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> Task | None:
    """Helper used by every state handler — fetch the in-progress task or None."""
    task_id = _draft_task_id(context)
    chat = update.effective_chat
    if task_id is None or chat is None:
        return None
    return get_task(task_id, chat.id)


def _abort(context: ContextTypes.DEFAULT_TYPE) -> int:
    """Drop user_data and end the conversation. Used by error paths."""
    context.user_data.pop(_USER_DATA_TASK_ID, None)
    return ConversationHandler.END


async def _finish_edit_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE, task: Task
) -> int:
    """Complete a text-input edit: persist, reply, refresh, end.

    The Updated card stays a separate reply because the user typed into the
    chat; the persistent dashboard is refreshed alongside it.
    """
    update_task(task)
    message = update.effective_message
    if message is not None:
        await message.reply_text(
            _updated_summary(task), parse_mode=ParseMode.HTML
        )
    chat = update.effective_chat
    if chat is not None:
        await refresh_deadline_dashboard(context.application, chat.id)
    return _abort(context)


async def _finish_edit_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, task: Task
) -> int:
    """Complete a button-driven edit: persist, then restore or card, end.

    When the edited message IS the persistent dashboard it is returned to the
    live list rather than left showing an Updated card. Otherwise the Updated
    card is kept (the user is looking at a historical message) and the tracked
    dashboard is refreshed separately.

    Callers have already answered the callback query, so no toast is raised
    here — answering the same query twice is an error.
    """
    update_task(task)
    query = update.callback_query
    chat = update.effective_chat
    chat_id = chat.id if chat is not None else None
    message_id = (
        query.message.message_id
        if query is not None and query.message is not None
        else None
    )

    if (
        chat_id is not None
        and message_id is not None
        and is_tracked_deadline_dashboard(chat_id, message_id)
    ):
        await refresh_deadline_dashboard(context.application, chat_id)
        return _abort(context)

    if query is not None:
        await query.edit_message_text(
            _updated_summary(task), parse_mode=ParseMode.HTML
        )
    if chat_id is not None:
        await refresh_deadline_dashboard(context.application, chat_id)
    return _abort(context)


@authorized_only
@safe
async def edit_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry: parse 'edit:N' callback, store id, show field picker."""
    query = update.callback_query
    chat = update.effective_chat
    if query is None or query.data is None or chat is None:
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

    task = get_task(task_id, chat.id)
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
    """EDIT_PICK_FIELD: receive 'editf:<field>:N', prompt for new value."""
    query = update.callback_query
    chat = update.effective_chat
    if query is None or query.data is None or chat is None:
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
        return _abort(context)

    context.user_data[_USER_DATA_TASK_ID] = task_id
    task = get_task(task_id, chat.id)
    if task is None:
        await query.edit_message_text(f"Task #{task_id} no longer exists.")
        return _abort(context)

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
        modules = get_modules()
        body = header + "\nPick a module:"
        if not modules:
            body += (
                "\n\n<i>No modules seeded. Pick 'Other (type it)…' "
                "or seed via <code>python seed/seed_modules.py</code>.</i>"
            )
        await query.edit_message_text(
            body,
            parse_mode=ParseMode.HTML,
            reply_markup=build_module_keyboard(
                modules, include_skip=False, include_clear=False
            ),
        )
        return EDIT_MODULE

    if field == "due":
        today = _date.today()
        await query.edit_message_text(
            header + "\nPick the new due date:\n\n"
            f"<code>{calendar_header_text()}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=build_calendar_keyboard(today.year, today.month),
        )
        return EDIT_DUE

    if field == "time":
        await query.edit_message_text(
            header + "\nPick the new time:",
            parse_mode=ParseMode.HTML,
            reply_markup=build_time_preset_keyboard(),
        )
        return EDIT_TIME

    if field == "notes":
        await query.edit_message_text(
            header + "\nSend the new notes, or tap Clear to remove them:",
            parse_mode=ParseMode.HTML,
            reply_markup=build_notes_keyboard(include_clear=True),
        )
        return EDIT_NOTES

    await query.edit_message_text("Unknown field. Edit cancelled.")
    return _abort(context)


@authorized_only
@safe
async def edit_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """EDIT_TITLE: receive new title, validate, save."""
    message = update.effective_message
    if message is None or message.text is None:
        return EDIT_TITLE
    new_title = message.text.strip()
    if not new_title:
        await message.reply_text(
            "Title can't be empty. Try again, or /cancel."
        )
        return EDIT_TITLE

    task = _load_task(update, context)
    if task is None:
        await message.reply_text("Task no longer exists. Edit cancelled.")
        return _abort(context)
    task.title = new_title
    return await _finish_edit_text(update, context, task)


@authorized_only
@safe
async def edit_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """EDIT_TYPE: receive type selection from inline keyboard."""
    query = update.callback_query
    chat = update.effective_chat
    if query is None or query.data is None or chat is None:
        return EDIT_TYPE
    await query.answer()

    parts = query.data.split(":", 2)
    if len(parts) != 3 or parts[0] != CB_EDIT_TYPE_VALUE:
        await query.edit_message_text("Invalid selection. Edit cancelled.")
        return _abort(context)
    new_type = parts[1]
    if new_type not in TASK_TYPES:
        await query.edit_message_text("Invalid type. Edit cancelled.")
        return _abort(context)
    try:
        task_id = int(parts[2])
    except ValueError:
        await query.edit_message_text("Invalid task id. Edit cancelled.")
        return _abort(context)

    task = get_task(task_id, chat.id)
    if task is None:
        await query.edit_message_text("Task no longer exists. Edit cancelled.")
        return _abort(context)
    task.task_type = new_type
    return await _finish_edit_callback(update, context, task)


@authorized_only
@safe
async def edit_module(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """EDIT_MODULE: receive module-picker callback (select / other / clear / cancel)."""
    query = update.callback_query
    if query is None or query.data is None:
        return EDIT_MODULE
    await query.answer()

    if not query.data.startswith(f"{CB_MODULE}:"):
        return EDIT_MODULE
    rest = query.data[len(CB_MODULE) + 1 :]

    if rest == "cancel":
        await query.edit_message_text("Edit cancelled. No changes made.")
        return _abort(context)

    if rest == "other":
        await query.edit_message_text(
            "Send the module code as text (e.g. <code>MH9999</code>), "
            "or /cancel.",
            parse_mode=ParseMode.HTML,
        )
        return EDIT_MODULE_TEXT

    task = _load_task(update, context)
    if task is None:
        await query.edit_message_text("Task no longer exists. Edit cancelled.")
        return _abort(context)

    if rest == "clear":
        task.module_code = None
    elif rest.startswith("select:"):
        task.module_code = rest[len("select:") :]
    else:
        return EDIT_MODULE

    return await _finish_edit_callback(update, context, task)


@authorized_only
@safe
async def edit_module_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """EDIT_MODULE_TEXT: capture custom module code from the 'Other…' branch."""
    message = update.effective_message
    if message is None or message.text is None:
        return EDIT_MODULE_TEXT
    code = message.text.strip()
    if not code:
        await message.reply_text(
            "Module code can't be empty. Try again, or /cancel."
        )
        return EDIT_MODULE_TEXT

    task = _load_task(update, context)
    if task is None:
        await message.reply_text("Task no longer exists. Edit cancelled.")
        return _abort(context)
    task.module_code = code
    return await _finish_edit_text(update, context, task)


@authorized_only
@safe
async def edit_due(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """EDIT_DUE: handle calendar nav / select / shortcut / cancel."""
    query = update.callback_query
    if query is None or query.data is None:
        return EDIT_DUE

    action, payload = parse_calendar_callback(query.data)

    if action == "noop":
        await query.answer()
        return EDIT_DUE

    if action == "cancel":
        await query.answer("Cancelled")
        await query.edit_message_text("Edit cancelled. No changes made.")
        return _abort(context)

    if action == "nav" and payload is not None:
        ym = parse_year_month(payload)
        await query.answer()
        if ym is not None:
            await query.edit_message_reply_markup(
                reply_markup=build_calendar_keyboard(ym[0], ym[1])
            )
        return EDIT_DUE

    selected: _date | None = None
    if action == "select" and payload is not None:
        selected = parse_iso_date(payload)
    elif action == "short" and payload is not None:
        selected = shortcut_to_date(payload)

    if selected is None:
        await query.answer()
        return EDIT_DUE

    task = _load_task(update, context)
    if task is None:
        await query.edit_message_text("Task no longer exists. Edit cancelled.")
        return _abort(context)
    task.due_date = selected
    await query.answer()
    return await _finish_edit_callback(update, context, task)


@authorized_only
@safe
async def edit_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """EDIT_TIME: handle time-picker callbacks (presets + custom hour/minute)."""
    query = update.callback_query
    if query is None or query.data is None:
        return EDIT_TIME

    action, payload = parse_time_callback(query.data)

    if action == "cancel":
        await query.answer("Cancelled")
        await query.edit_message_text("Edit cancelled. No changes made.")
        return _abort(context)

    if action == "skip":
        task = _load_task(update, context)
        if task is None:
            await query.edit_message_text(
                "Task no longer exists. Edit cancelled."
            )
            return _abort(context)
        task.due_time = None
        await query.answer()
        return await _finish_edit_callback(update, context, task)

    if action == "custom":
        await query.answer()
        await query.edit_message_reply_markup(
            reply_markup=build_hour_keyboard()
        )
        return EDIT_TIME

    if action == "hour" and payload is not None:
        try:
            hour = int(payload)
        except ValueError:
            await query.answer()
            return EDIT_TIME
        await query.answer()
        await query.edit_message_reply_markup(
            reply_markup=build_minute_keyboard(hour)
        )
        return EDIT_TIME

    if action == "set" and payload is not None:
        try:
            hh, mm = payload.split(":", 1)
            hour = int(hh)
            minute = int(mm)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except ValueError:
            await query.answer("Invalid time", show_alert=True)
            return EDIT_TIME
        time_str = f"{hour:02d}:{minute:02d}"
        task = _load_task(update, context)
        if task is None:
            await query.edit_message_text(
                "Task no longer exists. Edit cancelled."
            )
            return _abort(context)
        task.due_time = time_str
        await query.answer()
        return await _finish_edit_callback(update, context, task)

    await query.answer()
    return EDIT_TIME


@authorized_only
@safe
async def edit_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """EDIT_NOTES (text path): user typed new notes, save."""
    message = update.effective_message
    if message is None or message.text is None:
        return EDIT_NOTES
    raw = message.text.strip()

    task = _load_task(update, context)
    if task is None:
        await message.reply_text("Task no longer exists. Edit cancelled.")
        return _abort(context)

    task.notes = (
        None if raw.lower() == _CLEAR_KEYWORD or not raw else raw
    )
    return await _finish_edit_text(update, context, task)


@authorized_only
@safe
async def edit_notes_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """EDIT_NOTES (button path): Clear or Cancel via inline keyboard."""
    query = update.callback_query
    if query is None or query.data is None:
        return EDIT_NOTES
    await query.answer()

    action = parse_notes_callback(query.data)
    if action == "cancel":
        await query.edit_message_text("Edit cancelled. No changes made.")
        return _abort(context)
    if action in ("clear", "skip"):
        task = _load_task(update, context)
        if task is None:
            await query.edit_message_text(
                "Task no longer exists. Edit cancelled."
            )
            return _abort(context)
        task.notes = None
        return await _finish_edit_callback(update, context, task)
    return EDIT_NOTES


@authorized_only
@safe
async def edit_cancel_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle 'editcancel:N' tap: abort, edit message to confirmation."""
    query = update.callback_query
    if query is None:
        return ConversationHandler.END
    await query.answer("Cancelled")
    try:
        await query.edit_message_text("Edit cancelled. No changes made.")
    except Exception:
        logger.exception("Failed to edit message after edit cancel")
    return _abort(context)


@authorized_only
@safe
async def edit_cancel_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle /cancel during the edit flow: abort, send confirmation reply."""
    message = update.effective_message
    if message is not None:
        await message.reply_text("Edit cancelled. No changes made.")
    return _abort(context)


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
                CallbackQueryHandler(edit_module, pattern=r"^mod:"),
            ],
            EDIT_MODULE_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_module_text),
            ],
            EDIT_DUE: [
                CallbackQueryHandler(edit_due, pattern=r"^cal:"),
            ],
            EDIT_TIME: [
                CallbackQueryHandler(edit_time, pattern=r"^time:"),
                CallbackQueryHandler(edit_time, pattern=r"^timehr:"),
            ],
            EDIT_NOTES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_notes),
                CallbackQueryHandler(edit_notes_callback, pattern=r"^notes:"),
            ],
        },
        fallbacks=[
            CommandHandler(CMD_CANCEL, edit_cancel_command),
        ],
        name="edit_task_conversation",
        persistent=False,
    )
