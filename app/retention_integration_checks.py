"""Real archive/export boundaries after retention; no browser or inference.

Run in the application image: python -X utf8 -m retention_integration_checks
All database, settings, credentials and original files are synthetic and live
below a new temporary directory. Archive comparisons and ZIP imports are real.
"""
from datetime import datetime, timedelta, timezone
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch


scratch = tempfile.TemporaryDirectory(prefix='.retention-integration-', dir='/data')
os.environ['DATA_DIR'] = scratch.name
os.environ.setdefault('ANONYMIZED_TELEMETRY', 'false')
os.environ.setdefault('DO_NOT_TRACK', '1')
import store
store.CONFIG = Path(scratch.name) / 'bootstrap-settings.json'
store.SECRETS = Path(scratch.name) / 'bootstrap-credentials.json'
import engine
import retention
import transfer
from fixture import pdf
from PIL import Image


PID = 'retention-project'
SOURCE = 'https://source.test/anlage.pdf'
PAGE = 'https://source.test/unterlagen/'
AT = datetime(2030, 6, 1, 12, tzinfo=timezone.utc)
POLICY = {'document_versions': 2, 'detail_days': 30, 'cache_days': 30}


class RetentionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=scratch.name)
        self.base = Path(self.directory.name)
        self.no_inference = AsyncMock(side_effect=AssertionError('Inference forbidden in retention tests'))
        self.guards = [
            patch.object(engine, 'llm', self.no_inference),
            patch.object(engine.httpx.Client, 'send', side_effect=AssertionError('Network forbidden')),
            patch.object(engine.httpx.AsyncClient, 'send', side_effect=AssertionError('Network forbidden')),
        ]
        for guard in self.guards:
            guard.start()
        self.select('source')
        store.save_json(store.CONFIG, {'retention': POLICY})
        store.save_json(store.SECRETS, {})
        store.execute('''INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created)
            VALUES(?,?,?,?,?,?,?,?,?)''',
            (PID, 'Unterlagen-Test', PAGE, 'Änderungen an Anlagen beobachten.',
             'source.test', '0 6 * * *', 0, store.next_due('0 6 * * *'), store.now()))
        self.project = store.one('SELECT * FROM projects WHERE id=?', (PID,))

    def tearDown(self):
        self.no_inference.assert_not_awaited()
        for guard in reversed(self.guards):
            guard.stop()
        self.directory.cleanup()

    def select(self, name):
        root = self.base / name
        root.mkdir(exist_ok=True)
        store.DATA = engine.DATA = root
        store.CONFIG = root / 'settings.json'
        store.SECRETS = root / 'credentials.json'
        store.init()
        self.root = root

    def run_at(self, rid, days):
        instant = AT - timedelta(days=days)
        stamp = instant.isoformat()
        store.execute('''INSERT INTO runs(id,project_id,started,finished,status,summary,origin)
            VALUES(?,?,?,?,?,?,?)''',
            (rid, PID, stamp, stamp, 'success', '## Auswertung\nSynthetischer Prüfstand.', 'test'))
        return stamp

    def document(self, rid, version, days):
        stamp = self.run_at(rid, days)
        raw = pdf('Deadline ' + version + ' days')
        with patch.object(engine, 'now', return_value=stamp):
            sid = engine.archive(self.project, rid, SOURCE, 'document', raw, '', suffix='.pdf')
        snap = store.one('SELECT * FROM snapshots WHERE id=?', (sid,))
        extraction = {'version': 1, 'source_sha256': snap['sha256'],
                      'method': 'hash_only', 'comparison_mode': 'sha256',
                      'content_analysis_performed': False, 'original_sha256_verified': True}
        store.save_json(self.root / 'artifacts' / rid / (sid + '.extraction.json'), extraction)
        store.save_json(self.root / 'artifacts' / rid / 'evidence.json', {
            'agent': {'complete': True, 'pending': []}, 'errors': [],
            'document_check': {'found': 1, 'checked': 1, 'failed': [], 'unverified': [],
                               'documents': [{'url': SOURCE, 'status': 'checked', 'extraction': extraction,
                                              'hashes': {k: snap[k] for k in ('id', 'sha256', 'text_sha256')}}]},
        })
        return snap

    def three_versions(self):
        return [self.document('version-a', '10', 90), self.document('version-b', '20', 60),
                self.document('version-c', '30', 1)]

    def test_01_only_two_distinct_originals_survive_and_next_comparison_is_correct(self):
        before = self.three_versions()
        report_count = len(store.rows('SELECT * FROM changes'))
        result = retention.cleanup(at=AT)
        self.assertEqual(result['status'], 'cleaned', result)
        expired = store.one('SELECT * FROM snapshots WHERE id=?', (before[0]['id'],))
        self.assertIsNotNone(expired, 'Change-linked historical snapshot and hashes must remain')
        self.assertIsNone(expired['file'])
        self.assertFalse((self.root / before[0]['file']).exists())
        for snap in before[1:]:
            actual = store.one('SELECT * FROM snapshots WHERE id=?', (snap['id'],))
            self.assertEqual(actual['file'], snap['file'])
            self.assertEqual(hashlib.sha256((self.root / actual['file']).read_bytes()).hexdigest(), snap['sha256'])
        self.assertEqual(len(list((self.root / 'artifacts/objects').iterdir())), 2)
        self.assertEqual(len(store.rows('SELECT * FROM changes')), report_count)
        unchanged = self.document('repeat-c', '30', 0)
        self.assertEqual(store.rows('SELECT * FROM changes WHERE run_id=?', ('repeat-c',)), [])
        self.assertEqual(unchanged['file'], before[2]['file'])
        self.assertEqual(len(list((self.root / 'artifacts/objects').iterdir())), 2)
        changed = self.document('version-d', '31', -1)
        differences = store.rows('SELECT * FROM changes WHERE run_id=?', ('version-d',))
        self.assertEqual(len(differences), 1)
        self.assertEqual(differences[0]['kind'], 'Datei geändert')
        self.assertEqual(differences[0]['previous_id'], unchanged['id'])
        self.assertEqual(differences[0]['current_id'], changed['id'])
        evidence = json.loads(differences[0]['evidence'])
        self.assertNotIn('text_diff', evidence)
        self.assertEqual(evidence['comparison_mode'], 'sha256')
        self.assertFalse(evidence['content_analysis_performed'])
        proof = evidence['document_comparison']
        self.assertEqual(proof['previous_snapshot_id'], unchanged['id'])
        self.assertEqual(proof['current_snapshot_id'], changed['id'])
        self.assertEqual(proof['previous_sha256'], unchanged['sha256'])
        self.assertEqual(proof['current_sha256'], changed['sha256'])
        self.assertNotEqual(proof['previous_sha256'], proof['current_sha256'])
        self.assertTrue(proof['originals_verified'])
        self.assertEqual(unchanged['text'], '')
        self.assertEqual(changed['text'], '')
        self.assertEqual(changed['text_sha256'], store.digest(''))

    def test_02_expired_original_keeps_exact_text_hashes_and_report_history(self):
        before = self.three_versions()
        rows_before = {row['id']: row for row in store.rows('SELECT * FROM changes')}
        store.execute('''INSERT INTO reports(id,period_key,created,start,end,content,mail_status)
            VALUES(?,?,?,?,?,?,?)''', ('saved-report', 'week-retention-test', AT.isoformat(),
                                     (AT - timedelta(days=7)).isoformat(), AT.isoformat(),
                                     'Friständerung von 20 auf 30 Tage.', 'sent'))
        store.execute('INSERT INTO report_changes VALUES(?,?)', (next(iter(rows_before)), 'saved-report'))
        retention.cleanup(at=AT)
        for original in before:
            actual = store.one('SELECT * FROM snapshots WHERE id=?', (original['id'],))
            self.assertEqual(actual['text'], original['text'])
            self.assertEqual(actual['sha256'], original['sha256'])
            self.assertEqual(actual['text_sha256'], original['text_sha256'])
            self.assertEqual(store.digest(actual['text']), actual['text_sha256'])
        self.assertEqual({row['id']: row for row in store.rows('SELECT * FROM changes')}, rows_before)
        self.assertEqual(store.one('SELECT content FROM reports')['content'], 'Friständerung von 20 auf 30 Tage.')
        self.assertEqual(len(store.rows('SELECT * FROM report_changes')), 1)

    def page_section(self, rid, identity, text, days):
        stamp = self.run_at(rid, days)
        scope = engine.section_scope(PAGE, identity)
        folder = self.root / 'artifacts' / rid
        folder.mkdir(exist_ok=True)
        html_path = 'artifacts/' + rid + '/section.html'
        screenshot = 'artifacts/' + rid + '/section.png'
        html = '<main data-kiwm-scope="' + scope + '" data-kiwm-visual-scope="viewport-v1">' + text + '</main>'
        (self.root / html_path).write_text(html, encoding='utf-8')
        image = Image.new('RGB', (20, 20), 'white')
        image.save(self.root / screenshot)
        with patch.object(engine, 'now', return_value=stamp):
            sid = engine.archive(self.project, rid, PAGE, 'page', html.encode(), text,
                                 links=[SOURCE], screenshot=screenshot, html=html_path, scope=scope)
        return sid, scope, html_path, screenshot

    def test_03_independent_accordion_baselines_survive_even_when_older_than_details_limit(self):
        faq = self.page_section('old-faq', 'FAQ', 'FAQ zum Anschluss: Stand 1', 90)
        self.page_section('old-leaflets', 'Merkblätter', 'Merkblatt 1', 80)
        leaflets = self.page_section('latest-leaflets', 'Merkblätter', 'Merkblatt 1', 1)
        result = retention.cleanup(at=AT)
        self.assertEqual(result['status'], 'cleaned', result)
        for sid, scope, html, screenshot in (faq, leaflets):
            self.assertTrue((self.root / html).is_file())
            self.assertTrue((self.root / screenshot).is_file())
            previous, scope_changed = engine.previous_in_scope(PID, 'next-run', PAGE, scope)
            self.assertFalse(scope_changed)
            self.assertEqual(previous['id'], sid)
            self.assertEqual(engine.snapshot_scope(previous), scope)
        repeated = self.page_section('repeat-faq', 'FAQ', 'FAQ zum Anschluss: Stand 1', 0)
        self.assertEqual(store.rows('SELECT * FROM changes WHERE run_id=?', ('repeat-faq',)), [])
        updated = self.page_section('changed-leaflets', 'Merkblätter', 'Merkblatt 2', -1)
        changes = store.rows('SELECT * FROM changes WHERE run_id=?', ('changed-leaflets',))
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]['previous_id'], leaflets[0])
        self.assertEqual(changes[0]['current_id'], updated[0])
        self.assertNotEqual(repeated[1], updated[1])

    def test_04_extended_export_reimport_preserves_expiration_and_remains_idempotent(self):
        before = self.three_versions()
        retention.cleanup(at=AT)
        original_counts = {name: len(store.rows('SELECT * FROM ' + name)) for name in transfer.TABLES}
        archive = self.base / 'after-retention.zip'
        payload = transfer.export_to(archive, history=True)
        verified = transfer.read_package(archive)
        self.assertEqual(payload['settings']['retention'], POLICY)
        old = next(s for s in verified['history']['snapshots'] if s['id'] == before[0]['id'])
        self.assertIsNone(old['file'])
        self.assertEqual(old['text_sha256'], before[0]['text_sha256'])
        self.assertNotIn(before[0]['file'], verified['files'])
        self.select('restored')
        transfer.import_package(archive, verified, apply_settings=True)
        self.assertEqual(store.settings()['retention'], POLICY)
        for name, count in original_counts.items():
            self.assertEqual(len(store.rows('SELECT * FROM ' + name)), count)
        for relative, expected in verified['files'].items():
            self.assertEqual(transfer.file_hash(self.root / relative), expected['sha256'])
        restored = store.one('SELECT * FROM snapshots WHERE id=?', (before[0]['id'],))
        self.assertIsNone(restored['file'])
        self.assertEqual(restored['text'], before[0]['text'])
        self.assertEqual(restored['sha256'], before[0]['sha256'])
        again = transfer.import_package(archive, verified, apply_settings=True)
        self.assertTrue(all(value == 0 for value in again['added'].values()))
        self.assertEqual(again['files'], 0)

    def test_05_export_rejects_boolean_unknown_or_out_of_range_retention_settings(self):
        transfer.validate_settings({'retention': POLICY})
        invalid = [True, [], {'document_versions': True}, {'detail_days': False}, {'cache_days': True},
                   {'document_versions': 0}, {'document_versions': 4}, {'detail_days': 6},
                   {'cache_days': 91}, {'delete_reports': True}]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(transfer.Invalid):
                    transfer.validate_settings({'retention': value})

    def test_06_export_waits_for_another_process_exclusive_cleanup_lock(self):
        self.document('version-a', '10', 1)
        destination = self.base / 'locked-export.zip'
        code = '''
import os
from pathlib import Path
import store, transfer
store.CONFIG = Path(os.environ['DATA_DIR']) / 'settings.json'
store.SECRETS = Path(os.environ['DATA_DIR']) / 'credentials.json'
print('READY', flush=True)
transfer.export_to(Path(os.environ['TEST_EXPORT_PATH']), history=True)
print('EXPORTED', flush=True)
'''
        environment = dict(os.environ, DATA_DIR=str(self.root), TEST_EXPORT_PATH=str(destination))
        process = None
        try:
            with retention.maintenance_lock(exclusive=True):
                process = subprocess.Popen([sys.executable, '-u', '-c', code], cwd=Path(__file__).parent,
                                           env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           text=True)
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    self.assertTrue(selector.select(timeout=15), 'Export child did not finish importing its dependencies')
                self.assertEqual(process.stdout.readline().strip(), 'READY')
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.wait(timeout=0.5)
                self.assertFalse(destination.exists(), 'Export wrote evidence while cleanup held the exclusive lock')
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertIn('EXPORTED', stdout)
            self.assertEqual(transfer.read_package(destination)['kind'], 'archive')
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    def test_07_idle_scheduler_cleans_real_originals_once_and_persists_next_day(self):
        before = self.three_versions()
        self.assertIsNone(store.one("SELECT value FROM state WHERE key='storage_cleanup_due'"))
        with patch.object(engine, 'now', return_value=AT.isoformat()), \
                patch.object(engine, 'datetime', wraps=datetime) as clock, \
                patch.object(retention, 'cleanup', wraps=retention.cleanup) as cleanup:
            clock.now.return_value = AT

            async def two_ticks():
                await engine.storage_maintenance()
                first = store.one("SELECT value FROM state WHERE key='storage_cleanup_due'")
                self.assertIsNotNone(first)
                self.assertEqual(datetime.fromisoformat(first['value']), AT + timedelta(days=1))
                state = store.one('SELECT value FROM state WHERE key=?', (retention.LAST_KEY,))
                self.assertIsNotNone(state)
                await engine.storage_maintenance()
                self.assertEqual(store.one("SELECT value FROM state WHERE key='storage_cleanup_due'"), first)
                self.assertEqual(store.one('SELECT value FROM state WHERE key=?', (retention.LAST_KEY,)), state)

            asyncio.run(two_ticks())
            self.assertEqual(cleanup.call_count, 1)
        self.assertIsNone(store.one('SELECT file FROM snapshots WHERE id=?', (before[0]['id'],))['file'])
        self.assertFalse((self.root / before[0]['file']).exists())
        self.assertEqual(len(list((self.root / 'artifacts/objects').iterdir())), 2)

    def test_08_queued_monitoring_job_defers_cleanup_without_advancing_its_due_time(self):
        before = self.three_versions()
        rid = store.enqueue(PID, 'test')
        self.assertIsNotNone(rid)
        with patch.object(retention, 'cleanup', wraps=retention.cleanup) as cleanup:
            asyncio.run(engine.storage_maintenance())
            cleanup.assert_not_called()
        self.assertIsNone(store.one("SELECT value FROM state WHERE key='storage_cleanup_due'"))
        self.assertIsNone(store.one('SELECT value FROM state WHERE key=?', (retention.LAST_KEY,)))
        for snap in before:
            self.assertEqual(store.one('SELECT file FROM snapshots WHERE id=?', (snap['id'],))['file'], snap['file'])
            self.assertTrue((self.root / snap['file']).is_file())
        self.assertEqual(store.one('SELECT status FROM runs WHERE id=?', (rid,))['status'], 'queued')

    def test_09_manual_browser_mode_defers_cleanup_and_keeps_all_originals(self):
        before = self.three_versions()
        store.execute("UPDATE state SET value='1' WHERE key='manual'")
        with patch.object(retention, 'cleanup', wraps=retention.cleanup) as cleanup:
            asyncio.run(engine.storage_maintenance())
            cleanup.assert_not_called()
        self.assertIsNone(store.one("SELECT value FROM state WHERE key='storage_cleanup_due'"))
        self.assertIsNone(store.one('SELECT value FROM state WHERE key=?', (retention.LAST_KEY,)))
        for snap in before:
            self.assertEqual(store.one('SELECT file FROM snapshots WHERE id=?', (snap['id'],))['file'], snap['file'])
            self.assertTrue((self.root / snap['file']).is_file())


if __name__ == '__main__':
    unittest.main(verbosity=2)
