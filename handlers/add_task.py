"""Multi-step /add conversation: create a new task using inline pickers.

Step order (each step transitions on user input)::

    TYPE        Assessment picker (Quiz / Lab / Assignment / ... / Final)
    MODULE      Module picker (seeded NTU modules + Other / Skip)
    MODULE_TEXT Sub-state when user picks "Other (type it)…"
    TITLE       Free-text input
    DATE        Inline calendar (◀ prev / next ▶ / shortcuts / day grid)
    TIME        Hybrid time picker (presets + Custom hour-then-minute)
    NOTES       Free-text or 'skip', then save and END

The DATE state stays put across month-navigation taps — only ``select`` /
``short`` callbacks advance to TIME. The TIME state similarly stays put
when the user picks "Custom…" (re-renders to the hour picker) or taps an
hour (re-renders to the minute picker for that hour) — only ``set`` / ``skip``
advance to NOTES.

The in-progress draft lives in ``context.user_data['new_task_draft']`` and
is cleared on END / /cancel so future conversations start fresh.
"""
from __future__ import annotations

import logging
from datetime import date as _date
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
from database.db import add_task, get_modules
from handlers.tasks import refresh_deadline_dashboard
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
    CB_MODULE,
    TYPE_EMOJI,
    build_module_keyboard,
    build_notes_keyboard,
    format_task_card,
    parse_notes_callback,
)
from utils.timepicker import (
    build_hour_keyboard,
    build_minute_keyboard,
    build_time_preset_keyboard,
    parse_time_callback,
)

logger = logging.getLogger(__name__)

# Conversation states.
TYPE, MODULE, MODULE_TEXT, TITLE, DATE, TIME, NOTES = range(7)

_SKIP_KEYWORD: str = "skip"
_ADD_TYPE_CB_PREFIX: str = "addtype"
_USER_DATA_KEY: str = "new_task_draft"


