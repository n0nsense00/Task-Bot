"""Deadline command handlers: /deadlines, /done, and /delete.

The bot is intentionally assessment-focused. Timetable views such as /today
and /week are not registered; the primary view is one chronological list of
pending semester deadlines.
"""
from __future__ import annotations

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from database.db import get_semester_deadlines, get_task, mark_complete
from database.models import Task
from utils.auth import authorized_only
from utils.clock import today_local
from utils.errors import safe
from utils.format import (
    DEADLINE_PICKER_PAGE_SIZE,
    DIVIDER,
    STATUS_DUE_TODAY,
    STATUS_FUTURE,
    STATUS_THIS_WEEK,
    TYPE_EMOJI,
    build_deadline_dashboard_keyboard,
    build_deadline_picker_keyboard,
    build_delete_confirmation_keyboard,
    days_away_label,
    esc,
    format_task_card,
    module_prefix,
)

_DONE_USAGE_MESSAGE: str = "Usage: /done &lt;deadline_id&gt;  e.g. <code>/done 4</code>"
_DELETE_USAGE_MESSAGE: str = (
    "Usage: /delete &lt;deadline_id&gt;  e.g. <code>/delete 4</code>"
)


def _deadline_line(task: Task, status_emoji: str) -> list[str]:
    """Render one compact two-line deadline entry for Telegram."""
    type_emoji = TYPE_EMOJI.get(task.task_type, "📌")
    date_label = task.due_date.strftime("%a %d %b %Y")
    time_clause = f" at {esc(task.due_time)}" if task.due_time else ""
    relative = days_away_label(task.due_date)
    return [
        f"{status_emoji} {module_prefix(task)}<b>{esc(task.title)}</b>",
        f"   {type_emoji} {esc(task.task_type.capitalize())} · "
        f"{date_label}{time_clause} · {relative} · <code>#{task.id}</code>",
    ]


def get_upcoming_deadlines(chat_id: int) -> list[Task]:
    """Return pending assessed deadlines whose due date has not passed."""
    today = today_local()
    return [
        task for task in get_semester_deadlines(chat_id) if task.due_date >= today
    ]


def render_deadlines(chat_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build the chronological deadline dashboard and its compact action."""
    today = today_local()
    upcoming = get_upcoming_deadlines(chat_id)

    if not upcoming:
        return (
            "🎉 <b>No upcoming deadlines</b>\n\n"
            "<i>Add the next quiz, lab, assignment, project, midterm, or final "
            "with /add.</i>",
            None,
        )

    due_today: list[Task] = []
    next_seven_days: list[Task] = []
    later: list[Task] = []
    for task in upcoming:
        days = (task.due_date - today).days
        if days == 0:
            due_today.append(task)
        elif days <= 7:
            next_seven_days.append(task)
        else:
            later.append(task)

    def add_section(
        lines: list[str], header: str, items: list[Task], status: str
    ) -> None:
        if not items:
            return
        if len(lines) > 4:
            lines.append("")
        lines.append(f"<b>{header}</b>")
        for task in items:
            lines.extend(_deadline_line(task, status))

    lines: list[str] = ["📅 <b>Upcoming Deadlines</b>", "", DIVIDER, ""]
    add_section(lines, "⚠️ Due Today", due_today, STATUS_DUE_TODAY)
    add_section(lines, "🔥 Next 7 Days", next_seven_days, STATUS_THIS_WEEK)
    add_section(lines, "🗓️ Later", later, STATUS_FUTURE)
    lines.extend(["", DIVIDER, ""])
    lines.append(
        f"<i>{len(upcoming)} pending · sorted by due date</i>"
    )
    lines.append("<i>Tap Manage deadlines to complete, edit, or delete.</i>")

    return "\n".join(lines), build_deadline_dashboard_keyboard()


def render_deadline_picker(
    chat_id: int, page: int
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build a paginated picker used by the compact deadline manager."""
    upcoming = get_upcoming_deadlines(chat_id)
    if not upcoming:
        return render_deadlines(chat_id)

    total_pages = max(
        1,
        (len(upcoming) + DEADLINE_PICKER_PAGE_SIZE - 1)
        // DEADLINE_PICKER_PAGE_SIZE,
    )
    page = min(max(page, 0), total_pages - 1)
    start = page * DEADLINE_PICKER_PAGE_SIZE
    page_tasks = upcoming[start : start + DEADLINE_PICKER_PAGE_SIZE]

    text = (
        "⚙️ <b>Manage Deadlines</b>\n\n"
        "Choose a deadline to complete, edit, or delete.\n\n"
        f"<i>Page {page + 1} of {total_pages} · "
        f"{len(upcoming)} pending</i>"
    )
    keyboard = build_deadline_picker_keyboard(page_tasks, page, total_pages)
    return text, keyboard


@authorized_only
@safe
async def deadlines(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show every pending assessed deadline, grouped by urgency."""
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    text, keyboard = render_deadlines(chat.id)
    await message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=keyboard
    )


def _parse_task_id_arg(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Return the first command argument parsed as an integer, else None."""
    args = context.args or []
    if not args:
        return None
    try:
        return int(args[0])
    except ValueError:
        return None


@authorized_only
@safe
async def done_task_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /done <id>: mark a deadline complete and show its card."""
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    task_id = _parse_task_id_arg(context)
    if task_id is None:
        await message.reply_text(_DONE_USAGE_MESSAGE, parse_mode=ParseMode.HTML)
        return
    task = get_task(task_id, chat.id)
    if task is None:
        await message.reply_text(f"No deadline with ID {task_id}.")
        return
    mark_complete(task_id, chat.id)
    task.completed = True
    await message.reply_text(
        "✅ <b>Completed</b>\n\n" + format_task_card(task),
        parse_mode=ParseMode.HTML,
    )


@authorized_only
@safe
async def delete_task_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /delete <id>: show a Yes/No confirmation card."""
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    task_id = _parse_task_id_arg(context)
    if task_id is None:
        await message.reply_text(_DELETE_USAGE_MESSAGE, parse_mode=ParseMode.HTML)
        return
    task = get_task(task_id, chat.id)
    if task is None:
        await message.reply_text(f"No deadline with ID {task_id}.")
        return
    await message.reply_text(
        "🗑️ <b>Delete this deadline?</b>\n\n" + format_task_card(task),
        parse_mode=ParseMode.HTML,
        reply_markup=build_delete_confirmation_keyboard(task_id),
    )
