# Task-Bot Seeder

Bulk-load a semester's tasks from a CSV instead of sending `/add` fifty times.

## Files

- `seed_data.csv` — your private data. **Gitignored.** Edit this with real tasks.
- `seed_data.example.csv` — reference template, committed to git.
- `seed_tasks.py` — the import script.

When the repo is first cloned, `seed_data.csv` won't exist. Copy the example:

```bash
cp seed/seed_data.example.csv seed/seed_data.csv
```

Then open `seed/seed_data.csv` and replace the example rows with your real
semester data.

## Usage

From the project root, with the venv active:

```bash
# Append mode — adds CSV rows on top of whatever's already in the DB.
python seed/seed_tasks.py

# Replace mode — deletes ALL existing tasks first (prompts y/n), then imports.
python seed/seed_tasks.py --replace

# Custom CSV path.
python seed/seed_tasks.py --file path/to/other.csv
```

## CSV format

The header row must be exactly:

```
title,task_type,module_code,due_date,week_number,notes
```

Rules:

| Column        | Required | Format                                                                 |
| ------------- | -------- | ---------------------------------------------------------------------- |
| `title`       | yes      | any non-empty string                                                   |
| `task_type`   | yes      | `lecture`, `tutorial`, `assignment`, `midterm`, `final`, or `personal` |
| `module_code` | mostly   | required unless `task_type` is `personal` (leave blank for personal)   |
| `due_date`    | yes      | `YYYY-MM-DD`                                                           |
| `week_number` | no       | blank, or integer 1-13                                                 |
| `notes`       | no       | any string, or blank                                                   |

- Lines starting with `#` are comments and skipped.
- Blank lines are skipped.
- Titles containing commas must be quoted: `"CS2040, Lecture 1",lecture,...`

## Validation

Every row is validated **before anything is written**. If any single row fails,
the script prints the source line number and reason and aborts with a non-zero
exit code — no partial imports.

Example failure:

```
ERROR: line 5: task_type 'lectur' not in allowed set: lecture, tutorial, assignment, midterm, final, personal
  row: {'title': 'CS2040 Lecture 2', 'task_type': 'lectur', ...}
Aborting — no rows inserted.
```

## Success output

```
Imported 47 tasks (24 lectures, 12 tutorials, 8 assignments, 2 midterms, 1 final)
```

With `--replace`:

```
--replace will DELETE all 15 existing tasks and then insert the new ones. Are you sure? (y/n): y
Deleted 15 existing tasks.
Imported 47 tasks (...)
```

Answer `n` (or anything other than `y`/`yes`) and the script exits without
touching the database.
