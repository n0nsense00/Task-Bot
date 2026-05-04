"""Shared HTML formatting helpers + inline-keyboard builders for task display.

Centralised so on-demand command handlers (/today, /week, /semester, /brief),
the scheduled morning brief, and callback handlers all render identically.
Every user-supplied string flows through :func:`esc` before interpolation —
the single chokepoint that prevents injection of HTML tags into the rendered
Telegram message.
"""
from __future__ import annotations

import html
from datetime import date

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from database.models import (
    TASK_TYPE_ASSIGNMENT,
    TASK_TYPE_FINAL,
    TASK_TYPE_LECTURE,
    TASK_TYPE_MIDTERM,
    TASK_TYPE_PERSONAL,
    TASK_TYPE_TUTORIAL,
    Module,
    Task,
)

# ---------------------------------------------------------------------------
# Visual style constants
# ---------------------------------------------------------------------------

DIVIDER: str = "━━━━━━━━━━━━━━━━━━━"

TYPE_EMOJI: dict[str, str] = {
    TASK_TYPE_LECTURE: "📚",
    TASK_TYPE_TUTORIAL: "📝",
    TASK_TYPE_ASSIGNMENT: "📦",
    TASK_TYPE_MIDTERM: "🎯",
    TASK_TYPE_FINAL: "📕",
    TASK_TYPE_PERSONAL: "✏️",
}

TYPE_PLURAL: dict[str, str] = {
    TASK_TYPE_LECTURE: "Lectures",
    TASK_TYPE_TUTORIAL: "Tutorials",
    TASK_TYPE_ASSIGNMENT: "Assignments",
    TASK_TYPE_MIDTERM: "Midterms",
    TASK_TYPE_FINAL: "Finals",
    TASK_TYPE_PERSONAL: "Personal",
}

TYPE_DISPLAY_ORDER: tuple[str, ...] = (
    TASK_TYPE_LECTURE,
    TASK_TYPE_TUTORIAL,
    TASK_TYPE_ASSIGNMENT,
    TASK_TYPE_MIDTERM,
    TASK_TYPE_FINAL,
    TASK_TYPE_PERSONAL,
)

# Backward-compat alias kept for older imports — same content as TYPE_PLURAL.
TYPE_DISPLAY_LABEL: dict[str, str] = TYPE_PLURAL

STATUS_DUE_TODAY: str = "⚠️"
STATUS_THIS_WEEK: str = "🔥"
STATUS_DONE: str = "✅"
STATUS_FUTURE: str = "📅"

# Tips are rotated by date so the same tip shows all day.
TIPS: tuple[str, ...] = (
    "💡 Tip: tap ✅ on any task in /today to mark it complete instantly",
    "💡 Tip: tap 📝 on a task to edit any field",
    "💡 Tip: tap 🗑️ to delete a task (with a confirmation prompt)",
    "💡 Tip: send /add to create a new task step by step",
    "💡 Tip: /week shows lectures this week + tutorials next week",
    "💡 Tip: /semester lists every midterm and final by due date",
    "💡 Tip: /brief sends today's morning summary on demand",
)

# Callback-data prefixes used by the inline keyboards on /today.
CB_DONE: str = "done"
CB_DELETE: str = "del"   # del:N (entry) | del:yes:N | del:no:N
CB_EDIT: str = "edit"    # edit:N (entry) | editf:<field>:N (field pick)
CB_EDIT_FIELD: str = "editf"
CB_EDIT_TYPE_VALUE: str = "edittype"  # edittype:<value>:N
CB_EDIT_CANCEL: str = "editcancel"

# Module-picker keyboard (used by both /add and /edit module-field flows).
CB_MODULE: str = "mod"
# mod:select:<CODE>  → user picked a seeded module
# mod:other          → user wants to type a custom code
# mod:skip           → skip module (e.g. personal tasks)
# mod:clear          → clear the existing module (edit flow only)
# mod:cancel         → abort the picker