def _draft(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    """Return (creating if needed) the in-progress draft dict for this user."""
    draft = context.user_data.get(_USER_DATA_KEY)
    if draft is None:
        draft = {}
        context.user_data[_USER_DATA_KEY] = draft
    return draft


def _type_keyboard() -> InlineKeyboardMarkup:
    """Build the assessed-deadline type picker, two buttons per row."""
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for t in TASK_TYPES:
        emoji = TYPE_EMOJI.get(t, "")
        label = f"{emoji} {t.capitalize()}".strip()
        row.append(
            InlineKeyboardButton(
                label, callback_data=f"{_ADD_TYPE_CB_PREFIX}:{t}"
            )
        )
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


@authorized_only
@safe
async def add_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Begin /add: clear any stale draft and prompt for task type."""
    message = update.effective_message
    if message is None:
        return ConversationHandler.END
    context.user_data[_USER_DATA_KEY] = {}
    await message.reply_text(
        "📝 <b>New deadline</b>\n\nWhat kind of assessment is it?",
        parse_mode=ParseMode.HTML,
        reply_markup=_type_keyboard(),
    )
    return TYPE


@authorized_only
@safe
async def add_task_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """TYPE state: receive task-type pick, render module picker."""
    query = update.callback_query
    if query is None or query.data is None:
        return TYPE
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

    modules = get_modules()
    body = (
        "📝 <b>New deadline</b>\n\n"
        f"Type: {chosen.capitalize()}\n\n"
        "Pick a module:"
    )
    if not modules:
        body += (
            "\n\n<i>No modules seeded yet. Run "
            "<code>python seed/seed_modules.py</code> "
            "or pick 'Other (type it)…'.</i>"
        )
    await query.edit_message_text(
        body,
        parse_mode=ParseMode.HTML,
        reply_markup=build_module_keyboard(
            modules, include_skip=False
        ),
    )
    return MODULE


@authorized_only
@safe
async def add_module_picked(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """MODULE state: receive module-picker callback, route to next step."""
    query = update.callback_query
    if query is None or query.data is None:
        return MODULE
    await query.answer()

    if not query.data.startswith(f"{CB_MODULE}:"):
        return MODULE

    rest = query.data[len(CB_MODULE) + 1 :]

    if rest == "cancel":
        await query.edit_message_text("Cancelled. Nothing was added.")
        context.user_data.pop(_USER_DATA_KEY, None)
        return ConversationHandler.END

    if rest == "skip":
        return MODULE

    if rest == "other":
        await query.edit_message_text(
            "Send the module code as text (e.g. <code>MH9999</code>), "
            "or /cancel to abort.",
            parse_mode=ParseMode.HTML,
        )
        return MODULE_TEXT

    if rest.startswith("select:"):
        code = rest[len("select:") :]
        _draft(context)["module_code"] = code
        await query.edit_message_text(
            f"Module: <code>{code}</code>\n\n"
            "What's the title? (send the text, or /cancel)",
            parse_mode=ParseMode.HTML,
        )
        return TITLE

    return MODULE


@authorized_only
@safe
async def add_module_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """MODULE_TEXT state: capture a one-off custom module code."""
    message = update.effective_message
    if message is None or message.text is None:
        return MODULE_TEXT
    code = message.text.strip()
    if not code:
        await message.reply_text(
            "Module code can't be empty. Try again, or /cancel."
        )
        return MODULE_TEXT
    _draft(context)["module_code"] = code
    await message.reply_text(
        f"Module: <code>{code}</code>\n\n"
        "What's the title? (send the text, or /cancel)",
        parse_mode=ParseMode.HTML,
    )
    return TITLE


@authorized_only
@safe
async def add_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """TITLE state: capture title, then render the calendar picker."""
    message = update.effective_message
    if message is None or message.text is None:
        return TITLE
    title = message.text.strip()
    if not title:
        await message.reply_text(
            "Title can't be empty. Try again, or /cancel."
        )
        return TITLE
    _draft(context)["title"] = title

    today = _date.today()
    await message.reply_text(
        "📅 <b>When is it due?</b>\n\n"
        f"<code>{calendar_header_text()}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=build_calendar_keyboard(today.year, today.month),
    )
    return DATE


@authorized_only
@safe
async def add_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """DATE state: handle calendar nav / select / shortcut / cancel callbacks."""
    query = update.callback_query
    if query is None or query.data is None:
        return DATE

    action, payload = parse_calendar_callback(query.data)

    if action == "noop":
        await query.answer()
        return DATE

    if action == "cancel":
        await query.answer("Cancelled")
        await query.edit_message_text("Cancelled. Nothing was added.")
        context.user_data.pop(_USER_DATA_KEY, None)
        return ConversationHandler.END

    if action == "nav" and payload is not None:
        ym = parse_year_month(payload)
        await query.answer()
        if ym is not None:
            await query.edit_message_reply_markup(
                reply_markup=build_calendar_keyboard(ym[0], ym[1])
            )
        return DATE

    selected: _date | None = None
    if action == "select" and payload is not None:
        selected = parse_iso_date(payload)
    elif action == "short" and payload is not None:
        selected = shortcut_to_date(payload)

    if selected is None:
        await query.answer()
        return DATE

    _draft(context)["due_date"] = selected
    await query.answer()
    await query.edit_message_text(
        f"Due: <b>{selected.isoformat()}</b>\n\n"
        "🕐 <b>What time? (optional)</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=build_time_preset_keyboard(),
    )
    return TIME


@authorized_only
@safe
async def add_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """TIME state: handle time-picker callbacks (presets + custom hour/minute)."""
    query = update.callback_query
    if query is None or query.data is None:
        return TIME

    action, payload = parse_time_callback(query.data)

    if action == "cancel":
        await query.answer("Cancelled")
        await query.edit_message_text("Cancelled. Nothing was added.")
        context.user_data.pop(_USER_DATA_KEY, None)
        return ConversationHandler.END

    if action == "skip":
        _draft(context)["due_time"] = None
        await query.answer()
        await query.edit_message_text(
            "Time: <i>all day</i>\n\n"
            "📋 <b>Notes?</b> Tap Skip, or send any text:",
            parse_mode=ParseMode.HTML,
            reply_markup=build_notes_keyboard(),
        )
        return NOTES

    if action == "custom":
        await query.answer()
        await query.edit_message_reply_markup(
            reply_markup=build_hour_keyboard()
        )
        return TIME

    if action == "hour" and payload is not None:
        try:
            hour = int(payload)
        except ValueError:
            await query.answer()
            return TIME
        await query.answer()
        await query.edit_message_reply_markup(
            reply_markup=build_minute_keyboard(hour)
        )
        return TIME

    if action == "set" and payload is not None:
        try:
            hh, mm = payload.split(":", 1)
            hour = int(hh)
            minute = int(mm)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except ValueError:
            await query.answer("Invalid time", show_alert=True)
            return TIME
        time_str = f"{hour:02d}:{minute:02d}"
        _draft(context)["due_time"] = time_str
        await query.answer()
        await query.edit_message_text(
            f"Time: <b>{time_str}</b>\n\n"
            "📋 <b>Notes?</b> Tap Skip, or send any text:",
            parse_mode=ParseMode.HTML,
            reply_markup=build_notes_keyboard(),
        )
        return NOTES

    await query.answer()
    return TIME


async def _finalize_add(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    via_query=None,
) -> int:
    """Persist the in-progress draft as a Task, send the success card, end.

    Shared between :func:`add_notes` (text input path) and
    :func:`add_notes_callback` (Skip-button path) so the save + reply logic
    stays in one place.
    """
    draft = _draft(context)
    required = ("title", "task_type", "module_code", "due_date")
    if any(k not in draft for k in required):
        target = update.effective_message
        if target is not None:
            await target.reply_text(
                "Draft is missing required fields — aborting. Please /add again."
            )
        context.user_data.pop(_USER_DATA_KEY, None)
        return ConversationHandler.END

    try:
        chat = update.effective_chat
        if chat is None:
            raise RuntimeError("Cannot determine which Telegram chat owns this task")
        task = Task(
            title=draft["title"],
            task_type=draft["task_type"],
            due_date=draft["due_date"],
            chat_id=chat.id,
            module_code=draft.get("module_code"),
            notes=draft.get("notes"),
            due_time=draft.get("due_time"),
        )
        new_id = add_task(task)
    except Exception:
        logger.exception("Failed to save new task")
        target = update.effective_message
        if target is not None:
            await target.reply_text(
                "Couldn't save that task — check the logs. Nothing was stored."
            )
        context.user_data.pop(_USER_DATA_KEY, None)
        return ConversationHandler.END

    context.user_data.pop(_USER_DATA_KEY, None)
    task.id = new_id
    summary = "✅ <b>Added</b>\n\n" + format_task_card(task)
    if via_query is not None:
        await via_query.edit_message_text(summary, parse_mode=ParseMode.HTML)
    else:
        target = update.effective_message
        if target is not None:
            await target.reply_text(summary, parse_mode=ParseMode.HTML)
    # The row is already committed. refresh_deadline_dashboard logs its own
    # failures and never raises, so a dashboard problem cannot make a
    # successful add look like it failed.
    await refresh_deadline_dashboard(context.application, chat.id)
    return ConversationHandler.END


@authorized_only
@safe
async def add_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """NOTES state (text path): user typed notes, persist and end.

    Magic word ``skip`` (case-insensitive) still works as a fallback for
    typing-fast users — same behaviour as the Skip button.
    """
    message = update.effective_message
    if message is None or message.text is None:
        return NOTES
    raw = message.text.strip()
    _draft(context)["notes"] = (
        None if raw.lower() == _SKIP_KEYWORD or not raw else raw
    )
    return await _finalize_add(update, context)


@authorized_only
@safe
async def add_notes_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """NOTES state (button path): Skip or Cancel via inline keyboard."""
    query = update.callback_query
    if query is None or query.data is None:
        return NOTES
    await query.answer()

    action = parse_notes_callback(query.data)
    if action == "cancel":
        await query.edit_message_text("Cancelled. Nothing was added.")
        context.user_data.pop(_USER_DATA_KEY, None)
        return ConversationHandler.END
    if action in ("skip", "clear"):
        _draft(context)["notes"] = None
        return await _finalize_add(update, context, via_query=query)
    return NOTES


@authorized_only
@safe
async def add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Abort /add: drop the in-progress draft, send confirmation reply."""
    message = update.effective_message
    context.user_data.pop(_USER_DATA_KEY, None)
    if message is not None:
        await message.reply_text("Cancelled. Nothing was added.")
    return ConversationHandler.END


def build_add_conversation() -> ConversationHandler:
    """Construct the deadline-focused /add conversation."""
    return ConversationHandler(
        entry_points=[CommandHandler(CMD_ADD, add_entry)],
        states={
            TYPE: [
                CallbackQueryHandler(
                    add_task_type, pattern=f"^{_ADD_TYPE_CB_PREFIX}:"
                )
            ],
            MODULE: [
                CallbackQueryHandler(add_module_picked, pattern=r"^mod:"),
            ],
            MODULE_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_module_text),
            ],
            TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_title),
            ],
            DATE: [
                CallbackQueryHandler(add_date, pattern=r"^cal:"),
            ],
            TIME: [
                CallbackQueryHandler(add_time, pattern=r"^time:"),
                CallbackQueryHandler(add_time, pattern=r"^timehr:"),
            ],
            NOTES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_notes),
                CallbackQueryHandler(add_notes_callback, pattern=r"^notes:"),
            ],
        },
        fallbacks=[CommandHandler(CMD_CANCEL, add_cancel)],
        name="add_task_conversation",
        persistent=False,
    )
