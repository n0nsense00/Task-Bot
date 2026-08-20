"""Tests for the central persistent-dashboard refresh path.

Covers :func:`handlers.tasks.refresh_deadline_dashboard` and
:func:`handlers.tasks.refresh_all_deadline_dashboards`: what gets sent to
Telegram, and — more importantly — which Telegram failures drop the
``deadline_dashboards`` registration and which ones keep it so a later refresh
can retry.

Isolation notes:

* ``TASK_BOT_DB_PATH`` is pointed at a throwaway file in a per-run temp dir
  *before* ``database.db`` is imported, and both ``database.db`` and
  ``handlers.tasks`` are reloaded afterwards (the latter does
  ``from database.db import ...``, so it must be rebound whenever the data
  layer is reloaded). Every test asserts the active DB path is that temp file,
  so the real ``data/tasks.db`` can never be touched.
* No network: the ``Application`` is a ``Mock`` whose ``bot.edit_message_text``
  is an ``AsyncMock``.
* ``handlers.tasks.today_local`` is patched during every refresh so the
  rendered text is deterministic and comparable to a locally rendered
  expectation.
"""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

# ---------------------------------------------------------------------------
# Database isolation MUST happen before the first project import: database.db
# resolves DB_PATH once, at import time.
# ---------------------------------------------------------------------------
_TEMP_DIR: str = tempfile.mkdtemp(prefix="taskbot-test-refresh-")
_TEMP_DB: Path = Path(_TEMP_DIR) / "refresh-dashboards.db"
os.environ["TASK_BOT_DB_PATH"] = str(_TEMP_DB)
# config.py fails fast on missing secrets and handlers.tasks imports it via
# utils.clock. setdefault so a developer's real .env values are left alone.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0000000000:test-token-not-real")
os.environ.setdefault("MY_TELEGRAM_ID", "424242")

from telegram import InlineKeyboardMarkup  # noqa: E402
from telegram.constants import ParseMode  # noqa: E402
from telegram.error import (  # noqa: E402
    BadRequest,
    Forbidden,
    TelegramError,
    TimedOut,
)

import database.db as db  # noqa: E402
import handlers.tasks as tasks  # noqa: E402
from database.models import Task  # noqa: E402

# Another test module in the same run may already have imported database.db
# (possibly with a different path). Reloading re-reads TASK_BOT_DB_PATH;
# handlers.tasks is reloaded straight after so its ``from database.db import
# ...`` names point at the reloaded data layer.
importlib.reload(db)
importlib.reload(tasks)

_EXPECTED_DB_PATH: Path = Path(str(_TEMP_DB)).expanduser().resolve()

# Fixed "today" for every render, so expected text is reproducible.
TODAY: date = date(2026, 3, 2)

CHAT_A: int = -1002000000001
CHAT_B: int = -1002000000002
CHAT_C: int = -1002000000003
MSG_A: int = 5001
MSG_B: int = 5002
MSG_C: int = 5003


def tearDownModule() -> None:
    """Remove the temp database directory created for this module."""
    shutil.rmtree(_TEMP_DIR, ignore_errors=True)