# Week-picker keyboard (used by /add WEEK and /edit EDIT_WEEK).
CB_WEEK: str = "week"
# week:set:<N>       → user picked week number 1-13
# week:skip          → /add: no week (None)
# week:clear         → /edit: clear existing week (set to None)
# week:cancel        → abort the picker

# Notes-prompt skip / clear button (used by /add NOTES and /edit EDIT_NOTES).
CB_NOTES: str = "notes"
# notes:skip         → /add: leave notes blank (None)
# notes:clear        → /edit: clear existing notes (set to None)
# notes:cancel       → abort the conversation


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------

def esc(text: str | None) -> str:
    """HTML-escape a user-supplied string; map ``None`` to the empty string."""
    return html.escape(text) if text else ""


def module_prefix(task: Task) -> str:
    """Return ``[MODULE] `` (with trailing space) or ``""`` when no module_code."""
    return f"[{esc(task.module_code)}] " if task.module_code else ""


def module_label(module: Module) -> str:
    """Render a Module as ``CODE · Name`` if name is set, else just ``CODE``.

    Used in the module-picker keyboard buttons. Limit total length to ~30
    chars so it doesn't wrap awkwardly on narrow phone screens — Telegram
    truncates beyond that anyway.
    """
    if module.name:
        label = f"{module.code} · {module.name}"
        if len(label) > 30:
            label = label[:29] + "…"
        return label
    return module.code


def format_due(due: date, due_time: str | None) -> str:
    """Combine a date and optional time into a single human-readable string.

    Examples: ``today``, ``today at 23:59``, ``in 3 days at 09:00``,
    ``Wed 7 May``, ``Wed 7 May at 12:00``.
    """
    relative = format_relative_date(due)
    if due_time:
        return f"{relative} at {due_time}"
    return relative


# ---------------------------------------------------------------------------
# Date formatting
# ---------------------------------------------------------------------------

def format_relative_date(target: date) -> str:
    """Render ``target`` relative to today.

    Examples: ``today``, ``tomorrow``, ``yesterday``, ``in 3 days``,
    ``2 days ago``, or ``Wed 7 May`` for anything beyond a week.
    """
    delta = (target - date.today()).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    if 0 < delta <= 6:
        return f"in {delta} days"
    if -6 <= delta < 0:
        return f"{-delta} days ago"
    return target.strftime("%a %d %b")


def format_absolute_date(target: date) -> str:
    """Long-form date: ``Wednesday, 7 May 2026``.

    Built up manually rather than via ``%-d`` / ``%#d`` because Linux and
    Windows disagree on the no-leading-zero directive — and Task-Bot runs
    on both (Windows for dev, Linux for the Railway worker).
    """
    return f"{target.strftime('%A')}, {target.day} {target.strftime('%b %Y')}"


def days_away_label(target: date) -> str:
    """Legacy alias used by /semester — phrasing 'N days away' / 'N days ago'."""
    delta = (target - date.today()).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    if delta > 0:
        return f"{delta} days away"
    return f"{-delta} days ago"


# ---------------------------------------------------------------------------
# Section helpers
# ---------------------------------------------------------------------------

def format_section_header(title: str, emoji: str | None = None) -> str:
    """Bold section header line, optionally prefixed with ``emoji``."""
    prefix = f"{emoji} " if emoji else ""
    return f"<b>{prefix}{esc(title)}</b>"


# ---------------------------------------------------------------------------
# Task rendering
# ---------------------------------------------------------------------------

def task_status_emoji(task: Task) -> str:
    """Pick the most-relevant single status emoji for a task."""
    if task.completed:
        return STATUS_DONE
    delta = (task.due_date - date.today()).days
    if delta == 0:
        return STATUS_DUE_TODAY
    if 0 < delta <= 7:
        return STATUS_THIS_WEEK
    return STATUS_FUTURE


