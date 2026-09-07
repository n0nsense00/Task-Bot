"""Regression tests for the SQLite stress command."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from scripts import stress_db


class StressDbTests(unittest.TestCase):
    def test_zero_mutations_reports_not_applicable(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            stress_db.run_stress(task_count=3, query_count=1, mutation_count=0)

        self.assertIn(
            "mutation_latency_ms=n/a (no mutations run)", output.getvalue()
        )

    def test_normal_mutations_report_numeric_timings(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            stress_db.run_stress(task_count=3, query_count=1, mutation_count=3)

        mutation_line = next(
            line
            for line in output.getvalue().splitlines()
            if line.startswith("mutation_latency_ms=")
        )
        self.assertIn("avg ", mutation_line)
        self.assertIn("p95 ", mutation_line)
        self.assertIn("max ", mutation_line)
        self.assertNotIn("n/a", mutation_line)

    def test_cli_accepts_zero_mutations(self) -> None:
        with patch.object(stress_db, "run_stress") as run_stress:
            result = stress_db.main(
                ["--tasks", "1", "--queries", "1", "--mutations", "0"]
            )

        self.assertEqual(result, 0)
        run_stress.assert_called_once_with(1, 1, 0)

    def test_help_describes_two_query_types(self) -> None:
        output = io.StringIO()

        with self.assertRaises(SystemExit) as raised, redirect_stdout(output):
            stress_db.main(["--help"])

        self.assertEqual(raised.exception.code, 0)
        normalized_help = " ".join(output.getvalue().split())
        self.assertIn("each iteration runs two query types", normalized_help)


if __name__ == "__main__":
    unittest.main()
