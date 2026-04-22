# Task-Bot — Handoff Document

Self-contained context for resuming work in a new Claude conversation (Claude
Desktop, a new Claude Code session, or any other instance). Includes enough
background that the assistant doesn't need to re-explore the codebase before
being useful.

---

## What this project is

A **single-user Telegram bot** for managing one student's university
coursework across a semester. Only the bot owner (identified by a hardcoded
numeric Telegram user ID in `.env`) is allowed to interact; all other users
are silently dropped.

The bot surfaces three views of the owner's workload:

- `/today` — tasks due today (assignments, lectures, tutorials, personal)
- `/week` — this academic week's lectures + next week's tutorials (to prep ahead)
- `/semester` — all midterms and finals, chronological

Plus CRUD (`/add`, `/done`, `/delete`), a `/brief` manual-trigger for the
morning push, and a daily 08:00 brief delivered automatically by APScheduler.

**Important:** This is explicitly NOT a multi-tenant product. Do not propose
features, schemas, or abstractions that assume multiple users.

## Tech stack (locked — do not propose alternatives)

- **Python 3.13** — locally 3.13.6, on Railway 3.13.13 (latest 3.13 patch).
- **`python-telegram-bot` 22.7** (async, v20+ API).
- **SQLite** via stdlib `sqlite3`. Database file at `data/tasks.db`.
- **`python-dotenv` 1.2.2** for local config; Railway injects env vars directly.
- **APScheduler 3.11.2** (`AsyncIOScheduler` + `CronTrigger`) for daily brief + heartbeat.
- **`tzdata` 2026.1** so `zoneinfo` works on Windows + Linux.
- **`tzlocal` 5.3.1** (APScheduler transitive).

If the user considers migration to Postgres for a multi-user future, that's
deferred — see [DEPLOY.md](DEPLOY.md) "Migrating from SQLite to Postgres".

## Security rules (never violate)

- **Owner-only access.** Every handler is wrapped in `@authorized_only`
  (`utils/auth.py`), which silently drops any update whose
  `effective_user.id` != `MY_TELEGRAM_ID`. *No reply to unauthorized users* —
  this is deliberate, so strangers can't even confirm the bot exists.
- **Never log or print the bot token.** `bot.py:_configure_logging` silences
  the `httpx` logger to WARNING because httpx logs full request URLs that
  contain the token.
- **Never commit secrets.** `.env`, `data/`, and `seed/seed_data.csv` are
  gitignored. `.env.example`, `seed/seed_data.example.csv`, and everything
  else may be committed freely.

## Repo

- GitHub: <https://github.com/n0nsense00/Task-Bot>
- Branch: `main` only, auto-deployed to Railway on push.
- Current HEAD (as of 2026-04-23): `405711d` — "Fix requirements.txt
  encoding (UTF-16 LE → UTF-8) so Railway's pip can parse it".

## Local environment

- Windows 11, VS Code.
- venv active at `venv/` inside the repo.
- `.env` file exists locally with `TELEGRAM_BOT_TOKEN`, `MY_TELEGRAM_ID`;
  `TIMEZONE` defaults to `Asia/Singapore`.

## File map

```
Task-Bot/
├── bot.py                      Entry point; builds Application, registers handlers, starts polling.
├── config.py                   Loads .env; exports TELEGRAM_BOT_TOKEN, MY_TELEGRAM_ID,
│                                TIMEZONE, SEMESTER_START_DATE, BRIEF_HOUR, BRIEF_MINUTE,
│                                plus CMD_* constants and get_current_week().
├── scheduler.py                AsyncIOScheduler setup; build_morning_brief; catch-up logic;
│                                heartbeat job every 10 min; last-sent persisted at
│                                data/last_brief.txt.
│
├── database/
│   ├── models.py               Task dataclass + TASK_TYPE_* constants + TASK_TYPES tuple.
│   └── db.py                   SQLite layer — schema, _get_conn ctx manager, all CRUD
│                                (add/get/update/delete/mark_complete/count/delete_all)
│                                and queries (by_date, by_week, semester_deadlines, all_pending).
│
├── handlers/
│   ├── basic.py                /start, /help
│   ├── tasks.py                /today, /week, /semester, /done, /delete + delete_callback
│   ├── add_task.py             /add ConversationHandler (6-step flow: title → type →
│   │                            module → due → week → notes) + /cancel
│   ├── brief.py                /brief — manual trigger for morning brief
│   └── errors.py               unknown_command ("Unknown command. Try /help") + PTB error_handler
│
├── utils/
│   ├── auth.py                 @authorized_only decorator (silent drop + WARNING log)
│   ├── errors.py               @safe decorator (catches exceptions, logs, sends generic reply)
│   └── format.py               Shared HTML formatters: esc, module_prefix, format_task_line,
│                                days_away_label, format_grouped_today.
│
├── seed/
│   ├── seed_tasks.py           Bulk CSV loader. --replace wipes DB with y/n confirm.
│   │                            Validates EVERY row before inserting (atomic).
│   ├── seed_data.csv           Private, gitignored. User's real semester data lives here.
│   ├── seed_data.example.csv   Committed template with 8 example rows.
│   └── README.md               Seeder usage docs.
│
├── data/                       Gitignored.
│   ├── tasks.db                SQLite database (created on first run).
│   └── last_brief.txt          ISO date of last-sent morning brief.
│
├── .env                        Gitignored. Contains TELEGRAM_BOT_TOKEN, MY_TELEGRAM_ID, [TIMEZONE].
├── .env.example                Committed template.
├── .gitignore                  Standard Python + data/, seed/seed_data.csv.
├── requirements.txt            UTF-8 pinned deps.
├── Procfile                    worker: python bot.py
├── runtime.txt                 python-3.13.6
├── DEPLOY.md                   Railway deployment guide + Postgres-migration appendix.
├── test_db.py                  One-shot DB sanity-check script (Phase 3 leftover).
├── HANDOFF.md                  This file.
└── README.md                   Near-empty ("# Task-Bot") — nothing committed there yet.
```

