"""Validate full CSV records and atomic module imports."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from database import db
from database.models import Module
from seed import seed_modules, seed_tasks


class SeedImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="taskbot-seed-audit-")
        self.old_path = db.DB_PATH
        db.DB_PATH = Path(self.temp.name) / "tasks.db"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.old_path
        self.temp.cleanup()

    def csv(self, text):
        path = Path(self.temp.name) / "input.csv"
        path.write_text(text, encoding="utf-8", newline="")
        return path

    def test_task_notes_preserve_multiline_blank_and_comment_looking_lines(self):
        path = self.csv(
            '# comment outside a record\n'
            'title,task_type,module_code,due_date,notes\n'
            'First,quiz,SC2001,2026-11-04,"Line one\n# keep this note\n\nLine two"\n'
            'Second,quiz,SC2002,2026-11-17,\n'
        )
        rows = seed_tasks._read_rows(path)
        self.assertEqual([row.line_no for row in rows], [3, 7])
        self.assertEqual(seed_tasks._validate_row(rows[0]).notes,
                         "Line one\n# keep this note\n\nLine two")
        self.assertEqual(seed_tasks._validate_row(rows[1]).title, "Second")

    def test_module_names_preserve_multiline_records_and_line_numbers(self):
        path = self.csv(
            '# comment outside a record\ncode,name\n'
            'SC2001,"Line one\n# keep this name\n\nLine two"\n'
            'SC2002,OOP\n'
        )
        rows = seed_modules._read_rows(path)
        self.assertEqual([row.line_no for row in rows], [3, 7])
        self.assertEqual(seed_modules._validate_row(rows[0]).name,
                         "Line one\n# keep this name\n\nLine two")

    def test_extra_and_missing_csv_fields_are_rejected(self):
        for reader, header, bad_row in (
            (seed_tasks._read_rows, 'title,task_type,module_code,due_date,notes',
             'Quiz,quiz,SC2001,2026-11-04,note,EXTRA'),
            (seed_tasks._read_rows, 'title,task_type,module_code,due_date,notes',
             'Quiz,quiz,SC2001,2026-11-04'),
            (seed_modules._read_rows, 'code,name', 'SC2001,Algorithms,EXTRA'),
        ):
            with self.subTest(header=header, bad_row=bad_row):
                with self.assertRaises(ValueError):
                    reader(self.csv(header + '\n' + bad_row + '\n'))

    def test_failed_module_append_rolls_back_every_new_row(self):
        db.add_module(Module("KEEP", "Existing catalogue"))
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            conn.execute("""CREATE TRIGGER reject_second_module BEFORE INSERT ON modules
                WHEN NEW.code = 'FAIL' BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
            conn.commit()
        path = self.csv('code,name\nGOOD,First module\nFAIL,Second module\n')
        with self.assertRaises(sqlite3.IntegrityError):
            seed_modules.main(["--file", str(path)])
        self.assertEqual(db.count_modules(), 1)
        self.assertIsNotNone(db.get_module("KEEP"))
        self.assertIsNone(db.get_module("GOOD"))


if __name__ == "__main__":
    unittest.main()
