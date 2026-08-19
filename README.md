# Task-Bot

Task-Bot is a Telegram deadline tracker for university coursework. It uses
Telegram as the UI, Python as the backend, SQLite for persistence, and
APScheduler for a daily personal morning brief.

I built it as a Week 1-2 CRUD project while ramping up toward backend/platform
engineering internships. The goal is intentionally modest: ship a working tool,
keep the data model understandable, document the engineering choices, and make
the repo easy to run and review.

## What It Does

- Add assessed deadlines through a guided Telegram flow.
- Track quizzes, labs, assignments, projects, midterms, finals, and other items.
- Store module codes, titles, due dates, optional due times, and notes.
- View every pending deadline in one chronological list, soonest first.
- Complete, edit, or delete deadlines from Telegram inline buttons.
- Automatically keep personal-chat and group-chat deadlines separate.
- Serve a shared study group while restricting the bot to one configured chat.
- Silence the bot on demand with an owner-only kill switch that survives restarts.
- Send an on-demand `/brief` and an optional scheduled morning brief.
- Bulk-load semester data from CSV files.
- Keep all private runtime data out of git.

## Tech Stack

- Python 3.13
- `python-telegram-bot` 22.x
- SQLite via the Python standard library
- APScheduler for scheduled jobs
- `python-dotenv` for local configuration
- Deployed on an AWS EC2 instance under systemd

## Architecture

```text
Telegram chat
    |
    v
bot.py
    |
    +-- handlers/       Slash commands, callbacks, add/edit flows, admin, errors
    +-- database/       SQLite schema, CRUD helpers, query helpers
    +-- scheduler.py    Morning brief, catch-up logic, and heartbeat
    +-- seed/           CSV import scripts for tasks and modules
    +-- utils/          Auth, kill switch, message tracking, formatting, pickers
    +-- deploy/         systemd unit for the EC2 host
```

### Access Model

The bot answers in exactly two places: the owner's private chat with the bot,
and one group chat pinned by `ALLOWED_CHAT_ID`. Everything else — other DMs,
other groups, channels — is logged at WARNING and dropped.

Two decorators in [utils/auth.py](utils/auth.py) enforce this:

| Decorator | Who passes | Used for |
| --- | --- | --- |
| `@authorized_only` | Any user in the allowed group, plus the owner's DM | Normal commands |
| `@admin_only` | The owner only, in their DM or the allowed group | `/clear`, `/kill`, `/revive` |

Deadlines are scoped by `chat_id`, so the group's list and the owner's personal
list stay separate even though one bot process serves both.

### Kill Switch

`/kill` makes the bot play dead for everyone except the owner: every
`@authorized_only` handler silently drops its update. `/revive` brings it back.

State is a flag file at `data/killed.flag`, so it survives process restarts.
`@admin_only` deliberately bypasses the kill check — otherwise `/kill` would
lock the owner out with no way to issue `/revive`. Neither command appears in
the Telegram `/` suggestion menu.

## Commands

| Command | Access | Purpose |
| --- | --- | --- |
| `/start` | All | Show the welcome message |
| `/help` | All | Show command reference |
| `/deadlines` | All | Every pending deadline in one list, soonest first |
| `/add` | All | Add a deadline through an interactive flow |
| `/done <id>` | All | Mark a deadline complete |
| `/delete <id>` | All | Delete a deadline after confirmation |
| `/cancel` | All | Abort an in-progress `/add` or `/edit` flow |
| `/brief` | All | Preview today's and near-term deadlines |
| `/clear` | Owner | Delete this chat's bot-related messages from the last 48h |
| `/kill` | Owner | Silence the bot for everyone but the owner |
| `/revive` | Owner | Undo `/kill` |

"All" means any member of the allowed group, plus the owner. Editing is reached
through the inline buttons on `/deadlines`, not a slash command.

`/clear` only removes messages that constitute interaction with this bot — DMs,
slash commands aimed at it, replies to its messages, and `@`-mentions. Group
chatter from other members is never tracked, so `/clear` in a shared group
cannot nuke unrelated conversation.

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

Run the bot:

```bash
python bot.py
```

## Configuration

