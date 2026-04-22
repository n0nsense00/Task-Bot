"""Query and mutation commands: /today, /week, /semester, /done, /delete.

All Telegram messages sent from this module use HTML parse mode. Every
user-supplied string (title, module_code, notes, task_type) is run through
:func:`utils.format.esc` before interpolation into a template — the single
chokepoint preventing injection of ``<b>``, ``<a>``, etc.
"""
from __future__ import annotations

import logging
from datetime import date

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import get_current_week
from database.db import (
    delete_task,
    get_semester_deadlines,
    get_task,
    get_tasks_for_date,
    get_tasks_for_week,
    mark_complete,
)
from database.models import TASK_TYPE_LECTURE, TASK_TYPE_TUTORIAL, Task
from utils.auth import authorized_only
from utils.errors import safe
from utils.format import (
    days_away_label,
    esc,
    format_grouped_today,
    format_task_line,
    module_prefix,
)

logger = logging.getLogger(__name__)

_NO_TASKS_TODAY_MESSAGE: str = "🎉 Nothing scheduled for today. Enjoy!"
_DONE_USAGE_MESSAGE: str = "Usage: /done &lt;task_id&gt;  e.g. <code>/done 4</code>"
_DELETE_USAGE_MESSAGE: str = (
    "Usage: /delete &lt;task_id&gt;  e.g. <code>/delete 4</code>"
)

DELETE_CB_PREFIX: str = "del"


@authorized_only
@safe
async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /today: list tasks due today, grouped by task type."""
    message = update.effective_message
    if message is None:
        return

    target = date.today()
    tasks = get_tasks_for_date(target)
    if not tasks:
        await message.reply_text(_NO_TASKS_TODAY_MESSAGE)
        return

    lines = format_grouped_today(tasks, target)
    await message.reply_text(
        "\n".join(lines).rstrip(), parse_mode=ParseMode.HTML
    )


@authorized_only
@safe
async def week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /week: lectures this academic week + tutorials next academic week."""
    message = update.effective_message
    if message is None:
        return

    current = get_current_week()
    if current == 0:
        await message.reply_text(
            "Semester hasn't started yet. "
            "Update <code>SEMESTER_START_DATE</code> in config.py if that's wrong.",
            parse_mode=ParseMode.HTML,
        )
        return

    this_week_lectures = get_tasks_for_week(current, [TASK_TYPE_LECTURE])
    next_week_tutorials = get_tasks_for_week(current + 1, [TASK_TYPE_TUTORIAL])

    def render(header: str, items: list[Task]) -> list[str]:
        """Render a named section of the /week output as HTML lines."""
        rendered = [f"<b>{header}</b>"]
        if not items:
            rendered.append("(none)")
            return rendered
        for t in items:
            weekday = t.due_date.strftime("%a")
            rendered.append(
                f"• {weekday} {t.due_date.isoformat()} — "
                f"{module_prefix(t)}{esc(t.title)} <code>(id {t.id})</code>"
            )
        return rendered

    out: list[str] = [f"<b>Week {current}</b>", ""]
    out.extend(render("📚 This Week's Lectures", this_week_lectures))
    out.append("")
    out.extend(render("📝 Next Week's Tutorials", next_week_tutorials))

    await message.reply_text("\n".join(out), parse_mode=ParseMode.HTML)


@authorized_only
@safe
async def semester(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /semester: all midterms and finals, chronological, with ``days away``."""
    message = update.effective_message
    if message is None:
        return

    deadlines = get_semester_deadlines()
    if not deadlines:
        await message.reply_text("No midterms or finals recorded yet.")
        return

    today_date = date.today()
    lines: list[str] = ["<b>Semester Deadlines</b>", ""]
    for t in deadlines:
        is_past = t.due_date < today_date
        marker = "✅ " if is_past else ""
        type_label = t.task_type.capitalize()
        date_label = t.due_date.strftime("%a %d %b")  # e.g. "Mon 15 Dec"
        relative = days_away_label(t.due_date)
        lines.append(
            f"• {marker}{module_prefix(t)}{type_label} — "
            f"{date_label} ({relative})  <code>(id {t.id})</code>"
        )

    await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


def _parse_task_id_arg(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Return the first ``context.args`` entry parsed as int, or ``None`` if invalid."""
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
    """Handle ``/done <task_id>``: mark a task as completed."""
    message = update.effective_message
    if message is None:
        return

    task_id = _parse_task_id_arg(context)
    if task_id is None:
        await message.reply_text(_DONE_USAGE_MESSAGE, parse_mode=ParseMode.HTML)
        return

    task = get_task(task_id)
    if task is None:
        await message.reply_text(f"No task with ID {task_id}.")
        return

    mark_complete(task_id)
    await message.reply_text(
        f"Marked done: {module_prefix(task)}{esc(task.title)} "
        f"<code>(id {task_id})</code>",
        parse_mode=ParseMode.HTML,
    )


@authorized_only
@safe
async def delete_task_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/delete <task_id>``: show confirmation keyboard before deleting."""
    message = update.effective_message
    if message is None:
        return

    task_id = _parse_task_id_arg(context)
    if task_id is None:
        await message.reply_text(_DELETE_USAGE_MESSAGE, parse_mode=ParseMode.HTML)
        return

    task = get_task(task_id)
    if task is None:
        await message.reply_text(f"No task with ID {task_id}.")
        return

    week_info = f"  •  Week: {task.week_number}" if task.week_number else ""
    preview = (
        "<b>Delete this task?</b>\n"
        f"{module_prefix(task)}{esc(task.title)}\n"
        f"<i>Type: {esc(task.task_type)}  •  Due: {task.due_date.isoformat()}"
        f"{week_info}</i>"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Yes, delete",
                    callback_data=f"{DELETE_CB_PREFIX}:yes:{task_id}",
                ),
                InlineKeyboardButton(
                    "Cancel",
                    callback_data=f"{DELETE_CB_PREFIX}:no:{task_id}",
                ),
            ]
        ]
    )
    await message.reply_text(
        preview, parse_mode=ParseMode.HTML, reply_markup=keyboard
    )


@authorized_only
@safe
async def delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the yes/no callback from the /delete confirmation keyboard."""
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 3 or parts[0] != DELETE_CB_PREFIX:
        return

    action, raw_id = parts[1], parts[2]
    try:
        task_id = int(raw_id)
    except ValueError:
        await query.edit_message_text("Invalid callback payload.")
        return

    if action == "no":
        await query.edit_message_text("Cancelled. No tasks were deleted.")
        return
    if action != "yes":
        return

    task = get_task(task_id)
    if task is None:
        await query.edit_message_text(
            f"No task with ID {task_id} (already deleted?)."
        )
        return

    delete_task(task_id)
    await query.edit_message_text(
        f"Deleted: {module_prefix(task)}{esc(task.title)} "
        f"<code>(id {task_id})</code>",
        parse_mode=ParseMode.HTML,
    )
