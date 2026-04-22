"""Task-Bot entry point.

Builds the Telegram ``Application``, registers handlers, and starts polling.
Uses python-telegram-bot v22 (async). Shuts down cleanly on Ctrl+C.
"""
from __future__ import annotations

import logging

from telegram.ext import Application, CommandHandler

from config import CMD_HELP, CMD_START, TELEGRAM_BOT_TOKEN
from database.db import init_db
from handlers.basic import help_command, start


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


def main() -> None:
    """Build the Application, register handlers, and run until interrupted."""
    _configure_logging()
    logger = logging.getLogger(__name__)

    init_db()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler(CMD_START, start))
    application.add_handler(CommandHandler(CMD_HELP, help_command))

    logger.info("Task-Bot starting (polling mode)")
    try:
        application.run_polling()
    except KeyboardInterrupt:
        pass
    logger.info("Task-Bot shutting down")


if __name__ == "__main__":
    main()
