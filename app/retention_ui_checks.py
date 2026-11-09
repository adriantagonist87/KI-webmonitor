"""Storage controls and expired originals, using an isolated database/config.

Run inside the app image; no app lifespan or scheduler is started.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

scratch = tempfile.TemporaryDirectory(prefix='.retention-ui-check-', dir='/data')
os.environ['DATA_DIR'] = scratch.name
import store
store.CONFIG = Path(scratch.name) / 'settings.json'
store.SECRETS = Path(scratch.name) / 'credentials.json'
store.init()
import web
import engine
import retention
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

web.CONFIG = store.CONFIG
web.SECRETS = store.SECRETS

class StorageUI(unittest.TestCase):
    def setUp(self):
        self.original = {'omlx_url': 'http://model.test:8000/v1',
                         'analysis_model': 'local-model', 'smtp_enabled': False}
        store.save_json(store.CONFIG, self.original)
        self.client = TestClient(web.app)
        self.client.get('/')
        self.csrf = self.client.cookies['kiwm_csrf']

    def fields(self, **overrides):
        values = {'csrf': self.csrf, 'document_versions': '2',
                  'detail_days': '30', 'cache_days': '30'}
        values.update(overrides)
        return values

    def test_01_storage_rules_and_usage_are_in_maintenance(self):
        self.assertIn('href="/maintenance"', self.client.get('/settings').text)
        response = self.client.get('/maintenance')
        self.assertEqual(response.status_code, 200)
        page = BeautifulSoup(response.text, 'html.parser')
        section = page.select_one('details#speicher')
        self.assertIsNotNone(section)
        self.assertFalse(section.has_attr('open'))
        self.assertIn('Freier Platz auf dem Datenvolume', page.select_one('#speicherbelegung').get_text())
        self.assertEqual(section.select_one('[name=document_versions] option[selected]')['value'], '2')
        self.assertEqual(section.select_one('[name=detail_days]')['value'], '30')
        self.assertEqual(section.select_one('[name=cache_days]')['value'], '30')
        self.assertEqual(store.settings(), self.original)

    def test_02_valid_policy_preserves_other_settings(self):
        response = self.client.post('/settings/storage',
                                    data=self.fields(document_versions='1', detail_days='90', cache_days='7'),
                                    follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/settings?notice=storage_saved#speicher')
        self.assertEqual(store.settings(), {**self.original, 'retention': {
            'document_versions': 1, 'detail_days': 90, 'cache_days': 7}})

    def test_03_invalid_incomplete_and_duplicate_fields_do_not_write(self):
        for field, value in (('document_versions', '0'), ('document_versions', '4'),
                             ('detail_days', '6'), ('detail_days', '366'),
                             ('cache_days', '0'), ('cache_days', '91'),
                             ('detail_days', '3.0'), ('detail_days', '-30'),
                             ('detail_days', '３０'), ('detail_days', '30\n')):
            with self.subTest(field=field, value=value):
                response = self.client.post('/settings/storage', data=self.fields(**{field: value}))
                self.assertEqual(response.status_code, 400)
                self.assertEqual(store.settings(), self.original)
        missing = self.fields()
        del missing['cache_days']
        self.assertEqual(self.client.post('/settings/storage', data=missing).status_code, 400)
        duplicate = list(self.fields().items()) + [('detail_days', '90')]
        response = self.client.post('/settings/storage', content=urlencode(duplicate),
                                    headers={'content-type': 'application/x-www-form-urlencoded'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(store.settings(), self.original)

    def test_04_csrf_and_foreign_origin_are_rejected(self):
        with patch.object(retention, 'cleanup') as cleanup:
            self.assertEqual(self.client.post('/settings/storage', data=self.fields(csrf='wrong')).status_code, 403)
            self.assertEqual(self.client.post('/settings/storage/cleanup', data={'csrf': 'wrong'}).status_code, 403)
            self.assertEqual(self.client.post('/settings/storage/cleanup', data={'csrf': self.csrf},
                                             headers={'origin': 'https://foreign.test'}).status_code, 403)
            cleanup.assert_not_called()
        self.assertEqual(store.settings(), self.original)

    def test_05_manual_cleanup_holds_browser_job_lock(self):
        held = []

        def clean():
            held.append(engine.BUSY.locked())
            return {'status': 'cleaned', 'removed': {'files': 1, 'bytes': 1024}}

        with patch.object(retention, 'cleanup', side_effect=clean) as cleanup:
            response = self.client.post('/settings/storage/cleanup', data={'csrf': self.csrf}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/settings?notice=storage_cleaned#speicher')
        cleanup.assert_called_once_with()
        self.assertEqual(held, [True])
        self.assertFalse(engine.BUSY.locked())

    def test_06_busy_and_skipped_cleanup_never_claim_success(self):
        class Busy:
            def locked(self):
                return True

        with patch.object(engine, 'BUSY', Busy()), patch.object(retention, 'cleanup') as cleanup:
            response = self.client.post('/settings/storage/cleanup', data={'csrf': self.csrf})
            self.assertEqual(response.status_code, 409)
            cleanup.assert_not_called()
        with patch.object(retention, 'cleanup', return_value={'status': 'skipped', 'skipped': ['active run']}):
            response = self.client.post('/settings/storage/cleanup', data={'csrf': self.csrf})
            self.assertEqual(response.status_code, 409)
        self.assertEqual(store.settings(), self.original)

    def test_07_expired_original_keeps_text_and_source_links(self):
        pid, rid, sid = 'storage-ui-project', 'storage-ui-run', 'storage-ui-document'
        store.execute('''INSERT OR IGNORE INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created)
                         VALUES(?,?,?,?,?,?,?,?,?)''',
                      (pid, 'Projektalias', 'https://example.org', 'Pruefe Inhalte.', 'example.org',
                       '0 6 * * *', 0, store.next_due('0 6 * * *'), store.now()))
        summary = '### Sachliche Auswertung\n\nDie Frist wurde von 30 auf 45 Tage geaendert.'
        store.execute('INSERT OR IGNORE INTO runs(id,project_id,started,finished,status,summary,origin) VALUES(?,?,?,?,?,?,?)',
                      (rid, pid, store.now(), store.now(), 'success', summary, 'test'))
        text = 'Originaltext: Frist 45 Tage.'
        store.execute('INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (sid, rid, pid, 'https://example.org/richtlinie.pdf', 'document', store.now(),
                       store.digest(b'original'), store.digest(text), text, '[]', None, None, None))
        response = self.client.get('/documents/' + sid)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Originaldatei aus dem Archiv entfernt', response.text)
        self.assertIn(text, response.text)
        self.assertNotIn('href="/None', response.text)
        self.assertNotIn('Originaldatei herunterladen', response.text)
        response = self.client.get('/runs/' + rid)
        self.assertEqual(response.status_code, 200)
        page = BeautifulSoup(response.text, 'html.parser')
        self.assertIsNotNone(page.select_one('a[href="/documents/' + sid + '"]'))
        self.assertEqual(store.one('SELECT summary FROM runs WHERE id=?', (rid,))['summary'], summary)
        self.assertIn('Sachliche Auswertung', page.select_one('.report').get_text())


if __name__ == '__main__':
    unittest.main(verbosity=2)