class _RefreshTestBase(unittest.IsolatedAsyncioTestCase):
    """Shared fixtures: a clean temp DB and a network-free fake Application."""

    def setUp(self) -> None:
        # ``unittest discover`` imports every test module before running any
        # test, so a sibling module that points TASK_BOT_DB_PATH at its own
        # temp file and reloads database.db can leave DB_PATH pointing
        # elsewhere by the time these tests run. Re-point it here (DB_PATH is
        # read from the module globals on every connection, so a plain
        # assignment is enough and avoids reload side effects mid-run).
        if db.DB_PATH != _EXPECTED_DB_PATH:
            os.environ["TASK_BOT_DB_PATH"] = str(_TEMP_DB)
            db.DB_PATH = _EXPECTED_DB_PATH
        # Proof of isolation — re-checked for every single test in this module.
        self.assertEqual(
            db.DB_PATH,
            _EXPECTED_DB_PATH,
            "tests must run against the temp database, never the real one",
        )
        if db.DB_PATH.exists():
            db.DB_PATH.unlink()
        db.init_db()
        self.assertEqual(db.list_deadline_dashboards(), [])

    # -- fixtures ---------------------------------------------------------

    def make_application(self, side_effect=None) -> Mock:
        """Return a fake Application whose bot never touches the network."""
        application = Mock(name="application")
        application.bot.edit_message_text = AsyncMock(
            name="edit_message_text", side_effect=side_effect
        )
        return application

    def add_deadline(
        self,
        chat_id: int,
        title: str = "Lab Quiz 2",
        days_ahead: int = 5,
        task_type: str = "quiz",
        module_code: str | None = "CS1010",
    ) -> int:
        """Insert one pending deadline for ``chat_id`` and return its id."""
        return db.add_task(
            Task(
                title=title,
                task_type=task_type,
                due_date=TODAY + timedelta(days=days_ahead),
                chat_id=chat_id,
                module_code=module_code,
            )
        )

    def register(self, chat_id: int, message_id: int) -> None:
        """Register ``message_id`` as ``chat_id``'s live dashboard."""
        db.save_deadline_dashboard(chat_id, message_id)
        self.assertEqual(
            db.get_deadline_dashboard_message_id(chat_id), message_id
        )

    # -- helpers ----------------------------------------------------------

    async def refresh(self, application: Mock, chat_id: int) -> bool:
        """Run a single-chat refresh with a frozen local date."""
        with patch.object(tasks, "today_local", return_value=TODAY):
            return await tasks.refresh_deadline_dashboard(application, chat_id)

    async def refresh_all(self, application: Mock) -> None:
        """Run the fan-out refresh with a frozen local date."""
        with patch.object(tasks, "today_local", return_value=TODAY):
            return await tasks.refresh_all_deadline_dashboards(application)

    def expected_render(self, chat_id: int):
        """Render what the dashboard should currently show for ``chat_id``."""
        return tasks.render_deadlines(chat_id, TODAY)

    def assert_no_telegram_calls(self, application: Mock) -> None:
        """Assert nothing at all was invoked on the fake bot."""
        self.assertEqual(
            application.mock_calls,
            [],
            "no Telegram API call should have been made",
        )

    def edit_kwargs_by_chat(self, application: Mock) -> dict[int, dict]:
        """Map chat_id -> kwargs for every edit_message_text call made."""
        return {
            call.kwargs["chat_id"]: call.kwargs
            for call in application.bot.edit_message_text.await_args_list
        }


class TestRefreshPayload(_RefreshTestBase):
    """The happy path: exactly one correctly addressed edit."""

    def test_database_path_is_the_temp_file(self) -> None:
        self.assertEqual(db.DB_PATH, _EXPECTED_DB_PATH)
        self.assertEqual(db.DB_PATH.parent, Path(_TEMP_DIR).resolve())
        self.assertNotEqual(db.DB_PATH.name, "tasks.db")

    async def test_edits_registered_message_once_with_full_payload(self) -> None:
        self.add_deadline(CHAT_A, title="CS1010 Lab Quiz 2", days_ahead=4)
        self.register(CHAT_A, MSG_A)
        expected_text, expected_markup = self.expected_render(CHAT_A)
        application = self.make_application()

        result = await self.refresh(application, CHAT_A)

        self.assertTrue(result)
        application.bot.edit_message_text.assert_awaited_once_with(
            chat_id=CHAT_A,
            message_id=MSG_A,
            text=expected_text,
            parse_mode=ParseMode.HTML,
            reply_markup=expected_markup,
        )

    async def test_payload_pieces_are_individually_correct(self) -> None:
        self.add_deadline(CHAT_A, title="Data Structures Midterm", days_ahead=9)
        self.register(CHAT_A, MSG_A)
        expected_text, expected_markup = self.expected_render(CHAT_A)
        application = self.make_application()

        await self.refresh(application, CHAT_A)

        kwargs = application.bot.edit_message_text.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], CHAT_A)
        self.assertEqual(kwargs["message_id"], MSG_A)
        self.assertEqual(kwargs["parse_mode"], ParseMode.HTML)
        self.assertEqual(kwargs["text"], expected_text)
        self.assertIn("Data Structures Midterm", kwargs["text"])
        self.assertIn("Upcoming Deadlines", kwargs["text"])
        self.assertIsInstance(kwargs["reply_markup"], InlineKeyboardMarkup)
        self.assertEqual(kwargs["reply_markup"], expected_markup)

    async def test_edit_is_called_entirely_with_keyword_arguments(self) -> None:
        self.add_deadline(CHAT_A)
        self.register(CHAT_A, MSG_A)
        application = self.make_application()

        await self.refresh(application, CHAT_A)

        self.assertEqual(application.bot.edit_message_text.await_args.args, ())

    async def test_text_reflects_current_database_state(self) -> None:
        """The refresh re-renders; it never replays a stale snapshot."""
        stale_id = self.add_deadline(
            CHAT_A, title="Already Submitted", days_ahead=2
        )
        self.add_deadline(CHAT_A, title="Still Pending", days_ahead=6)
        self.register(CHAT_A, MSG_A)
        db.mark_complete(stale_id, CHAT_A)
        application = self.make_application()

        await self.refresh(application, CHAT_A)

        text = application.bot.edit_message_text.await_args.kwargs["text"]
        self.assertNotIn("Already Submitted", text)
        self.assertIn("Still Pending", text)

    async def test_empty_dashboard_is_edited_with_no_markup(self) -> None:
        self.register(CHAT_A, MSG_A)
        expected_text, expected_markup = self.expected_render(CHAT_A)
        self.assertIsNone(expected_markup)
        application = self.make_application()

        result = await self.refresh(application, CHAT_A)

        self.assertTrue(result)
        application.bot.edit_message_text.assert_awaited_once_with(
            chat_id=CHAT_A,
            message_id=MSG_A,
            text=expected_text,
            parse_mode=ParseMode.HTML,
            reply_markup=None,
        )

    async def test_reregistration_targets_the_newest_message(self) -> None:
        self.add_deadline(CHAT_A)
        self.register(CHAT_A, MSG_A)
        self.register(CHAT_A, MSG_A + 77)
        application = self.make_application()

        await self.refresh(application, CHAT_A)

        self.assertEqual(
            application.bot.edit_message_text.await_args.kwargs["message_id"],
            MSG_A + 77,
        )
        self.assertEqual(len(db.list_deadline_dashboards()), 1)