def format_task_line(task: Task) -> str:
    """One-line render of a task for grouped views.

    Layout: ``• [MODULE] Title  #ID``, with an italic ``HH:MM`` after the
    title when ``due_time`` is set. Notes (if present) on a second indented
    italic line.
    """
    time_suffix = f" <i>at {esc(task.due_time)}</i>" if task.due_time else ""
    line = (
        f"• {module_prefix(task)}{esc(task.title)}{time_suffix}  "
        f"<code>#{task.id}</code>"
    )
    if task.notes:
        line += f"\n  <i>{esc(task.notes)}</i>"
    return line


def format_task_card(task: Task) -> str:
    """Multi-line rich render of a single task.

    Used for confirmations (delete, edit) and the post-/add summary.
    Includes type emoji, module, title, type label, relative + absolute
    date, time (if set), week (if set), notes (if set), and ID.
    """
    type_emoji = TYPE_EMOJI.get(task.task_type, "•")
    relative = format_relative_date(task.due_date)
    absolute = task.due_date.strftime("%a %d %b %Y")
    time_clause = f" at {esc(task.due_time)}" if task.due_time else ""

    lines: list[str] = [
        f"{type_emoji} {module_prefix(task)}<b>{esc(task.title)}</b>  "
        f"<code>#{task.id}</code>",
        f"<i>{esc(task.task_type.capitalize())} · "
        f"{relative} ({absolute}){time_clause}</i>",
    ]
    if task.week_number is not None:
        lines.append(f"<i>Week {task.week_number}</i>")
    if task.notes:
        lines.append("")
        lines.append(esc(task.notes))
    return "\n".join(lines)


def format_grouped_today(tasks: list[Task], target_date: date) -> list[str]:
    """Tasks due on ``target_date`` rendered as HTML lines, grouped by type.

    Returns a list of lines for the caller to compose into a larger message.
    Empty lines between sections are intentional spacing in Telegram.
    """
    grouped: dict[str, list[Task]] = {}
    for t in tasks:
        grouped.setdefault(t.task_type, []).append(t)

    lines: list[str] = []
    for ttype in TYPE_DISPLAY_ORDER:
        bucket = grouped.get(ttype)
        if not bucket:
            continue
        lines.append("")
        lines.append(f"<b>{TYPE_EMOJI[ttype]} {TYPE_PLURAL[ttype]}</b>")
        lines.extend(format_task_line(t) for t in bucket)
    return lines


def format_task_list(
    tasks: list[Task],
    group_by_type: bool = True,
    header: str | None = None,
) -> str:
    """Render a list of tasks as a single HTML block.

    ``group_by_type=True`` (default) sections by type with emojis. False
    produces a flat bulleted list — useful for "upcoming deadlines" sections
    where order is by date, not type.
    """
    if not tasks:
        return ""

    out: list[str] = []
    if header:
        out.append(format_section_header(header))

    if group_by_type:
        grouped: dict[str, list[Task]] = {}
        for t in tasks:
            grouped.setdefault(t.task_type, []).append(t)
        for ttype in TYPE_DISPLAY_ORDER:
            bucket = grouped.get(ttype)
            if not bucket:
                continue
            if out:
                out.append("")
            out.append(f"<b>{TYPE_EMOJI[ttype]} {TYPE_PLURAL[ttype]}</b>")
            out.extend(format_task_line(t) for t in bucket)
    else:
        for t in tasks:
            out.append(format_task_line(t))

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Inline keyboards
# ---------------------------------------------------------------------------

# Soft cap on how many tasks get inline-action buttons in /today. Telegram
# allows up to 100 buttons per message; beyond ~5 tasks (15 buttons) the
# keyboard becomes visually cluttered on phone screens.
TASK_KEYBOARD_MAX_TASKS: int = 5


