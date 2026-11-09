"""Maintenance safety and web-flow regressions with disposable archives."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from starlette.datastructures import FormData

import engine
import maintenance
import report_mail
import retention
import store
import transfer
import web


class MaintenanceChecks(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory(dir=store.DATA)
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        for name, value in (('DATA', self.root), ('CONFIG', self.root/'settings.json'), ('SECRETS', self.root/'secrets.json')):
            for module in (store, web):
                override = patch.object(module, name, value)
                override.start()
                self.addCleanup(override.stop)
        self.stamp = datetime.now(timezone.utc).isoformat()
        store.init()
        for pid in ('alpha', 'beta'):
            store.execute('''INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,last_success,created)
                             VALUES(?,?,?,?,?,?,?,?,?,?)''', (pid, pid.title(), 'https://example.org/', 'Prompt',
                              'example.org', '0 6 * * *', 0, self.stamp, self.stamp, self.stamp))
        self.add_run('alpha', 'a-old', 10)
        self.add_run('alpha', 'a-new', 1)
        self.add_run('beta', 'b-new', 1)
        original = b'%PDF-1.7 test'
        self.document = 'artifacts/objects/' + store.digest(original) + '.pdf'
        self.file(self.document, original)
        self.snap('a-new', 'alpha', 'a-doc', 'document', self.document)
        self.snap('b-new', 'beta', 'b-doc', 'document', self.document)
        self.detail = 'artifacts/a-old/capture.html'
        self.file(self.detail, b'<main>Stored text</main>')
        self.snap('a-old', 'alpha', 'a-page', 'page', html=self.detail)
        store.execute('INSERT INTO events(run_id,time,message) VALUES(?,?,?)', ('a-old', self.stamp, 'Check'))

    def file(self, relative, raw):
        path = self.root/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)

    def add_run(self, pid, rid, days, status='success'):
        stamp = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        store.execute('INSERT INTO runs(id,project_id,started,finished,status,origin) VALUES(?,?,?,?,?,?)',
                      (rid, pid, stamp, stamp, status, 'manual'))

    def snap(self, rid, pid, sid, kind, file=None, html=None):
        raw_hash = store.digest((self.root/file).read_bytes()) if file else store.digest('raw')
        text = '' if kind == 'document' else 'Stored text'
        url = 'https://example.org/' + ('item.pdf' if kind == 'document' else '')
        store.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (sid, rid, pid, url, kind, self.stamp, raw_hash, store.digest(text), text, '[]', file, None, html))

    def options(self, *items, kind='cleanup', global_areas=()):
        return maintenance.selection(FormData([('kind', kind), *[('item', value) for value in items],
                                               *[('global', value) for value in global_areas]]))

    def clean(self, options):
        return maintenance.clean(options, maintenance.preview(options)['fingerprint'])

    def test_overview_separates_shared_document_and_comparison_usage(self):
        data = maintenance.overview()
        length = (self.root/self.document).stat().st_size
        self.assertEqual([p['documents'] for p in data['projects']], [length, length])
        self.assertEqual(data['usage']['documents_bytes'], length)
        self.assertEqual(data['usage']['history_files_bytes'], (self.root/self.detail).stat().st_size)
        self.assertGreater(data['projects'][0]['history'], data['usage']['history_files_bytes'])
        self.assertGreater(data['disk'].total, 0)

    def test_document_cleanup_preserves_hashes_and_remaining_references(self):
        self.assertEqual(self.clean(self.options('documents:alpha'))['removed_files'], 0)
        self.assertTrue((self.root/self.document).exists())
        self.assertIsNone(store.one("SELECT file FROM snapshots WHERE id='a-doc'")['file'])
        self.assertEqual(engine.previous_document('alpha', 'next', 'https://example.org/item.pdf')['sha256'],
                         Path(self.document).stem)
        self.assertEqual(self.clean(self.options('documents:beta'))['removed_files'], 1)
        self.assertFalse((self.root/self.document).exists())
        self.assertEqual(len(store.rows("SELECT id FROM snapshots WHERE kind='document'")), 2)

    def test_history_preserves_an_older_baseline_and_limits_cleanup_to_selected_projects(self):
        options = self.options('history:alpha')
        self.assertEqual(maintenance.preview(options)['runs'], 0)
        self.add_run('alpha', 'a-before', 20)
        self.add_run('beta', 'b-before', 20)
        self.assertEqual(self.clean(options)['runs'], 1)
        self.assertTrue((self.root/self.detail).exists())
        self.assertEqual({r['id'] for r in store.rows('SELECT id FROM runs')}, {'a-old', 'a-new', 'b-new', 'b-before'})

    def test_reset_removes_comparison_state_and_preserves_project_configuration(self):
        for key in ('content_scopes:alpha', 'collection_index:alpha:main', 'site_auth:alpha', 'navigation_macros:alpha'):
            store.execute('INSERT INTO state VALUES(?,?)', (key, '{}'))
        result = self.clean(self.options(kind='reset'))
        self.assertEqual(result['runs'], 3)
        self.assertEqual(store.rows('SELECT * FROM snapshots'), [])
        self.assertEqual(store.rows('SELECT last_success FROM projects'), [{'last_success': None}]*2)
        self.assertEqual(store.one("SELECT prompt FROM projects WHERE id='alpha'")['prompt'], 'Prompt')
        self.assertIsNone(engine.previous_document('alpha', 'next', 'https://example.org/item.pdf'))
        self.assertEqual({r['key'] for r in store.rows("SELECT key FROM state WHERE key LIKE '%:alpha%'")},
                         {'site_auth:alpha', 'navigation_macros:alpha'})

    def test_report_cleanup_preserves_open_delivery_and_no_resend_marker(self):
        for rid, key, status, parent in (('week', 'week-2026-45', 'sent', None),
                                          ('child', 'week-2026-45-distribution-a', 'sent', 'week'),
                                          ('next', 'week-2026-46', 'pending', None)):
            store.execute('INSERT INTO reports(id,period_key,created,start,end,content,mail_status,parent_id) VALUES(?,?,?,?,?,?,?,?)',
                          (rid, key, self.stamp, self.stamp, self.stamp, 'Report', status, parent))
            self.file('reports/'+rid+'.html', b'Report')
        store.execute('INSERT INTO changes VALUES(?,?,?,?,?,?,?,?,?,?)',
                      ('change', 'alpha', 'a-old', 'https://example.org/', 'Inhalt geändert', self.stamp,
                       None, 'a-page', '{}', 'change'))
        store.execute('INSERT INTO report_changes VALUES(?,?)', ('change', 'week'))
        options = self.options(global_areas=('reports',))
        self.assertEqual(maintenance.preview(options)['deferred'], 1)
        self.assertEqual(self.clean(options)['removed_files'], 2)
        self.assertEqual(store.one("SELECT mail_status,content FROM reports WHERE id='week'"), {'mail_status': 'purged', 'content': ''})
        self.assertIn('purged', report_mail.TERMINAL)
        self.assertEqual(store.one('SELECT report_id FROM report_changes')['report_id'], 'week')
        self.assertEqual(report_mail.due_ids(), ['next'])
        self.assertTrue((self.root/'reports/next.html').exists())
        client = TestClient(web.app)
        self.assertEqual(client.get('/reports/week').status_code, 404)
        self.assertEqual(len(BeautifulSoup(client.get('/reports').text, 'html.parser').select('a.report-name')), 1)
        archive = self.root/'report-backup.zip'
        transfer.export_to(archive, history=True)
        self.assertEqual({r['id']: r['mail_status'] for r in transfer.read_package(archive)['history']['reports']},
                         {'week': 'purged', 'next': 'pending'})

    def test_logs_cleanup_keeps_pending_telegram_and_comparison_records(self):
        self.add_run('beta', 'sent', 3)
        for rid, status in (('b-new', 'queued'), ('sent', 'sent')):
            store.execute('''INSERT INTO notifications(id,run_id,project_id,created,status,due_at,message)
                             VALUES(?,?,?,?,?,?,?)''', (rid, rid, 'beta', self.stamp, status, self.stamp, 'Log'))
        self.file('logs/app.log', b'old log')
        result = self.clean(self.options(global_areas=('logs',)))
        self.assertEqual((result['logs'], result['removed_files']), (2, 1))
        self.assertEqual(store.rows('SELECT status FROM notifications'), [{'status': 'queued'}])
        self.assertEqual(len(store.rows('SELECT id FROM snapshots')), 3)

    def test_storage_policy_preview_cannot_be_applied_after_policy_change(self):
        options = self.options(kind='policy')
        planned = maintenance.preview(options)
        self.assertEqual(planned['fingerprint'], retention.inspect()['fingerprint'])
        store.save_json(store.CONFIG, {'retention': {'document_versions': 1}})
        with self.assertRaisesRegex(ValueError, 'Vorschau'):
            maintenance.clean(options, planned['fingerprint'])

    def test_preview_does_not_mutate_and_stale_selection_and_queued_runs_are_rejected(self):
        options = self.options('documents:*')
        planned = maintenance.preview(options)
        self.assertIsNotNone(store.one("SELECT file FROM snapshots WHERE id='a-doc'")['file'])
        self.add_run('alpha', 'waiting', 0, 'queued')
        with self.assertRaisesRegex(ValueError, 'wartende Prüfungen'):
            maintenance.clean(options, planned['fingerprint'])
        store.execute("UPDATE runs SET status='success' WHERE id='waiting'")
        self.file(self.document, b'new bytes')
        with self.assertRaisesRegex(ValueError, 'Vorschau'):
            maintenance.clean(options, planned['fingerprint'])
        self.assertTrue((self.root/self.document).exists())

    def test_failed_unlink_is_retried_and_leaves_database_references_consistent(self):
        original = Path.unlink

        def unlink(path, *args, **kwargs):
            if path == self.root/self.document:
                raise PermissionError('simulated failure')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'unlink', unlink):
            self.assertEqual(self.clean(self.options('documents:*'))['pending'], 1)
        self.assertFalse(store.rows('SELECT file FROM snapshots WHERE file IS NOT NULL'))
        self.assertEqual(self.clean(self.options(kind='retry'))['pending'], 0)
        self.assertFalse((self.root/self.document).exists())

    def test_web_preview_csrf_and_typed_reset_confirmation(self):
        client = TestClient(web.app)
        page = BeautifulSoup(client.get('/maintenance').text, 'html.parser')
        self.assertEqual(page.select_one('.nav-link[aria-current=page]').get_text(strip=True), 'Wartung & Bereinigung')
        self.assertEqual(len(page.select('.maintenance-table input[type=checkbox]')), 9)
        csrf = client.cookies['kiwm_csrf']
        preview = client.post('/maintenance/preview', data={'csrf': csrf, 'kind': 'reset'})
        self.assertEqual(preview.status_code, 200)
        form = BeautifulSoup(preview.text, 'html.parser').select_one('#vorschau form')
        fields = {node['name']: node['value'] for node in form.select('input[type=hidden]')}
        self.assertEqual(client.post('/maintenance/cleanup', data={**fields, 'csrf': 'bad'}).status_code, 403)
        self.assertEqual(client.post('/maintenance/cleanup', data={**fields, 'confirm': 'on'}).status_code, 400)
        response = client.post('/maintenance/cleanup', data={**fields, 'confirm': 'on', 'reset_confirmation': 'NEUSTART'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Neuer Vergleichsstand vorgemerkt', response.text)
