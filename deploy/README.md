# CI and EC2 deployment

GitHub Actions tests pull requests on Python 3.12 (EC2) and 3.13 (local development).
It runs the regression suite, deployment recovery tests, compilation, and the
isolated database check. Test jobs use dummy credentials and temporary databases.

Passing pushes to `main` deploy that exact commit to the `production` environment.
Deployments are serialized in GitHub and with a host file lock. A queued workflow
for an older main commit is skipped. PR jobs never receive deployment secrets.

## Server layout

- `/home/ubuntu/task-bot/.env` and `data/`: shared production settings and data.
- `/home/ubuntu/task-bot-deploy/releases/`: separate code and venv for each release.
- `/home/ubuntu/task-bot-deploy/current`: symlink used by systemd.
- `/home/ubuntu/task-bot-deploy/backups/`: verified SQLite snapshots, mode 0600.
- `/home/ubuntu/task-bot-deploy/last-deployment.json`: deployment result and backup path.
- `/usr/local/lib/task-bot/deploy.py`: reviewed, root-owned deployment controller.
- `/etc/task-bot-deploy.json`: host configuration; see `config.example.json`.

The original checkout remains available for first-deployment rollback. It is no
longer the running code after the first successful release. Change code through
GitHub; continue editing the shared `.env` there for configuration changes.

## Installation

On the configured Ubuntu host, run `bash deploy/install.sh` from a reviewed copy.
It installs the systemd override and controller without restarting the bot.
The configuration example matches this repository's existing EC2 layout.

Create a dedicated Ed25519 key. Add its public key to `ubuntu`'s
`~/.ssh/authorized_keys` with this prefix (on the same line):

```text
restrict,command="/usr/bin/python3 /usr/local/lib/task-bot/deploy.py --ssh" ssh-ed25519 PUBLIC_KEY task-bot-github-actions
```

Configure GitHub environment `production`, allowing only branch `main`:

| Setting | Value |
| --- | --- |
| Secret `EC2_DEPLOY_KEY` | Dedicated private SSH key |
| Variable `EC2_HOST` | EC2 public hostname/IP |
| Variable `EC2_USER` | `ubuntu` |
| Variable `EC2_KNOWN_HOSTS` | Verified SSH host-key line for that hostname/IP |

The forced command accepts only `deploy` and a complete commit SHA. The controller
checks that SHA against remote `main`. The key cannot start an interactive shell,
transfer arbitrary files, or forward ports. The Telegram token stays on EC2.

Require `Tests (Python 3.12)` and `Tests (Python 3.13)` checks on `main`.
Action versions are pinned to commit SHAs. Controller or systemd changes require
re-running the installer from a reviewed revision; ordinary bot changes deploy
automatically. Update `EC2_HOST` and its verified known-host line if the IP changes.

## Deployment and recovery

The controller builds a new venv and tests it while the existing bot runs. After
preflight succeeds, it stops the bot gracefully, makes a consistent SQLite backup,
switches the current release, and starts the service. Health requires the expected
build, completed Telegram application startup, and a stable PID without restarts.

If activation fails, it stops the failed release, preserves its database when
possible, restores the pre-deployment database, switches back to the previous
code/venv, and verifies the previous bot starts. The workflow stays failed so the
deployment failure remains visible even when recovery succeeds. The failed-state
snapshot preserves any changes received during the short startup-check interval.

Backups also run daily at 03:00 Asia/Singapore. The newest 14 daily, 14 pre-deploy,
and 14 failed-state snapshots are retained. The newest three releases plus the
active/just-previous release are retained after deployment. These backups are on
the same EC2 disk: they recover bad deployments, but do not cover deletion/loss of
the instance's disk. Existing manual backups are left alone.

```bash
systemctl status task-bot
systemctl list-timers task-bot-backup.timer
cat /home/ubuntu/task-bot-deploy/last-deployment.json
sudo systemctl start task-bot-backup.service  # on-demand verified backup
journalctl -u task-bot-backup.service -n 30
```

Re-run `CI and CD` on `main` from the GitHub Actions page to retry a deployment.
No bot token, task database, or backup is uploaded to GitHub.