class TestRefreshWithoutRegistration(_RefreshTestBase):
    """An unregistered chat must be a pure no-op."""

    async def test_returns_false(self) -> None:
        self.add_deadline(CHAT_A)
        application = self.make_application()

        self.assertFalse(await self.refresh(application, CHAT_A))

    async def test_makes_no_telegram_call_at_all(self) -> None:
        self.add_deadline(CHAT_A)
        application = self.make_application()

        await self.refresh(application, CHAT_A)

        application.bot.edit_message_text.assert_not_awaited()
        self.assert_no_telegram_calls(application)

    async def test_other_chats_registration_is_not_consumed(self) -> None:
        """Refreshing chat A must not read or clear chat B's registration."""
        self.register(CHAT_B, MSG_B)
        application = self.make_application()

        self.assertFalse(await self.refresh(application, CHAT_A))
        self.assertEqual(db.get_deadline_dashboard_message_id(CHAT_B), MSG_B)


class TestNotModified(_RefreshTestBase):
    """"Message is not modified" means the dashboard is already current."""

    NOT_MODIFIED: str = (
        "Message is not modified: specified new message content and reply "
        "markup are exactly the same as a current content and reply markup "
        "of the message"
    )

    async def test_not_modified_is_treated_as_success(self) -> None:
        self.add_deadline(CHAT_A)
        self.register(CHAT_A, MSG_A)
        application = self.make_application(
            side_effect=BadRequest(self.NOT_MODIFIED)
        )

        self.assertTrue(await self.refresh(application, CHAT_A))

    async def test_not_modified_retains_the_registration(self) -> None:
        self.add_deadline(CHAT_A)
        self.register(CHAT_A, MSG_A)
        application = self.make_application(
            side_effect=BadRequest(self.NOT_MODIFIED)
        )

        await self.refresh(application, CHAT_A)

        self.assertEqual(db.get_deadline_dashboard_message_id(CHAT_A), MSG_A)
        self.assertEqual(db.list_deadline_dashboards(), [(CHAT_A, MSG_A)])

    async def test_not_modified_matching_is_case_insensitive(self) -> None:
        """Telegram's casing varies; matching must not depend on it."""
        variants = (
            "Message is not modified",
            "message is not modified",
            "MESSAGE IS NOT MODIFIED: nothing to change",
            "Bad Request: Message Is Not Modified",
        )
        for variant in variants:
            with self.subTest(variant=variant):
                db.delete_deadline_dashboard(CHAT_A)
                self.register(CHAT_A, MSG_A)
                application = self.make_application(
                    side_effect=BadRequest(variant)
                )

                result = await self.refresh(application, CHAT_A)

                self.assertTrue(result, "not-modified must count as success")
                self.assertEqual(
                    db.get_deadline_dashboard_message_id(CHAT_A),
                    MSG_A,
                    "not-modified must never drop the registration",
                )

    async def test_a_later_refresh_still_edits_after_not_modified(self) -> None:
        self.register(CHAT_A, MSG_A)
        application = self.make_application(
            side_effect=BadRequest(self.NOT_MODIFIED)
        )
        await self.refresh(application, CHAT_A)

        application.bot.edit_message_text.side_effect = None
        self.add_deadline(CHAT_A, title="Newly Added Project")
        await self.refresh(application, CHAT_A)

        self.assertEqual(application.bot.edit_message_text.await_count, 2)
        self.assertIn(
            "Newly Added Project",
            application.bot.edit_message_text.await_args.kwargs["text"],
        )


