"""Basic command handlers: ``/start``, ``/help``, ``/clear``.

``/start`` and ``/help`` produce a sectioned command list with a rotated
daily tip in the footer. ``/help`` adds a section describing the inline-
keyboard buttons available on ``/today`` so the user discovers them
without having to tap around blindly.

``/clear`` wipes the visible chat history with the bot — both bot-sent
messages and the user's own commands and replies — by iterating through
the tracked-message list maintained by :class:`utils.tracking_bot.TrackingBot`
plus the group-1 incoming-message tracker. Telegram's 48-hour
deletion window means anything older than that can't be removed; the
confirmation message reports the count and self-deletes after 5 seconds.
"""
from __future__ import annotations

import asyncio
import logging

from telegram import Message, Update
from telegram.constants import ChatType, MessageEntityType, ParseMode
from telegram.ext import ContextTypes

from utils.auth import admin_only, authorized_only
from utils.errors import safe
from utils.format import DIVIDER, todays_tip
from utils.tracking_bot import (
    append_tracked_message,
    get_tracked_messages,
    replace_tracked_messages,
    set_skip_tracking,
)

logger = logging.getLogger(__name__)


def _start_message() -> str:
    """Build the /start welcome message with sectioned command list + tip."""
    sections = [
        "☀️ <b>Welcome to Task-Bot</b>",
        "<i>Your personal university tasks tracker.</i>",
        "",
        DIVIDER,
        "",
        "<b>📅 View</b>",
        "• /today — tasks due today",
        "• /week — this week's lectures + next week's tutorials",
        "• /semester — midterms &amp; finals",
        "",
        "<b>✏️ Manage</b>",
        "• /add — create a new task (interactive)",
        "• /done &lt;id&gt; — mark a task complete",
        "• /delete &lt;id&gt; — delete a task",
        "• /brief — today's morning summary on demand",
        "",
        DIVIDER,
        "",
        todays_tip(),
    ]
    return "\n".join(sections)


def _help_message() -> str:
    """Build the /help reference message — more detail than /start."""
    sections = [
        "📖 <b>Task-Bot Commands</b>",
        "",
        DIVIDER,
        "",
        "<b>📅 View</b>",
        "• /today — tasks due today, with quick-action buttons",
        "• /week — this week's lectures + next week's tutorials",
        "• /semester — upcoming midterms and finals (auto-cleans past ones)",
        "",
        "<b>✏️ Manage</b>",
        "• /add — create a task (tap-driven: type → module → date → time → week)",
        "• /done &lt;id&gt; — mark a task done; e.g. <code>/done 24</code>",
        "• /delete &lt;id&gt; — delete a task (asks confirmation); "
        "e.g. <code>/delete 24</code>",
        "• /cancel — abort an /add or /edit flow",
        "• /brief — send today's morning summary now",
        "",
        "<b>🧹 Maintenance</b>",
        "• /clear — wipe this chat (your messages + bot replies, last 48h)",
        "",
        "<b>🎮 Inline Buttons</b>",
        "On /today, tap:",
        "• ✅ to mark a task complete",
        "• 📝 to edit any field (pickers for module, date, time, week)",
        "• 🗑️ to delete (with confirmation)",
        "",
        DIVIDER,
        "",
        todays_tip(),
    ]
    return "\n".join(sections)


@authorized_only
@safe
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/start``: greet the owner with a sectioned command list."""
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(_start_message(), parse_mode=ParseMode.HTML)


@authorized_only
@safe
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/help``: reference message with examples and inline-button cheat sheet."""
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(_help_message(), parse_mode=ParseMode.HTML)


_CLEAR_CONFIRMATION_TTL_SECONDS: int = 5


