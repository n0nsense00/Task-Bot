"""Fixed-date tests for the /deadlines dashboard renderer.

Every assertion in this module is anchored to an explicit ``today=`` date. The
machine clock is never read: :func:`utils.clock.today_local` is patched to
*raise* for the duration of each test, so a renderer that silently fell back to
the real date would fail loudly instead of passing on the days the numbers
happen to line up. The one test that exercises the implicit-clock path patches
``today_local`` to a fixed date on purpose.

Database isolation
------------------
``database.db`` resolves ``DB_PATH`` at import time from ``TASK_BOT_DB_PATH``,
so the variable is pointed at a temporary directory *before this module imports
anything from the data layer* — including an import-guard path set at module
import, so even an indirect import of ``database.db`` while collecting tests
cannot resolve to the real ``data/tasks.db``. Each test then gets its own
freshly-created SQLite file: ``setUp`` rewrites the env var, reloads
``database.db`` (which recomputes ``DB_PATH``) and then reloads
``handlers.tasks`` (which imports names *from* ``database.db``). Both the temp
path and the "this is not the real database" invariant are asserted in
``setUp`` — see :meth:`RendererTestBase.setUp` and
:meth:`DatabaseIsolationTests`.

No Telegram network call is made anywhere here: the renderers are pure
functions over SQLite plus keyboard construction.
"""
from __future__ import annotations

import importlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Import-time database guard
# ---------------------------------------------------------------------------
# Set before any project import so that *nothing* reachable from this module
# can ever open the developer's real data/tasks.db. Per-test databases live
# under this same root and are removed in tearDownModule().
_TEMP_ROOT: str = tempfile.mkdtemp(prefix="taskbot-render-tests-")
_ORIGINAL_DB_PATH_ENV: str | None = os.environ.get("TASK_BOT_DB_PATH")
os.environ["TASK_BOT_DB_PATH"] = str(Path(_TEMP_ROOT) / "import-guard.db")

# config.py fails fast on missing secrets; supply throwaway values for CI. A
# developer's real .env cannot clobber these — python-dotenv does not override
# variables that are already present in os.environ.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-not-a-real-bot")
os.environ.setdefault("MY_TELEGRAM_ID", "424242")

from telegram import InlineKeyboardMarkup  # noqa: E402

from database.models import (  # noqa: E402
    TASK_TYPE_ASSIGNMENT,
    Task,
)
from utils.format import (  # noqa: E402
    STATUS_DUE_TODAY,
    STATUS_FUTURE,
    STATUS_THIS_WEEK,
    urgency_emoji,
)

# ---------------------------------------------------------------------------
# Fixed reference dates — the whole point of this file
# ---------------------------------------------------------------------------
REFERENCE_DAY: date = date(2026, 8, 19)          # Wed 19 Aug 2026
NEXT_DAY: date = date(2026, 8, 20)               # Thu 20 Aug 2026
DEADLINE_DAY: date = date(2026, 8, 22)           # Sat 22 Aug 2026
EMPTY_STATE_TEXT: str = "No upcoming deadlines"


def tearDownModule() -> None:
    """Remove the temp database root and restore the original env var."""
    shutil.rmtree(_TEMP_ROOT, ignore_errors=True)
    if _ORIGINAL_DB_PATH_ENV is None:
        os.environ.pop("TASK_BOT_DB_PATH", None)
    else:
        os.environ["TASK_BOT_DB_PATH"] = _ORIGINAL_DB_PATH_ENV