class TestUnrecoverableBadRequest(_RefreshTestBase):
    """A permanently unusable message must drop its registration."""

    async def test_message_to_edit_not_found_drops_registration(self) -> None:
        self.add_deadline(CHAT_A)
        self.register(CHAT_A, MSG_A)
        application = self.make_application(
            side_effect=BadRequest("Message to edit not found")
        )

        result = await self.refresh(application, CHAT_A)

        self.assertFalse(result)
        self.assertIsNone(db.get_deadline_dashboard_message_id(CHAT_A))
        self.assertEqual(db.list_deadline_dashboards(), [])
        application.bot.edit_message_text.assert_awaited_once()

    async def test_all_unrecoverable_fragments_drop_registration(self) -> None:
        messages = (
            "Message to edit not found",
            "Bad Request: message to edit not found",
            "Message can't be edited",
            "Bad Request: message can't be edited",
            "MESSAGE_ID_INVALID",
            "Bad Request: message_id_invalid",
            "Bad Request: message identifier is not specified",
        )
        for message in messages:
            with self.subTest(message=message):
                self.register(CHAT_A, MSG_A)
                application = self.make_application(
                    side_effect=BadRequest(message)
                )

                result = await self.refresh(application, CHAT_A)

                self.assertFalse(result)
                self.assertIsNone(
                    db.get_deadline_dashboard_message_id(CHAT_A),
                    f"{message!r} must drop the registration",
                )

    async def test_only_the_affected_chat_is_dropped(self) -> None:
        self.register(CHAT_A, MSG_A)
        self.register(CHAT_B, MSG_B)
        application = self.make_application(
            side_effect=BadRequest("Message to edit not found")
        )

        await self.refresh(application, CHAT_A)

        self.assertIsNone(db.get_deadline_dashboard_message_id(CHAT_A))
        self.assertEqual(db.get_deadline_dashboard_message_id(CHAT_B), MSG_B)

    async def test_unrelated_bad_request_keeps_the_registration(self) -> None:
        """A malformed-render BadRequest may be fixed by the next render."""
        self.add_deadline(CHAT_A)
        self.register(CHAT_A, MSG_A)
        application = self.make_application(
            side_effect=BadRequest("Can't parse entities: unsupported start tag")
        )

        result = await self.refresh(application, CHAT_A)

        self.assertTrue(result)
        self.assertEqual(db.get_deadline_dashboard_message_id(CHAT_A), MSG_A)


class TestForbidden(_RefreshTestBase):
    """Losing access to a chat must drop its registration."""

    async def test_forbidden_drops_registration_and_returns_false(self) -> None:
        self.add_deadline(CHAT_A)
        self.register(CHAT_A, MSG_A)
        application = self.make_application(
            side_effect=Forbidden(
                "Forbidden: bot was kicked from the supergroup chat"
            )
        )

        result = await self.refresh(application, CHAT_A)

        self.assertFalse(result)
        self.assertIsNone(db.get_deadline_dashboard_message_id(CHAT_A))
        self.assertEqual(db.list_deadline_dashboards(), [])

    async def test_forbidden_does_not_touch_other_chats(self) -> None:
        self.register(CHAT_A, MSG_A)
        self.register(CHAT_B, MSG_B)
        application = self.make_application(
            side_effect=Forbidden("Forbidden: bot is not a member of the chat")
        )

        await self.refresh(application, CHAT_A)

        self.assertEqual(db.list_deadline_dashboards(), [(CHAT_B, MSG_B)])

    async def test_forbidden_is_swallowed_not_raised(self) -> None:
        """A failed refresh must never undo the mutation that triggered it."""
        self.register(CHAT_A, MSG_A)
        application = self.make_application(
            side_effect=Forbidden("Forbidden: bot was blocked by the user")
        )

        await self.refresh(application, CHAT_A)

        application.bot.edit_message_text.assert_awaited_once()


