"""Wiring tests for the persistent /deadlines dashboard.

Four areas are covered:

A. The ``/deadlines`` lifecycle — first call registers, later calls edit the
   registered message, a dead registration is replaced.
B. Mutation paths that MUST push a dashboard refresh.
C. Paths that must NOT push one.
D. The scheduler job set, including the fact that the dashboard rollover job
   is installed independently of ``MORNING_BRIEF_ENABLED``.

Database isolation
------------------
``TASK_BOT_DB_PATH`` is pointed at a throwaway file *before* any project
module is imported, which is the only moment that matters: ``database.db``
resolves ``DB_PATH`` at import time. ``DatabaseIsolationTests`` asserts the
outcome instead of trusting the import order.

No Telegram traffic happens: the bot, the application and every Message /
CallbackQuery are mocks.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

# --- must run before the first project import -------------------------------
_TMP_DIR: str = tempfile.mkdtemp(prefix="taskbot-wiring-")
_TEST_DB_PATH: Path = (Path(_TMP_DIR) / "wiring.db").resolve()
os.environ["TASK_BOT_DB_PATH"] = str(_TEST_DB_PATH)
# ----------------------------------------------------------------------------

import asyncio  # noqa: E402
import importlib  # noqa: E402
import unittest  # noqa: E402
from datetime import timedelta  # noqa: E402
from unittest.mock import AsyncMock, Mock, patch  # noqa: E402

from telegram.error import BadRequest  # noqa: E402

import config  # noqa: E402
import database.db as db  # noqa: E402

if db.DB_PATH != _TEST_DB_PATH:
    # Something imported database.db before this module ran (e.g. a broader
    # discovery run). reload() re-executes the module body against the SAME
    # module __dict__, so helpers already imported elsewhere read the
    # corrected DB_PATH through their unchanged __globals__.
    importlib.reload(db)

import handlers.callbacks as callbacks_mod  # noqa: E402
import handlers.tasks as tasks_mod  # noqa: E402
import scheduler as scheduler_mod  # noqa: E402
import utils.errors as errors_mod  # noqa: E402
from database.models import Task  # noqa: E402
from utils.clock import today_local  # noqa: E402

# The auth decorator only lets the owner's private chat (or ALLOWED_CHAT_ID)
# through, so every fake update is stamped with the owner id.
OWNER_ID: int = config.MY_TELEGRAM_ID

DASHBOARD_HEADER: str = "Upcoming Deadlines"


def setUpModule() -> None:
    """Claim the throwaway database, then prove we are not on the real one.

    Import-time isolation is not sufficient under ``unittest discover``:
    discovery imports every test module first and only then runs them, so a
    sibling module that re-points ``DB_PATH`` in its own ``setUp`` would still
    own the path by the time these tests execute. Re-asserting the env var and
    reloading here — immediately before this module's tests run — makes the
    isolation independent of module ordering.
    """
    os.environ["TASK_BOT_DB_PATH"] = str(_TEST_DB_PATH)
    importlib.reload(db)
    # Reload the dependants in dependency order too. A sibling module that
    # reloads handlers.tasks leaves handlers.callbacks (which did
    # ``from handlers.tasks import ...``) bound to the previous function
    # objects, so the two modules would disagree about which function is
    # "the" refresh helper — and patch targets asserted below would be wrong.
    importlib.reload(tasks_mod)
    importlib.reload(callbacks_mod)
    importlib.reload(scheduler_mod)
    if db.DB_PATH != _TEST_DB_PATH:
        raise AssertionError(
            f"DB isolation failed: DB_PATH={db.DB_PATH!r} "
            f"expected={_TEST_DB_PATH!r}"
        )
    db.init_db()


def tearDownModule() -> None:
    """Remove the temporary database directory."""
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _reset_db() -> None:
    """Empty both tables through the public API."""
    for chat_id, _message_id in db.list_deadline_dashboards():
        db.delete_deadline_dashboard(chat_id)
    db.delete_all_tasks(OWNER_ID)


class _WiringTestCase(unittest.TestCase):
    """Shared fakes: message, callback query, context, handler runner."""

    def setUp(self) -> None:
        _reset_db()
        # Each test drives its own event loop via asyncio.run(). asyncio.Lock
        # binds to the first loop that awaits it, so a lock cached from an
        # earlier test would raise "bound to a different event loop".
        tasks_mod._dashboard_locks.clear()
        self.chat_id = OWNER_ID
        self.sent_messages: list[Mock] = []
        self._next_sent_id = 1000

    # -- fakes ---------------------------------------------------------------

    def _reply(self, text=None, **kwargs) -> Mock:
        """Stand in for ``Message.reply_text``; returns the 'sent' Message."""
        self._next_sent_id += 1
        sent = Mock()
        sent.message_id = self._next_sent_id
        sent.chat_id = self.chat_id
        sent.text = text
        self.sent_messages.append(sent)
        return sent

    def make_message(self, message_id: int = 500) -> Mock:
        message = Mock()
        message.message_id = message_id
        message.chat_id = self.chat_id
        message.reply_text = AsyncMock(side_effect=self._reply)
        return message

    def make_command_update(self, message: Mock | None = None) -> Mock:
        message = message if message is not None else self.make_message()
        update = Mock()
        update.effective_message = message
        update.effective_chat = Mock()
        update.effective_chat.id = self.chat_id
        update.effective_user = Mock()
        update.effective_user.id = OWNER_ID
        update.callback_query = None
        return update

    def make_callback_update(
        self, data: str, message_id: int
    ) -> tuple[Mock, Mock]:
        query = Mock()
        query.data = data
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message = self.make_message(message_id=message_id)

        update = Mock()
        update.callback_query = query
        update.effective_message = query.message
        update.effective_chat = Mock()
        update.effective_chat.id = self.chat_id
        update.effective_user = Mock()
        update.effective_user.id = OWNER_ID
        return update, query

    def make_context(self, args: list[str] | None = None) -> Mock:
        context = Mock()
        context.args = args
        context.application = Mock()
        context.application.bot.edit_message_text = AsyncMock()
        return context

    # -- helpers -------------------------------------------------------------

    def seed_task(
        self,
        title: str = "Compilers project",
        days_ahead: int = 30,
        task_type: str = "project",
    ) -> int:
        """Insert one pending, still-upcoming deadline and return its id."""
        return db.add_task(
            Task(
                title=title,
                task_type=task_type,
                due_date=today_local() + timedelta(days=days_ahead),
                chat_id=self.chat_id,
            )
        )

    def run_handler(self, handler, update: Mock, context: Mock) -> None:
        """Await a handler and fail loudly if ``@safe`` swallowed an error.

        Without this, a mis-built fake would make the handler raise, ``@safe``
        would log it and reply "Something went wrong", and a naive
        assert-not-called test would pass for entirely the wrong reason.
        """
        with patch.object(errors_mod.logger, "exception") as swallowed:
            asyncio.run(handler(update, context))
        if swallowed.call_args_list:
            self.fail(
                "@safe swallowed an exception raised by the handler: "
                f"{swallowed.call_args_list}"
            )

    @staticmethod
    def text_of(async_mock: AsyncMock) -> str:
        """Return the first positional (or ``text=``) arg of the last call."""
        call = async_mock.await_args
        if call.args:
            return call.args[0]
        return call.kwargs["text"]


class DatabaseIsolationTests(_WiringTestCase):
    """Guard rail: these tests must never see the real database."""

    def test_db_path_is_the_temporary_file(self) -> None:
        self.assertEqual(db.DB_PATH, _TEST_DB_PATH)
        self.assertEqual(
            Path(os.environ["TASK_BOT_DB_PATH"]).resolve(), db.DB_PATH
        )

    def test_db_path_is_not_the_project_database(self) -> None:
        real_db = (db.PROJECT_ROOT / "data" / "tasks.db").resolve()
        self.assertNotEqual(db.DB_PATH, real_db)
        self.assertTrue(
            str(db.DB_PATH).startswith(str(Path(_TMP_DIR).resolve())),
            f"{db.DB_PATH} is outside the temp dir {_TMP_DIR}",
        )


# ---------------------------------------------------------------------------
# A. /deadlines lifecycle
# ---------------------------------------------------------------------------


class DeadlinesLifecycleTests(_WiringTestCase):
    def test_first_call_sends_dashboard_and_saves_message_id(self) -> None:
        """A1: no registration yet -> send the list, persist its message id."""
        self.seed_task()
        self.assertIsNone(db.get_deadline_dashboard_message_id(self.chat_id))

        message = self.make_message()
        update = self.make_command_update(message)
        context = self.make_context()

        self.run_handler(tasks_mod.deadlines, update, context)

        # One full dashboard was posted as a reply...
        self.assertEqual(message.reply_text.await_count, 1)
        body = self.text_of(message.reply_text)
        self.assertIn(DASHBOARD_HEADER, body)
        self.assertIn("Compilers project", body)
        self.assertIsNotNone(
            message.reply_text.await_args.kwargs.get("reply_markup"),
            "the dashboard must carry its Manage-deadlines keyboard",
        )
        # ...nothing was edited (there was nothing to edit)...
        context.application.bot.edit_message_text.assert_not_awaited()
        # ...and exactly that message is now the tracked dashboard.
        self.assertEqual(len(self.sent_messages), 1)
        sent = self.sent_messages[0]
        self.assertEqual(
            db.get_deadline_dashboard_message_id(self.chat_id),
            sent.message_id,
        )
        self.assertEqual(
            db.list_deadline_dashboards(), [(self.chat_id, sent.message_id)]
        )

    def test_second_call_edits_tracked_message_and_replies_short(self) -> None:
        """A2: registered -> edit in place, reply with the confirmation only."""
        self.seed_task()
        db.save_deadline_dashboard(self.chat_id, 555)

        message = self.make_message()
        update = self.make_command_update(message)
        context = self.make_context()

        self.run_handler(tasks_mod.deadlines, update, context)

        edit = context.application.bot.edit_message_text
        edit.assert_awaited_once()
        self.assertEqual(edit.await_args.kwargs["chat_id"], self.chat_id)
        self.assertEqual(edit.await_args.kwargs["message_id"], 555)
        self.assertIn(DASHBOARD_HEADER, edit.await_args.kwargs["text"])

        # The reply is the short confirmation, NOT a second dashboard.
        self.assertEqual(message.reply_text.await_count, 1)
        reply = self.text_of(message.reply_text)
        self.assertEqual(reply, tasks_mod._DASHBOARD_REFRESHED_MESSAGE)
        self.assertEqual(reply, "✅ Existing deadline dashboard refreshed.")
        self.assertNotIn(DASHBOARD_HEADER, reply)
        self.assertIsNone(
            message.reply_text.await_args.kwargs.get("reply_markup")
        )

        # The registration is unchanged — no new message was adopted.
        self.assertEqual(
            db.get_deadline_dashboard_message_id(self.chat_id), 555
        )

    def test_stale_registration_is_replaced_and_resaved(self) -> None:
        """A3: 'message to edit not found' -> send + register a replacement."""
        self.seed_task()
        db.save_deadline_dashboard(self.chat_id, 777)

        message = self.make_message()
        update = self.make_command_update(message)
        context = self.make_context()
        context.application.bot.edit_message_text = AsyncMock(
            side_effect=BadRequest("Message to edit not found")
        )

        self.run_handler(tasks_mod.deadlines, update, context)

        edit = context.application.bot.edit_message_text
        edit.assert_awaited_once()
        self.assertEqual(edit.await_args.kwargs["message_id"], 777)

        # A replacement dashboard (not the short confirmation) was sent.
        self.assertEqual(message.reply_text.await_count, 1)
        body = self.text_of(message.reply_text)
        self.assertIn(DASHBOARD_HEADER, body)
        self.assertNotEqual(body, tasks_mod._DASHBOARD_REFRESHED_MESSAGE)

        # ...and the dead id was swapped out for the new one.
        self.assertEqual(len(self.sent_messages), 1)
        sent = self.sent_messages[0]
        self.assertNotEqual(sent.message_id, 777)
        self.assertEqual(
            db.get_deadline_dashboard_message_id(self.chat_id),
            sent.message_id,
        )

    def test_not_modified_edit_keeps_registration(self) -> None:
        """Telegram's benign 'not modified' must not drop the registration."""
        self.seed_task()
        db.save_deadline_dashboard(self.chat_id, 888)

        message = self.make_message()
        update = self.make_command_update(message)
        context = self.make_context()
        context.application.bot.edit_message_text = AsyncMock(
            side_effect=BadRequest("Message is not modified")
        )

        self.run_handler(tasks_mod.deadlines, update, context)

        self.assertEqual(
            self.text_of(message.reply_text),
            tasks_mod._DASHBOARD_REFRESHED_MESSAGE,
        )
        self.assertEqual(
            db.get_deadline_dashboard_message_id(self.chat_id), 888
        )
        # No replacement dashboard was posted.
        self.assertEqual(len(self.sent_messages), 1)
        self.assertNotIn(DASHBOARD_HEADER, self.sent_messages[0].text)


