"""Per-chat state helpers for user-scoped Telegram conversations.

``python-telegram-bot`` shares ``context.user_data`` across every chat for a
given Telegram user.  ConversationHandler itself is chat-aware, but values
stored directly in ``user_data`` are not.  These helpers add the missing
``chat_id`` level and coordinate mutually exclusive flows within one chat.
"""
from __future__ import annotations

from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

_ACTIVE_FLOWS_KEY: str = "active_conversation_flows"


def _chat_id(update: Update) -> int | None:
    """Return the effective chat id, or ``None`` for chat-less updates."""
    chat = update.effective_chat
    return chat.id if chat is not None else None


def _per_chat_values(
    context: ContextTypes.DEFAULT_TYPE,
    key: str,
    *,
    create: bool,
) -> dict[int, Any] | None:
    """Return a mapping stored under ``key``, optionally creating it."""
    values = context.user_data.get(key)
    if isinstance(values, dict):
        return values
    if not create:
        return None
    values = {}
    context.user_data[key] = values
    return values


def get_chat_value(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    key: str,
) -> Any | None:
    """Read ``key``'s value for the update's chat without creating state."""
    chat_id = _chat_id(update)
    values = _per_chat_values(context, key, create=False)
    if chat_id is None or values is None:
        return None
    return values.get(chat_id)


def set_chat_value(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    key: str,
    value: Any,
) -> bool:
    """Store ``value`` under the update's chat; return False without a chat."""
    chat_id = _chat_id(update)
    if chat_id is None:
        return False
    values = _per_chat_values(context, key, create=True)
    assert values is not None
    values[chat_id] = value
    return True


def clear_chat_value(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    key: str,
) -> None:
    """Remove only the current chat's value, preserving all other chats."""
    chat_id = _chat_id(update)
    values = _per_chat_values(context, key, create=False)
    if chat_id is None or values is None:
        return
    values.pop(chat_id, None)
    if not values:
        context.user_data.pop(key, None)


def begin_chat_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    flow: str,
) -> str | None:
    """Claim the current chat for ``flow`` and return any conflicting flow.

    Re-entering the same flow is allowed so a stale draft can be restarted.
    A different active flow is left untouched and returned to the caller.
    """
    chat_id = _chat_id(update)
    if chat_id is None:
        return None
    flows = _per_chat_values(context, _ACTIVE_FLOWS_KEY, create=True)
    assert flows is not None
    active = flows.get(chat_id)
    if active is not None and active != flow:
        return str(active)
    flows[chat_id] = flow
    return None


def finish_chat_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    flow: str,
) -> None:
    """Release ``flow`` in this chat without disturbing another chat/flow."""
    chat_id = _chat_id(update)
    flows = _per_chat_values(context, _ACTIVE_FLOWS_KEY, create=False)
    if chat_id is None or flows is None or flows.get(chat_id) != flow:
        return
    flows.pop(chat_id, None)
    if not flows:
        context.user_data.pop(_ACTIVE_FLOWS_KEY, None)