class TestTransientErrors(_RefreshTestBase):
    """Temporary failures must keep the row so a later refresh retries."""

    async def test_timed_out_retains_registration(self) -> None:
        self.add_deadline(CHAT_A)
        self.register(CHAT_A, MSG_A)
        application = self.make_application(side_effect=TimedOut())

        result = await self.refresh(application, CHAT_A)

        self.assertTrue(result)
        self.assertEqual(db.get_deadline_dashboard_message_id(CHAT_A), MSG_A)

    async def test_plain_telegram_error_retains_registration(self) -> None:
        self.add_deadline(CHAT_A)
        self.register(CHAT_A, MSG_A)
        application = self.make_application(
            side_effect=TelegramError(
                "Flood control exceeded. Retry in 12 seconds"
            )
        )

        result = await self.refresh(application, CHAT_A)

        self.assertTrue(result)
        self.assertEqual(db.list_deadline_dashboards(), [(CHAT_A, MSG_A)])

    async def test_retry_after_transient_failure_succeeds(self) -> None:
        self.add_deadline(CHAT_A, title="Networks Assignment 3")
        self.register(CHAT_A, MSG_A)
        application = self.make_application(side_effect=TimedOut())
        await self.refresh(application, CHAT_A)

        application.bot.edit_message_text.side_effect = None
        result = await self.refresh(application, CHAT_A)

        self.assertTrue(result)
        self.assertEqual(application.bot.edit_message_text.await_count, 2)
        self.assertEqual(
            application.bot.edit_message_text.await_args.kwargs["message_id"],
            MSG_A,
        )


