"""Daily push notifications via APScheduler.

Two cron jobs run here. The morning brief fires at
``BRIEF_HOUR:BRIEF_MINUTE`` and is gated on ``MORNING_BRIEF_ENABLED``; the
deadline-dashboard refresh fires shortly after local midnight and is always
installed, because a stale countdown is wrong regardless of whether the user
wants a morning push.

The brief job fires every day at ``BRIEF_HOUR:BRIEF_MINUTE`` in
:data:`config.TIMEZONE`, builds a "morning brief" (today's tasks plus the
next few semester deadlines), and sends it to the bot owner.

On startup, if the brief for today has not yet been sent and the current
local time is already past the scheduled hour, the brief fires immediately
(``catch_up_missed_brief``). This covers the common case of the bot not
running at 08:00 — laptop closed, restarted mid-day, etc. The last-sent
date is persisted to ``data/last_brief.txt`` so the check survives restarts.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram.constants import ParseMode
from telegram.ext import Application

from config import (
    BRIEF_HOUR,
    BRIEF_MINUTE,
    MORNING_BRIEF_ENABLED,
    MY_TELEGRAM_ID,
    TIMEZONE,
)
from database.db import get_semester_deadlines
from handlers.tasks import refresh_all_deadline_dashboards
from database.models import Task
from utils.clock import today_local
from utils.format import (
    DIVIDER,
    TYPE_DISPLAY_ORDER,
    TYPE_EMOJI,
    TYPE_PLURAL,
    days_away_label,
    esc,
    format_task_line,
    module_prefix,
    morning_greeting,
)
from utils.limits import TITLE_MAX_LENGTH, fits_telegram_message, shorten_text

logger = logging.getLogger(__name__)

_PROJECT_ROOT: Path = Path(__file__).resolve().parent
_LAST_BRIEF_FILE: Path = _PROJECT_ROOT / "data" / "last_brief.txt"

_JOB_ID: str = "morning_brief"
# Shortly after local midnight: late enough that the date has definitely
# rolled over, early enough that a countdown is corrected before anyone reads
# it. Runs regardless of MORNING_BRIEF_ENABLED — that flag governs only the
# morning push, not the dashboard.
_DEADLINE_REFRESH_JOB_ID: str = "deadline_dashboard_refresh"
_DEADLINE_REFRESH_HOUR: int = 0
_DEADLINE_REFRESH_MINUTE: int = 6
_HEARTBEAT_JOB_ID: str = "heartbeat"
_HEARTBEAT_INTERVAL_MINUTES: int = 10
_UPCOMING_WINDOW_DAYS: int = 14
_UPCOMING_LIMIT: int = 3

_CLEAR_DAY_MESSAGE: str = (
    "🎉 Clear day ahead — nothing scheduled, no upcoming deadlines."
)


def _resolve_timezone() -> ZoneInfo:
    """Return the configured timezone, falling back to Asia/Singapore on error."""
    try:
        return ZoneInfo(TIMEZONE)
    except ZoneInfoNotFoundError:
        logger.warning(
            "Invalid TIMEZONE %r — falling back to Asia/Singapore", TIMEZONE
        )
        return ZoneInfo("Asia/Singapore")


def _upcoming_deadlines(chat_id: int, today: date) -> list[Task]:
    """Return up to ``_UPCOMING_LIMIT`` upcoming deadlines within the window.

    "Upcoming" means ``today < due_date <= today + _UPCOMING_WINDOW_DAYS``.
    Items due today are rendered in their own section by
    :func:`build_morning_brief` and must not consume the future-preview limit.
    :func:`get_semester_deadlines` already sorts by ``due_date`` ascending, so
    slicing the filtered list preserves chronological order.
    """
    window_end_ord = today.toordinal() + _UPCOMING_WINDOW_DAYS
    candidates = [
        t
        for t in get_semester_deadlines(chat_id)
        if today.toordinal() < t.due_date.toordinal() <= window_end_ord
    ]
    return candidates[:_UPCOMING_LIMIT]


def build_morning_brief(chat_id: int, today: date | None = None) -> str:
    """Assemble the morning brief as an HTML-formatted string.

    Layout shows today's assessed items followed by a near-term preview.
    Short-circuits to a terse "clear day" message
    when there is nothing due today AND no upcoming deadlines in the next
    fortnight, so the morning push doesn't pester the user with empty lists.
    """
    reference_date = today if today is not None else today_local()
    tasks = [
        t for t in get_semester_deadlines(chat_id) if t.due_date == reference_date
    ]
    upcoming = _upcoming_deadlines(chat_id, reference_date)

    if not tasks and not upcoming:
        return _CLEAR_DAY_MESSAGE

    lines: list[str] = [morning_greeting(reference_date), "", DIVIDER]
    blocks: list[list[str]] = []

    if tasks:
        grouped: dict[str, list[Task]] = {}
        for task in tasks:
            grouped.setdefault(task.task_type, []).append(task)
        for task_type in TYPE_DISPLAY_ORDER:
            bucket = grouped.get(task_type, [])
            for index, task in enumerate(bucket):
                prefix = (
                    [
                        "",
                        f"<b>{TYPE_EMOJI[task_type]} "
                        f"{TYPE_PLURAL[task_type]}</b>",
                    ]
                    if index == 0
                    else []
                )
                blocks.append(prefix + [format_task_line(task)])
    else:
        lines.extend(["", "<i>Nothing due today.</i>"])

    for index, task in enumerate(upcoming):
        prefix = (
            ["", DIVIDER, "", "⏰ <b>Upcoming deadlines</b>"]
            if index == 0
            else []
        )
        type_label = task.task_type.capitalize()
        date_label = task.due_date.strftime("%a %d %b")
        relative = days_away_label(task.due_date, reference_date)
        time_clause = f" at {esc(task.due_time)}" if task.due_time else ""
        blocks.append(
            prefix
            + [
                f"• {module_prefix(task)}"
                f"<b>{esc(shorten_text(task.title, TITLE_MAX_LENGTH))}</b> · "
                f"{esc(type_label)} — "
                f"{date_label}{time_clause} ({relative})  "
                f"<code>#{task.id}</code>"
            ]
        )

    shown = 0
    total = len(blocks)
    for block in blocks:
        remaining = total - shown - 1
        overflow = _brief_overflow_footer(remaining)
        if not fits_telegram_message("\n".join(lines + block + overflow)):
            break
        lines.extend(block)
        shown += 1

    lines.extend(_brief_overflow_footer(total - shown))
    text = "\n".join(lines).rstrip()
    # Even a legacy row is bounded by format_task_line; this assertion catches
    # future layout changes before Telegram rejects a scheduled message.
    assert fits_telegram_message(text)
    return text


def _brief_overflow_footer(omitted: int) -> list[str]:
    """Explain brief truncation while routing users to the full manager."""
    if not omitted:
        return []
    return [
        "",
        f"<i>{omitted} more — use /deadlines, then Manage deadlines, "
        "to view all.</i>",
    ]


def _brief_already_sent_today(today: date | None = None) -> bool:
    """Return ``True`` if ``data/last_brief.txt`` records today's date."""
    reference_date = today if today is not None else today_local()
    try:
        content = _LAST_BRIEF_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return False
    try:
        return date.fromisoformat(content) == reference_date
    except ValueError:
        return False


