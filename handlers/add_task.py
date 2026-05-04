"""Multi-step /add conversation: create a new task using inline pickers.

Step order (each step transitions on user input)::

    TYPE        Type picker (📚 Lecture / 📝 Tutorial / ... / ✏️ Personal)
    MODULE      Module picker (seeded NTU modules + Other / Skip)
    MODULE_TEXT Sub-state when user picks "Other (type it)…"
    TITLE       Free-text input
    DATE        Inline calendar (◀ prev / next ▶ / shortcuts / day grid)
    TIME        Hybrid time picker (presets + Custom hour-then-minute)
    WEEK        Free-text 1-13 or 'skip'
    NOTES       Free-text or 'skip', then save and END

The DATE state stays put across month-navigation taps — only ``select`` /
``short`` callbacks advance to TIME. The TIME state similarly stays put
when the user picks "Custom…" (re-renders to the hour picker) or taps an
hour (re-renders to the minute picker for that hour) — only ``set`` / ``skip``
advance to WEEK.

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
from database.models import TASK_TYPE_PERSONAL, TASK_TYPES, Task
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
    format_task_card,
)
from utils.timepicker import (
    build_hour_keyboard,
    build_minute_keyboard,
    build_time_preset_keyboard,
    parse_time_callback,
)

logger = logging.getLogger(__name__)

# Conversation states.
TYPE, MODULE, MODULE_TEXT, TITLE, DATE, TIME, WEEK, NOTES = range(8)

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
    """Build the 6-button task-type picker (2 per row, with type emojis)."""
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
        "📝 <b>New task</b>\n\nWhat type of task?",
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
        "📝 <b>New task</b>\n\n"
        f"Type: {chosen}\n\n"
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
            modules, include_skip=(chosen == TASK_TYPE_PERSONAL)
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
        _draft(context)["module_code"] = None
        await query.edit_message_text(
            "Module: <i>skipped</i>\n\n"
            "What's the title? (send the text, or /cancel)",
            parse_mode=ParseMode.HTML,
        )
        return TITLE

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
            "Week number? Send 1-13, 'skip', or /cancel.",
            parse_mode=ParseMode.HTML,
        )
        return WEEK

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
            "Week number? Send 1-13, 'skip', or /cancel.",
            parse_mode=ParseMode.HTML,
        )
        return WEEK

    await query.answer()
    return TIME


@authorized_only
@safe
async def add_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """WEEK state: validate week number 1-13 or 'skip'."""
    message = update.effective_message
    if message is None or message.text is None:
        return WEEK
    raw = message.text.strip()
    if raw.lower() == _SKIP_KEYWORD:
        _draft(context)["week_number"] = None
    else:
        try:
            week_num = int(raw)
        except ValueError:
            await message.reply_text(
                "That's not a number. Try 1-13, 'skip', or /cancel."
            )
            return WEEK
        if not 1 <= week_num <= 13:
            await message.reply_text(
                "Week must be between 1 and 13. Try again, 'skip', or /cancel."
            )
            return WEEK
        _draft(context)["week_number"] = week_num
    await message.reply_text(
        "Notes? (any text, 'skip', or /cancel)"
    )
    return NOTES


@authorized_only
@safe
async def add_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """NOTES state: capture optional notes, persist task, end."""
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
            due_time=draft.get("due_time"),
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
    task.id = new_id
    summary = "✅ <b>Added</b>\n\n" + format_task_card(task)
    await message.reply_text(summary, parse_mode=ParseMode.HTML)
    return ConversationHandler.END


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
    """Construct the ConversationHandler wiring all 8 /add states."""
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
            WEEK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_week),
            ],
            NOTES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_notes),
            ],
        },
        fallbacks=[CommandHandler(CMD_CANCEL, add_cancel)],
        name="add_task_conversation",
        persistent=False,
    )