# ---------------------------------------------------------------------------
# B. Mutations that must trigger a refresh
# ---------------------------------------------------------------------------


class RefreshPatchTargetTests(_WiringTestCase):
    """Prove the patch target used below is the name the module calls."""

    def test_patching_handlers_tasks_does_not_reach_callbacks(self) -> None:
        original = callbacks_mod.refresh_deadline_dashboard
        self.assertIs(original, tasks_mod.refresh_deadline_dashboard)

        with patch(
            "handlers.tasks.refresh_deadline_dashboard", new_callable=AsyncMock
        ) as tasks_fake:
            self.assertIs(tasks_mod.refresh_deadline_dashboard, tasks_fake)
            # handlers.callbacks did `from handlers.tasks import ...`, so it
            # still holds the real function: patching tasks alone is useless
            # for asserting on the callback handlers.
            self.assertIs(callbacks_mod.refresh_deadline_dashboard, original)

    def test_patching_handlers_callbacks_takes_effect(self) -> None:
        original = callbacks_mod.refresh_deadline_dashboard
        with patch(
            "handlers.callbacks.refresh_deadline_dashboard",
            new_callable=AsyncMock,
        ) as fake:
            self.assertIs(callbacks_mod.refresh_deadline_dashboard, fake)
        self.assertIs(callbacks_mod.refresh_deadline_dashboard, original)