def _record_brief_sent(when: date) -> None:
    """Persist ``when`` as the last date the brief was delivered."""
    _LAST_BRIEF_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LAST_BRIEF_FILE.write_text(when.isoformat(), encoding="utf-8")


async def send_morning_brief(application: Application) -> None:
    """Build and send the morning brief to the bot owner.

    Called by the daily cron job and by the startup catch-up path. All
    failures are logged rather than raised, so a transient Telegram outage
    can't crash the scheduler.
    """
    try:
        local_day = today_local()
        text = build_morning_brief(MY_TELEGRAM_ID, today=local_day)
        await application.bot.send_message(
            chat_id=MY_TELEGRAM_ID,
            text=text,
            parse_mode=ParseMode.HTML,
        )
        _record_brief_sent(local_day)
        logger.info("Morning brief sent")
    except Exception:
        logger.exception("Morning brief job failed")


async def catch_up_missed_brief(application: Application) -> None:
    """If today's brief is overdue and not yet sent, fire it now.

    Short-circuits when ``MORNING_BRIEF_ENABLED`` is False so a disabled
    deploy doesn't pop a brief on startup.
    """
    if not MORNING_BRIEF_ENABLED:
        logger.info(
            "Morning brief is disabled (MORNING_BRIEF_ENABLED=false) — "
            "skipping catch-up."
        )
        return
    tz = _resolve_timezone()
    now = datetime.now(tz)
    if _brief_already_sent_today(now.date()):
        logger.info("Today's brief already sent — skipping catch-up.")
        return
    scheduled_today = now.replace(
        hour=BRIEF_HOUR, minute=BRIEF_MINUTE, second=0, microsecond=0
    )
    if now < scheduled_today:
        logger.info(
            "Current time %s is before today's scheduled brief at %02d:%02d — "
            "no catch-up needed.",
            now.strftime("%H:%M"),
            BRIEF_HOUR,
            BRIEF_MINUTE,
        )
        return
    logger.info(
        "Missed the %02d:%02d brief; sending catch-up now.",
        BRIEF_HOUR,
        BRIEF_MINUTE,
    )
    await send_morning_brief(application)