class RendererTestBase(unittest.TestCase):
    """Per-test temp SQLite file + a clock that refuses to be read."""

    CHAT_ID: int = -1002222222222
    OTHER_CHAT_ID: int = 987654321

    def setUp(self) -> None:
        case_dir = tempfile.mkdtemp(dir=_TEMP_ROOT)
        self.addCleanup(shutil.rmtree, case_dir, ignore_errors=True)
        self.db_path = (Path(case_dir) / "tasks.db").resolve()
        os.environ["TASK_BOT_DB_PATH"] = str(self.db_path)

        # Reload order matters: db first (recomputes DB_PATH from the env),
        # then handlers.tasks, which does `from database.db import ...`.
        import database.db as db_module
        import handlers.tasks as tasks_module

        self.db = importlib.reload(db_module)
        self.tasks = importlib.reload(tasks_module)

        # Prove the isolation rather than assuming it.
        self.assertEqual(
            self.db.DB_PATH,
            self.db_path,
            "database.db is not pointing at this test's temporary file",
        )
        self.assertNotEqual(
            self.db.DB_PATH,
            (self.db.PROJECT_ROOT / "data" / "tasks.db").resolve(),
            "refusing to run against the real database",
        )

        self.db.init_db()
        self.assertTrue(self.db_path.exists())

        # Any renderer call that omits today= reads the machine clock; make
        # that an immediate, obvious failure. Tests that want the implicit
        # path re-patch today_local locally.
        clock_guard = patch.object(
            self.tasks,
            "today_local",
            side_effect=AssertionError(
                "renderer fell back to the machine clock; "
                "these tests must drive it with an explicit today="
            ),
        )
        clock_guard.start()
        self.addCleanup(clock_guard.stop)

    # -- helpers ----------------------------------------------------------

    def add_deadline(
        self,
        title: str,
        due: date,
        *,
        task_type: str = TASK_TYPE_ASSIGNMENT,
        chat_id: int | None = None,
        module_code: str | None = None,
        due_time: str | None = None,
        completed: bool = False,
    ) -> int:
        """Insert one deadline and return its row id."""
        return self.db.add_task(
            Task(
                title=title,
                task_type=task_type,
                due_date=due,
                chat_id=self.CHAT_ID if chat_id is None else chat_id,
                module_code=module_code,
                due_time=due_time,
                completed=completed,
            )
        )

    def render(self, today: date) -> tuple[str, InlineKeyboardMarkup | None]:
        """Render this chat's dashboard for an explicit reference date."""
        return self.tasks.render_deadlines(self.CHAT_ID, today=today)

    def render_text(self, today: date) -> str:
        return self.render(today)[0]

    def raw_rows(self, task_id: int) -> list[sqlite3.Row]:
        """Read straight from the temp SQLite file, bypassing the data layer."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchall()
        finally:
            conn.close()

    @staticmethod
    def title_line(title: str, status_emoji: str) -> str:
        """First rendered line of an entry: '<emoji> <b>Title</b>'."""
        return f"{status_emoji} <b>{title}</b>"

    @staticmethod
    def detail_fragment(task_id: int, due: date, relative: str) -> str:
        """Tail of the detail line: '· <date> · <relative> · <code>#id</code>'."""
        return (
            f"· {due.strftime('%a %d %b %Y')} · {relative} · "
            f"<code>#{task_id}</code>"
        )


class DatabaseIsolationTests(RendererTestBase):
    """Guard rails for the guard rails."""

    def test_db_path_is_a_temporary_file(self) -> None:
        self.assertEqual(self.db.DB_PATH, self.db_path)
        self.assertTrue(str(self.db_path).startswith(str(Path(_TEMP_ROOT))))
        self.assertNotIn(
            str(Path("data") / "tasks.db"), str(self.db.DB_PATH)
        )

    def test_writes_land_in_the_temp_file_only(self) -> None:
        task_id = self.add_deadline("Isolation probe", DEADLINE_DAY)
        self.assertEqual(len(self.raw_rows(task_id)), 1)
        real_db = (self.db.PROJECT_ROOT / "data" / "tasks.db").resolve()
        self.assertNotEqual(self.db_path, real_db)


class CountdownRolloverTests(RendererTestBase):
    """The same row must read differently as the reference date advances."""

    def test_three_days_away_on_the_reference_day(self) -> None:
        task_id = self.add_deadline("Algorithms Assignment", DEADLINE_DAY)

        text = self.render_text(REFERENCE_DAY)

        self.assertIn("3 days away", text)
        self.assertIn(
            self.detail_fragment(task_id, DEADLINE_DAY, "3 days away"), text
        )

    def test_two_days_away_one_day_later_with_identical_data(self) -> None:
        # Core rollover behaviour: nothing in SQLite changes between the two
        # renders — only the date handed to the renderer.
        task_id = self.add_deadline("Algorithms Assignment", DEADLINE_DAY)

        day_one = self.render_text(REFERENCE_DAY)
        day_two = self.render_text(NEXT_DAY)

        self.assertIn("3 days away", day_one)
        self.assertNotIn("2 days away", day_one)

        self.assertIn("2 days away", day_two)
        self.assertNotIn("3 days away", day_two)
        self.assertIn(
            self.detail_fragment(task_id, DEADLINE_DAY, "2 days away"), day_two
        )

        # Same task, same id, still exactly one entry on both days.
        self.assertIn(f"<code>#{task_id}</code>", day_one)
        self.assertIn(f"<code>#{task_id}</code>", day_two)
        self.assertNotEqual(day_one, day_two)

    def test_countdown_counts_down_every_day_until_due(self) -> None:
        task_id = self.add_deadline("Networks Lab", DEADLINE_DAY)
        expected = {
            REFERENCE_DAY - timedelta(days=1): "4 days away",
            REFERENCE_DAY: "3 days away",
            NEXT_DAY: "2 days away",
            DEADLINE_DAY - timedelta(days=1): "tomorrow",
            DEADLINE_DAY: "today",
        }
        for reference, label in expected.items():
            with self.subTest(today=reference.isoformat()):
                self.assertIn(
                    self.detail_fragment(task_id, DEADLINE_DAY, label),
                    self.render_text(reference),
                )

    def test_today_and_tomorrow_labels(self) -> None:
        today_id = self.add_deadline("Discrete Maths Quiz", REFERENCE_DAY)
        tomorrow_id = self.add_deadline("Physics Lab", NEXT_DAY)

        text = self.render_text(REFERENCE_DAY)

        self.assertIn(
            self.detail_fragment(today_id, REFERENCE_DAY, "today"), text
        )
        self.assertIn(
            self.detail_fragment(tomorrow_id, NEXT_DAY, "tomorrow"), text
        )
        # "1 days away" / "0 days away" must never appear.
        self.assertNotIn("1 days away", text)
        self.assertNotIn("0 days away", text)

    def test_a_deadline_becomes_today_the_day_it_is_due(self) -> None:
        task_id = self.add_deadline("Midterm", NEXT_DAY)

        self.assertIn(
            self.detail_fragment(task_id, NEXT_DAY, "tomorrow"),
            self.render_text(REFERENCE_DAY),
        )
        self.assertIn(
            self.detail_fragment(task_id, NEXT_DAY, "today"),
            self.render_text(NEXT_DAY),
        )


class UrgencyEmojiTests(RendererTestBase):
    """Status icons are chosen from the same reference date as the labels."""

    def test_due_today_uses_the_due_today_status(self) -> None:
        self.add_deadline("Due Today Item", REFERENCE_DAY)

        text = self.render_text(REFERENCE_DAY)

        self.assertIn(self.title_line("Due Today Item", STATUS_DUE_TODAY), text)

    def test_within_seven_days_uses_the_this_week_status(self) -> None:
        for offset in (1, 3, 7):
            with self.subTest(days_ahead=offset):
                title = f"Week Item {offset}"
                self.add_deadline(title, REFERENCE_DAY + timedelta(days=offset))
                text = self.render_text(REFERENCE_DAY)
                self.assertIn(self.title_line(title, STATUS_THIS_WEEK), text)

    def test_beyond_seven_days_uses_the_future_status(self) -> None:
        for offset in (8, 30, 120):
            with self.subTest(days_ahead=offset):
                title = f"Future Item {offset}"
                self.add_deadline(title, REFERENCE_DAY + timedelta(days=offset))
                text = self.render_text(REFERENCE_DAY)
                self.assertIn(self.title_line(title, STATUS_FUTURE), text)

    def test_seven_to_eight_day_boundary(self) -> None:
        seven = self.add_deadline("Seven Days", REFERENCE_DAY + timedelta(days=7))
        eight = self.add_deadline("Eight Days", REFERENCE_DAY + timedelta(days=8))
        self.assertNotEqual(seven, eight)

        text = self.render_text(REFERENCE_DAY)

        self.assertIn(self.title_line("Seven Days", STATUS_THIS_WEEK), text)
        self.assertIn(self.title_line("Eight Days", STATUS_FUTURE), text)

    def test_all_three_statuses_coexist_in_one_render(self) -> None:
        self.add_deadline("Today Item", REFERENCE_DAY)
        self.add_deadline("Soon Item", REFERENCE_DAY + timedelta(days=2))
        self.add_deadline("Later Item", REFERENCE_DAY + timedelta(days=40))

        text = self.render_text(REFERENCE_DAY)

        self.assertIn(self.title_line("Today Item", STATUS_DUE_TODAY), text)
        self.assertIn(self.title_line("Soon Item", STATUS_THIS_WEEK), text)
        self.assertIn(self.title_line("Later Item", STATUS_FUTURE), text)

    def test_urgency_emoji_marks_overdue_dates_as_due_today(self) -> None:
        # Overdue rows are filtered out of the dashboard (see
        # PastDeadlineRetentionTests), so this branch is only reachable
        # through the helper itself — assert it directly.
        for offset in (-1, -5, -365):
            with self.subTest(days_overdue=-offset):
                overdue = REFERENCE_DAY + timedelta(days=offset)
                self.assertEqual(
                    urgency_emoji(overdue, REFERENCE_DAY), STATUS_DUE_TODAY
                )
        self.assertEqual(
            urgency_emoji(REFERENCE_DAY, REFERENCE_DAY), STATUS_DUE_TODAY
        )


class ChronologicalOrderTests(RendererTestBase):
    """Insertion order must not leak into the rendered list."""

    def test_entries_are_ordered_by_due_date_not_insertion(self) -> None:
        # Deliberately inserted latest-first.
        last = self.add_deadline("Zulu Final", date(2026, 12, 1))
        middle = self.add_deadline("Alpha Project", date(2026, 9, 18))
        first = self.add_deadline("Mike Quiz", DEADLINE_DAY)

        text = self.render_text(REFERENCE_DAY)

        positions = [
            text.index(f"<code>#{task_id}</code>")
            for task_id in (first, middle, last)
        ]
        self.assertEqual(
            positions,
            sorted(positions),
            "rendered entries are not in due-date order",
        )

        due_dates = [
            task.due_date
            for task in self.tasks.get_upcoming_deadlines(
                self.CHAT_ID, REFERENCE_DAY
            )
        ]
        self.assertEqual(due_dates, sorted(due_dates))
        self.assertEqual(
            due_dates, [DEADLINE_DAY, date(2026, 9, 18), date(2026, 12, 1)]
        )

    def test_same_day_entries_are_ordered_by_time_then_title(self) -> None:
        untimed = self.add_deadline("Untimed Item", DEADLINE_DAY)
        late = self.add_deadline("Late Item", DEADLINE_DAY, due_time="18:00")
        early = self.add_deadline("Early Item", DEADLINE_DAY, due_time="09:00")

        text = self.render_text(REFERENCE_DAY)

        positions = [
            text.index(f"<code>#{task_id}</code>")
            for task_id in (early, late, untimed)
        ]
        self.assertEqual(positions, sorted(positions))


class PastDeadlineRetentionTests(RendererTestBase):
    """Past deadlines drop out of the view but never out of the database."""

    def test_past_deadline_is_hidden_but_row_is_retained(self) -> None:
        past_id = self.add_deadline(
            "Expired Midterm", REFERENCE_DAY - timedelta(days=1)
        )
        upcoming_id = self.add_deadline("Upcoming Quiz", DEADLINE_DAY)

        text = self.render_text(REFERENCE_DAY)

        # Hidden from the rendered dashboard...
        self.assertNotIn("Expired Midterm", text)
        self.assertNotIn(f"<code>#{past_id}</code>", text)
        self.assertIn("Upcoming Quiz", text)
        self.assertIn(f"<code>#{upcoming_id}</code>", text)
        self.assertIn("1 pending · sorted by due date", text)

        # ...but still present in SQLite, unmodified and not completed.
        rows = self.raw_rows(past_id)
        self.assertEqual(len(rows), 1, "the past deadline row was deleted")
        row = rows[0]
        self.assertEqual(row["title"], "Expired Midterm")
        self.assertEqual(
            row["due_date"], (REFERENCE_DAY - timedelta(days=1)).isoformat()
        )
        self.assertEqual(row["completed"], 0)
        self.assertEqual(row["chat_id"], self.CHAT_ID)

        # And still reachable through the data layer, so /done and /delete
        # can act on it by id.
        self.assertIsNotNone(self.db.get_task(past_id, self.CHAT_ID))
        self.assertEqual(self.db.count_tasks(self.CHAT_ID), 2)

    def test_a_deadline_hides_only_after_its_due_date_passes(self) -> None:
        task_id = self.add_deadline("Boundary Item", REFERENCE_DAY)

        on_the_day = self.render_text(REFERENCE_DAY)
        day_after = self.render_text(REFERENCE_DAY + timedelta(days=1))

        self.assertIn("Boundary Item", on_the_day)
        self.assertNotIn("Boundary Item", day_after)
        self.assertIn(EMPTY_STATE_TEXT, day_after)
        self.assertEqual(len(self.raw_rows(task_id)), 1)

    def test_completed_deadlines_are_hidden_but_retained(self) -> None:
        task_id = self.add_deadline("Finished Lab", DEADLINE_DAY)
        self.assertTrue(self.db.mark_complete(task_id, self.CHAT_ID))

        text = self.render_text(REFERENCE_DAY)

        self.assertNotIn("Finished Lab", text)
        self.assertIn(EMPTY_STATE_TEXT, text)
        rows = self.raw_rows(task_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["completed"], 1)


class EmptyStateTests(RendererTestBase):
    """Nothing upcoming renders the friendly message and no keyboard."""

    def test_empty_database_renders_empty_state_with_no_keyboard(self) -> None:
        text, keyboard = self.render(REFERENCE_DAY)

        self.assertIn(EMPTY_STATE_TEXT, text)
        self.assertIsNone(keyboard)
        self.assertNotIn("Upcoming Deadlines", text)

    def test_only_past_deadlines_still_renders_empty_state(self) -> None:
        self.add_deadline("Old Quiz", REFERENCE_DAY - timedelta(days=10))
        self.add_deadline("Older Final", REFERENCE_DAY - timedelta(days=200))

        text, keyboard = self.render(REFERENCE_DAY)

        self.assertIn(EMPTY_STATE_TEXT, text)
        self.assertIsNone(keyboard)
        self.assertEqual(self.db.count_tasks(self.CHAT_ID), 2)

    def test_non_empty_render_returns_a_keyboard(self) -> None:
        self.add_deadline("Something Due", DEADLINE_DAY)

        text, keyboard = self.render(REFERENCE_DAY)

        self.assertIn("Upcoming Deadlines", text)
        self.assertIsInstance(keyboard, InlineKeyboardMarkup)


class CapturedReferenceDateTests(RendererTestBase):
    """One captured date drives filtering, urgency and labels — nothing else."""

    def test_explicit_today_never_reads_the_clock(self) -> None:
        self.add_deadline("Clock Independent", DEADLINE_DAY)

        # today_local is patched in setUp to raise if called at all.
        text, keyboard = self.render(REFERENCE_DAY)

        self.assertIn("3 days away", text)
        self.assertIsNotNone(keyboard)
        self.tasks.today_local.assert_not_called()

    def test_one_date_drives_filter_urgency_and_label_together(self) -> None:
        yesterday = self.add_deadline(
            "Yesterday Item", REFERENCE_DAY - timedelta(days=1)
        )
        due_today = self.add_deadline("Today Item", REFERENCE_DAY)
        this_week = self.add_deadline("Week Item", DEADLINE_DAY)
        far_off = self.add_deadline(
            "Far Item", REFERENCE_DAY + timedelta(days=20)
        )

        text = self.render_text(REFERENCE_DAY)

        # Filtering is anchored to REFERENCE_DAY...
        self.assertNotIn(f"<code>#{yesterday}</code>", text)
        # ...and so are the icons...
        self.assertIn(self.title_line("Today Item", STATUS_DUE_TODAY), text)
        self.assertIn(self.title_line("Week Item", STATUS_THIS_WEEK), text)
        self.assertIn(self.title_line("Far Item", STATUS_FUTURE), text)
        # ...and so are the countdown labels.
        self.assertIn(
            self.detail_fragment(due_today, REFERENCE_DAY, "today"), text
        )
        self.assertIn(
            self.detail_fragment(this_week, DEADLINE_DAY, "3 days away"), text
        )
        self.assertIn(
            self.detail_fragment(
                far_off, REFERENCE_DAY + timedelta(days=20), "20 days away"
            ),
            text,
        )
        self.assertIn("3 pending · sorted by due date", text)

    def test_explicit_today_equals_the_clock_path_for_the_same_date(self) -> None:
        self.add_deadline("Comparison Item", DEADLINE_DAY)
        self.add_deadline("Stale Item", REFERENCE_DAY - timedelta(days=2))

        explicit_text, explicit_keyboard = self.render(REFERENCE_DAY)
        with patch.object(
            self.tasks, "today_local", return_value=REFERENCE_DAY
        ) as clock:
            implicit_text, implicit_keyboard = self.tasks.render_deadlines(
                self.CHAT_ID
            )

        clock.assert_called_once_with()
        self.assertEqual(implicit_text, explicit_text)
        self.assertEqual(
            implicit_keyboard.to_dict(), explicit_keyboard.to_dict()
        )

    def test_a_different_reference_date_changes_the_render(self) -> None:
        self.add_deadline("Moving Target", DEADLINE_DAY)

        renders = {
            self.render_text(reference)
            for reference in (
                REFERENCE_DAY - timedelta(days=1),
                REFERENCE_DAY,
                NEXT_DAY,
                DEADLINE_DAY,
            )
        }

        self.assertEqual(len(renders), 4, "renders did not vary with today=")

    def test_reference_date_is_captured_once_for_the_whole_message(self) -> None:
        # Every entry in one message must be measured from the same date: the
        # header count, the icons and the labels are all consistent with
        # REFERENCE_DAY and with each other.
        for offset in range(0, 10):
            self.add_deadline(
                f"Item {offset:02d}", REFERENCE_DAY + timedelta(days=offset)
            )

        text = self.render_text(REFERENCE_DAY)

        self.assertIn("10 pending · sorted by due date", text)
        self.assertEqual(text.count(STATUS_DUE_TODAY), 1)   # offset 0
        self.assertEqual(text.count(STATUS_THIS_WEEK), 7)   # offsets 1..7
        # offsets 8 and 9, plus the 📅 in the message header.
        self.assertEqual(text.count(STATUS_FUTURE), 2 + 1)

    def test_render_is_scoped_to_one_chat(self) -> None:
        mine = self.add_deadline("My Deadline", DEADLINE_DAY)
        theirs = self.add_deadline(
            "Their Deadline", DEADLINE_DAY, chat_id=self.OTHER_CHAT_ID
        )

        text = self.render_text(REFERENCE_DAY)

        self.assertIn(f"<code>#{mine}</code>", text)
        self.assertNotIn(f"<code>#{theirs}</code>", text)
        self.assertNotIn("Their Deadline", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