@admin_only
@safe
async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/clear``: delete every tracked bot/user message in this chat.

    Iterates the ``tracked_messages`` list (chat-scoped) and calls
    ``bot.delete_message`` for each. Telegram only allows deletion within 48h,
    so older entries fail silently — the count of failures is reported in
    the self-deleting confirmation. The /clear command's own message is
    explicitly added to the deletion list before iterating so it disappears
    on the first call (the group-1 tracker would otherwise only add it
    AFTER /clear runs, requiring two calls to clean up).
    """
    chat = update.effective_chat
    if chat is None:
        return
    chat_id = chat.id

    tracked = get_tracked_messages(context.bot)
    to_delete: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for c, m in tracked:
        if c != chat_id:
            continue
        key = (c, m)
        if key in seen:
            continue
        seen.add(key)
        to_delete.append(key)

    # The /clear command itself isn't yet in tracked_messages (group-1
    # tracker runs AFTER this handler). Add it explicitly so it gets
    # deleted on this very call.
    if update.message is not None:
        own = (chat_id, update.message.message_id)
        if own not in seen:
            seen.add(own)
            to_delete.append(own)

    deleted = 0
    failed = 0
    for c, m in to_delete:
        try:
            await context.bot.delete_message(chat_id=c, message_id=m)
            deleted += 1
        except Exception:
            # >48h old, already deleted, or otherwise un-deletable.
            failed += 1

    # Drop this chat's entries from tracking — succeeded or not, we shouldn't
    # keep retrying these.
    replace_tracked_messages(context.bot, [
        (c, m) for c, m in tracked if c != chat_id
    ])

    # Send a self-deleting confirmation. Skip tracking it (otherwise the
    # next /clear would inherit a stale id).
    set_skip_tracking(context.bot, True)
    try:
        text = f"🧹 Cleared {deleted} message{'s' if deleted != 1 else ''}."
        if failed:
            text += (
                f"  <i>{failed} couldn't be removed "
                f"(older than 48h or already gone).</i>"
            )
        confirmation = await context.bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML
        )
    finally:
        set_skip_tracking(context.bot, False)

    async def _delete_confirmation_later() -> None:
        """Auto-delete the /clear confirmation after the TTL elapses."""
        await asyncio.sleep(_CLEAR_CONFIRMATION_TTL_SECONDS)
        try:
            await context.bot.delete_message(
                chat_id=chat_id, message_id=confirmation.message_id
            )
        except Exception:
            # Best-effort; confirmation may have aged out or user deleted it.
            pass

    application = context.application
    if application is not None:
        application.create_task(_delete_confirmation_later())


def _is_bot_related_message(message: Message, bot_id: int, bot_username: str) -> bool:
    """Return True if ``message`` constitutes interaction with this bot.

    Bounds what /clear is allowed to touch. Without this filter, an admin
    bot in a group would receive every member's message (Telegram disables
    privacy mode for admin bots), the tracker would record all of them,
    and /clear would nuke 48h of unrelated chitchat. The four conditions:

    1. Private (DM) chat — the entire conversation is with the bot.
    2. Slash command directed at this bot — either untargeted ("/today")
       or explicitly targeted ("/today@CharlieKirkBot"). Commands carrying
       a different bot's @suffix are ignored (they belong to that bot).
    3. Reply to a message previously sent by this bot — typical follow-up
       interaction with one of the bot's cards.
    4. Free-text message that @-mentions this bot's username.

    Captions (on photo/video/document messages) are intentionally NOT
    inspected: PTB's CommandHandler only matches commands in ``text``, so
    a "/add" inside a caption isn't actually treated as a command and
    shouldn't be tracked as if it were.
    """
    chat = message.chat
    if chat is None:
        return False

    # (1) DMs with the bot — every message is bot-related by definition.
    if chat.type == ChatType.PRIVATE:
        return True

    text = message.text or ""

    # (2) Slash command targeted at us (or untargeted, which Telegram
    # delivers to all bots in the chat — still legitimately ours).
    if text.startswith("/"):
        first_token = text.split(maxsplit=1)[0]
        if "@" not in first_token:
            return True
        if bot_username and first_token.lower().endswith(
            f"@{bot_username.lower()}"
        ):
            return True

    # (3) Reply to one of our messages.
    reply = message.reply_to_message
    if (
        reply is not None
        and reply.from_user is not None
        and reply.from_user.id == bot_id
    ):
        return True

    # (4) @-mention of the bot's username inside the message text.
    if message.entities and bot_username:
        for entity in message.entities:
            if entity.type != MessageEntityType.MENTION:
                continue
            mentioned = text[entity.offset : entity.offset + entity.length]
            if mentioned.lstrip("@").lower() == bot_username.lower():
                return True

    return False


async def track_incoming_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Group-1 catch-all: record bot-related incoming messages for /clear.

    Registered in :func:`bot.main` at ``group=1`` so it runs *after* every
    group-0 handler for the same update. This means /clear (which lives in
    group 0) can read a tracked-list that excludes its own command — the
    /clear handler manually adds its own id before deletion.

    Filters via :func:`_is_bot_related_message` so /clear stays bounded
    even if the bot is later promoted to admin in a shared group (where
    Telegram would otherwise feed it every member's message).
    """
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    bot = context.bot
    if not _is_bot_related_message(
        message,
        bot_id=bot.id,
        bot_username=bot.username or "",
    ):
        return

    append_tracked_message(bot, chat.id, message.message_id)
