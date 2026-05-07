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

# Delete all existing tasks first, then import. Prompts for y/n.
python seed/seed_tasks.py --replace

# Import a different CSV file.
python seed/seed_tasks.py --file path/to/other.csv
```

## Task CSV Format

The current task CSV header is:

```csv
title,task_type,module_code,due_date,due_time,week_number,notes
```

The older no-time header is still accepted for compatibility:

```csv
title,task_type,module_code,due_date,week_number,notes
```

Rules:

| Column | Required | Format |
| --- | --- | --- |
| `title` | yes | any non-empty string |
| `task_type` | yes | `lecture`, `tutorial`, `assignment`, `midterm`, `final`, or `personal` |
| `module_code` | mostly | required unless `task_type` is `personal` |
| `due_date` | yes | `YYYY-MM-DD` |
| `due_time` | no | blank, or `HH:MM` 24-hour time |
| `week_number` | no | blank, or integer 1-13 |
| `notes` | no | any string, or blank |

Lines starting with `#` are comments and skipped. Blank lines are skipped.
Titles containing commas must be quoted, for example:

```csv
"CS2040, Lecture 1",lecture,CS2040,2026-01-13,09:00,1,Intro
```

Every row is validated before anything is written. If one row fails, the
script prints the source line number and exits without partial inserts.

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
