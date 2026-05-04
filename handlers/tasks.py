"""Slash-command handlers: /today, /week, /semester, /done, /delete.

Inline-keyboard callbacks (Done / Edit / Delete buttons under /today, plus
the Yes/No delete confirmation) are handled in :mod:`handlers.callbacks` and
:mod:`handlers.edit_task` — this module is purely for the slash entry points
and their text replies.

All Telegram messages use HTML parse mode. Every user-supplied string flows
through :func:`utils.format.esc` before interpolation.
"""
from __future__ import annotations

import logging
from datetime import date

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import get_current_week
from database.db import (
    cleanup_past_deadlines,
    get_semester_deadlines,
    get_task,
    get_tasks_for_date,
    get_tasks_for_week,
    mark_complete,
)
from database.models import TASK_TYPE_LECTURE, TASK_TYPE_TUTORIAL, Task
from utils.auth import authorized_only
from utils.clock import today_local
from utils.errors import safe
from utils.format import (
    DIVIDER,
    STATUS_FUTURE,
    STATUS_THIS_WEEK,
    build_delete_confirmation_keyboard,
    build_task_keyboard,
    days_away_label,
    esc,
    format_grouped_today,
    format_relative_date,
    format_task_card,
    module_prefix,
    morning_greeting,
    todays_tip,
)

logger = logging.getLogger(__name__)

_NO_TASKS_TODAY_MESSAGE: str = "🎉 Nothing scheduled for today. Enjoy!"
_DONE_USAGE_MESSAGE: str = "Usage: /done &lt;task_id&gt;  e.g. <code>/done 4</code>"
_DELETE_USAGE_MESSAGE: str = (
    "Usage: /delete &lt;task_id&gt;  e.g. <code>/delete 4</code>"
)


# ---------------------------------------------------------------------------
# /today
# ---------------------------------------------------------------------------

def _render_today() -> tuple[str, "InlineKeyboardMarkup | None"]:  # type: ignore[name-defined]
    """Compose today's text + keyboard. Shared with the callback re-render path."""
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
async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /today: list tasks due today with action buttons."""
    message = update.effective_message
    if message is None:
        return

    text, keyboard = _render_today()
    await message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=keyboard
    )


# ---------------------------------------------------------------------------
# /week
# ---------------------------------------------------------------------------

@authorized_only
@safe
async def week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /week: this week's lectures + next week's tutorials, text-only."""
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
        """Render a labelled section as a list of HTML lines."""
        rendered = [f"<b>{header}</b>"]
        if not items:
            rendered.append("<i>(none)</i>")
            return rendered
        for t in items:
            weekday = t.due_date.strftime("%a")
            relative = format_relative_date(t.due_date)
            time_clause = f" at {esc(t.due_time)}" if t.due_time else ""
            rendered.append(
                f"• {weekday} {t.due_date.isoformat()}{time_clause} "
                f"({relative}) — "
                f"{module_prefix(t)}{esc(t.title)}  <code>#{t.id}</code>"
            )
            if t.notes:
                rendered.append(f"  <i>{esc(t.notes)}</i>")
        return rendered

    out: list[str] = [
        f"📅 <b>Week {current}</b>",
        f"<i>{date.today().strftime('%A, %d %b %Y')}</i>",
        "",
        DIVIDER,
        "",
    ]
    out.extend(render("📚 This Week's Lectures", this_week_lectures))
    out.append("")
    out.extend(render("📝 Next Week's Tutorials", next_week_tutorials))
    out.append("")
    out.append(DIVIDER)
    out.append("")
    out.append(
        "<i>Tip: head to /today for an actionable, button-equipped view.</i>"
    )

    await message.reply_text("\n".join(out), parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# /semester
# ---------------------------------------------------------------------------

@authorized_only
@safe
async def semester(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /semester: upcoming midterms and finals only.

    Past midterms and finals auto-delete — this command lazily purges them
    before rendering, in addition to the daily 00:05 cron that does the
    same purge in the background. Two sections only:

    - 🔥 This Week (due in the next 7 days, including today)
    - 📅 Upcoming (due more than 7 days from now)
    """
    message = update.effective_message
    if message is None:
        return

    today = today_local()
    purged = cleanup_past_deadlines(today)
    if purged:
        logger.info("Lazy cleanup removed %d past deadline(s) on /semester", purged)

    deadlines = get_semester_deadlines()
    if not deadlines:
        await message.reply_text(
            "🎯 <b>No upcoming midterms or finals</b>\n\n"
            "<i>Add some via /add when your professors announce them. "
            "Past deadlines auto-delete after their date passes.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    this_week: list[Task] = []
    upcoming: list[Task] = []
    for t in deadlines:
        delta = (t.due_date - today).days
        if delta <= 7:
            this_week.append(t)
        else:
            upcoming.append(t)

    def render_bucket(
        header: str, items: list[Task], status_emoji: str
    ) -> list[str]:
        """Render a labelled section of the /semester output."""
        if not items:
            return []
        rendered: list[str] = [f"<b>{header}</b>"]
        for t in items:
            type_label = t.task_type.capitalize()
            date_label = t.due_date.strftime("%a %d %b")
            relative = days_away_label(t.due_date)
            time_clause = f" at {t.due_time}" if t.due_time else ""
            rendered.append(
                f"{status_emoji} {module_prefix(t)}{type_label} — "
                f"{date_label}{time_clause} ({relative})  "
                f"<code>#{t.id}</code>"
            )
        rendered.append("")
        return rendered

    out: list[str] = ["🎯 <b>Upcoming Deadlines</b>", "", DIVIDER, ""]
    out.extend(render_bucket("🔥 This Week", this_week, STATUS_THIS_WEEK))
    out.extend(render_bucket("📅 Upcoming", upcoming, STATUS_FUTURE))

    while out and out[-1] == "":
        out.pop()

    out.append("")
    out.append(DIVIDER)
    out.append("")
    out.append(
        f"<i>{len(this_week)} this week · {len(upcoming)} upcoming · "
        "past deadlines auto-delete</i>"
    )

    await message.reply_text("\n".join(out), parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# /done <id>
# ---------------------------------------------------------------------------

def _parse_task_id_arg(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Return the first ``context.args`` entry parsed as int, else None."""
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
    """Handle ``/done <task_id>``: mark complete, reply with task card."""
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
    # Refresh the task object so the card shows completed=True.
    task.completed = True
    body = "✅ <b>Done</b>\n\n" + format_task_card(task)
    await message.reply_text(body, parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# /delete <id>
# ---------------------------------------------------------------------------

@authorized_only
@safe
async def delete_task_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/delete <task_id>``: show Yes/No confirmation card."""
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

    confirmation = "🗑️ <b>Delete this task?</b>\n\n" + format_task_card(task)
    await message.reply_text(
        confirmation,
        parse_mode=ParseMode.HTML,
        reply_markup=build_delete_confirmation_keyboard(task_id),
    )
