"""Shared HTML formatting helpers + inline-keyboard builders for task display.

Centralised so /deadlines, /brief, the scheduled morning brief, and callback
handlers render consistently.
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
    TASK_TYPE_LAB,
    TASK_TYPE_MIDTERM,
    TASK_TYPE_OTHER,
    TASK_TYPE_PROJECT,
    TASK_TYPE_QUIZ,
    Module,
    Task,
)

# ---------------------------------------------------------------------------
# Visual style constants
# ---------------------------------------------------------------------------

DIVIDER: str = "━━━━━━━━━━━━━━━━━━━"

TYPE_EMOJI: dict[str, str] = {
    TASK_TYPE_QUIZ: "❓",
    TASK_TYPE_LAB: "🧪",
    TASK_TYPE_ASSIGNMENT: "📦",
    TASK_TYPE_PROJECT: "🛠️",
    TASK_TYPE_MIDTERM: "🎯",
    TASK_TYPE_FINAL: "📕",
    TASK_TYPE_OTHER: "📌",
}

TYPE_PLURAL: dict[str, str] = {
    TASK_TYPE_QUIZ: "Quizzes",
    TASK_TYPE_LAB: "Labs",
    TASK_TYPE_ASSIGNMENT: "Assignments",
    TASK_TYPE_PROJECT: "Projects",
    TASK_TYPE_MIDTERM: "Midterms",
    TASK_TYPE_FINAL: "Finals",
    TASK_TYPE_OTHER: "Other",
}

TYPE_DISPLAY_ORDER: tuple[str, ...] = (
    TASK_TYPE_QUIZ,
    TASK_TYPE_LAB,
    TASK_TYPE_ASSIGNMENT,
    TASK_TYPE_PROJECT,
    TASK_TYPE_MIDTERM,
    TASK_TYPE_FINAL,
    TASK_TYPE_OTHER,
)

STATUS_DUE_TODAY: str = "⚠️"
STATUS_THIS_WEEK: str = "🔥"
STATUS_FUTURE: str = "📅"

# Tips are rotated by date so the same tip shows all day.
TIPS: tuple[str, ...] = (
    "💡 Tip: /deadlines shows every assessed item in due-date order",
    "💡 Tip: tap ⚙️ Manage deadlines to complete, edit, or delete an item",
    "💡 Tip: the deadline manager shows six items per page for easier scanning",
    "💡 Tip: use a clear title such as “Lab Quiz 2” or “Project Demo”",
    "💡 Tip: add venue or submission details in Notes",
    "💡 Tip: /brief previews what is due soon",
)

# Callback-data prefixes used by the inline keyboards on /deadlines.
CB_DONE: str = "done"
CB_DELETE: str = "del"   # del:N (entry) | del:yes:N | del:no:N
CB_EDIT: str = "edit"    # edit:N (entry) | editf:<field>:N (field pick)
CB_EDIT_FIELD: str = "editf"
CB_EDIT_TYPE_VALUE: str = "edittype"  # edittype:<value>:N
CB_EDIT_CANCEL: str = "editcancel"
CB_MANAGE: str = "manage"  # manage:<page>
CB_MANAGE_ITEM: str = "manageitem"  # manageitem:<task_id>:<page>
CB_MANAGE_DASHBOARD: str = "managedash"

# Module-picker keyboard (used by both /add and /edit module-field flows).
CB_MODULE: str = "mod"
# mod:select:<CODE>  → user picked a seeded module
# mod:other          → user wants to type a custom code
# mod:skip           → skip module (e.g. personal tasks)
# mod:clear          → clear the existing module (edit flow only)
# mod:cancel         → abort the picker

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


def days_away_label(target: date) -> str:
    """Return concise phrasing such as 'N days away' or 'N days ago'."""
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


def urgency_emoji(due: date, today: date) -> str:
    """Urgency marker for a pending deadline, by how soon it falls due.

    ``today`` is passed in rather than read from :func:`date.today` so callers
    can anchor to the configured local timezone (see :func:`utils.clock.today_local`).
    Reading UTC here would mislabel deadlines by a day for most of the evening
    in Asia/Singapore.
    """
    delta = (due - today).days
    if delta <= 0:
        return STATUS_DUE_TODAY
    if delta <= 7:
        return STATUS_THIS_WEEK
    return STATUS_FUTURE


# ---------------------------------------------------------------------------
# Task rendering
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Inline keyboards
# ---------------------------------------------------------------------------

# Six rows keep the picker usable on smaller phone screens. The main
# /deadlines dashboard itself only has one button, so it stays compact.
DEADLINE_PICKER_PAGE_SIZE: int = 6


def build_deadline_dashboard_keyboard() -> InlineKeyboardMarkup:
    """Return the single compact action shown under /deadlines."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⚙️ Manage deadlines", callback_data=f"{CB_MANAGE}:0"
                )
            ]
        ]
    )


def _deadline_picker_label(task: Task) -> str:
    """Build a short, identifiable button label for the manager list."""
    date_label = task.due_date.strftime("%d %b")
    module = f"{task.module_code} · " if task.module_code else ""
    label = f"{date_label} · {module}{task.title}"
    return label if len(label) <= 52 else label[:51] + "…"


def build_deadline_picker_keyboard(
    tasks: list[Task], page: int, total_pages: int
) -> InlineKeyboardMarkup:
    """Build one deadline button per row plus compact pagination controls."""
    rows: list[list[InlineKeyboardButton]] = []
    for task in tasks:
        rows.append(
            [
                InlineKeyboardButton(
                    _deadline_picker_label(task),
                    callback_data=f"{CB_MANAGE_ITEM}:{task.id}:{page}",
                )
            ]
        )

    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "← Previous", callback_data=f"{CB_MANAGE}:{page - 1}"
            )
        )
    if page + 1 < total_pages:
        navigation.append(
            InlineKeyboardButton(
                "Next →", callback_data=f"{CB_MANAGE}:{page + 1}"
            )
        )
    if navigation:
        rows.append(navigation)

    rows.append(
        [
            InlineKeyboardButton(
                "← Back to deadlines", callback_data=CB_MANAGE_DASHBOARD
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def build_deadline_action_keyboard(task_id: int, page: int) -> InlineKeyboardMarkup:
    """Show actions immediately below one selected deadline."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Complete", callback_data=f"{CB_DONE}:{task_id}"
                ),
                InlineKeyboardButton(
                    "📝 Edit", callback_data=f"{CB_EDIT}:{task_id}"
                ),
                InlineKeyboardButton(
                    "🗑️ Delete", callback_data=f"{CB_DELETE}:{task_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    "← Back to list", callback_data=f"{CB_MANAGE}:{page}"
                )
            ],
        ]
    )


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

    Layout: two buttons per row. Academic week was removed from the active
    workflow because the deadline's calendar date is the source of truth.
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
                    "📋 Notes",
                    callback_data=f"{CB_EDIT_FIELD}:notes:{task_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=f"{CB_EDIT_CANCEL}:{task_id}",
                ),
            ],
        ]
    )


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