class MutationTriggersRefreshTests(_WiringTestCase):
    def test_done_command_triggers_refresh(self) -> None:
        """B: /done <id> refreshes the dashboard after completing."""
        task_id = self.seed_task()
        db.save_deadline_dashboard(self.chat_id, 400)

        message = self.make_message()
        update = self.make_command_update(message)
        context = self.make_context(args=[str(task_id)])

        with patch(
            "handlers.tasks.refresh_deadline_dashboard", new_callable=AsyncMock
        ) as refresh:
            self.run_handler(tasks_mod.done_task_cmd, update, context)

        refresh.assert_awaited_once_with(context.application, self.chat_id)
        stored = db.get_task(task_id, self.chat_id)
        self.assertIsNotNone(stored)
        self.assertTrue(stored.completed)
        self.assertIn("Completed", self.text_of(message.reply_text))

    def test_done_callback_on_tracked_dashboard_triggers_refresh(self) -> None:
        """B: tapping Complete ON the dashboard refreshes centrally."""
        task_id = self.seed_task()
        db.save_deadline_dashboard(self.chat_id, 606)

        update, query = self.make_callback_update(f"done:{task_id}", 606)
        context = self.make_context()

        with patch(
            "handlers.callbacks.refresh_deadline_dashboard",
            new_callable=AsyncMock,
        ) as refresh:
            self.run_handler(callbacks_mod.done_callback, update, context)

        refresh.assert_awaited_once_with(context.application, self.chat_id)
        query.answer.assert_awaited_once()
        # The central refresh owns the edit; a second write here would be a
        # redundant identical edit.
        query.edit_message_text.assert_not_awaited()
        self.assertTrue(db.get_task(task_id, self.chat_id).completed)

    def test_done_callback_on_untracked_message_triggers_refresh(self) -> None:
        """B: tapping Complete on an older list edits it AND refreshes."""
        task_id = self.seed_task()
        db.save_deadline_dashboard(self.chat_id, 606)

        update, query = self.make_callback_update(f"done:{task_id}", 999)
        context = self.make_context()

        with patch(
            "handlers.callbacks.refresh_deadline_dashboard",
            new_callable=AsyncMock,
        ) as refresh:
            self.run_handler(callbacks_mod.done_callback, update, context)

        query.edit_message_text.assert_awaited_once()
        refresh.assert_awaited_once_with(context.application, self.chat_id)
        self.assertTrue(db.get_task(task_id, self.chat_id).completed)

    def test_confirmed_delete_on_tracked_dashboard_refreshes(self) -> None:
        """B: del:yes on the dashboard deletes and restores the live list."""
        task_id = self.seed_task()
        db.save_deadline_dashboard(self.chat_id, 707)

        update, query = self.make_callback_update(f"del:yes:{task_id}", 707)
        context = self.make_context()

        with patch(
            "handlers.callbacks.refresh_deadline_dashboard",
            new_callable=AsyncMock,
        ) as refresh:
            self.run_handler(
                callbacks_mod.delete_confirm_callback, update, context
            )

        refresh.assert_awaited_once_with(context.application, self.chat_id)
        self.assertIsNone(db.get_task(task_id, self.chat_id))
        query.edit_message_text.assert_not_awaited()

    def test_confirmed_delete_on_untracked_message_refreshes(self) -> None:
        """B: del:yes elsewhere shows the Deleted card AND refreshes."""
        task_id = self.seed_task()
        db.save_deadline_dashboard(self.chat_id, 707)

        update, query = self.make_callback_update(f"del:yes:{task_id}", 123)
        context = self.make_context()

        with patch(
            "handlers.callbacks.refresh_deadline_dashboard",
            new_callable=AsyncMock,
        ) as refresh:
            self.run_handler(
                callbacks_mod.delete_confirm_callback, update, context
            )

        query.edit_message_text.assert_awaited_once()
        self.assertIn("Deleted", query.edit_message_text.await_args.args[0])
        refresh.assert_awaited_once_with(context.application, self.chat_id)
        self.assertIsNone(db.get_task(task_id, self.chat_id))


