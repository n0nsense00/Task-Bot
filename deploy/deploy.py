"""EC2 release deployment and SQLite recovery. Uses only Python's stdlib.

Install this reviewed controller at /usr/local/lib/task-bot/deploy.py. The
restricted CI SSH key invokes --ssh; only a full commit on current main is
accepted. No production secrets are needed by the test jobs or this module.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from uuid import uuid4

CONFIG_PATH = Path('/etc/task-bot-deploy.json')
SHA = re.compile(r'[0-9a-f]{40}')
RELEASE_NAME = re.compile(r'[0-9a-f]{40}-[0-9a-f]{8}')


@dataclass(frozen=True)
class Config:
    root: Path
    shared: Path
    database: Path
    repository: str = 'https://github.com/n0nsense00/Task-Bot.git'
    service: str = 'task-bot'
    python: str = '/usr/bin/python3'
    health_timeout: int = 180
    keep_releases: int = 3
    keep_backups: int = 14

    @classmethod
    def load(cls, path: Path = CONFIG_PATH):
        values = json.loads(path.read_text())
        for key in ('root', 'shared', 'database'):
            values[key] = Path(values[key]).resolve()
        return cls(**values)


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def output(*args, **kwargs):
    kwargs.setdefault('timeout', 30)
    return run(*args, capture_output=True, **kwargs).stdout.strip()


def sqlite_copy(source: Path, destination: Path) -> None:
    """Online backup API handles WAL consistently; also used while stopped."""
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Database integrity check failed')
    destination.chmod(0o600)


def backup_database(config: Config, label: str) -> Path:
    if label not in ('daily', 'pre-deploy', 'failed'):
        raise ValueError('Invalid backup label')
    directory = config.root / 'backups'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    destination = directory / f'{label}-{stamp}-{uuid4().hex[:8]}.sqlite3'
    sqlite_copy(config.database, destination)
    print(f'Database backup: {destination}', flush=True)
    return destination


def switch_release(config: Config, release: Path) -> None:
    link = config.root / 'current'
    if link.exists() and not link.is_symlink():
        raise RuntimeError('current must be a symlink')
    temporary = config.root / f'.current-{uuid4().hex}'
    temporary.symlink_to(release, target_is_directory=True)
    os.replace(temporary, link)


def revision(release: Path) -> str:
    marker = release / '.build-revision'
    value = marker.read_text().strip() if marker.exists() else output('git', '-C', str(release), 'rev-parse', 'HEAD')
    if not SHA.fullmatch(value):
        raise RuntimeError('Release has no valid build revision')
    return value


class SystemdService:
    def __init__(self, config: Config):
        self.config = config

    def stop(self):
        run('sudo', '-n', 'systemctl', 'stop', self.config.service)

    def start(self):
        run('sudo', '-n', 'systemctl', 'reset-failed', self.config.service)
        run('sudo', '-n', 'systemctl', 'start', self.config.service)

    def healthy(self, expected: str) -> bool:
        """Require the expected build to finish Telegram startup and stay up."""
        deadline = time.monotonic() + self.config.health_timeout
        stable_pid = None
        stable_since = None
        while time.monotonic() < deadline:
            raw = output('systemctl', 'show', self.config.service,
                         '--property=ActiveState,SubState,MainPID,NRestarts')
            status = dict(line.split('=', 1) for line in raw.splitlines())
            if status['ActiveState'] == 'failed' or int(status['NRestarts']) > 0:
                return False
            pid = status['MainPID']
            if status['ActiveState'] == 'active' and status['SubState'] == 'running' and pid != '0':
                logs = output('journalctl', '-u', self.config.service,
                              '_PID=' + pid, '--no-pager', '-o', 'cat')
                ready = f'build={expected[:7]}' in logs and 'telegram.ext.Application: Application started' in logs
                if ready:
                    if stable_pid != pid:
                        stable_pid, stable_since = pid, time.monotonic()
                    if time.monotonic() - stable_since >= 8:
                        return True
                else:
                    stable_pid = stable_since = None
            time.sleep(2)
        return False


@contextmanager
def deployment_lock(config: Config):
    import fcntl  # Host deployment is Linux; database helpers also work on Windows.
    config.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (config.root / 'deployment.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def prepare_release(config: Config, commit: str) -> Path | None:
    """Build/test in isolation while the previous release is still running."""
    if not SHA.fullmatch(commit):
        raise ValueError('Expected a full commit SHA')
    mirror = config.root / 'repository.git'
    if not mirror.exists():
        run('git', 'init', '--bare', str(mirror))
    run('git', '--git-dir=' + str(mirror), 'fetch', '--no-tags', config.repository,
        'main:refs/heads/main')
    latest = output('git', '--git-dir=' + str(mirror), 'rev-parse', 'refs/heads/main')
    if latest != commit:
        print('Skipping superseded deployment: a newer commit is on main.', flush=True)
        return None
    if shutil.disk_usage(config.root).free < 512 * 1024 * 1024:
        raise RuntimeError('Less than 512 MiB disk space remains; service was not stopped')
    releases = config.root / 'releases'
    releases.mkdir(mode=0o700, exist_ok=True)
    release = releases / f'{commit}-{uuid4().hex[:8]}'
    release.mkdir(mode=0o700)
    with tempfile.TemporaryFile() as archive:
        subprocess.run(['git', '--git-dir=' + str(mirror), 'archive', commit], stdout=archive, check=True)
        archive.seek(0)
        with tarfile.open(fileobj=archive) as contents:
            contents.extractall(release, filter='data')
    (release / '.build-revision').write_text(commit + '\n')
    run(config.python, '-m', 'venv', str(release / 'venv'))
    python = str(release / 'venv' / 'bin' / 'python')
    run(python, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
        '-r', str(release / 'requirements.txt'))
    with tempfile.TemporaryDirectory(prefix='task-bot-test-') as temporary:
        environment = dict(os.environ)
        environment.update(TELEGRAM_BOT_TOKEN='123456:dummy-token-for-tests',
                           MY_TELEGRAM_ID='424242', ALLOWED_CHAT_ID='-1009876543210',
                           MORNING_BRIEF_ENABLED='false', TIMEZONE='Asia/Singapore',
                           TASK_BOT_DB_PATH=str(Path(temporary) / 'test.db'))
        run(python, '-m', 'unittest', 'discover', '-s', 'tests', '-q', cwd=release, env=environment)
        run(python, 'scripts/check_db.py', cwd=release, env=environment)
    # Only after tests finish may this release access production configuration.
    (release / '.env').symlink_to(config.shared / '.env')
    (release / 'data').symlink_to(config.shared / 'data', target_is_directory=True)
    configured_db = output(python, '-c', 'from database.db import DB_PATH; print(DB_PATH.resolve())', cwd=release)
    if Path(configured_db) != config.database:
        raise RuntimeError('Configured database does not match the backup target')
    return release


def prune(config: Config, protected: set[Path]) -> None:
    """Only prune controller-created releases/backups inside its state folder."""
    releases = config.root / 'releases'
    candidates = sorted(
        (p for p in releases.iterdir() if p.is_dir() and not p.is_symlink()
         and RELEASE_NAME.fullmatch(p.name)), key=lambda p: p.stat().st_mtime, reverse=True,
    ) if releases.exists() else []
    keep = set(candidates[:config.keep_releases]) | protected
    for path in candidates:
        if path not in keep and path.resolve().parent == releases.resolve():
            shutil.rmtree(path)
    backups = config.root / 'backups'
    for label in ('daily', 'pre-deploy', 'failed'):
        files = sorted(backups.glob(label + '-*.sqlite3'), reverse=True)
        for path in files[config.keep_backups:]:
            if path.is_file() and not path.is_symlink() and path.resolve().parent == backups.resolve():
                path.unlink()


def record(config: Config, **values) -> None:
    destination = config.root / 'last-deployment.json'
    temporary = config.root / '.last-deployment.tmp'
    temporary.write_text(json.dumps(values, indent=2) + '\n')
    os.replace(temporary, destination)


def activate_release(config: Config, release: Path, service: SystemdService) -> None:
    """Stop writes, back up, switch, and restore code/venv/database on failure."""
    previous = (config.root / 'current').resolve(strict=True)
    if previous != config.shared and previous.parent != config.root / 'releases':
        raise RuntimeError('Previous release is outside the managed deployment paths')
    previous_sha, requested_sha = revision(previous), revision(release)
    snapshot = None
    switched = False
    stopped = False
    try:
        # A graceful stop finishes outstanding SQLite writes before the snapshot.
        stopped = True
        service.stop()
        snapshot = backup_database(config, 'pre-deploy')
        switched = True
        switch_release(config, release)
        service.start()
        if not service.healthy(requested_sha):
            raise RuntimeError('New release failed its startup health check')
    except BaseException as error:
        if stopped:
            service.stop()
            if switched and snapshot is not None:
                try:
                    backup_database(config, 'failed')
                except Exception:
                    print('Could not preserve failed database; restoring verified snapshot.', flush=True)
                sqlite_copy(snapshot, config.database)
                switch_release(config, previous)
            service.start()
            if not service.healthy(previous_sha):
                record(config, status='rollback_failed', requested=requested_sha, previous=previous_sha,
                       backup=str(snapshot), error=type(error).__name__)
                raise RuntimeError('Deployment failed and previous release needs attention') from error
            record(config, status='rolled_back', requested=requested_sha, active=previous_sha,
                   backup=str(snapshot), error=type(error).__name__)
            print('Deployment failed; previous code, Python environment, and database restored.', flush=True)
        raise
    # A healthy bot must not be rolled back because housekeeping fails.
    try:
        record(config, status='success', active=requested_sha, previous=previous_sha,
               previous_release=str(previous), backup=str(snapshot))
        prune(config, {previous, release})
    except Exception as error:
        print(f'Deployment is healthy; housekeeping needs attention: {type(error).__name__}', flush=True)
    print(f'Deployment healthy: {requested_sha}', flush=True)


def ssh_commit(command: str) -> str:
    match = re.fullmatch(r'deploy ([0-9a-f]{40})', command)
    if match is None:
        raise ValueError('This SSH key only accepts deploy followed by a full commit SHA')
    return match.group(1)


def interrupted(signum, _frame):
    raise InterruptedError(f'Deployment interrupted by signal {signum}')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', nargs='?', choices=('deploy', 'backup'))
    parser.add_argument('commit', nargs='?')
    parser.add_argument('--ssh', action='store_true')
    args = parser.parse_args()
    if args.ssh:
        args.operation = 'deploy'
        args.commit = ssh_commit(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
    if args.operation is None:
        parser.error('Choose deploy or backup')
    config = Config.load()
    for name in ('SIGINT', 'SIGTERM', 'SIGHUP'):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), interrupted)
    with deployment_lock(config):
        if args.operation == 'backup':
            backup_database(config, 'daily')
            prune(config, {(config.root / 'current').resolve()})
        else:
            release = prepare_release(config, args.commit or '')
            if release is not None:
                activate_release(config, release, SystemdService(config))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f'Deployment controller failed: {type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(1)
