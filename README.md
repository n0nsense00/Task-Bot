# Task-Bot

Task-Bot is a single-user Telegram task manager for university coursework. It
uses Telegram as the UI, Python as the backend, SQLite for persistence, and
APScheduler for a daily morning brief.

I built it as a Week 1-2 CRUD project while ramping up toward backend/platform
engineering internships. The goal is intentionally modest: ship a working tool,
keep the data model understandable, document the engineering choices, and make
the repo easy to run and review.

## What It Does

- Add tasks through a guided Telegram flow.
- Track lectures, tutorials, assignments, midterms, finals, and personal tasks.
- Store module codes, due dates, optional due times, academic week numbers, and
  notes.
- View tasks due today, this week's lectures, next week's tutorials, and
  upcoming semester deadlines.
- Mark tasks done, edit fields, or delete tasks from Telegram inline buttons.
- Send an on-demand `/brief` and an optional scheduled morning brief.
- Bulk-load semester data from CSV files.
- Keep all private runtime data out of git.

## Tech Stack

- Python 3.13
- `python-telegram-bot` 22.x
- SQLite via the Python standard library
- APScheduler for scheduled jobs
- `python-dotenv` for local configuration
- Railway-compatible `Procfile` deployment

## Architecture

```text
Telegram chat
    |
    v
bot.py
    |
    +-- handlers/       Slash commands, callback buttons, add/edit flows
    +-- database/       SQLite schema, CRUD helpers, query helpers
    +-- scheduler.py    Morning brief, catch-up logic, heartbeat, cleanup job
    +-- seed/           CSV import scripts for tasks and modules
    +-- utils/          Auth, formatting, calendar picker, time picker
```

The bot is deliberately single-user. Every handler is wrapped with an owner-only
authorization decorator, and unauthorized users are silently ignored.

## Commands

| Command | Purpose |
| --- | --- |
| `/start` | Show the welcome message |
| `/help` | Show command reference |
| `/add` | Add a task through an interactive flow |
| `/today` | Show tasks due today with action buttons |
| `/week` | Show this week's lectures and next week's tutorials |
| `/semester` | Show upcoming midterms and finals |
| `/done <id>` | Mark a task complete |
| `/delete <id>` | Delete a task after confirmation |
| `/brief` | Send the morning summary on demand |
| `/clear` | Delete recent chat messages with the bot |

## Local Setup

Create and activate a virtual environment:

```bash
python -m venv venv
venv\Scripts\activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Create your local environment file:

```bash
copy .env.example .env
```

Fill in:

```env
TELEGRAM_BOT_TOKEN=your_token_from_botfather
MY_TELEGRAM_ID=your_numeric_telegram_user_id
```

Run the bot:

```bash
python bot.py
```

## Seeding Data

Copy the CSV templates:

```bash
copy seed\seed_data.example.csv seed\seed_data.csv
copy seed\seed_modules.example.csv seed\seed_modules.csv
```

Import modules:

```bash
python seed/seed_modules.py --replace
```

Import tasks:

```bash
python seed/seed_tasks.py --replace
```

Private files such as `.env`, `data/tasks.db`, `seed/seed_data.csv`, and
`seed/seed_modules.csv` are gitignored.

## Verification And Stress Testing

Run a temp-database sanity check:

```bash
python scripts/check_db.py
```

Run the SQLite stress harness:

```bash
python scripts/stress_db.py
```

Larger run:

```bash
python scripts/stress_db.py --tasks 10000 --queries 5000 --mutations 2000
```

Both scripts use temporary SQLite databases by default, so they do not mutate
your real `data/tasks.db`.

## Deployment

This repo includes a Railway worker setup:

- `Procfile` runs `python bot.py`.
- `runtime.txt` pins the Python runtime.
- `DEPLOY.md` documents Railway setup, required environment variables, and
  persistent volume configuration for `data/`.

For Railway, mount a persistent volume at `/app/data`; otherwise SQLite data
will disappear on redeploy.

## Engineering Notes

- SQLite is enough here because the app is single-user and write volume is low.
- The database layer opens short-lived connections per operation and uses a
  busy timeout to tolerate brief SQLite locks.
- User-supplied text is HTML-escaped before Telegram rendering.
- Telegram API request logging is reduced so the bot token does not appear in
  logs.
- The daily brief can be disabled with `MORNING_BRIEF_ENABLED=false` while
  keeping `/brief` available for manual checks.

## Next Improvements

- Add GitHub Actions for `scripts/check_db.py` and import validation.
- Add focused unit tests around formatter and callback parser helpers.
- Add a small architecture diagram image for the README.
- Add a short demo recording or screenshots before pinning the repo publicly.
- Keep Postgres as a future migration only if this grows beyond a single-user
  bot.
