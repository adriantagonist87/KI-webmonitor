"""Real SQLite/filesystem retention tests; no network, browser or model calls."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import store
import retention
from document_types import EXTENSIONS
from retention import LEGACY_OCR_CACHE_POLICY as POLICY


AT = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


class RetentionChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.retention-')
        self.original = store.DATA, store.CONFIG, store.SECRETS
        store.DATA = Path(self.temp.name).resolve()
        store.CONFIG = store.DATA / 'settings.json'
        store.SECRETS = store.DATA / 'credentials.json'
        store.init()
        store.save_json(store.CONFIG, {})
        self.project('p')

    def tearDown(self):
        store.DATA, store.CONFIG, store.SECRETS = self.original
        self.temp.cleanup()

    def project(self, pid):
        store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created) VALUES(?,?,?,?,?,?,?,?,?)',
                      (pid, pid, 'https://test.example/', 'test', 'test.example', '0 6 * * *', 0, AT.isoformat(), AT.isoformat()))

    def file(self, relative, raw=b'fixture', days=60):
        path = store.DATA / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        timestamp = (AT - timedelta(days=days)).timestamp()
        os.utime(path, (timestamp, timestamp))
        return relative

    def job(self, rid, days=60, status='success', pid='p'):
        date = (AT - timedelta(days=days)).isoformat()
        store.execute('INSERT INTO runs(id,project_id,started,finished,status,summary,origin) VALUES(?,?,?,?,?,?,?)',
                      (rid, pid, date, date if status not in ('running', 'queued') else None, status, 'Unverändert.', 'test'))
        return date

    def snap(self, sid, rid, raw=b'fixture', days=60, pid='p', url='https://test.example/file.pdf', kind='document', scope=None, extension='.pdf'):
        digest = hashlib.sha256(raw).hexdigest()
        file = self.file('artifacts/objects/' + digest + extension, raw, days) if kind == 'document' else None
        html = self.file('artifacts/' + rid + '/' + sid + '.html',
                         ('<main data-kiwm-scope="' + (scope or 'default') + '">Text</main>').encode(), days) if kind == 'page' else None
        screenshot = self.file('artifacts/' + rid + '/' + sid + '.png', b'png' + raw, days) if kind == 'page' else None
        text = raw.decode('utf-8')
        store.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (sid, rid, pid, url, kind, (AT - timedelta(days=days)).isoformat(), digest,
                       store.digest(text), text, '[]', file, screenshot, html))
        if kind == 'document':
            self.file('artifacts/' + rid + '/' + sid + '.extraction.json', b'{"method":"embedded_text"}', days)
        return file

    def inventory(self, rid, urls, **updates):
        doc = {'found': len(urls), 'checked': len(urls), 'failed': [], 'unverified': [],
               'documents': [{'url': url, 'status': 'checked'} for url in urls]}
        doc.update(updates)
        return self.file('artifacts/' + rid + '/evidence.json', json.dumps({'agent': {'complete': True}, 'document_check': doc}).encode())

    def change(self, cid, previous, current):
        row = store.one('SELECT * FROM snapshots WHERE id=?', (current,))
        store.execute('INSERT INTO changes VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (cid, row['project_id'], row['run_id'], row['url'], 'Inhalt geändert', row['captured'], previous, current, '{}', cid))

    def three(self):
        paths = []
        for number, days in enumerate((90, 60, 40)):
            rid, sid = 'r' + str(number), 's' + str(number)
            self.job(rid, days)
            paths.append(self.snap(sid, rid, str(number).encode(), days))
        self.inventory('r2', ['https://test.example/file.pdf'])
        return paths

    def test_policy_is_strict_and_defaults_are_fresh(self):
        self.assertEqual(retention.policy(), retention.DEFAULT_POLICY)
        self.assertEqual(retention.validate_policy({'document_versions': 1})['detail_days'], 30)
        for bad in (None, [], {'unknown': 1}, {'document_versions': True}, {'document_versions': 0},
                    {'detail_days': 6}, {'cache_days': 91}, {'cache_days': '30'}):
            with self.assertRaises(ValueError):
                retention.validate_policy(bad)

    def test_optional_usage_file_can_disappear_between_walk_and_stat(self):
        import inspect
        relative = self.file('artifacts/objects/' + 'a' * 64 + '.pdf', b'optional cache')
        target = store.DATA / relative
        original_stat = Path.stat
        for method in ('_bytes', '_files'):
            missed = []

            def disappearing_stat(path, *args, **kwargs):
                if path == target and inspect.currentframe().f_back.f_code.co_name == method:
                    missed.append(path)
                    raise FileNotFoundError('Evicted between directory listing and stat')
                return original_stat(path, *args, **kwargs)

            with self.subTest(method=method), patch.object(Path, 'stat', disappearing_stat):
                result = getattr(retention, method)(store.DATA, 'artifacts')
                self.assertEqual(result, 0 if method == '_bytes' else {})
            self.assertTrue(missed)

    def test_latest_two_distinct_versions_retained(self):
        paths = self.three()
        result = retention.cleanup(at=AT)
        self.assertEqual(result['status'], 'cleaned')
        self.assertFalse((store.DATA / paths[0]).exists())
        self.assertTrue(all((store.DATA / p).is_file() for p in paths[1:]))
        self.assertIsNotNone(store.one('SELECT id FROM snapshots WHERE id=?', ('s2',)))

    def test_shared_document_formats_all_expire_only_the_oldest_of_three_originals(self):
        originals={}
        for version,days in enumerate((90,60,40)):
            rid='formats-r'+str(version)
            self.job(rid,days)
            for extension in sorted(EXTENSIONS):
                token=extension[1:];sid='formats-s'+str(version)+'-'+token
                url='https://test.example/original'+extension
                raw=('Original '+token+' version '+str(version)).encode()
                relative=self.snap(sid,rid,raw,days,url=url,extension=extension)
                originals[(extension,version)]={'id':sid,'file':relative,'sha256':store.digest(raw),'raw':raw}
                if version:self.change('formats-c'+str(version)+'-'+token,
                    originals[(extension,version-1)]['id'],sid)
        self.inventory('formats-r2',['https://test.example/original'+extension for extension in sorted(EXTENSIONS)])
        result=retention.cleanup(at=AT)
        self.assertEqual(result['status'],'cleaned',result)
        self.assertEqual(result['policy']['document_versions'],2)
        for extension in sorted(EXTENSIONS):
            for version in range(3):
                original=originals[(extension,version)]
                row=store.one('SELECT * FROM snapshots WHERE id=?',(original['id'],))
                self.assertIsNotNone(row,'Referenced snapshot must survive original cleanup')
                self.assertEqual(row['sha256'],original['sha256'])
                if version==0:
                    self.assertIsNone(row['file'])
                    self.assertFalse((store.DATA/original['file']).exists(),extension)
                else:
                    self.assertEqual(row['file'],original['file'])
                    raw=(store.DATA/row['file']).read_bytes()
                    self.assertEqual(raw,original['raw'])
                    self.assertEqual(hashlib.sha256(raw).hexdigest(),row['sha256'])
            changes=store.rows('SELECT previous_id,current_id FROM changes WHERE url=?',
                ('https://test.example/original'+extension,))
            self.assertEqual(len(changes),2)
            self.assertEqual({row['current_id'] for row in changes},
                {originals[(extension,1)]['id'],originals[(extension,2)]['id']})
        self.assertEqual(len(list((store.DATA/'artifacts/objects').iterdir())),2*len(EXTENSIONS))

    def test_object_name_matcher_uses_shared_suffixes_without_managing_other_files(self):
        sha='a'*64
        for extension in EXTENSIONS:
            self.assertIsNotNone(retention._OBJECT.fullmatch(sha+extension),extension)
        for name in (sha+'.exe',sha+'.png',sha+'.pdf.extra','manual.zip','../'+sha+'.zip'):
            self.assertIsNone(retention._OBJECT.fullmatch(name),name)

    def test_change_references_keep_text_hash_and_row_without_old_original(self):
        paths = self.three()
        self.change('c1', 's0', 's1')
        self.change('c2', 's1', 's2')
        original = store.one('SELECT text,text_sha256 FROM snapshots WHERE id=?', ('s0',))
        retention.cleanup(at=AT)
        after = store.one('SELECT text,text_sha256,file FROM snapshots WHERE id=?', ('s0',))
        self.assertEqual(after['text'], original['text'])
        self.assertEqual(after['text_sha256'], original['text_sha256'])
        self.assertIsNone(after['file'])
        self.assertEqual(len(store.rows('SELECT * FROM changes')), 2)

    def test_shared_blob_required_by_another_project_is_not_removed(self):
        paths = self.three()
        self.project('other')
        self.job('other-run', 70, pid='other')
        self.snap('other-snapshot', 'other-run', b'0', 70, pid='other')
        retention.cleanup(at=AT)
        self.assertTrue((store.DATA / paths[0]).is_file())
        self.assertEqual(store.one('SELECT file FROM snapshots WHERE id=?', ('other-snapshot',))['file'], paths[0])

    def test_unchanged_snapshot_rows_compacted_but_latest_baseline_and_sidecar_remain(self):
        for n, days in enumerate((100, 90, 80)):
            self.job('r' + str(n), days)
            self.snap('s' + str(n), 'r' + str(n), b'same', days)
        result = retention.cleanup(at=AT)
        self.assertEqual(result['removed']['snapshot_rows'], 2)
        self.assertEqual([r['id'] for r in store.rows('SELECT id FROM snapshots')], ['s2'])
        self.assertTrue((store.DATA / 'artifacts/r2/s2.extraction.json').is_file())

    def test_complete_successful_inventory_expires_retired_source(self):
        self.job('old', 100)
        path = self.snap('old-snapshot', 'old', b'old', 100)
        self.job('recent', 50)
        evidence = self.inventory('recent', [])
        retention.cleanup(at=AT)
        self.assertFalse((store.DATA / path).exists())
        self.assertTrue((store.DATA / evidence).is_file())
        self.assertIsNone(store.one('SELECT file FROM snapshots WHERE id=?', ('old-snapshot',))['file'])

    def test_partial_or_failed_run_cannot_certify_retirement(self):
        for status in ('partial', 'failed'):
            with self.subTest(status=status):
                rid = status + '-old'
                self.job(rid, 100)
                url = 'https://test.example/' + status + '.pdf'
                path = self.snap(status + '-snapshot', rid, status.encode(), 100, url=url)
                self.job(status + '-new', 50, status=status)
                self.inventory(status + '-new', [])
                retention.cleanup(at=AT)
                self.assertTrue((store.DATA / path).is_file())

    def test_incomplete_success_inventory_does_not_certify_retirement(self):
        self.job('old', 100)
        path = self.snap('s', 'old', b'old', 100)
        self.job('new', 50)
        self.inventory('new', [], unverified=['https://test.example/file.pdf'])
        retention.cleanup(at=AT)
        self.assertTrue((store.DATA / path).is_file())

    def test_inventory_older_than_latest_observation_cannot_retire_source(self):
        self.job('success', 100)
        self.inventory('success', [])
        self.job('partial', 50, status='partial')
        path = self.snap('s', 'partial', b'observed', 50)
        retention.cleanup(at=AT)
        self.assertTrue((store.DATA / path).is_file())

    def test_scope_baselines_survive_and_obsolete_screenshot_is_expired(self):
        for rid, sid, days, scope in [('old', 'old', 100, 'section-v1:faq'),
                                      ('faq', 'faq', 70, 'section-v1:faq'),
                                      ('docs', 'docs', 60, 'section-v1:docs')]:
            self.job(rid, days)
            self.snap(sid, rid, sid.encode(), days, url='https://test.example/page', kind='page', scope=scope)
        retention.cleanup(at=AT)
        for sid in ('faq', 'docs'):
            row = store.one('SELECT html,screenshot FROM snapshots WHERE id=?', (sid,))
            self.assertTrue(all((store.DATA / p).is_file() for p in row.values()))
        self.assertIsNone(store.one('SELECT id FROM snapshots WHERE id=?', ('old',)))
        self.assertFalse((store.DATA / 'artifacts/old/old.png').exists())

    def test_dry_run_changes_neither_database_nor_files(self):
        self.three()
        before = {p.relative_to(store.DATA).as_posix(): p.read_bytes() for p in store.DATA.rglob('*') if p.is_file()}
        result = retention.cleanup(dry_run=True, at=AT)
        after = {p.relative_to(store.DATA).as_posix(): p.read_bytes() for p in store.DATA.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertGreater(result['candidates']['files'], 0)
        self.assertFalse((store.DATA / '.storage.lock').exists())

    def test_running_or_queued_job_blocks_cleanup(self):
        paths = self.three()
        for n, status in enumerate(('running', 'queued')):
            self.job('active' + str(n), 0, status=status)
        result = retention.cleanup(at=AT)
        self.assertEqual(result['status'], 'skipped')
        self.assertTrue(all((store.DATA / p).is_file() for p in paths))

    def test_recent_failed_versions_never_displace_or_add_to_two_stable_versions(self):
        paths = self.three()
        failed_paths = []
        for n in range(3):
            rid = 'failed-' + str(n)
            self.job(rid, n + 1, status='failed')
            failed_paths.append(self.snap('failed-s' + str(n), rid, ('failed' + str(n)).encode(), n + 1))
        retention.cleanup(at=AT)
        self.assertTrue(all((store.DATA / p).is_file() for p in paths[1:]))
        self.assertTrue(all(not (store.DATA / p).exists() for p in failed_paths))

    def test_canonical_timestamp_aliases_do_not_pin_duplicate_baseline_rows(self):
        for n, days in enumerate((90, 80, 70)):
            rid = 'r' + str(n)
            self.job(rid, days)
            self.snap('s' + str(n), rid, b'unchanged', days,
                      url='https://test.example/file.pdf?ts=' + str(1770000000 + n))
        retention.cleanup(at=AT)
        self.assertEqual([r['id'] for r in store.rows('SELECT id FROM snapshots')], ['s2'])

    def test_orphan_objects_have_creation_grace_and_other_areas_untouched(self):
        old = self.file('artifacts/objects/' + 'a' * 64 + '.pdf', days=2)
        new = self.file('artifacts/objects/' + 'b' * 64 + '.pdf', days=0)
        user = store.DATA / 'browser/profile/source.pdf'
        user.parent.mkdir(parents=True)
        user.write_bytes(b'session')
        report = store.DATA / 'reports/report.html'
        report.write_bytes(b'report')
        custom = self.file('artifacts/objects/user-not-a-content-hash.pdf', days=70)
        retention.cleanup(at=AT)
        self.assertFalse((store.DATA / old).exists())
        self.assertTrue((store.DATA / new).is_file())
        self.assertEqual(user.read_bytes(), b'session')
        self.assertEqual(report.read_bytes(), b'report')
        self.assertTrue((store.DATA / custom).is_file())

    def test_hostile_reference_aborts_without_deleting_valid_candidates(self):
        paths = self.three()
        store.execute('UPDATE snapshots SET screenshot=? WHERE id=?', ('artifacts/../outside', 's2'))
        result = retention.cleanup(at=AT)
        self.assertEqual(result['status'], 'skipped')
        self.assertTrue(all((store.DATA / p).is_file() for p in paths))

    def test_symlink_in_managed_tree_is_never_followed(self):
        self.three()
        outside = store.DATA / 'private'
        outside.mkdir()
        (outside / 'do-not-delete').write_bytes(b'secret')
        (store.DATA / 'artifacts/link').symlink_to(outside, target_is_directory=True)
        result = retention.cleanup(at=AT)
        self.assertEqual(result['status'], 'skipped')
        self.assertTrue((outside / 'do-not-delete').is_file())

    def cache(self, raw_hash, visual_hash, days=60, policy=POLICY):
        raw = self.file('ocr/' + policy + '/raw/' + raw_hash + '.json',
                        json.dumps({'raw_sha256': raw_hash, 'visual_sha256': visual_hash}).encode(), days)
        visual = self.file('ocr/' + policy + '/visual/' + visual_hash + '.json', b'{"text":"cache"}', days)
        return raw, visual

    def test_ocr_visual_shared_by_a_kept_manifest_survives(self):
        self.job('r', 60)
        self.snap('s', 'r', b'kept', 60)
        needed = store.digest(b'kept')
        stale = store.digest(b'gone')
        visual_hash = store.digest(b'pixels')
        keep_raw, visual = self.cache(needed, visual_hash)
        stale_raw, _ = self.cache(stale, visual_hash)
        retention.cleanup(at=AT)
        self.assertTrue((store.DATA / keep_raw).is_file())
        self.assertTrue((store.DATA / visual).is_file())
        self.assertFalse((store.DATA / stale_raw).exists())

    def test_obsolete_policy_cache_expires_even_for_current_original(self):
        self.job('r', 60)
        self.snap('s', 'r', b'kept', 60)
        raw, visual = self.cache(store.digest(b'kept'), store.digest(b'pixels'), policy='old-policy')
        retention.cleanup(at=AT)
        self.assertFalse((store.DATA / raw).exists())
        self.assertFalse((store.DATA / visual).exists())

    def test_cleanup_is_idempotent_and_lock_is_private_reentrant(self):
        self.three()
        first = retention.cleanup(at=AT)
        second = retention.cleanup(at=AT)
        self.assertGreater(first['removed']['files'], 0)
        self.assertEqual(second['removed']['files'], 0)
        self.assertEqual(second['removed']['snapshot_rows'], 0)
        self.assertEqual((store.DATA / '.storage.lock').stat().st_mode & 0o777, 0o600)
        with retention.maintenance_lock(exclusive=True):
            with retention.maintenance_lock(exclusive=False):
                pass
        with retention.maintenance_lock(exclusive=False):
            with self.assertRaises(RuntimeError):
                with retention.maintenance_lock(exclusive=True):
                    pass


if __name__ == '__main__':
    unittest.main(verbosity=2)