## Build history (7 phases, all complete)

Each phase was committed in sequence on `main`. Commit SHAs in parentheses:

1. **Phase 1** (`5d8af18`) — project scaffolding: requirements.txt, .gitignore, empty README.
2. **Phase 2** (`0a23123`) — `/start`, `/help`, `@authorized_only`, config.py with fail-fast env validation.
3. **Phase 3** (`d08cbfa`) — SQLite data layer: Task dataclass, schema, CRUD, test_db.py.
4. **Phase 4** (`397aa27`) — core commands: `/today`, `/week`, `/semester`, `/add` (ConversationHandler), `/done`, `/delete` (with inline confirmation), `SEMESTER_START_DATE`, `get_current_week()`.
5. **Phase 5** (`e2ec3e7`) — CSV seeder with atomic-validate-then-insert semantics.
6. **Phase 6** (`3acd051`) — APScheduler daily 08:00 brief; `/brief` manual trigger; catch-up on startup for missed mornings; `utils/format.py` shared HTML helpers (refactor).
7. **Phase 7** (`2aff4fa`) — Railway deploy prep: Procfile, runtime.txt, `_get_build_identifier()` startup log, 10-min heartbeat, DEPLOY.md.
   - Plus hotfix `405711d` — requirements.txt encoding (UTF-16 LE → UTF-8). Linux pip
     rejected nulls; Windows pip silently decoded them, so this bug only surfaced on
     Railway's first Phase 7 build attempt and killed the deploy.

## Deployment state (as of 2026-04-23)

- **Railway service "Task-Bot" is Active** on the `main` commit `405711d`.
- **Persistent volume mounted at `/app/data`** — verified via startup log
  `Database initialized at /app/data/tasks.db`.
- **All env vars set** in Railway Variables: `TELEGRAM_BOT_TOKEN`,
  `MY_TELEGRAM_ID`, `TIMEZONE=Asia/Singapore`.
- **DB is empty.** User has deliberately not seeded yet.
- **Railway credit**: ~$30 remaining, burning ~$5/month on always-on worker.
- **One harmless log warning**: PTB `PTBUserWarning: per_message=False` on the
  /add ConversationHandler's CallbackQueryHandler. Cosmetic; /add works.

## Current user situation and next action

**Semester starts ~2026-07-23** (three months out from 2026-04-23). User has
explicitly **deferred seeding** until closer to semester start. Rationale:
they don't want to enter weeks/lectures/deadlines for something still months
away and then have to re-enter if the schedule shifts.

**Until semester start**, the bot sits online returning `🎉 Clear day ahead`
every morning. User may pause the Railway service to save credit if desired.

**When the user returns to seed**, the correct sequence is:

1. **Update [config.py:59](config.py#L59)** — `SEMESTER_START_DATE` is currently
   stale at `date(2026, 1, 12)` (left over from Phase 4 default). Change to the
   actual Monday of their week 1. All `/week` math anchors off this.
2. **Fill in [seed/seed_data.csv](seed/seed_data.csv)** — replace the 8 example
   rows with real modules/lectures/tutorials/exams.
3. **Seed locally + copy DB to Railway** — "Option 2" from the deploy
   conversation (and "Option A" in DEPLOY.md section 4): run
   `python seed/seed_tasks.py --replace` locally to produce a populated
   `data/tasks.db`, then use Railway CLI to copy that file onto the volume
   at `/app/data/tasks.db`. Keeps the real data out of git.

**Do not** seed before `SEMESTER_START_DATE` is updated — `/week` calculations
will be wrong.

## Code quality standards (enforced throughout)

- Full type hints (params + returns) on every function.
- `async`/`await` throughout; PTB v22 handlers are `async def`.
- Separation of concerns by module: config, database, handlers, scheduler,
  utils. Do not merge.
- **`logging` module only, never `print`.** Format has timestamp + level + name.
- **Handlers never crash the process** — `@safe` wraps every handler body in
  try/except; `@authorized_only` drops non-owner updates silently.
- **Full docstrings** on every function.
- **Magic strings as constants** — command names in `config.CMD_*`, callback
  data prefixes in `DELETE_CB_PREFIX` / `_ADD_TYPE_CB_PREFIX`, etc.
- **HTML parse mode**, not MarkdownV2 — MarkdownV2 escaping is brittle. Every
  user-supplied string goes through `utils.format.esc()` before interpolation.

## Gotchas worth remembering

- **Windows writes files as UTF-16 LE by default** via PowerShell redirection
  (`pip freeze > requirements.txt`). This silently broke the Railway build
  once. Always write requirements.txt as UTF-8 explicitly.
- **`httpx` logs long-poll URLs at INFO level** and those URLs include the
  bot token. `bot.py` silences httpx to WARNING for this reason — do not
  re-enable.
- **PTB v22's `run_polling()` handles SIGINT/SIGTERM natively** on POSIX.
  Railway sends SIGTERM on redeploy; no explicit signal wiring needed.
- **`_brief_already_sent_today()` reads `data/last_brief.txt`** and survives
  restarts only because of the persistent volume. Without the volume, the
  bot would send a "catch-up" brief on every restart.
- **Academic week 0** means "before semester starts" in `get_current_week()`.
  `/week` explicitly checks for this and prints a hint about updating
  `SEMESTER_START_DATE`.
- **APScheduler jobs log to the `apscheduler` logger, not the app logger.**
  It's muted to WARNING to keep startup noise down; re-enable to DEBUG if
  you're diagnosing missed jobs.