# ---------------------------------------------------------------------------
# C. Paths that must NOT trigger a refresh
# ---------------------------------------------------------------------------


class NoRefreshTests(_WiringTestCase):
    def test_cancelled_delete_on_untracked_message_no_refresh(self) -> None:
        """C: del:no away from the dashboard just says Cancelled."""
        task_id = self.seed_task()
        db.save_deadline_dashboard(self.chat_id, 111)

        update, query = self.make_callback_update(f"del:no:{task_id}", 222)
        context = self.make_context()

        with patch(
            "handlers.callbacks.refresh_deadline_dashboard",
            new_callable=AsyncMock,
        ) as refresh:
            self.run_handler(
                callbacks_mod.delete_confirm_callback, update, context
            )

        refresh.assert_not_awaited()
        query.answer.assert_awaited_once_with("Cancelled")
        query.edit_message_text.assert_awaited_once_with(
            "Cancelled. No tasks were deleted."
        )
        # Nothing was deleted and the registration is untouched.
        self.assertIsNotNone(db.get_task(task_id, self.chat_id))
        self.assertEqual(
            db.get_deadline_dashboard_message_id(self.chat_id), 111
        )

    def test_done_command_with_unknown_id_no_refresh(self) -> None:
        """C: /done on a non-existent id changes nothing, so no refresh."""
        db.save_deadline_dashboard(self.chat_id, 333)

        message = self.make_message()
        update = self.make_command_update(message)
        context = self.make_context(args=["4242"])

        with patch(
            "handlers.tasks.refresh_deadline_dashboard", new_callable=AsyncMock
        ) as refresh:
            self.run_handler(tasks_mod.done_task_cmd, update, context)

        refresh.assert_not_awaited()
        self.assertEqual(
            self.text_of(message.reply_text), "No deadline with ID 4242."
        )
        self.assertEqual(
            db.get_deadline_dashboard_message_id(self.chat_id), 333
        )

    def test_done_command_without_argument_no_refresh(self) -> None:
        """C: bare /done prints usage; nothing mutated, nothing refreshed."""
        db.save_deadline_dashboard(self.chat_id, 333)

        message = self.make_message()
        update = self.make_command_update(message)
        context = self.make_context(args=[])

        with patch(
            "handlers.tasks.refresh_deadline_dashboard", new_callable=AsyncMock
        ) as refresh:
            self.run_handler(tasks_mod.done_task_cmd, update, context)

        refresh.assert_not_awaited()
        self.assertIn("Usage: /done", self.text_of(message.reply_text))

    def test_delete_command_only_confirms_and_no_refresh(self) -> None:
        """C: /delete <id> shows a Yes/No card; the delete has not happened."""
        task_id = self.seed_task()
        db.save_deadline_dashboard(self.chat_id, 333)

        message = self.make_message()
        update = self.make_command_update(message)
        context = self.make_context(args=[str(task_id)])

        with patch(
            "handlers.tasks.refresh_deadline_dashboard", new_callable=AsyncMock
        ) as refresh:
            self.run_handler(tasks_mod.delete_task_cmd, update, context)

        refresh.assert_not_awaited()
        self.assertIn(
            "Delete this deadline?", self.text_of(message.reply_text)
        )
        self.assertIsNotNone(db.get_task(task_id, self.chat_id))


