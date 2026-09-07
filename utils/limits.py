"""Shared limits for user input and Telegram transport payloads."""
from __future__ import annotations


# Telegram limits message text after entity parsing and callback data in UTF-8
# bytes.  We deliberately count the raw HTML message, which is conservative:
# stripping markup can only make the delivered text shorter.
TELEGRAM_MESSAGE_MAX_UNITS: int = 4096
TELEGRAM_CALLBACK_DATA_MAX_BYTES: int = 64
TELEGRAM_CALLBACK_ANSWER_MAX_UNITS: int = 200

TITLE_MAX_LENGTH: int = 120
MODULE_CODE_MAX_LENGTH: int = 20
MODULE_CODE_MAX_BYTES: int = 40
MODULE_NAME_MAX_LENGTH: int = 100
NOTES_MAX_LENGTH: int = 500


def telegram_text_units(text: str) -> int:
    """Return Telegram's UTF-16 code-unit length for ``text``."""
    return len(text.encode("utf-16-le")) // 2


def fits_telegram_message(text: str) -> bool:
    """Return whether ``text`` is safely within Telegram's message limit."""
    return telegram_text_units(text) <= TELEGRAM_MESSAGE_MAX_UNITS


def fits_callback_data(data: str) -> bool:
    """Return whether callback data fits Telegram's 64-byte UTF-8 limit."""
    return len(data.encode("utf-8")) <= TELEGRAM_CALLBACK_DATA_MAX_BYTES


def shorten_text(text: str | None, maximum: int) -> str:
    """Bound display text, adding an ellipsis when legacy data is oversized."""
    value = text or ""
    if len(value) <= maximum:
        return value
    return value[: maximum - 1] + "…"


def field_length_error(
    value: str,
    *,
    label: str,
    maximum: int,
    maximum_bytes: int | None = None,
) -> str | None:
    """Return a user-facing validation error, or ``None`` when valid."""
    if len(value) > maximum:
        return f"{label} must be {maximum} characters or fewer."
    if maximum_bytes is not None and len(value.encode("utf-8")) > maximum_bytes:
        return f"{label} contains too many multi-byte characters."
    return None
