# Task-Bot Seeder

Bulk-load a semester's tasks from CSV instead of sending `/add` dozens of
times. You can also seed module codes so the `/add` and `/edit` pickers show
your real classes.

## Files

- `seed_data.csv` - private task data, gitignored.
- `seed_data.example.csv` - committed task template.
- `seed_tasks.py` - task import script.
- `seed_modules.csv` - private module list, gitignored.
- `seed_modules.example.csv` - committed module template.
- `seed_modules.py` - module import script.

When the repo is first cloned, the private CSV files will not exist. Copy the
examples:

```bash
cp seed/seed_data.example.csv seed/seed_data.csv
cp seed/seed_modules.example.csv seed/seed_modules.csv
```

Then replace the example rows with your real semester data.

## Task Usage

From the project root, with the venv active:

```bash
# Append rows on top of the current DB.
python seed/seed_tasks.py

# Delete the owner's existing personal tasks first, then import. Prompts for y/n.
python seed/seed_tasks.py --replace

# Import a different CSV file.
python seed/seed_tasks.py --file path/to/other.csv
```

## Task CSV Format

The current task CSV header is:

```csv
title,task_type,module_code,due_date,due_time,notes
```

The older no-time header is still accepted for compatibility:

```csv
title,task_type,module_code,due_date,notes
```

Both headers are also accepted with a `week_number` column in the
second-to-last position. That column was dropped from the data model; it is
still parsed so existing CSVs keep importing, but its value is discarded.

Rules:

| Column | Required | Format |
| --- | --- | --- |
| `title` | yes | any non-empty string |
| `task_type` | yes | `quiz`, `lab`, `assignment`, `project`, `midterm`, `final`, or `other` |
| `module_code` | yes | a module code from your semester |
| `due_date` | yes | `YYYY-MM-DD` |
| `due_time` | no | blank, or `HH:MM` 24-hour time |
| `notes` | no | any string, or blank |

Lines starting with `#` are comments and skipped. Blank lines are skipped.
Titles containing commas must be quoted, for example:

```csv
"SC2001, Quiz 1",quiz,SC2001,2026-09-03,09:00,,Covers weeks 1-3
```

Every row is validated before anything is written. If one row fails, the
script prints the source line number and exits without partial inserts.
Imported rows belong to the personal chat configured by `MY_TELEGRAM_ID`.
Group deadlines are created through `/add` inside the group and are unaffected
by `--replace`.

## Module Usage

```bash
# Append/update module rows.
python seed/seed_modules.py

# Delete all existing modules first, then import. Prompts for y/n.
python seed/seed_modules.py --replace
```

Module CSV format:

```csv
code,name
CS2040,Data Structures and Algorithms
MH2100,Calculus III
```

`code` is required. `name` is optional; if blank, Telegram picker buttons show
only the code.