def build_task_keyboard(tasks: list[Task]) -> InlineKeyboardMarkup | None:
    """Three-buttons-per-task keyboard: ✅ Done, 📝 Edit, 🗑️ Delete.

    Returns ``None`` for an empty list so callers can pass
    ``reply_markup=None`` and Telegram drops the keyboard entirely.
    Truncates silently to ``TASK_KEYBOARD_MAX_TASKS`` to keep the keyboard
    height sane on mobile.
    """
    if not tasks:
        return None
    capped = tasks[:TASK_KEYBOARD_MAX_TASKS]
    rows: list[list[InlineKeyboardButton]] = []
    for t in capped:
        rows.append(
            [
                InlineKeyboardButton(
                    f"✅ #{t.id}", callback_data=f"{CB_DONE}:{t.id}"
                ),
                InlineKeyboardButton(
                    f"📝 #{t.id}", callback_data=f"{CB_EDIT}:{t.id}"
                ),
                InlineKeyboardButton(
                    f"🗑️ #{t.id}", callback_data=f"{CB_DELETE}:{t.id}"
                ),
            ]
        )
    return InlineKeyboardMarkup(rows)


def build_delete_confirmation_keyboard(task_id: int) -> InlineKeyboardMarkup:
    """Yes/No confirmation keyboard used by both /delete slash and 🗑️ button."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Yes, delete", callback_data=f"{CB_DELETE}:yes:{task_id}"
                ),
                InlineKeyboardButton(
                    "❌ Cancel", callback_data=f"{CB_DELETE}:no:{task_id}"
                ),
            ]
        ]
    )


def build_edit_field_keyboard(task_id: int) -> InlineKeyboardMarkup:
    """Field picker shown after the user taps 📝 Edit on a task.

    Layout: 2 buttons per row, 4 rows + final Cancel row. Time was added
    when /add gained an optional time field; keeping all 7 fields editable
    so /edit doesn't have a partial-coverage gap.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📝 Title", callback_data=f"{CB_EDIT_FIELD}:title:{task_id}"
                ),
                InlineKeyboardButton(
                    "🏷️ Type", callback_data=f"{CB_EDIT_FIELD}:type:{task_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🎓 Module",
                    callback_data=f"{CB_EDIT_FIELD}:module:{task_id}",
                ),
                InlineKeyboardButton(
                    "📅 Due date",
                    callback_data=f"{CB_EDIT_FIELD}:due:{task_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🕐 Time",
                    callback_data=f"{CB_EDIT_FIELD}:time:{task_id}",
                ),
                InlineKeyboardButton(
                    "📊 Week",
                    callback_data=f"{CB_EDIT_FIELD}:week:{task_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📋 Notes",
                    callback_data=f"{CB_EDIT_FIELD}:notes:{task_id}",
                ),
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=f"{CB_EDIT_CANCEL}:{task_id}",
                ),
            ],
        ]
    )


def build_week_keyboard(
    *,
    include_skip: bool = False,
    include_clear: bool = False,
) -> InlineKeyboardMarkup:
    """Build a week-number picker: 13 buttons (1-13) + Skip/Clear + Cancel.

    Layout: 5 + 5 + 3 grid of numbered buttons. ``include_skip`` adds a
    "Skip" row (used by /add to mean 'no week assigned'). ``include_clear``
    adds a "Clear" button (used by /edit to mean 'remove the current week').
    """
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for week_num in range(1, 14):
        row.append(
            InlineKeyboardButton(
                str(week_num), callback_data=f"{CB_WEEK}:set:{week_num}"
            )
        )
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    bottom: list[InlineKeyboardButton] = []
    if include_skip:
        bottom.append(
            InlineKeyboardButton("Skip", callback_data=f"{CB_WEEK}:skip")
        )
    if include_clear:
        bottom.append(
            InlineKeyboardButton("Clear", callback_data=f"{CB_WEEK}:clear")
        )
    if bottom:
        rows.append(bottom)
    rows.append(
        [
            InlineKeyboardButton(
                "❌ Cancel", callback_data=f"{CB_WEEK}:cancel"
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def parse_week_callback(data: str) -> tuple[str, int | None]:
    """Decode a week-picker callback into ``(action, payload)``.

    Actions: ``set`` (payload = int 1-13), ``skip``, ``clear``, ``cancel``,
    ``unknown``.
    """
    if data == f"{CB_WEEK}:skip":
        return ("skip", None)
    if data == f"{CB_WEEK}:clear":
        return ("clear", None)
    if data == f"{CB_WEEK}:cancel":
        return ("cancel", None)
    if data.startswith(f"{CB_WEEK}:set:"):
        try:
            return ("set", int(data[len(f"{CB_WEEK}:set:") :]))
        except ValueError:
            return ("unknown", None)
    return ("unknown", None)


def build_notes_keyboard(
    *,
    include_clear: bool = False,
) -> InlineKeyboardMarkup:
    """Build a Skip/Clear + Cancel keyboard for the notes prompt.

    Used inline alongside a text-input prompt — user can either tap a
    button (no notes / clear notes / abort) or send any text message
    (notes content). The conversation state's CallbackQueryHandler and
    MessageHandler each handle one of those input modes.
    """
    label = "Clear" if include_clear else "Skip"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"{CB_NOTES}:{'clear' if include_clear else 'skip'}",
                ),
                InlineKeyboardButton(
                    "❌ Cancel", callback_data=f"{CB_NOTES}:cancel"
                ),
            ]
        ]
    )