class TestRefreshAllDashboards(_RefreshTestBase):
    """The fan-out used by the post-midnight cron job and at startup."""

    async def test_each_registered_chat_is_refreshed_independently(self) -> None:
        self.add_deadline(CHAT_A, title="Alpha Quiz", days_ahead=3)
        self.add_deadline(CHAT_B, title="Bravo Project", days_ahead=8)
        self.register(CHAT_A, MSG_A)
        self.register(CHAT_B, MSG_B)
        application = self.make_application()

        await self.refresh_all(application)

        self.assertEqual(application.bot.edit_message_text.await_count, 2)
        calls = self.edit_kwargs_by_chat(application)
        self.assertEqual(set(calls), {CHAT_A, CHAT_B})
        self.assertEqual(calls[CHAT_A]["message_id"], MSG_A)
        self.assertEqual(calls[CHAT_B]["message_id"], MSG_B)
        # Each chat gets its OWN rendered content, not one shared blob.
        self.assertIn("Alpha Quiz", calls[CHAT_A]["text"])
        self.assertNotIn("Bravo Project", calls[CHAT_A]["text"])
        self.assertIn("Bravo Project", calls[CHAT_B]["text"])
        self.assertNotIn("Alpha Quiz", calls[CHAT_B]["text"])
        self.assertEqual(calls[CHAT_A]["parse_mode"], ParseMode.HTML)
        self.assertEqual(calls[CHAT_B]["parse_mode"], ParseMode.HTML)
        self.assertEqual(
            calls[CHAT_A]["text"], self.expected_render(CHAT_A)[0]
        )
        self.assertEqual(
            calls[CHAT_B]["text"], self.expected_render(CHAT_B)[0]
        )

    async def test_no_registrations_is_a_silent_no_op(self) -> None:
        self.add_deadline(CHAT_A)
        application = self.make_application()

        result = await self.refresh_all(application)

        self.assertIsNone(result)
        application.bot.edit_message_text.assert_not_awaited()
        self.assert_no_telegram_calls(application)

    async def test_one_failing_chat_does_not_block_the_others(self) -> None:
        for chat_id, message_id, title in (
            (CHAT_A, MSG_A, "Alpha Quiz"),
            (CHAT_B, MSG_B, "Bravo Project"),
            (CHAT_C, MSG_C, "Charlie Final"),
        ):
            self.add_deadline(chat_id, title=title)
            self.register(chat_id, message_id)

        def explode_for_middle_chat(**kwargs):
            if kwargs["chat_id"] == CHAT_B:
                raise Forbidden("Forbidden: bot was kicked from the group chat")
            return Mock(name="edited_message")

        application = self.make_application(side_effect=explode_for_middle_chat)

        await self.refresh_all(application)

        self.assertEqual(application.bot.edit_message_text.await_count, 3)
        calls = self.edit_kwargs_by_chat(application)
        self.assertEqual(set(calls), {CHAT_A, CHAT_B, CHAT_C})
        self.assertEqual(calls[CHAT_A]["message_id"], MSG_A)
        self.assertEqual(calls[CHAT_C]["message_id"], MSG_C)
        self.assertIn("Alpha Quiz", calls[CHAT_A]["text"])
        self.assertIn("Charlie Final", calls[CHAT_C]["text"])
        # Only the broken chat loses its registration.
        self.assertEqual(
            set(db.list_deadline_dashboards()),
            {(CHAT_A, MSG_A), (CHAT_C, MSG_C)},
        )

    async def test_unexpected_exception_in_one_chat_is_isolated(self) -> None:
        for chat_id, message_id in (
            (CHAT_A, MSG_A),
            (CHAT_B, MSG_B),
            (CHAT_C, MSG_C),
        ):
            self.add_deadline(chat_id)
            self.register(chat_id, message_id)

        def explode_for_middle_chat(**kwargs):
            if kwargs["chat_id"] == CHAT_B:
                raise RuntimeError("boom: a genuine bug, not a Telegram error")
            return Mock(name="edited_message")

        application = self.make_application(side_effect=explode_for_middle_chat)

        with self.assertLogs("handlers.tasks", level="ERROR"):
            await self.refresh_all(application)

        calls = self.edit_kwargs_by_chat(application)
        self.assertEqual(set(calls), {CHAT_A, CHAT_B, CHAT_C})
        # A non-Telegram bug is no reason to forget a healthy dashboard.
        self.assertEqual(len(db.list_deadline_dashboards()), 3)

    async def test_dropped_chats_are_skipped_by_the_next_run(self) -> None:
        self.register(CHAT_A, MSG_A)
        self.register(CHAT_B, MSG_B)
        application = self.make_application(
            side_effect=BadRequest("Message to edit not found")
        )

        await self.refresh_all(application)
        self.assertEqual(application.bot.edit_message_text.await_count, 2)
        self.assertEqual(db.list_deadline_dashboards(), [])

        application.bot.edit_message_text.reset_mock()
        await self.refresh_all(application)

        application.bot.edit_message_text.assert_not_awaited()




class TestUnexpectedErrorContainment(_RefreshTestBase):
    """A non-Telegram failure must never escape into the caller.

    Callers refresh AFTER committing a task mutation, so an exception escaping
    refresh_deadline_dashboard would make an add/edit/delete that actually
    succeeded look as though it had failed.
    """

    async def test_render_failure_is_contained_and_registration_kept(self) -> None:
        db.save_deadline_dashboard(CHAT_A, MSG_A)
        application = self.make_application()

        with patch.object(
            tasks, "render_deadlines", side_effect=RuntimeError("db exploded")
        ):
            result = await tasks.refresh_deadline_dashboard(application, CHAT_A)

        self.assertTrue(
            result,
            "an unexpected error must not be reported as a lost registration",
        )
        self.assertEqual(
            db.get_deadline_dashboard_message_id(CHAT_A),
            MSG_A,
            "a bug in rendering is no evidence the Telegram message is gone",
        )

    async def test_refresh_all_survives_an_unexpected_error(self) -> None:
        db.save_deadline_dashboard(CHAT_B, MSG_B)
        db.save_deadline_dashboard(CHAT_C, MSG_C)
        application = self.make_application()
        calls: list[int] = []

        real_render = tasks.render_deadlines

        def flaky(chat_id, today=None):
            calls.append(chat_id)
            if chat_id == CHAT_B:
                raise RuntimeError("boom")
            return real_render(chat_id, today)

        with patch.object(tasks, "render_deadlines", side_effect=flaky):
            await tasks.refresh_all_deadline_dashboards(application)

        self.assertIn(CHAT_C, calls, "a failing chat must not stop the others")

if __name__ == "__main__":
    unittest.main(verbosity=2)
