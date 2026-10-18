"""Exercise real SQLite backup/restore and release switching without Telegram."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from deploy import deploy

OLD = 'a' * 40
NEW = 'b' * 40


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / 'tasks.db'
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT)')
            connection.execute("INSERT INTO tasks VALUES (1, 'Semester deadline')")

    def test_online_backup_includes_committed_wal_and_restores_schema(self):
        backup = self.root / 'backup.sqlite3'
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute("INSERT INTO tasks VALUES (2, 'Group deadline')")
            connection.commit()
            deploy.sqlite_copy(self.database, backup)
            connection.execute('ALTER TABLE tasks ADD COLUMN broken_migration TEXT')
            connection.execute('DELETE FROM tasks')
            connection.commit()
        deploy.sqlite_copy(backup, self.database)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute('SELECT * FROM tasks ORDER BY id').fetchall(),
                             [(1, 'Semester deadline'), (2, 'Group deadline')])
            self.assertEqual([r[1] for r in connection.execute('PRAGMA table_info(tasks)')], ['id', 'title'])

    def test_restricted_ssh_rejects_shell_commands_and_partial_hashes(self):
        self.assertEqual(deploy.ssh_commit('deploy ' + NEW), NEW)
        for value in ('bash', 'backup', 'deploy main', 'deploy ' + NEW + '; id',
                      'deploy ' + NEW + '\n', 'deploy ' + NEW + ' extra'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                deploy.ssh_commit(value)


@unittest.skipUnless(os.name == 'posix', 'Release switching and systemd deployment run on Linux')
class DeploymentRecoveryTests(BackupTests):
    def setUp(self):
        super().setUp()
        self.shared = self.root / 'original'
        self.shared.mkdir()
        (self.shared / '.build-revision').write_text(OLD)
        self.state = self.root / 'deployment'
        self.state.mkdir()
        (self.state / 'releases').mkdir()
        self.release = self.state / 'releases' / (NEW + '-12345678')
        self.release.mkdir()
        (self.release / '.build-revision').write_text(NEW)
        self.config = deploy.Config(root=self.state, shared=self.shared, database=self.database)
        deploy.switch_release(self.config, self.shared)
        self.service = Mock()
        self.service.healthy.return_value = True

    def test_success_keeps_new_release_and_a_verified_backup(self):
        deploy.activate_release(self.config, self.release, self.service)
        self.assertEqual((self.state / 'current').resolve(), self.release)
        self.service.healthy.assert_called_once_with(NEW)
        result = json.loads((self.state / 'last-deployment.json').read_text())
        self.assertEqual(result['status'], 'success')
        with closing(sqlite3.connect(result['backup'])) as connection:
            self.assertEqual(connection.execute('SELECT title FROM tasks').fetchone()[0], 'Semester deadline')

    def test_failed_startup_restores_code_environment_and_database(self):
        def start():
            if (self.state / 'current').resolve() == self.release:
                with closing(sqlite3.connect(self.database)) as connection, connection:
                    connection.execute('ALTER TABLE tasks ADD COLUMN new_column TEXT')
                    connection.execute("UPDATE tasks SET title='Bad new state'")
        self.service.start.side_effect = start
        self.service.healthy.side_effect = [False, True]
        with self.assertRaisesRegex(RuntimeError, 'startup health check'):
            deploy.activate_release(self.config, self.release, self.service)
        self.assertEqual((self.state / 'current').resolve(), self.shared)
        self.assertEqual(self.service.start.call_count, 2)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute('SELECT * FROM tasks').fetchall(), [(1, 'Semester deadline')])
        self.assertEqual(json.loads((self.state / 'last-deployment.json').read_text())['status'], 'rolled_back')
        failed = list((self.state / 'backups').glob('failed-*.sqlite3'))
        self.assertEqual(len(failed), 1)
        with closing(sqlite3.connect(failed[0])) as connection:
            self.assertEqual(connection.execute('SELECT title FROM tasks').fetchone()[0], 'Bad new state')

    def test_backup_failure_restarts_previous_release_without_switching(self):
        with patch.object(deploy, 'backup_database', side_effect=OSError('Disk full')):
            with self.assertRaisesRegex(OSError, 'Disk full'):
                deploy.activate_release(self.config, self.release, self.service)
        self.assertEqual((self.state / 'current').resolve(), self.shared)
        self.service.healthy.assert_called_once_with(OLD)

    def test_failed_rollback_is_reported_as_failure(self):
        self.service.healthy.return_value = False
        with self.assertRaisesRegex(RuntimeError, 'previous release needs attention'):
            deploy.activate_release(self.config, self.release, self.service)
        self.assertEqual(json.loads((self.state / 'last-deployment.json').read_text())['status'], 'rollback_failed')

    def test_interruption_after_switch_restores_previous_release(self):
        self.service.start.side_effect = [InterruptedError('SSH disconnected'), None]
        with self.assertRaises(InterruptedError):
            deploy.activate_release(self.config, self.release, self.service)
        self.assertEqual((self.state / 'current').resolve(), self.shared)
        self.service.healthy.assert_called_once_with(OLD)

    def test_old_workflow_commit_is_skipped_before_build_or_stop(self):
        with patch.object(deploy, 'run'), patch.object(deploy, 'output', return_value=NEW):
            self.assertIsNone(deploy.prepare_release(self.config, OLD))
        self.assertEqual((self.state / 'current').resolve(), self.shared)
        self.service.stop.assert_not_called()

    def test_retention_keeps_protected_releases_and_unrelated_files(self):
        config = deploy.Config(root=self.state, shared=self.shared, database=self.database,
                               keep_releases=1, keep_backups=1)
        extra = self.state / 'releases' / ('c' * 40 + '-12345678')
        extra.mkdir()
        unrelated = self.state / 'releases' / 'do-not-delete'
        unrelated.mkdir()
        first = deploy.backup_database(config, 'daily')
        second = deploy.backup_database(config, 'daily')
        deploy.prune(config, {self.release})
        self.assertTrue(self.release.exists())
        self.assertTrue(unrelated.exists())
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())


class HealthCheckTests(unittest.TestCase):
    def test_alive_process_without_telegram_startup_is_unhealthy(self):
        config = deploy.Config(root=Path('/unused'), shared=Path('/unused'),
                               database=Path('/unused'), health_timeout=3)
        status = 'ActiveState=active\nSubState=running\nMainPID=123\nNRestarts=0'
        def result(*args):
            return status if args[0] == 'systemctl' else f'Task-Bot starting (polling mode, build={NEW[:7]})'
        with patch.object(deploy, 'output', side_effect=result), \
             patch.object(deploy.time, 'monotonic', side_effect=[0, 1, 2, 4]), \
             patch.object(deploy.time, 'sleep'):
            self.assertFalse(deploy.SystemdService(config).healthy(NEW))

    def test_crash_restart_is_unhealthy_even_if_service_is_running(self):
        config = deploy.Config(root=Path('/unused'), shared=Path('/unused'), database=Path('/unused'))
        with patch.object(deploy, 'output', return_value='ActiveState=active\nSubState=running\nMainPID=123\nNRestarts=1'):
            self.assertFalse(deploy.SystemdService(config).healthy(NEW))


if __name__ == '__main__':
    unittest.main()
