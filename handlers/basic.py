"""Basic command handlers: ``/start``, ``/help``, ``/clear``.

``/start`` and ``/help`` introduce the deadline-focused workflow and explain
the inline actions available on ``/deadlines``.

``/clear`` wipes the visible chat history with the bot — both bot-sent
messages and the user's own commands and replies — by iterating through
the persistent SQLite tracker populated by :class:`utils.tracking_bot.TrackingBot`
plus the group-1 incoming-message tracker. Telegram's 48-hour
deletion window means anything older than that can't be removed; the
confirmation message reports the count and self-deletes after 5 seconds.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from telegram import Message, Update
from telegram.constants import ChatType, MessageEntityType, ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter
from telegram.ext import ContextTypes

from config import CMD_CLEAR
from database.db import (
    delete_deadline_dashboard,
    delete_tracked_messages,
    get_deadline_dashboard_message_id,
    list_tracked_messages,
    save_tracked_message,
)
from utils.auth import admin_only, authorized_only, is_supported_chat
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
        "🎓 <b>Semester Deadline Tracker</b>",
        "<i>Keep every assessed deadline in one chronological view.</i>",
        "",
        DIVIDER,
        "",
        "<b>📅 Your dashboard</b>",
        "• /deadlines — all upcoming deadlines, sorted by urgency",
        "",
        "<b>✏️ Add &amp; manage</b>",
        "• /add — add a quiz, lab, assignment, project, midterm, or final",
        "• /done &lt;id&gt; — mark a deadline complete",
        "• /delete &lt;id&gt; — delete a deadline",
        "• /brief — preview deadlines due soon",
        "",
        DIVIDER,
        "",
        todays_tip(),
    ]
    return "\n".join(sections)


def _help_message() -> str:
    """Build the /help reference message — more detail than /start."""
    sections = [
        "📖 <b>Deadline Tracker Commands</b>",
        "",
        DIVIDER,
        "",
        "<b>📅 View</b>",
        "• /deadlines — every pending assessed item in one list, "
        "soonest first",
        "",
        "<b>✏️ Manage</b>",
        "• /add — guided flow: type → module → title → date → time → notes",
        "• /done &lt;id&gt; — complete a deadline; e.g. <code>/done 24</code>",
        "• /delete &lt;id&gt; — delete a deadline (asks confirmation); "
        "e.g. <code>/delete 24</code>",
        "• /cancel — abort an /add or /edit flow",
        "• /brief — preview today's and near-term deadlines",
        "",
        "<b>🧹 Maintenance</b>",
        "• /clear — wipe this chat (your messages + bot replies, last 48h)",
        "",
        "<b>🎮 Inline Buttons</b>",
        "On /deadlines, tap ⚙️ Manage deadlines, choose an item, then:",
        "• ✅ Complete",
        "• 📝 Edit",
        "• 🗑️ Delete (with confirmation)",
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
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None:
        return
    logger.info(
        "/start received: chat_id=%s chat_type=%s user_id=%s",
        chat.id,
        chat.type,
        getattr(user, "id", None),
    )
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
_TELEGRAM_DELETE_WINDOW: timedelta = timedelta(hours=48)


def _message_is_permanently_gone(error: BadRequest) -> bool:
    """Return whether Telegram says the target message no longer exists."""
    detail = str(error).lower()
    return any(
        marker in detail
        for marker in (
            "message to delete not found",
            "message to edit not found",
            "message not found",
            "message_id_invalid",
            "message identifier is not specified",
        )
    )


@admin_only
@safe
async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/clear``: delete every tracked bot/user message in this chat.

    Loads the chat-scoped persistent rows and calls ``bot.delete_message`` for
    each recent candidate. Rows older than Telegram's 48-hour deletion window
    are pruned without a guaranteed-to-fail API request. The /clear command's
    own message is explicitly added to the deletion list before iterating so
    it disappears on the first call (the group-1 tracker would otherwise only
    add it AFTER /clear runs, requiring two calls to clean up).
    """
    chat = update.effective_chat
    if chat is None:
        return
    chat_id = chat.id

    tracked_memory = get_tracked_messages(context.bot)
    tracked_rows = list_tracked_messages(chat_id)
    cutoff = datetime.now(timezone.utc) - _TELEGRAM_DELETE_WINDOW
    tracked_dates = {message_id: created_at for message_id, created_at in tracked_rows}
    expired_ids = {
        message_id
        for message_id, created_at in tracked_rows
        if created_at <= cutoff
    }
    if expired_ids:
        delete_tracked_messages(chat_id, list(expired_ids))

    to_delete: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for message_id, _created_at in tracked_rows:
        if message_id in expired_ids:
            continue
        key = (chat_id, message_id)
        if key not in seen:
            seen.add(key)
            to_delete.append(key)

    # A dashboard registration may predate the persistent message-tracker
    # migration. Include it explicitly unless its tracked timestamp proves it
    # is already outside Telegram's deletion window.
    dashboard_id = get_deadline_dashboard_message_id(chat_id)
    if dashboard_id is not None and dashboard_id not in expired_ids:
        dashboard = (chat_id, dashboard_id)
        if dashboard not in seen:
            seen.add(dashboard)
            to_delete.append(dashboard)

    # The /clear command itself isn't yet in tracked_messages (group-1
    # tracker runs AFTER this handler). Add it explicitly so it gets
    # deleted on this very call.
    if update.message is not None:
        own = (chat_id, update.message.message_id)
        if own not in seen:
            seen.add(own)
            to_delete.append(own)

    deleted = 0
    permanently_unavailable = len(expired_ids)
    retryable = 0
    deleted_ids: set[int] = set()
    gone_ids: set[int] = set()
    forget_ids: set[int] = set(expired_ids)
    for c, m in to_delete:
        try:
            await context.bot.delete_message(chat_id=c, message_id=m)
            deleted += 1
            deleted_ids.add(m)
            forget_ids.add(m)
        except BadRequest as exc:
            # Invalid/already-gone/too-old message ids cannot become deletable
            # on retry. Forget their tracker rows. Only drop a dashboard
            # registration when Telegram specifically says the message is gone;
            # an old dashboard can still be edited even though it cannot be
            # deleted.
            permanently_unavailable += 1
            forget_ids.add(m)
            if _message_is_permanently_gone(exc):
                gone_ids.add(m)
            logger.info(
                "Telegram permanently rejected deleting message %s in chat %s: %s",
                m,
                c,
                exc,
            )
        except Forbidden as exc:
            permanently_unavailable += 1
            forget_ids.add(m)
            logger.warning(
                "No permission to delete message %s in chat %s: %s", m, c, exc
            )
        except (NetworkError, RetryAfter) as exc:
            # Preserve the row so the next /clear can retry after a transient
            # Telegram/network failure. Legacy dashboards do not yet have a
            # row, so adopt them here for the retry path.
            retryable += 1
            if m not in tracked_dates:
                save_tracked_message(c, m)
            logger.warning(
                "Temporary failure deleting message %s in chat %s: %s", m, c, exc
            )
        except Exception:
            # Unknown failures may be temporary. Keeping the row is safer than
            # silently losing the only reference after a deployment restart.
            retryable += 1
            if m not in tracked_dates:
                save_tracked_message(c, m)
            logger.exception(
                "Unexpected failure deleting message %s in chat %s", m, c
            )

    if forget_ids:
        delete_tracked_messages(chat_id, list(forget_ids))

    # Only forget the persistent dashboard if it was ACTUALLY removed. A
    # dashboard Telegram refused to delete (older than 48h) still exists and
    # stays perfectly editable, so its registration must survive.
    if dashboard_id is not None and dashboard_id in (deleted_ids | gone_ids):
        delete_deadline_dashboard(chat_id)
        logger.info(
            "Dashboard message %s deleted by /clear — registration dropped",
            dashboard_id,
        )

    # Keep the legacy in-memory cache aligned with SQLite. Other chats are
    # untouched; retryable rows in this chat remain visible to old callers.
    retained_ids = {message_id for message_id, _ in list_tracked_messages(chat_id)}
    retained_memory = [
        (c, m)
        for c, m in tracked_memory
        if c != chat_id or m in retained_ids
    ]
    for message_id in retained_ids:
        pair = (chat_id, message_id)
        if pair not in retained_memory:
            retained_memory.append(pair)
    replace_tracked_messages(context.bot, retained_memory)

    # Send a self-deleting confirmation. Skip tracking it (otherwise the
    # next /clear would inherit a stale id).
    set_skip_tracking(context.bot, True)
    try:
        text = f"🧹 Cleared {deleted} message{'s' if deleted != 1 else ''}."
        if permanently_unavailable:
            text += (
                f"  <i>{permanently_unavailable} couldn't be removed "
                f"(older than 48h or already gone).</i>"
            )
        if retryable:
            text += (
                f"  <i>{retryable} temporary failure"
                f"{'s' if retryable != 1 else ''} will be retried next time.</i>"
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
    if not is_supported_chat(update):
        return

    bot = context.bot
    if not _is_bot_related_message(
        message,
        bot_id=bot.id,
        bot_username=bot.username or "",
    ):
        return

    # /clear handles its own command message immediately. The group-1 tracker
    # runs afterwards, so recording it here would resurrect a row for a message
    # that was just deleted.
    first_token = (message.text or "").split(maxsplit=1)[0].lower()
    if first_token.split("@", maxsplit=1)[0] == f"/{CMD_CLEAR}":
        return

    append_tracked_message(
        bot,
        chat.id,
        message.message_id,
        created_at=message.date,
    )
