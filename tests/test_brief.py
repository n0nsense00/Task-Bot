"""Focused regression tests for the due-today and upcoming brief sections."""
from __future__ import annotations

import os
import unittest
from datetime import date, timedelta
from unittest.mock import patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-not-a-real-bot")
os.environ.setdefault("MY_TELEGRAM_ID", "424242")

import scheduler  # noqa: E402
from database.models import Task  # noqa: E402


class MorningBriefDeadlineSectionsTests(unittest.TestCase):
    CHAT_ID = 424242
    TODAY = date(2026, 9, 7)

    @classmethod
    def task(
        cls,
        task_id: int,
        title: str,
        days_ahead: int,
        *,
        module_code: str = "SC2001",
        task_type: str = "assignment",
        due_time: str | None = None,
    ) -> Task:
        return Task(
            id=task_id,
            chat_id=cls.CHAT_ID,
            title=title,
            task_type=task_type,
            module_code=module_code,
            due_date=cls.TODAY + timedelta(days=days_ahead),
            due_time=due_time,
        )

    def render(self, tasks: list[Task]) -> str:
        with (
            patch.object(scheduler, "today_local", return_value=self.TODAY),
            patch.object(
                scheduler, "get_semester_deadlines", return_value=tasks
            ),
        ):
            return scheduler.build_morning_brief(self.CHAT_ID)

    def assert_id_occurs_once(self, text: str, task_id: int) -> None:
        self.assertEqual(
            text.count(f"<code>#{task_id}</code>"),
            1,
            f"task #{task_id} should appear in exactly one brief section",
        )

    def test_one_today_deadline_appears_only_in_due_today_section(self) -> None:
        due_today = self.task(1, "Algorithm Analysis Quiz", 0)

        text = self.render([due_today])

        self.assertIn("Algorithm Analysis Quiz", text)
        self.assertNotIn("Upcoming deadlines", text)
        self.assert_id_occurs_once(text, due_today.id)

    def test_multiple_today_deadlines_each_appear_once(self) -> None:
        due_today = [
            self.task(10, "Calculus Midterm", 0, task_type="midterm"),
            self.task(11, "Operating Systems Lab", 0, task_type="lab"),
            self.task(12, "Probability Quiz", 0, task_type="quiz"),
        ]

        text = self.render(due_today)

        self.assertNotIn("Upcoming deadlines", text)
        for task in due_today:
            self.assertIn(task.title, text)
            self.assert_id_occurs_once(text, task.id)

    def test_today_does_not_consume_three_item_future_limit(self) -> None:
        due_today = self.task(20, "Due Today", 0)
        future = [
            self.task(21, "Future One", 1),
            self.task(22, "Future Two", 2),
            self.task(23, "Future Three", 3),
            self.task(24, "Future Four", 4),
        ]

        text = self.render([due_today, *future])

        self.assertIn("Upcoming deadlines", text)
        self.assert_id_occurs_once(text, due_today.id)
        for task in future[:3]:
            self.assertIn(f"<b>{task.title}</b>", text)
            self.assert_id_occurs_once(text, task.id)
        self.assertNotIn("Future Four", text)
        self.assertNotIn(f"<code>#{future[3].id}</code>", text)

    def test_no_today_deadline_shows_titled_future_preview(self) -> None:
        future = self.task(
            30,
            "Research <Draft> & Review",
            2,
            module_code="SC&2002",
            task_type="project",
            due_time="23:59",
        )

        text = self.render([future])

        self.assertIn("Nothing due today.", text)
        self.assertIn("Upcoming deadlines", text)
        self.assertIn("[SC&amp;2002]", text)
        self.assertIn("<b>Research &lt;Draft&gt; &amp; Review</b>", text)
        self.assertIn("· Project —", text)
        self.assertIn("at 23:59", text)
        self.assertNotIn("<b>Research <Draft>", text)
        self.assert_id_occurs_once(text, future.id)

    def test_no_today_or_future_deadline_returns_clear_day(self) -> None:
        outside_window = self.task(40, "Later in semester", 15)

        text = self.render([outside_window])

        self.assertEqual(text, scheduler._CLEAR_DAY_MESSAGE)
        self.assertNotIn(f"<code>#{outside_window.id}</code>", text)

    def test_no_rendered_task_id_appears_more_than_once(self) -> None:
        tasks = [
            self.task(50, "Today A", 0),
            self.task(51, "Today B", 0),
            self.task(52, "Tomorrow", 1),
            self.task(53, "Next Week", 7),
        ]

        text = self.render(tasks)

        for task in tasks:
            self.assertLessEqual(
                text.count(f"<code>#{task.id}</code>"),
                1,
                f"task #{task.id} was duplicated in the brief",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
