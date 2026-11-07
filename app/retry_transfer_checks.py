"""Isolated retry archive, import and full-database-restore integration checks."""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

scratch = tempfile.TemporaryDirectory(prefix='.retry-transfer-', dir='/data')
os.environ['DATA_DIR'] = scratch.name
import store
import transfer
import project_lifecycle
import scheduling

STAMP = datetime(2026, 11, 6, 1, 0, tzinfo=timezone.utc)
DUE = (STAMP + timedelta(minutes=30)).isoformat()


class RetryTransferChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=scratch.name)
        self.base = Path(self.directory.name)
        self.select('source')
        store.save_json(store.CONFIG, {'agent_model': 'Local test', 'global_schedule': scheduling.DEFAULT})
        store.save_json(store.SECRETS, {})
        self.project('alpha')

    def tearDown(self):
        self.directory.cleanup()

    def select(self, name, initialize=True):
        self.root = self.base / name
        self.root.mkdir(exist_ok=True)
        store.DATA = self.root
        store.CONFIG = self.root / 'settings.json'
        store.SECRETS = self.root / 'credentials.json'
        if initialize:
            store.init()

    def project(self, pid):
        store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created) VALUES(?,?,?,?,?,?,?,?,?)',
                      (pid, pid, 'https://example.org/', 'Read only.', 'example.org', '0 6 * * *', 1,
                       store.next_due('0 6 * * *'), STAMP.isoformat()))

    def add_run(self, rid='parent', pid='alpha', status='failed', origin='manual'):
        # Fixture data only: these tables never belong to the production DB.
        start = STAMP + (timedelta(minutes=30) if origin == 'retry' else timedelta(seconds=-10))
        finish = start + timedelta(seconds=10)
        if origin != 'retry':
            finish = STAMP
        store.execute('INSERT INTO runs(id,project_id,started,finished,status,error,summary,origin) VALUES(?,?,?,?,?,?,?,?)',
                      (rid, pid, start.isoformat(), finish.isoformat(), status, 'Test failure', 'Archived result.', origin))

    def retry(self, source='parent', pid='alpha', state='pending', child=None):
        store.execute('INSERT INTO run_retries(source_run_id,project_id,due_at,retry_run_id,state) VALUES(?,?,?,?,?)',
                      (source, pid, DUE, child, state))

    def package(self, history=True):
        path = self.base / ('archive.zip' if history else 'config.json')
        value = transfer.export_to(path, history=history, include_secrets=False)
        return path, value

    def archive(self, name, value):
        path = self.base / name
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('webmonitor.json', transfer.dump(value))
        return path

    def test_01_configuration_does_not_export_runtime_retry_timer(self):
        self.add_run(); self.retry()
        path, value = self.package(history=False)
        self.assertEqual(value['version'], transfer.VERSION)
        self.assertIsNone(value['history'])
        self.assertNotIn('run_retries', value)
        self.assertIsNone(transfer.read_package(path)['credentials'])

    def test_02_archive_preserves_pending_lineage_but_import_does_not_replay(self):
        self.add_run(); self.retry()
        path, value = self.package()
        saved = transfer.read_package(path)['history']['run_retries']
        self.assertEqual(saved, [{'source_run_id': 'parent', 'project_id': 'alpha', 'due_at': DUE,
                                 'retry_run_id': None, 'state': 'pending'}])
        self.select('target')
        transfer.import_package(path, value, activate=True)
        self.assertEqual(store.one('SELECT state FROM run_retries')['state'], 'cancelled')
        self.assertEqual(store.one('SELECT status FROM runs')['status'], 'failed')
        self.assertEqual(store.rows("SELECT id FROM runs WHERE status IN ('queued','running')"), [])
        store.init(); store.init()
        self.assertEqual(store.one('SELECT state FROM run_retries')['state'], 'cancelled')
        again = transfer.import_package(path, value, activate=True)
        self.assertTrue(all(number == 0 for number in again['added'].values()))
        self.assertEqual(len(store.rows('SELECT * FROM run_retries')), 1)

    def test_03_consumed_failed_retry_remains_consumed(self):
        self.add_run(); self.add_run('child', origin='retry'); self.retry(state='finished', child='child')
        path, value = self.package()
        self.select('target'); transfer.import_package(path, value, activate=True)
        self.assertEqual(store.one('SELECT * FROM run_retries'), value['history']['run_retries'][0])
        store.init()
        self.assertEqual(store.one("SELECT origin,status FROM runs WHERE id='child'"), {'origin': 'retry', 'status': 'failed'})
        self.assertEqual(len(store.rows('SELECT * FROM run_retries')), 1)

    def test_04_config_replacement_cancels_only_affected_existing_pending_plan(self):
        self.add_run(); self.retry()
        path, value = self.package(history=False)
        self.project('beta'); self.add_run('beta-parent', pid='beta'); self.retry('beta-parent', 'beta')
        transfer.import_package(path, value, apply_settings=False, activate=True)
        self.assertEqual(store.one("SELECT state FROM run_retries WHERE project_id='alpha'")['state'], 'cancelled')
        self.assertEqual(store.one("SELECT state FROM run_retries WHERE project_id='beta'")['state'], 'pending')

    def test_05_all_legacy_archive_versions_import_without_backfilling_failed_runs(self):
        self.add_run(); self.retry()
        _, original = self.package()
        for version in range(1, 6):
            with self.subTest(version=version):
                value = copy.deepcopy(original)
                value['version'] = version; value['history'].pop('run_retries')
                for project in value['projects']:project.pop('telegram_enabled')
                if version == 1:
                    value.pop('site_auth')
                if version < 3:
                    value.pop('tag_registry')
                    for project in value['projects']:
                        project.pop('use_global_schedule'); project.pop('schedule_config')
                path = self.archive('legacy-' + str(version) + '.zip', value)
                self.select('legacy-target-' + str(version))
                transfer.import_package(path, transfer.read_package(path), activate=True)
                store.init()
                self.assertEqual(store.rows('SELECT * FROM run_retries'), [])
                self.assertEqual(store.one('SELECT status FROM runs')['status'], 'failed')

    def test_06_invalid_retry_parent_date_state_and_child_are_rejected(self):
        self.add_run(); self.retry()
        _, original = self.package()
        variants = []
        for field, value in [('due_at', (STAMP + timedelta(minutes=29)).isoformat()),
                             ('due_at', '2026-11-06T01:30:00'), ('state', 'again'),
                             ('retry_run_id', 'parent'), ('project_id', 'missing')]:
            bad = copy.deepcopy(original); bad['history']['run_retries'][0][field] = value; variants.append(bad)
        bad = copy.deepcopy(original); bad['history']['runs'][0]['origin'] = 'retry'; variants.append(bad)
        bad = copy.deepcopy(original); bad['history']['run_retries'][0]['state'] = 'queued'; variants.append(bad)
        for value in variants:
            with self.assertRaises(transfer.Invalid):
                transfer.validate(value)

    def test_07_one_retry_source_and_one_child_cannot_be_duplicated(self):
        self.add_run(); self.add_run('child', origin='retry'); self.retry(state='finished', child='child')
        _, original = self.package()
        bad = copy.deepcopy(original)
        bad['history']['run_retries'].append(copy.deepcopy(bad['history']['run_retries'][0]))
        with self.assertRaises(transfer.Invalid): transfer.validate(bad)
        bad = copy.deepcopy(original)
        parent = copy.deepcopy(bad['history']['runs'][0]); parent['id'] = 'second-parent'
        bad['history']['runs'].append(parent)
        second = copy.deepcopy(bad['history']['run_retries'][0]); second['source_run_id'] = 'second-parent'
        bad['history']['run_retries'].append(second)
        with self.assertRaises(transfer.Invalid): transfer.validate(bad)

    def test_08_cancelled_never_started_retry_history_is_portable(self):
        self.add_run(); self.add_run('child', status='cancelled', origin='retry'); self.retry(state='cancelled', child='child')
        path, value = self.package()
        self.select('target'); transfer.import_package(path, value, activate=True)
        self.assertEqual(store.one("SELECT status FROM runs WHERE id='child'")['status'], 'cancelled')
        self.assertEqual(store.one('SELECT state FROM run_retries')['state'], 'cancelled')
        bad = copy.deepcopy(value)
        next(r for r in bad['history']['runs'] if r['id'] == 'child')['origin'] = 'manual'
        with self.assertRaises(transfer.Invalid): transfer.validate(bad)

    def test_09_failed_config_import_rolls_back_retry_cancellation(self):
        self.add_run(); self.retry()
        path, value = self.package(history=False)
        value['projects'][0]['name'] = 'Changed by import'
        with patch('store.save_json', side_effect=OSError('Simulated write failure')):
            with self.assertRaises(OSError): transfer.import_package(path, value, activate=True)
        self.assertEqual(store.one('SELECT name FROM projects')['name'], 'alpha')
        self.assertEqual(store.one('SELECT state FROM run_retries')['state'], 'pending')

    def test_10_confirmed_project_delete_cascades_retry_without_orphans(self):
        self.add_run(); self.retry()
        project_lifecycle.remove('alpha')
        self.assertEqual(store.rows('SELECT * FROM projects'), [])
        self.assertEqual(store.rows('SELECT * FROM run_retries'), [])
        with store.db() as connection:
            self.assertIsNone(connection.execute('PRAGMA foreign_key_check').fetchone())

    def test_11_full_database_restore_preserves_exact_pending_timer(self):
        self.add_run(); self.retry()
        before = store.one('SELECT * FROM run_retries')
        config = store.CONFIG.read_bytes(); credentials = store.SECRETS.read_bytes()
        target = self.base / 'database-restore'; target.mkdir()
        with sqlite3.connect(store.DATA / 'monitor.db') as source, sqlite3.connect(target / 'monitor.db') as copy_db:
            source.backup(copy_db)
        self.select('database-restore', initialize=False)
        store.CONFIG.write_bytes(config); store.SECRETS.write_bytes(credentials)
        store.init(); store.init()
        self.assertEqual(store.one('SELECT * FROM run_retries'), before)
        self.assertEqual(len(store.rows('SELECT * FROM runs')), 1)

    def test_12_conflicting_consumed_child_is_rejected_before_import(self):
        self.add_run(); self.add_run('child', origin='retry'); self.retry(state='finished', child='child')
        path, value = self.package()
        self.add_run('other-child', origin='retry')
        store.execute("UPDATE run_retries SET retry_run_id='other-child'")
        with self.assertRaises(transfer.Invalid): transfer.import_package(path, value, activate=True)
        self.assertEqual(store.one('SELECT retry_run_id FROM run_retries')['retry_run_id'], 'other-child')

    def test_13_preview_explains_only_affected_pending_timers_without_duplicates(self):
        self.add_run(); self.retry()
        _, archive = self.package()
        self.assertEqual(transfer.preview(archive)['retry_plans_cancelled'], 1)
        _, config = self.package(history=False)
        self.project('beta'); self.add_run('beta-parent', pid='beta'); self.retry('beta-parent', 'beta')
        self.assertEqual(transfer.preview(config)['retry_plans_cancelled'], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
