"""Task-Bot entry point.

Builds the Telegram ``Application``, registers handlers, starts the
APScheduler daily brief, and begins polling. Uses python-telegram-bot v22
(async).

Shutdown signals: PTB's ``run_polling`` installs handlers for SIGINT, SIGTERM,
and SIGABRT by default on POSIX (Windows gets SIGINT only, since the OS lacks
the others). SIGTERM is what Railway sends on redeploy/restart, so graceful
cleanup — including the ``post_shutdown`` hook that stops the scheduler —
runs automatically without any explicit signal wiring.
"""
from __future__ import annotations

import logging
import os
import subprocess

from telegram import BotCommand
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from config import (
    ALLOWED_CHAT_ID,
    CMD_ADD,
    CMD_BRIEF,
    CMD_CANCEL,
    CMD_CLEAR,
    CMD_DELETE,
    CMD_DONE,
    CMD_HELP,
    CMD_SEMESTER,
    CMD_START,
    CMD_TODAY,
    CMD_WEEK,
    TELEGRAM_BOT_TOKEN,
)
from database.db import init_db
from handlers.add_task import build_add_conversation
from handlers.basic import (
    clear,
    help_command,
    start,
    track_incoming_message,
)
from handlers.brief import brief
from handlers.callbacks import (
    delete_confirm_callback,
    delete_request_callback,
    done_callback,
)
from handlers.edit_task import build_edit_conversation
from handlers.errors import error_handler, unknown_command
from handlers.tasks import (
    delete_task_cmd,
    done_task_cmd,
    semester,
    today,
    week,
)
from scheduler import build_scheduler, catch_up_missed_brief
from utils.tracking_bot import TrackingBot

logger = logging.getLogger(__name__)

_SCHEDULER_KEY: str = "scheduler"


def _configure_logging() -> None:
    """Configure root logging: INFO to stderr with timestamp, level, and name."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # httpx logs every long-poll request at INFO and the URL contains the bot
    # token — silence it to WARNING so the token never reaches the console/logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # APScheduler is chatty at INFO during job-missed scenarios; keep it readable.
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def _get_build_identifier() -> str:
    """Return a short identifier for the running build, for startup logs.

    Order of precedence:
      1. ``RAILWAY_GIT_COMMIT_SHA`` env var (set automatically on Railway builds),
         truncated to 7 chars.
      2. Local ``git rev-parse --short HEAD`` if a git repo is available.
      3. The string ``'unknown'`` — should never hit in normal dev/deploy.
    """
    railway_sha = os.getenv("RAILWAY_GIT_COMMIT_SHA")
    if railway_sha:
        return railway_sha[:7]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "unknown"


def _bot_command_menu() -> list[BotCommand]:
    """Return the list registered with Telegram as the '/' suggestion menu.

    Telegram clients show this list when the user taps the '/' button or
    types '/' in the chat — eliminates the need to remember command names.
    """
    return [
        BotCommand(CMD_TODAY, "Tasks due today"),
        BotCommand(CMD_WEEK, "This week + next week"),
        BotCommand(CMD_SEMESTER, "Upcoming midterms & finals"),
        BotCommand(CMD_BRIEF, "Send today's morning summary now"),
        BotCommand(CMD_ADD, "Add a new task"),
        BotCommand(CMD_DONE, "Mark a task done by id"),
        BotCommand(CMD_DELETE, "Delete a task by id"),
        BotCommand(CMD_CLEAR, "Wipe this chat (last 48h)"),
        BotCommand(CMD_CANCEL, "Cancel current /add or /edit flow"),
        BotCommand(CMD_HELP, "Show command list"),
        BotCommand(CMD_START, "Welcome message"),
    ]


async def _post_init(application: Application) -> None:
    """Register Telegram command menu and start scheduler."""
    logger.info("Message tracker active (TrackingBot.send_message override)")

    try:
        await application.bot.set_my_commands(_bot_command_menu())
        logger.info("Telegram '/' command menu registered")
    except Exception:
        # Non-fatal — the menu is a UX nicety, not load-bearing.
        logger.exception("Failed to register Telegram command menu")

    scheduler = build_scheduler(application)
    scheduler.start()
    application.bot_data[_SCHEDULER_KEY] = scheduler

    job = scheduler.get_job("morning_brief")
    if job is not None:
        logger.info(
            "Scheduler started — next morning brief at %s", job.next_run_time
        )

    await catch_up_missed_brief(application)


async def _post_shutdown(application: Application) -> None:
    """Stop the scheduler cleanly so no asyncio tasks outlive the bot."""
    scheduler = application.bot_data.get(_SCHEDULER_KEY)
    if scheduler is not None:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


def main() -> None:
    """Build the Application, register handlers, and run until interrupted."""
    _configure_logging()

    init_db()

    application = (
        Application.builder()
        .bot(TrackingBot(token=TELEGRAM_BOT_TOKEN))
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # Direct commands first so they take precedence over the catch-all below.
    application.add_handler(CommandHandler(CMD_START, start))
    application.add_handler(CommandHandler(CMD_HELP, help_command))
    application.add_handler(CommandHandler(CMD_TODAY, today))
    application.add_handler(CommandHandler(CMD_WEEK, week))
    application.add_handler(CommandHandler(CMD_SEMESTER, semester))
    application.add_handler(CommandHandler(CMD_BRIEF, brief))
    application.add_handler(CommandHandler(CMD_DONE, done_task_cmd))
    application.add_handler(CommandHandler(CMD_DELETE, delete_task_cmd))
    application.add_handler(CommandHandler(CMD_CLEAR, clear))

    # Conversation handlers — must register BEFORE bare CallbackQueryHandlers
    # so their state-scoped callbacks claim updates ahead of the global ones.
    application.add_handler(build_add_conversation())
    application.add_handler(build_edit_conversation())

    # Inline-keyboard callbacks attached to /today task buttons. Patterns are
    # anchored with ``^`` and ``$`` so the colon-count disambiguates between
    # ``del:N`` (entry → confirmation prompt) and ``del:yes:N`` /
    # ``del:no:N`` (confirmation answer).
    application.add_handler(
        CallbackQueryHandler(done_callback, pattern=r"^done:\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(delete_request_callback, pattern=r"^del:\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(
            delete_confirm_callback, pattern=r"^del:(yes|no):\d+$"
        )
    )

    # Catch-all for unrecognised /commands — must come AFTER all known commands,
    # because PTB runs the first matching handler in group 0.
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    # Group-1 message tracker for /clear: records every incoming message ID.
    # Lives in a separate group so it runs alongside (not in competition with)
    # the group-0 handlers above.
    application.add_handler(
        MessageHandler(filters.ALL, track_incoming_message), group=1
    )

    # PTB-level safety net for anything that slips past @safe on individual
    # handlers — logs only, never replies, to avoid double-messaging.
    application.add_error_handler(error_handler)

    logger.info(
        "Task-Bot starting (polling mode, build=%s)", _get_build_identifier()
    )
    if ALLOWED_CHAT_ID == 0:
        logger.warning(
            "ALLOWED_CHAT_ID is unset — bot is in DISCOVERY MODE: every "
            "incoming message will be rejected and its chat id logged. Add "
            "the bot to the target group, send a message, copy the chat_id "
            "from the next 'Unauthorized chat' warning into the env var, and "
            "restart."
        )
    try:
        application.run_polling()
    except KeyboardInterrupt:
        pass
    logger.info("Task-Bot shutting down")


if __name__ == "__main__":
    main()