All configuration is environment variables, read from `.env` at import time.
[config.py](config.py) fails fast on startup if a required one is missing or
malformed, so typos surface immediately rather than at first use.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | Yes | — | Bot token from @BotFather |
| `MY_TELEGRAM_ID` | Yes | — | Owner's numeric Telegram user ID |
| `ALLOWED_CHAT_ID` | No | `0` | Group chat the bot is restricted to (negative int) |
| `TIMEZONE` | No | `Asia/Singapore` | IANA timezone for the daily brief |
| `MORNING_BRIEF_ENABLED` | No | `false` | Enables the scheduled 08:00 push |
| `TASK_BOT_DB_PATH` | No | `data/tasks.db` | Override the SQLite path for isolated runs |

Leaving `ALLOWED_CHAT_ID` unset starts the bot in discovery mode: only the owner
can use it, and rejected updates log their `chat_id`. Add the bot to the target
group, send `/start`, read the id from the logs, then set the variable and
restart. See [.env.example](.env.example) for the full procedure.

### Runtime Data

Three files live under `data/`, which is gitignored in full:

| File | Written by | Purpose |
| --- | --- | --- |
| `tasks.db` | [database/db.py](database/db.py) | All deadlines and modules |
| `last_brief.txt` | [scheduler.py](scheduler.py) | Date of the last morning brief, so restarts don't re-send |
| `killed.flag` | [utils/kill_switch.py](utils/kill_switch.py) | Presence means the kill switch is engaged |

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

CSV imports are assigned to `MY_TELEGRAM_ID`, so they populate the owner's
personal bot chat. Deadlines added interactively are automatically assigned to
the Telegram chat where `/add` was used.

Private files such as `.env`, `data/`, `seed/seed_data.csv`, and
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

The bot runs 24/7 on an **AWS EC2** instance (Ubuntu, `ap-southeast-1`) as a
systemd service. [deploy/task-bot.service](deploy/task-bot.service) is the unit
file: it runs `bot.py` from a venv as the `ubuntu` user and restarts on failure
after 5 seconds.

First-time setup on a fresh instance:

```bash
git clone https://github.com/n0nsense00/Task-Bot.git ~/task-bot
cd ~/task-bot
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env        # fill in token, ids, timezone

sudo cp deploy/task-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now task-bot
```

Day-to-day operations:

```bash
sudo systemctl status task-bot     # is it up?
sudo systemctl restart task-bot    # apply a config or code change
journalctl -u task-bot -f          # follow logs
journalctl -u task-bot -n 100      # last 100 lines
```

Deploying a change is `git pull && sudo systemctl restart task-bot`. The unit
sets `PYTHONUNBUFFERED=1`, so log lines reach journald immediately instead of
sitting in a stdio buffer.

`data/` sits on the instance's EBS volume and persists across restarts and
redeploys with no extra configuration — unlike an ephemeral container
filesystem, which needs a mounted volume to avoid losing the database.

`runtime.txt` records the Python version the bot is built and tested against
(3.13.6). Nothing on the EC2 host reads it; it is there so the pin is written
down next to the code.

### Liveness

The bot is a polling worker with no HTTP endpoint, so there is nothing to health
check from outside. Liveness is confirmed three ways:

1. A `heartbeat: alive` line in the journal every 10 minutes.
2. `sudo systemctl status task-bot` showing `active (running)`.
3. Sending `/brief` in Telegram — a healthy bot replies in about a second.

## Engineering Notes

- SQLite is enough here because write volume is low and the group is small.
- The database layer opens short-lived connections per operation and uses a
  busy timeout to tolerate brief SQLite locks.
- Rows are scoped by `chat_id`, which is what keeps one bot process from
  leaking the owner's personal deadlines into the group's list.
- User-supplied text is HTML-escaped before Telegram rendering.
- Telegram API request logging is reduced so the bot token does not appear in
  logs.
- The kill switch is a file rather than an in-memory flag specifically so a
  crash or restart cannot silently revive a bot that was meant to stay quiet.
- The daily brief can be disabled with `MORNING_BRIEF_ENABLED=false` while
  keeping `/brief` available for manual checks.

## Next Improvements

- Add GitHub Actions for `scripts/check_db.py` and import validation.
- Add focused unit tests around formatter and callback parser helpers.
- Add a small architecture diagram image for the README.
- Add a short demo recording or screenshots before pinning the repo publicly.
- Keep Postgres as a future migration only if this outgrows a single group.
