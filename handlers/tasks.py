"""Deadline command handlers: /deadlines, /done, and /delete.

The bot is intentionally assessment-focused. Timetable views such as /today
and /week are not registered; the primary view is one chronological list of
pending semester deadlines.

Each chat keeps ONE persistent dashboard message. /deadlines registers it in
SQLite (:func:`database.db.save_deadline_dashboard`); afterwards every
mutation and the daily post-midnight job edit that same message in place via
:func:`refresh_deadline_dashboard` rather than sending a fresh list. The
registration is chat-scoped, so the owner's DM and the allowed group each
track their own dashboard.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date

from telegram import InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import Application, ContextTypes

from database.db import (
    delete_deadline_dashboard,
    get_deadline_dashboard_message_id,
    get_semester_deadlines,
    get_task,
    list_deadline_dashboards,
    mark_complete,
    save_deadline_dashboard,
)
from database.models import Task
from utils.auth import authorized_only
from utils.clock import today_local
from utils.errors import safe
from utils.format import (
    DEADLINE_PICKER_PAGE_SIZE,
    DIVIDER,
    TYPE_EMOJI,
    build_deadline_dashboard_keyboard,
    build_deadline_picker_keyboard,
    build_delete_confirmation_keyboard,
    days_away_label,
    esc,
    format_task_card,
    module_prefix,
    urgency_emoji,
)

logger = logging.getLogger(__name__)

_DONE_USAGE_MESSAGE: str = "Usage: /done &lt;deadline_id&gt;  e.g. <code>/done 4</code>"
_DELETE_USAGE_MESSAGE: str = (
    "Usage: /delete &lt;deadline_id&gt;  e.g. <code>/delete 4</code>"
)


def _deadline_line(task: Task, status_emoji: str, today: date) -> list[str]:
    """Render one compact two-line deadline entry for Telegram."""
    type_emoji = TYPE_EMOJI.get(task.task_type, "📌")
    date_label = task.due_date.strftime("%a %d %b %Y")
    time_clause = f" at {esc(task.due_time)}" if task.due_time else ""
    relative = days_away_label(task.due_date, today)
    return [
        f"{status_emoji} {module_prefix(task)}<b>{esc(task.title)}</b>",
        f"   {type_emoji} {esc(task.task_type.capitalize())} · "
        f"{date_label}{time_clause} · {relative} · <code>#{task.id}</code>",
    ]


def get_upcoming_deadlines(
    chat_id: int, today: date | None = None
) -> list[Task]:
    """Return pending assessed deadlines whose due date has not passed.

    ``today`` defaults to :func:`utils.clock.today_local`. Callers pass it
    explicitly so one render uses a single date for filtering, urgency and
    countdown text; tests pass a fixed date for determinism.
    """
    reference_date = today if today is not None else today_local()
    return [
        task
        for task in get_semester_deadlines(chat_id)
        if task.due_date >= reference_date
    ]


def render_deadlines(
    chat_id: int, today: date | None = None
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build the chronological deadline dashboard and its compact action.

    The local date is captured once and reused for filtering, urgency icons
    and countdown labels, so a render can never mix two dates mid-message.
    """
    reference_date = today if today is not None else today_local()
    upcoming = get_upcoming_deadlines(chat_id, reference_date)

    if not upcoming:
        return (
            "🎉 <b>No upcoming deadlines</b>\n\n"
            "<i>Add the next quiz, lab, assignment, project, midterm, or final "
            "with /add.</i>",
            None,
        )

    lines: list[str] = ["📅 <b>Upcoming Deadlines</b>", "", DIVIDER, ""]
    for task in upcoming:
        lines.extend(
            _deadline_line(
                task,
                urgency_emoji(task.due_date, reference_date),
                reference_date,
            )
        )
    lines.extend(["", DIVIDER, ""])
    lines.append(
        f"<i>{len(upcoming)} pending · sorted by due date</i>"
    )
    lines.append("<i>Tap Manage deadlines to complete, edit, or delete.</i>")

    return "\n".join(lines), build_deadline_dashboard_keyboard()