# ---------------------------------------------------------------------------
# D. Scheduler
# ---------------------------------------------------------------------------


class SchedulerWiringTests(unittest.TestCase):
    """build_scheduler() job set. The scheduler is never started."""

    REFRESH_JOB_ID = "deadline_dashboard_refresh"
    BRIEF_JOB_ID = "morning_brief"
    HEARTBEAT_JOB_ID = "heartbeat"

    def _build(self, brief_enabled: bool):
        application = Mock()
        # scheduler.py did `from config import MORNING_BRIEF_ENABLED`, so the
        # binding build_scheduler reads lives on the scheduler module.
        with patch.object(
            scheduler_mod, "MORNING_BRIEF_ENABLED", brief_enabled
        ):
            self.assertEqual(
                scheduler_mod.MORNING_BRIEF_ENABLED, brief_enabled
            )
            built = scheduler_mod.build_scheduler(application)
        return application, built

    def test_refresh_job_installed_when_morning_brief_disabled(self) -> None:
        """D: the rollover job does not depend on the morning brief flag."""
        application, sched = self._build(False)

        job = sched.get_job(self.REFRESH_JOB_ID)
        self.assertIsNotNone(
            job, "deadline_dashboard_refresh must exist with the brief off"
        )
        self.assertIs(job.func, scheduler_mod.refresh_all_deadline_dashboards)
        self.assertEqual(tuple(job.args), (application,))

    def test_morning_brief_job_absent_when_disabled(self) -> None:
        _application, sched = self._build(False)
        self.assertIsNone(sched.get_job(self.BRIEF_JOB_ID))
        self.assertEqual(
            {job.id for job in sched.get_jobs()},
            {self.REFRESH_JOB_ID, self.HEARTBEAT_JOB_ID},
        )

    def test_heartbeat_job_present_when_brief_disabled(self) -> None:
        _application, sched = self._build(False)
        heartbeat = sched.get_job(self.HEARTBEAT_JOB_ID)
        self.assertIsNotNone(heartbeat)
        self.assertIs(heartbeat.func, scheduler_mod._log_heartbeat)

    def test_refresh_job_uses_resolved_timezone(self) -> None:
        """D: the rollover must fire on local midnight, not UTC midnight."""
        _application, sched = self._build(False)
        job = sched.get_job(self.REFRESH_JOB_ID)

        expected_tz = scheduler_mod._resolve_timezone()
        self.assertEqual(job.trigger.timezone, expected_tz)
        self.assertEqual(sched.timezone, expected_tz)
        # ...and just after that midnight, per the module's own constants.
        trigger_repr = str(job.trigger)
        self.assertIn(
            f"hour='{scheduler_mod._DEADLINE_REFRESH_HOUR}'", trigger_repr
        )
        self.assertIn(
            f"minute='{scheduler_mod._DEADLINE_REFRESH_MINUTE}'", trigger_repr
        )

    def test_all_three_jobs_present_when_brief_enabled(self) -> None:
        _application, sched = self._build(True)
        self.assertEqual(
            {job.id for job in sched.get_jobs()},
            {
                self.REFRESH_JOB_ID,
                self.BRIEF_JOB_ID,
                self.HEARTBEAT_JOB_ID,
            },
        )
        self.assertEqual(
            sched.get_job(self.REFRESH_JOB_ID).trigger.timezone,
            scheduler_mod._resolve_timezone(),
        )


if __name__ == "__main__":
    unittest.main()