async def _log_heartbeat() -> None:
    """Emit a periodic 'alive' log line for deployed-bot liveness checks.

    With the bot running as a systemd service on EC2 there is no public HTTP
    endpoint to poll, so the journal is the health signal — read it with
    ``journalctl -u task-bot -f``. A steady ``heartbeat: alive`` every
    ``_HEARTBEAT_INTERVAL_MINUTES`` means the asyncio loop is running and the
    scheduler is servicing jobs.
    """
    logger.info("heartbeat: alive")


def build_scheduler(application: Application) -> AsyncIOScheduler:
    """Construct the AsyncIOScheduler with the daily brief and heartbeat jobs.

    The caller is responsible for ``start()`` and ``shutdown()``.
    ``misfire_grace_time`` on the brief is set to an hour so that if the bot
    was paused at the scheduled moment (e.g. laptop sleep, a ``systemctl
    restart`` on the EC2 host) but wakes within an hour, the morning brief
    still fires.
    """
    tz = _resolve_timezone()
    scheduler = AsyncIOScheduler(timezone=tz)
    if MORNING_BRIEF_ENABLED:
        scheduler.add_job(
            send_morning_brief,
            trigger=CronTrigger(
                hour=BRIEF_HOUR, minute=BRIEF_MINUTE, timezone=tz
            ),
            args=(application,),
            id=_JOB_ID,
            replace_existing=True,
            misfire_grace_time=3600,
        )
    else:
        logger.info(
            "Morning brief disabled (MORNING_BRIEF_ENABLED=false) — "
            "cron not installed. /brief still works on demand."
        )
    # Dashboard rollover. Unconditional: a chat with a registered dashboard
    # must see "34 days away" become "33 days away" even when the morning
    # brief is switched off.
    scheduler.add_job(
        refresh_all_deadline_dashboards,
        trigger=CronTrigger(
            hour=_DEADLINE_REFRESH_HOUR,
            minute=_DEADLINE_REFRESH_MINUTE,
            timezone=tz,
        ),
        args=(application,),
        id=_DEADLINE_REFRESH_JOB_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        _log_heartbeat,
        trigger=IntervalTrigger(minutes=_HEARTBEAT_INTERVAL_MINUTES),
        id=_HEARTBEAT_JOB_ID,
        replace_existing=True,
    )
    return scheduler
