#!/usr/bin/env bash
# Run as the existing ubuntu service owner from a reviewed copy of deploy/.
set -euo pipefail
cd -- "$(dirname -- "$0")"
test "$(id -un)" = ubuntu
test -f /home/ubuntu/task-bot/.env
test -f /home/ubuntu/task-bot/data/tasks.db
state=/home/ubuntu/task-bot-deploy
install -d -m 700 "$state" "$state/backups" "$state/releases"
if [ ! -e "$state/current" ]; then
  ln -s /home/ubuntu/task-bot "$state/current"
fi
test -L "$state/current"
sudo install -d -m 755 /usr/local/lib/task-bot /etc/systemd/system/task-bot.service.d
sudo install -m 644 deploy.py /usr/local/lib/task-bot/deploy.py
if [ ! -f /etc/task-bot-deploy.json ]; then
  sudo install -m 644 config.example.json /etc/task-bot-deploy.json
fi
sudo install -m 644 task-bot-release.conf /etc/systemd/system/task-bot.service.d/release.conf
sudo install -m 644 task-bot-backup.service task-bot-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now task-bot-backup.timer
printf '%s\n' 'Deployment controller installed; running bot is unchanged until the first deployment.'
