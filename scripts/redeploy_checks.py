#!/usr/bin/env python3
"""Offline checks: real SQLite locking/manual-state recovery; Docker/API are not called."""
from contextlib import closing
import sqlite3
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import redeploy


class RedeployChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kiwm-redeploy-check-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'data').mkdir()
        self.database = self.root / 'data/monitor.db'
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.executescript("CREATE TABLE runs(status TEXT); CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT); INSERT INTO state VALUES('manual','0');")
        self.running = [{'id': 'old-app', 'service': 'app'}, {'id': 'old-ingress', 'service': 'ingress'},
                        {'id': 'unchanged-browser', 'service': 'browser'}]
        self.calls = []

    def manual(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            return connection.execute("SELECT value FROM state WHERE key='manual'").fetchone()[0]

    def test_busy_projects_do_not_stop_any_container_or_change_manual_state(self):
        for status in ('queued', 'running'):
            with closing(sqlite3.connect(self.database)) as connection, connection:
                connection.execute('DELETE FROM runs')
                connection.execute('INSERT INTO runs VALUES(?)', (status,))
            with self.subTest(status=status), self.assertRaises(redeploy.DeployError):
                with redeploy.paused_app(self.root, self.running, run=self.calls.append):
                    self.fail('busy update entered')
            self.assertEqual(self.calls, [])
            self.assertEqual(self.manual(), '0')

    def test_enqueue_locked_until_app_stops_but_health_can_write_during_update(self):
        held = []
        def run(command):
            self.calls.append(command)
            if command[1] == 'stop' and command[-1] == 'old-app':
                with closing(sqlite3.connect(self.database, timeout=0)) as other, other:
                    with self.assertRaises(sqlite3.OperationalError):
                        other.execute('BEGIN IMMEDIATE')
                    held.append(True)
        with redeploy.paused_app(self.root, self.running, run=run) as outcome:
            self.assertEqual(self.manual(), '1')
            with closing(sqlite3.connect(self.database, timeout=0)) as health, health:
                health.execute('BEGIN IMMEDIATE')
                health.execute("INSERT INTO state VALUES('health','ok')")
            outcome.update(put_started=True, success=True)
        self.assertEqual(held, [True])
        self.assertEqual(self.manual(), '0')
        self.assertEqual([c[-1] for c in self.calls], ['old-ingress', 'old-app'])

    def test_pre_update_failure_restores_exact_old_ids_and_previous_manual_state(self):
        with self.assertRaisesRegex(RuntimeError, 'preflight changed'):
            with redeploy.paused_app(self.root, self.running, run=self.calls.append):
                raise RuntimeError('preflight changed')
        self.assertEqual(self.manual(), '0')
        self.assertEqual([c[-1] for c in self.calls if c[1] == 'start'], ['old-app', 'old-ingress'])
        self.assertNotIn('unchanged-browser', [c[-1] for c in self.calls])

    def test_stop_failure_rolls_back_uncommitted_manual_change(self):
        def run(command):
            self.calls.append(command)
            if command[1] == 'stop' and command[-1] == 'old-app':
                raise redeploy.DeployError('stop failed')
        with self.assertRaises(redeploy.DeployError):
            with redeploy.paused_app(self.root, self.running, run=run):
                self.fail('failed stop entered update')
        self.assertEqual(self.manual(), '0')
        self.assertEqual([c[-1] for c in self.calls if c[1] == 'start'], ['old-app', 'old-ingress'])

    def test_ambiguous_api_failure_keeps_automation_paused(self):
        with self.assertRaisesRegex(RuntimeError, 'API timeout'):
            with redeploy.paused_app(self.root, self.running, run=self.calls.append) as outcome:
                outcome['put_started'] = True
                raise RuntimeError('API timeout')
        self.assertEqual(self.manual(), '1')
        self.assertEqual([c[-1] for c in self.calls if c[1] == 'start'], ['old-app', 'old-ingress'])

    def test_success_does_not_restart_removed_ids_or_end_existing_manual_mode(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE state SET value='1' WHERE key='manual'")
        with redeploy.paused_app(self.root, self.running, run=self.calls.append) as outcome:
            outcome.update(put_started=True, success=True)
        self.assertEqual(self.manual(), '1')
        self.assertFalse(any(c[1] == 'start' for c in self.calls))

    def test_existing_env_is_preserved_and_explicit_local_image_update_is_allowed(self):
        existing = [{'name': 'UNCHANGED', 'value': 'kept-test-value'},
                    {'name': 'APP_IMAGE', 'value': 'local-app:old'}, {'name': 'WEB_PORT', 'value': '8765'}]
        result = redeploy.merged_env(existing, {'APP_IMAGE': 'local-app:new', 'WEB_PORT': '8765', 'FIXTURE_IP': '172.29.133.40'})
        self.assertEqual(result['UNCHANGED'], 'kept-test-value')
        self.assertEqual(result['APP_IMAGE'], 'local-app:new')
        self.assertEqual(existing[1]['value'], 'local-app:old')

    def test_conflicting_runtime_env_or_duplicate_values_are_rejected(self):
        with self.assertRaises(redeploy.DeployError):
            redeploy.merged_env([{'name': 'KIWM_ROOT', 'value': '/original'}], {'KIWM_ROOT': '/different'})
        with self.assertRaises(redeploy.DeployError):
            redeploy.merged_env([{'name': 'PORT', 'value': '1'}, {'name': 'PORT', 'value': '2'}], {})

    def test_release_migration_removes_only_obsolete_options(self):
        values={'APP_IMAGE':'old:test','KIWM_ROOT':'/old/root','KIWM_DATA':'/old/root/data',
                'VERSION':'1.1.0','KIWM_OMLX_API_KEY':'SYNTHETIC-RELEASE-KEY',
                'KIWM_AGENT_MODEL':'','KIWM_VISION_MODEL':'specific-vision','CUSTOM_SETTING':'keep'}
        updated=redeploy.release_env(values,True)
        self.assertNotIn('APP_IMAGE',updated);self.assertNotIn('KIWM_ROOT',updated)
        self.assertNotIn('KIWM_AGENT_MODEL',updated)
        for key in ('KIWM_DATA','VERSION','KIWM_OMLX_API_KEY','KIWM_VISION_MODEL','CUSTOM_SETTING'):
            self.assertEqual(updated[key],values[key])
        self.assertIn('APP_IMAGE',redeploy.release_env(values))

    def test_wrong_identity_or_git_stack_is_rejected(self):
        stack = {'Id': 2, 'EndpointId': 3, 'Name': 'test-stack', 'Type': 2, 'Status': 1}
        redeploy.identity(stack, 2, 3, 'test-stack')
        for key, value in [('Id', 9), ('EndpointId', 8), ('Name', 'other'), ('Type', 1), ('Status', 2), ('GitConfig', {'URL': 'unused'})]:
            with self.subTest(key=key), self.assertRaises(redeploy.DeployError):
                redeploy.identity(dict(stack, **{key: value}), 2, 3, 'test-stack')

    def test_docker_is_pinned_to_local_socket_without_changing_global_context(self):
        remote = {'DOCKER_HOST': 'ssh://remote.invalid', 'DOCKER_CONTEXT': 'remote-test',
                  'DOCKER_TLS_VERIFY': '1', 'DOCKER_CERT_PATH': '/unused'}
        completed = subprocess.CompletedProcess([], 0, stdout='ok', stderr='')
        with patch.dict(os.environ, remote), patch.object(redeploy.subprocess, 'run', return_value=completed) as run:
            self.assertEqual(redeploy.command(['docker', 'info']), 'ok')
            arguments, options = run.call_args
            self.assertEqual(arguments[0], ['docker', '--host', 'unix:///var/run/docker.sock', 'info'])
            self.assertFalse(any(k.startswith('DOCKER_') for k in options['env']))
            self.assertEqual(os.environ['DOCKER_CONTEXT'], 'remote-test')

    def test_asset_probes_are_isolated_and_do_not_mount_private_paths(self):
        model = {'services': {name: {'image': 'local-' + name + ':test'} for name in ('app', 'browser', 'proxy')}}
        redeploy.probe_assets(model, run=self.calls.append)
        self.assertEqual(len(self.calls), 3)
        for command in self.calls:
            self.assertEqual(command[:3], ['docker', 'run', '--rm'])
            for flag in ('--read-only', 'no-new-privileges:true'):
                self.assertIn(flag, command)
            self.assertEqual(command[command.index('--network') + 1], 'none')
            self.assertEqual(command[command.index('--cap-drop') + 1], 'ALL')
            self.assertNotIn('--mount', command)
            self.assertNotIn('-v', command)
        self.assertEqual(self.manual(), '0')

    def test_bad_image_permissions_fail_before_any_api_stop_or_database_change(self):
        model = {'services': {name: {'image': 'local-' + name + ':test'} for name in ('app', 'browser', 'proxy')}}
        def run(command):
            self.calls.append(command)
            if 'local-browser:test' in command:
                raise redeploy.DeployError('unreadable public image asset')
        with self.assertRaisesRegex(redeploy.DeployError, 'für browser fehlgeschlagen; keine App gestoppt'):
            redeploy.probe_assets(model, run=run)
        self.assertEqual(self.manual(), '0')
        self.assertTrue(all(command[1] == 'run' for command in self.calls))
        self.assertEqual(len(self.calls), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
