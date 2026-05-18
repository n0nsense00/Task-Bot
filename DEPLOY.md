# Deploying Task-Bot to Railway

Guide for running the bot 24/7 on [Railway](https://railway.app). Written
for someone deploying Task-Bot for the first time; should take ~10 minutes
start to finish.

> **Note on cost:** Railway's free hobby plan (as of early 2026) covers a
> small always-on service like this comfortably, but an always-polling worker
> uses ~720 hrs/month of execution time. Watch your usage dashboard in the
> first week to confirm it fits within the included credit.

---

## Prerequisites

- A [Railway](https://railway.app) account.
- The Task-Bot repo pushed to GitHub (the one at `github.com/n0nsense00/Task-Bot`).
- Your `TELEGRAM_BOT_TOKEN` and `MY_TELEGRAM_ID` from initial setup.
- (Optional, can be set later) `ALLOWED_CHAT_ID` — the numeric ID of the
  group chat the bot is restricted to. If you don't have this yet, leave it
  unset for the first deploy: the bot starts in "discovery mode" and logs
  the chat id of every rejected message so you can pick it up from Railway
  logs. See `.env.example` for the full discovery procedure.

## 1. Create a Railway project from GitHub

1. Open <https://railway.app/new> and choose **Deploy from GitHub repo**.
2. Authorize Railway to access your GitHub account if prompted.
3. Select the `Task-Bot` repository.
4. Railway detects it as a Python project and starts building. The first
   build **will fail** because environment variables aren't set yet — that's
   expected.

Railway reads `Procfile` and `runtime.txt` automatically:

- `Procfile` declares one process: `worker: python bot.py`.
- `runtime.txt` pins Python to `3.13.6`. Update this line and redeploy when
  you want to move to a newer patch release.

## 2. Set environment variables

In your Railway project, open the **Variables** tab and add:

| Variable                     | Value                                                                                                                                       |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN`         | Your bot token from @BotFather.                                                                                                             |
| `MY_TELEGRAM_ID`             | Your numeric Telegram user ID. Used as the admin gate for `/clear`.                                                                         |
| `ALLOWED_CHAT_ID` (optional) | Numeric chat ID of the group the bot is restricted to (negative int). Leave unset for "discovery mode" — chat ids of rejected msgs get logged. |
| `TIMEZONE` (optional)        | IANA tz name. Defaults to `Asia/Singapore` unset.                                                                                           |

Do **not** commit these to git. They only live in Railway.

Save, and Railway will automatically rebuild.

## 3. Configure a persistent volume for `data/`

Railway containers have **ephemeral** filesystems — by default, everything
under `/app` is wiped every redeploy. You need a Volume so `data/tasks.db`
and `data/last_brief.txt` survive deploys.

1. In the service view, click **+ New** → **Volume**.
2. Set the **mount path** to `/app/data`.
3. Attach the volume to the Task-Bot service.
4. Trigger a redeploy (push a commit, or click **Deploy** in the dashboard).

After this, every write to `data/` inside the container goes to the Volume
and persists across restarts.

> **Skipping this step will DELETE all your tasks on every git push.** The
> SQLite file lives at `data/tasks.db`, which disappears without the Volume.

## 4. Load your initial semester data

Two paths — pick whichever matches your setup:

**Option A (safer): seed locally, upload the SQLite file.**

1. Run `python seed/seed_tasks.py --replace` on your laptop.
2. Install the Railway CLI: `npm i -g @railway/cli`.
3. `railway login`, then `railway link` inside the repo.
4. `railway run bash` gives you a shell in the container. Copy the `.db`
   onto the volume using the method you prefer (the CLI's `railway run`
   supports piping local files).

**Option B: seed inside the container.**

1. Ensure `seed/seed_data.csv` contains only non-sensitive data, then commit
   it temporarily. (Yes, this adds your data to git history — Option A avoids
   that, which is why it's preferred.)
2. After the next deploy, run `railway run python seed/seed_tasks.py --replace`.
3. Remove `seed_data.csv` from the commit and push.

## 5. Auto-deploy on push

Railway auto-deploys from your default branch (usually `main`) on every push.
To change this:

- **Different branch:** Service **Settings** → **Source** → set the
  **Production Branch**.
- **Pause auto-deploy:** Service **Settings** → **Deploy** → **Manual deploys**.

## 6. Viewing logs

- **Dashboard:** Project → service → **Deployments** → click the latest
  deploy → **View Logs**.
- **CLI:** `railway logs` inside the linked project.

A healthy startup looks like:

```
[INFO] __main__: Task-Bot starting (polling mode, build=3acd051)
[INFO] database.db: Database initialized at /app/data/tasks.db
[INFO] __main__: Scheduler started — next morning brief at 2026-04-24 08:00:00+08:00
[INFO] scheduler: Today's brief already sent — skipping catch-up.
```

Then every 10 minutes:

```
[INFO] scheduler: heartbeat: alive
```

If heartbeat lines stop, the bot has crashed or been killed. Railway restarts
crashed workers automatically; look for a traceback immediately before the
restart marker in the log.

## 7. Rolling back a bad deploy

**Dashboard:** Project → **Deployments** → find a previously-healthy deploy
→ ⋯ menu → **Redeploy**. Railway keeps the last ~20 deploys.

**Git:** `git revert <bad-commit-sha> && git push` — a new deploy supersedes
the broken one.

## 8. Confirming liveness (health check)

Since the bot runs as a worker with no public URL, there's no HTTP endpoint
to hit from UptimeRobot. Liveness is verified three ways:

1. **Heartbeat logs** — a `heartbeat: alive` line every 10 minutes in the
   Railway dashboard. If you haven't seen one in 15+ minutes, the bot is
   down (or the log stream is delayed).
2. **Send `/brief` in Telegram** — instant round-trip confirmation. The bot
   replies within a second if healthy.
3. **Morning brief arrival** — if the 08:00 push arrived, the bot was alive
   at 08:00.

If you want *external* HTTP health checks (e.g. UptimeRobot pinging a URL):

- Switch the `Procfile` from `worker:` to `web:`.
- Add a small aiohttp server alongside the bot that binds to `$PORT` and
  responds `200 OK` on `/`. Railway auto-generates a public domain for
  `web:` processes.

This wasn't done in Phase 7 because the heartbeat logs cover the "is it
alive" question adequately for a single-user bot.

---

## Migrating from SQLite to Postgres (deferred)

Don't do this now. This bot is single-user and SQLite will serve it forever.
But if Task-Bot ever grows into a shared tool with multiple concurrent
writers, here's the path:

1. **Provision Postgres in Railway.** One-click via the Plugins UI; Railway
   injects `DATABASE_URL` as an env var automatically.
2. **Swap the driver.** Replace `sqlite3` imports in [database/db.py](database/db.py)
   with `psycopg` (v3) or `asyncpg`. The latter is idiomatic for async PTB;
   you'd need to rewrite `_get_conn` as an async context manager and make
   every DB function an `async def`.
3. **Port the schema.** SQLite's
   `INTEGER PRIMARY KEY AUTOINCREMENT` becomes
   `BIGSERIAL PRIMARY KEY` or `GENERATED ALWAYS AS IDENTITY` in Postgres.
   `TEXT` and `INTEGER` carry over unchanged. The `CHECK(task_type IN (...))`
   constraint works identically.
4. **Introduce Alembic.** The current `init_db()` is idempotent via
   `CREATE TABLE IF NOT EXISTS`, which doesn't scale to real schema
   evolution. Alembic gives you version-controlled migrations.
5. **Migrate existing data.** Export your SQLite rows to CSV (
   `sqlite3 data/tasks.db -csv "SELECT * FROM tasks" > tasks.csv`), then
   run a Postgres-aware variant of [seed/seed_tasks.py](seed/seed_tasks.py).
   Or use [pgloader](https://pgloader.io/) for a one-shot copy.
6. **Add connection pooling.** With concurrent writes you'll want an
   `asyncpg.create_pool()` held at application scope. SQLite got away with
   per-call connections because writes are serialised anyway.

Estimated effort: one focused afternoon for the code port, then a week of
real usage to catch edge cases. Do not migrate pre-emptively.
