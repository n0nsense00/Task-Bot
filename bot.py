"""Task-Bot entry point.

Builds the Telegram ``Application``, registers handlers, starts the
APScheduler daily brief, and begins polling. Uses python-telegram-bot v22
(async). Shuts down cleanly on Ctrl+C — ``post_shutdown`` stops the
scheduler so no threads or asyncio tasks are orphaned.
"""
from __future__ import annotations

import logging

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from config import (
    CMD_BRIEF,
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
from handlers.basic import help_command, start
from handlers.brief import brief
from handlers.errors import error_handler, unknown_command
from handlers.tasks import (
    DELETE_CB_PREFIX,
    delete_callback,
    delete_task_cmd,
    done_task_cmd,
    semester,
    today,
    week,
)
from scheduler import build_scheduler, catch_up_missed_brief

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


async def _post_init(application: Application) -> None:
    """Start the scheduler, then send a catch-up brief if one was missed today."""
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
        .token(TELEGRAM_BOT_TOKEN)
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

    # Multi-step /add conversation (its own CommandHandler entry point).
    application.add_handler(build_add_conversation())

    # Inline-keyboard callback for the /delete confirmation.
    application.add_handler(
        CallbackQueryHandler(delete_callback, pattern=f"^{DELETE_CB_PREFIX}:")
    )

    # Catch-all for unrecognised /commands — must come AFTER all known commands,
    # because PTB runs the first matching handler in group 0.
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    # PTB-level safety net for anything that slips past @safe on individual
    # handlers — logs only, never replies, to avoid double-messaging.
    application.add_error_handler(error_handler)

    logger.info("Task-Bot starting (polling mode)")
    try:
        application.run_polling()
    except KeyboardInterrupt:
        pass
    logger.info("Task-Bot shutting down")


if __name__ == "__main__":
    main()