def render_deadline_picker(
    chat_id: int, page: int, today: date | None = None
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build a paginated picker used by the compact deadline manager."""
    reference_date = today if today is not None else today_local()
    upcoming = get_upcoming_deadlines(chat_id, reference_date)
    if not upcoming:
        return render_deadlines(chat_id, reference_date)

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


# ---------------------------------------------------------------------------
# Persistent dashboard refresh
# ---------------------------------------------------------------------------
# Telegram rejects an edit whose text and markup are byte-identical to what is
# already displayed. That is not a failure for us: it means the dashboard is
# already current, so the registration must survive.
_NOT_MODIFIED_FRAGMENT: str = "message is not modified"

# Errors meaning the target message is permanently unusable. The registration
# is dropped so the scheduler stops retrying a message that can never succeed.
_UNRECOVERABLE_FRAGMENTS: tuple[str, ...] = (
    "message to edit not found",
    "message can't be edited",
    "message_id_invalid",
    "message identifier is not specified",
)

# One lock per chat. Two group members mutating deadlines at once would
# otherwise race: the slower render could land after the faster one and leave
# the dashboard showing stale content.
_dashboard_locks: dict[int, asyncio.Lock] = {}


def _dashboard_lock(chat_id: int) -> asyncio.Lock:
    """Return (creating on first use) the refresh lock for ``chat_id``."""
    lock = _dashboard_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _dashboard_locks[chat_id] = lock
    return lock


def is_tracked_deadline_dashboard(chat_id: int, message_id: int) -> bool:
    """Return ``True`` if ``message_id`` is the live dashboard for ``chat_id``."""
    return get_deadline_dashboard_message_id(chat_id) == message_id


def _is_not_modified(exc: BadRequest) -> bool:
    """Return ``True`` for Telegram's benign "nothing changed" rejection."""
    return _NOT_MODIFIED_FRAGMENT in str(exc).lower()


def _is_unrecoverable(exc: BadRequest) -> bool:
    """Return ``True`` when the target message can never be edited again."""
    text = str(exc).lower()
    return any(fragment in text for fragment in _UNRECOVERABLE_FRAGMENTS)


async def refresh_deadline_dashboard(
    application: Application, chat_id: int
) -> bool:
    """Re-render ``chat_id``'s tracked dashboard and edit it in place.

    Returns ``True`` when a usable registration remains afterwards — including
    the "not modified" case, where the dashboard is simply already current.
    Returns ``False`` when there was nothing registered, or the registration
    was dropped because the message is permanently gone.

    Never raises. Callers invoke this *after* committing a task mutation, so an
    exception escaping here would make an add, edit or delete that actually
    succeeded look as though it had failed.
    """
    try:
        return await _refresh_dashboard(application, chat_id)
    except Exception:
        # Last-resort guard, deliberately NOT control flow — the Telegram
        # outcomes that matter are classified explicitly in _refresh_dashboard.
        # This exists only to stop a genuine bug (a render failure, a database
        # hiccup) from escaping into a caller whose mutation is already
        # committed. The registration is kept: a crash here is no evidence that
        # the Telegram message is gone.
        logger.exception(
            "Unexpected failure refreshing dashboard for chat %s", chat_id
        )
        return True


async def _refresh_dashboard(application: Application, chat_id: int) -> bool:
    """Locked refresh body. See :func:`refresh_deadline_dashboard`."""
    async with _dashboard_lock(chat_id):
        message_id = get_deadline_dashboard_message_id(chat_id)
        if message_id is None:
            return False

        text, keyboard = render_deadlines(chat_id)
        try:
            await application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            return True
        except BadRequest as exc:
            if _is_not_modified(exc):
                # Already showing exactly this. Keep the registration.
                return True
            if _is_unrecoverable(exc):
                logger.info(
                    "Dashboard message %s in chat %s is gone (%s) — "
                    "dropping registration",
                    message_id,
                    chat_id,
                    exc,
                )
                delete_deadline_dashboard(chat_id)
                return False
            # Some other BadRequest (malformed markup, entity problem...).
            # Keep the row so a later refresh can retry.
            logger.warning(
                "Dashboard refresh rejected for chat %s: %s", chat_id, exc
            )
            return True
        except Forbidden as exc:
            logger.warning(
                "Lost access to chat %s (%s) — dropping dashboard registration",
                chat_id,
                exc,
            )
            delete_deadline_dashboard(chat_id)
            return False
        except TelegramError as exc:
            # Timeout, network blip, flood limit: potentially temporary, so the
            # registration stays and the next refresh retries.
            logger.warning(
                "Transient Telegram error refreshing chat %s: %s", chat_id, exc
            )
            return True


async def refresh_all_deadline_dashboards(application: Application) -> None:
    """Refresh every registered dashboard, isolating per-chat failures.

    Used by the post-midnight cron job and once at startup. One broken chat
    must not stop the others from updating, so each chat is handled
    independently and only a compact summary is logged.
    """
    registrations = list_deadline_dashboards()
    if not registrations:
        logger.debug("No deadline dashboards registered — nothing to refresh")
        return

    refreshed = 0
    dropped = 0
    for chat_id, _message_id in registrations:
        try:
            if await refresh_deadline_dashboard(application, chat_id):
                refreshed += 1
            else:
                dropped += 1
        except Exception:
            # Defensive: refresh_deadline_dashboard already swallows Telegram
            # errors, so this only catches genuine bugs. Keep going.
            dropped += 1
            logger.exception("Unexpected failure refreshing chat %s", chat_id)

    logger.info(
        "Deadline dashboards refreshed: %d ok, %d unavailable (of %d)",
        refreshed,
        dropped,
        len(registrations),
    )


async def send_and_register_dashboard(
    message: Message, chat_id: int
) -> None:
    """Send a fresh dashboard as a reply and register it as the live one."""
    text, keyboard = render_deadlines(chat_id)
    sent = await message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=keyboard
    )
    save_deadline_dashboard(chat_id=sent.chat_id, message_id=sent.message_id)
    logger.info(
        "Registered dashboard message %s for chat %s",
        sent.message_id,
        sent.chat_id,
    )


_DASHBOARD_REFRESHED_MESSAGE: str = "✅ Existing deadline dashboard refreshed."


@authorized_only
@safe
async def deadlines(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show or refresh this chat's one persistent deadline dashboard.

    First call in a chat sends the dashboard and registers it. Later calls
    edit that same message and reply with a short confirmation instead of
    posting a second full list. If the registered message has since been
    deleted, the registration is dropped and a replacement is sent.
    """
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    if get_deadline_dashboard_message_id(chat.id) is None:
        await send_and_register_dashboard(message, chat.id)
        return

    if await refresh_deadline_dashboard(context.application, chat.id):
        await message.reply_text(_DASHBOARD_REFRESHED_MESSAGE)
        return

    # The tracked message was unusable and its row has been dropped; the
    # command is an explicit user request, so send a replacement now.
    await send_and_register_dashboard(message, chat.id)


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
    # The completion is already committed; a dashboard refresh failure is
    # logged inside the helper and must not surface as a failed /done.
    await refresh_deadline_dashboard(context.application, chat.id)


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