def parse_notes_callback(data: str) -> str:
    """Decode a notes-prompt callback. Returns ``skip`` / ``clear`` / ``cancel`` / ``unknown``."""
    if data == f"{CB_NOTES}:skip":
        return "skip"
    if data == f"{CB_NOTES}:clear":
        return "clear"
    if data == f"{CB_NOTES}:cancel":
        return "cancel"
    return "unknown"


def build_module_keyboard(
    modules: list[Module],
    *,
    include_skip: bool = True,
    include_clear: bool = False,
) -> InlineKeyboardMarkup:
    """Build a module-picker keyboard for /add and /edit module-field flows.

    Layout: one row per seeded module (single button each — module names
    are too long for two-column reliably). Optional Skip / Clear / Other
    rows below, then Cancel. Reused by both the /add module step and the
    edit-task module field.

    ``include_skip``: include a "Skip" button (used in /add for personal tasks).
    ``include_clear``: include a "Clear current module" button (used in /edit).
    """
    rows: list[list[InlineKeyboardButton]] = []
    for module in modules:
        rows.append(
            [
                InlineKeyboardButton(
                    module_label(module),
                    callback_data=f"{CB_MODULE}:select:{module.code}",
                )
            ]
        )

    bottom: list[InlineKeyboardButton] = [
        InlineKeyboardButton(
            "Other (type it)…", callback_data=f"{CB_MODULE}:other"
        ),
    ]
    if include_skip:
        bottom.append(
            InlineKeyboardButton("Skip", callback_data=f"{CB_MODULE}:skip")
        )
    if include_clear:
        bottom.append(
            InlineKeyboardButton(
                "Clear", callback_data=f"{CB_MODULE}:clear"
            )
        )
    rows.append(bottom)
    rows.append(
        [
            InlineKeyboardButton(
                "❌ Cancel", callback_data=f"{CB_MODULE}:cancel"
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def build_edit_type_keyboard(task_id: int) -> InlineKeyboardMarkup:
    """Inline keyboard listing every valid task_type for the type-edit step."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for ttype in TYPE_DISPLAY_ORDER:
        emoji = TYPE_EMOJI[ttype]
        row.append(
            InlineKeyboardButton(
                f"{emoji} {ttype.capitalize()}",
                callback_data=f"{CB_EDIT_TYPE_VALUE}:{ttype}:{task_id}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(
                "❌ Cancel", callback_data=f"{CB_EDIT_CANCEL}:{task_id}"
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Greetings, tips, dividers
# ---------------------------------------------------------------------------

def todays_tip() -> str:
    """Return one tip from :data:`TIPS`, rotated by date.

    Same tip shows all day; cycles forward each midnight. Stable across
    multiple commands within the same day so the user isn't whipsawed.
    """
    idx = date.today().toordinal() % len(TIPS)
    return TIPS[idx]


def morning_greeting() -> str:
    """Two-line bold greeting + italic full date used at the top of /today and /brief."""
    today = date.today()
    return (
        f"☀️ <b>Good morning!</b>\n"
        f"<i>{today.strftime('%A, %d %b %Y')}</i>"
    )
