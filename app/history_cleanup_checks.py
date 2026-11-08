"""History cleanup tests with an isolated database and real archive files."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

import comparison_validity
import history_cleanup
import store
import transfer
import web


class HistoryCleanupChecks(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='history-check-', dir='/data')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for module, name, value in (
            (store, 'DATA', self.root), (web, 'DATA', self.root),
            (store, 'CONFIG', self.root/'settings.json'), (store, 'SECRETS', self.root/'credentials.json'),
        ):
            override = patch.object(module, name, value)
            override.start()
            self.addCleanup(override.stop)
        self.now = datetime.now(timezone.utc)
        store.init()
        self.shared = 'artifacts/objects/shared.pdf'
        self.old_detail = 'artifacts/alpha-old/detail.json'
        self.old_beta = 'artifacts/beta-old/document.pdf'
        for relative in (self.shared, self.old_detail, self.old_beta):
            target = self.root/relative
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(relative.encode())
        with store.db() as c:
            for pid in ('alpha', 'beta'):
                c.execute('''INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,last_success,created)
                    VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (pid, pid, 'https://example.org/', 'Prompt', 'example.org', '0 6 * * *',
                     0, self.stamp(-1), self.stamp(1), self.stamp(30)))
                for prefix in ('content_scopes:', 'site_auth:'):
                    c.execute('INSERT INTO state VALUES(?,?)', (prefix+pid, '{}'))
                c.execute('INSERT INTO state VALUES(?,?)', ('collection_index:'+pid+':sample', '{}'))
            for rid, days in (
                ('alpha-old', 10), ('alpha-middle', 5), ('alpha-new', 1),
                ('beta-old', 15), ('beta-middle', 12), ('beta-new', 8),
            ):
                self.add_run(c, rid, days)
            for rid, relative in (('alpha-old', self.shared), ('alpha-new', self.shared), ('beta-old', self.old_beta)):
                digest = store.digest((self.root/relative).read_bytes())
                c.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                          (rid+'-snapshot', rid, rid.split('-')[0], 'https://example.org/file.pdf',
                           'document', self.stamp(1), digest, store.digest(''), '', '[]', relative, None, None))
            for rid, previous in (('alpha-old', None), ('alpha-new', 'alpha-old-snapshot')):
                c.execute('INSERT INTO changes VALUES(?,?,?,?,?,?,?,?,?,?)',
                          (rid+'-change', 'alpha', rid, 'https://example.org/file.pdf', 'Datei geändert',
                           self.stamp(1), previous, rid+'-snapshot', '{}', rid))
                c.execute('INSERT INTO change_reviews VALUES(?,?)', (rid+'-change', self.stamp(1)))
                c.execute('INSERT INTO state VALUES(?,?)', (comparison_validity.PREFIX+rid+'-snapshot', '{}'))
            c.execute('INSERT INTO reports(id,period_key,created,start,end,content) VALUES(?,?,?,?,?,?)',
                      ('week', 'week-test', self.stamp(1), self.stamp(8), self.stamp(1), 'Archivierter Bericht'))
            c.execute('INSERT INTO report_changes VALUES(?,?)', ('alpha-old-change', 'week'))
            c.execute('INSERT INTO events(run_id,time,message) VALUES(?,?,?)', ('alpha-old', self.stamp(10), 'Geprüft'))
            c.execute('''INSERT INTO notifications(id,run_id,project_id,created,status,due_at,message)
                         VALUES(?,?,?,?,?,?,?)''',
                      ('notify', 'alpha-old', 'alpha', self.stamp(10), 'queued', self.stamp(10), 'Test'))

    def stamp(self, days):
        return (self.now - timedelta(days=days)).isoformat()

    def add_run(self, connection, rid, days, status='success'):
        connection.execute('INSERT INTO runs(id,project_id,started,finished,status,origin) VALUES(?,?,?,?,?,?)',
                           (rid, rid.split('-')[0], self.stamp(days), self.stamp(days), status, 'test'))

    def clean(self, mode, amount=None):
        selection = history_cleanup.preview(mode, amount)
        return history_cleanup.clean(mode, amount, selection['fingerprint'])

    def test_last_is_per_project_and_preserves_shared_files_and_reports(self):
        result = self.clean('last', 2)
        self.assertEqual((result['runs'], result['projects'], result['pending']), (2, 2, 0))
        self.assertEqual({r['id'] for r in store.rows('SELECT id FROM runs')},
                         {'alpha-new', 'alpha-middle', 'beta-new', 'beta-middle'})
        self.assertTrue((self.root/self.shared).is_file())
        for path in (self.old_detail, self.old_beta):
            self.assertFalse((self.root/path).exists())
        self.assertIsNone(store.one('SELECT previous_id FROM changes')['previous_id'])
        self.assertIsNone(store.one('SELECT value FROM state WHERE key=?',
                                    (comparison_validity.PREFIX+'alpha-old-snapshot',)))
        self.assertEqual(store.rows('SELECT change_id FROM change_reviews'), [{'change_id': 'alpha-new-change'}])
        self.assertEqual(store.rows('SELECT * FROM report_changes'), [])
        self.assertEqual(store.one('SELECT content FROM reports')['content'], 'Archivierter Bericht')
        self.assertEqual(store.rows('SELECT * FROM events'), [])
        self.assertEqual(store.rows('SELECT * FROM notifications'), [])
        store.execute("DELETE FROM state WHERE key LIKE 'site_auth:%' OR key LIKE 'collection_index:%'")
        archive = self.root/'post-cleanup.zip'
        transfer.export_to(archive, history=True)
        self.assertEqual(len(transfer.read_package(archive)['history']['runs']), 4)

    def test_days_uses_finish_time_and_resets_empty_project_baseline(self):
        store.execute("UPDATE runs SET finished=? WHERE id='alpha-old'", (self.stamp(2),))
        self.assertEqual(self.clean('days', 7)['runs'], 3)
        self.assertEqual(len(store.rows("SELECT id FROM runs WHERE project_id='alpha'")), 3)
        self.assertIsNone(store.one("SELECT last_success FROM projects WHERE id='beta'")['last_success'])
        self.assertIsNone(store.one("SELECT value FROM state WHERE key='collection_index:beta:sample'"))
        self.assertIsNotNone(store.one("SELECT value FROM state WHERE key='collection_index:alpha:sample'"))

    def test_all_retains_settings_projects_and_access(self):
        store.save_json(store.CONFIG, {'agent_model': 'test-model'})
        before = store.rows('SELECT id,name,prompt,enabled,next_due FROM projects ORDER BY id')
        self.assertEqual(self.clean('all')['runs'], 6)
        self.assertEqual(store.rows('SELECT id,name,prompt,enabled,next_due FROM projects ORDER BY id'), before)
        self.assertEqual(store.rows('SELECT * FROM runs'), [])
        self.assertEqual(store.rows('SELECT * FROM snapshots'), [])
        self.assertEqual(store.rows('SELECT * FROM changes'), [])
        self.assertEqual(store.rows('SELECT id FROM reports'), [{'id': 'week'}])
        self.assertEqual(len(store.rows("SELECT key FROM state WHERE key LIKE 'site_auth:%'")), 2)
        self.assertEqual(store.persisted_settings(), {'agent_model': 'test-model'})

    def test_running_or_stale_preview_is_rejected(self):
        planned = history_cleanup.preview('all', None)
        with store.db() as c:
            self.add_run(c, 'alpha-queued', 0, 'queued')
        with self.assertRaisesRegex(ValueError, 'wartende'):
            history_cleanup.clean('all', None, planned['fingerprint'])
        store.execute("UPDATE runs SET status='success' WHERE id='alpha-queued'")
        with self.assertRaisesRegex(ValueError, 'Vorschau'):
            history_cleanup.clean('all', None, planned['fingerprint'])
        self.assertEqual(len(store.rows('SELECT id FROM runs')), 7)

    def test_failed_file_removal_can_be_retried_after_commit(self):
        real_unlink = Path.unlink

        def unlink(path, missing_ok=False):
            if path == self.root/self.old_detail:
                raise PermissionError('simulated failure')
            return real_unlink(path, missing_ok=missing_ok)

        with patch.object(Path, 'unlink', unlink):
            self.assertEqual(self.clean('last', 2)['pending'], 1)
        self.assertTrue((self.root/self.old_detail).exists())
        self.assertEqual(history_cleanup.preview('last', 2)['pending'], 1)
        self.assertEqual(self.clean('last', 2)['pending'], 0)
        self.assertFalse((self.root/self.old_detail).exists())

    def test_unsafe_archive_path_aborts_before_removing_runs(self):
        (self.root/'artifacts/alpha-old/link').symlink_to(self.root/'monitor.db')
        with self.assertRaisesRegex(ValueError, 'Symbolisch'):
            self.clean('all')
        self.assertEqual(len(store.rows('SELECT id FROM runs')), 6)

    def test_preview_and_confirm_require_csrf_and_keep_the_selected_rule(self):
        client = TestClient(web.app)
        client.get('/settings')
        csrf = client.cookies['kiwm_csrf']
        preview = client.post('/settings/history/preview', data={'csrf': csrf, 'mode': 'last', 'amount': '2'})
        self.assertEqual(preview.status_code, 200)
        self.assertIn('2 Prüfungen in 2 Projekten', preview.text)
        form = BeautifulSoup(preview.text, 'html.parser').select_one('form[action="/settings/history/cleanup"]')
        fields = {i['name']: i.get('value', '') for i in form.select('input[type=hidden]')}
        self.assertEqual(client.post('/settings/history/cleanup', data=fields).status_code, 400)
        self.assertEqual(client.post('/settings/history/cleanup', data={**fields, 'csrf': 'bad', 'confirm': 'on'}).status_code, 403)
        applied = client.post('/settings/history/cleanup', data={**fields, 'confirm': 'on'}, follow_redirects=False)
        self.assertEqual(applied.status_code, 303)
        self.assertEqual(applied.headers['location'], '/settings?notice=history_cleaned#pruefverlauf')
        self.assertEqual(len(store.rows('SELECT id FROM runs')), 4)

    def test_invalid_values_are_rejected(self):
        for mode, value in (('invalid', '2'), ('last', '0'), ('days', '-7'), ('days', '36501'), ('days', '７')):
            with self.subTest(mode=mode, value=value), self.assertRaises(ValueError):
                history_cleanup.rule(mode, value)
