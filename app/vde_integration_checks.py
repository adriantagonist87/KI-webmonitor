"""Isolated collection/DB/archive integration; no production data or website.

All files live in private temporary directories under /data. Tests use the real
archive, review, lifecycle and transfer functions with model/network calls
blocked. The source URLs and records are deliberately synthetic.
"""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

scratch = tempfile.TemporaryDirectory(prefix='.vde-integration-', dir='/data')
os.environ['DATA_DIR'] = scratch.name
os.environ.setdefault('ANONYMIZED_TELEMETRY', 'false')
os.environ.setdefault('DO_NOT_TRACK', '1')
import store
store.CONFIG = Path(scratch.name) / 'bootstrap-settings.json'
store.SECRETS = Path(scratch.name) / 'bootstrap-credentials.json'
import engine
import attention
import collection_index as index
import project_lifecycle
import transfer


PID = 'collection-project'
SOURCE = 'https://source.test/normen?kind=normen'
SELECTORS = {'item_selector': '#content .card', 'title_selector': 'h2 a',
             'date_label': 'Ausgabedatum', 'container_selector': '#content'}
FINGERPRINT = index.scope_fingerprint(SELECTORS, 'Normen')
SCOPE = 'collection-v1:' + FINGERPRINT
STATE_KEY = 'collection_index:' + PID + ':' + FINGERPRINT


def record(name='a', *, text='Technischer Inhalt 10', date='2026-10', url=None):
    return {'url': url or 'https://source.test/item/' + name + '?version=2',
            'title': 'Norm ' + name, 'date': date, 'text': text}


class CollectionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=scratch.name)
        self.base = Path(self.directory.name)
        self.counter = 0
        self.guards = [patch('engine.llm', side_effect=AssertionError('No model call permitted')),
                       patch('httpx.Client.send', side_effect=AssertionError('No network permitted')),
                       patch('httpx.AsyncClient.send', side_effect=AssertionError('No network permitted'))]
        for guard in self.guards: guard.start()
        self.select('source')
        self.seed(PID)

    def tearDown(self):
        for guard in reversed(self.guards): guard.stop()
        self.directory.cleanup()

    def select(self, name):
        self.root = self.base / name
        self.root.mkdir(exist_ok=True)
        store.DATA = engine.DATA = self.root / 'data'
        store.CONFIG = engine.CONFIG = self.root / 'config/settings.json'
        store.SECRETS = engine.SECRETS = self.root / 'secrets/credentials.json'
        store.init()
        store.save_json(store.CONFIG, {})

    def seed(self, pid):
        store.execute('''INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created)
                         VALUES(?,?,?,?,?,?,?,?,?)''',
                      (pid, 'Sammlung ' + pid, SOURCE, 'Lies nur bestätigte Normeinträge.',
                       'source.test', '0 6 * * *', 0, store.next_due('0 6 * * *'), store.now()))

    def tick(self):
        self.counter += 1
        return (datetime(2026, 11, 5, 9, tzinfo=timezone.utc)
                + timedelta(seconds=self.counter)).isoformat()

    def run_row(self, rid, status='success'):
        stamp = self.tick()
        store.execute('''INSERT INTO runs(id,project_id,started,finished,status,origin)
                         VALUES(?,?,?,?,?,?)''', (rid, PID, stamp, stamp, status, 'integration-test'))
        return stamp

    def archive_index(self, rid, records, *, source=SOURCE, status='success', scope=SCOPE):
        stamp = self.run_row(rid, status)
        text = index.canonical_text(records)
        html = index.index_html(records, scope)
        relative = Path('artifacts') / rid / 'index.html.txt'
        path = store.DATA / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding='utf-8')
        with patch('engine.now', return_value=stamp):
            sid = engine.archive({'id': PID}, rid, engine.clean_url(source), 'page',
                                 text.encode('utf-8'), text,
                                 [r['url'] for r in index.normalize_records(records)],
                                 html=relative.as_posix(), scope=scope)
        return sid

    def archive_partial_raw(self, rid):
        stamp = self.run_row(rid, 'partial')
        text = 'Unvollständige rohe Seitenansicht ohne bestätigten Sammlungsindex.'
        html = '<main data-kiwm-scope="main-content-v2"><p>' + text + '</p></main>'
        relative = Path('artifacts') / rid / 'raw.html.txt'
        path = store.DATA / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding='utf-8')
        with patch('engine.now', return_value=stamp):
            return engine.archive({'id': PID}, rid, SOURCE, 'page', html.encode('utf-8'),
                                  text, [], html=relative.as_posix(), scope='main-content-v2')

    def persist(self, records):
        state = index.plan_update(None, records, True)['next_state']
        store.execute('INSERT OR REPLACE INTO state(key,value) VALUES(?,?)',
                      (STATE_KEY, json.dumps(state, ensure_ascii=False)))
        return state

    def test_01_matching_index_before_intervening_partial_is_used_for_real_change(self):
        original = [record('a')]
        before_id = self.archive_index('baseline', original)
        self.persist(original)
        raw_id = self.archive_partial_raw('intervening-raw')
        previous, changed_scope = engine.previous_in_scope(PID, 'next', SOURCE, SCOPE)
        self.assertFalse(changed_scope)
        self.assertEqual(previous['id'], before_id)
        self.assertNotEqual(previous['id'], raw_id)
        plan = index.plan_update(index.plan_update(None, original, True)['next_state'],
                                 [record('new'), record('a')], True)
        current_id = self.archive_index('next', plan['next_state']['records'])
        changes = store.rows('SELECT * FROM changes WHERE run_id=?', ('next',))
        self.assertEqual(len(changes), 1)
        change = changes[0]
        self.assertEqual(change['previous_id'], before_id)
        self.assertEqual(change['current_id'], current_id)
        self.assertIn('Inhalt geändert', change['kind'])
        evidence = json.loads(change['evidence'])
        self.assertIn('Norm new', evidence['text_diff'])
        self.assertEqual(evidence['links_added'], [record('new')['url']])
        self.assertEqual(evidence['links_removed'], [])

    def test_02_reordering_and_transport_session_values_do_not_create_changes(self):
        old = [record('a', url=record('a')['url'] + '&sid=old&sinst=111'),
               record('b', url=record('b')['url'] + '&sid=old&sinst=111')]
        new = [record('b', url=record('b')['url'] + '&sid=new&sinst=999'),
               record('a', url=record('a')['url'] + '&sid=new&sinst=999')]
        normalize = lambda rows: [{**r, 'url': engine.clean_url(r['url'])} for r in rows]
        old = normalize(old); new = normalize(new)
        a = self.archive_index('old', old, source=SOURCE + '&sid=old&sinst=111')
        b = self.archive_index('new', new, source=SOURCE + '&sid=new&sinst=999')
        self.assertEqual(store.one('SELECT url FROM snapshots WHERE id=?', (a,))['url'], SOURCE)
        self.assertEqual(store.one('SELECT url FROM snapshots WHERE id=?', (b,))['url'], SOURCE)
        self.assertEqual(engine.clean_url('https://source.test/item/a?version=2&sinst=123'),
                         'https://source.test/item/a?version=2')
        self.assertEqual(store.rows('SELECT * FROM changes WHERE run_id=?', ('new',)), [])
        self.assertEqual(store.one('SELECT text_sha256 FROM snapshots WHERE id=?', (a,)),
                         store.one('SELECT text_sha256 FROM snapshots WHERE id=?', (b,)))

    def test_03_shorter_followup_prefix_retains_history_and_creates_no_removals(self):
        original = [record('a'), record('b'), record('c')]
        before = self.archive_index('full', original)
        state = self.persist(original)
        plan = index.plan_update(state, [record('a')], True)
        self.assertEqual(len(plan['retained']), 2)
        after = self.archive_index('prefix', plan['next_state']['records'])
        self.assertEqual(store.rows('SELECT * FROM changes WHERE run_id=?', ('prefix',)), [])
        self.assertEqual(store.one('SELECT text_sha256,links FROM snapshots WHERE id=?', (before,)),
                         store.one('SELECT text_sha256,links FROM snapshots WHERE id=?', (after,)))
        self.assertIn(record('c')['url'], json.loads(store.one('SELECT links FROM snapshots WHERE id=?', (after,))['links']))

    def test_04_verified_digit_change_sets_attention_until_acknowledged(self):
        original = [record('a', text='Prüffrist 10 Tage')]
        self.archive_index('old', original)
        plan = index.plan_update(self.persist(original), [record('a', text='Prüffrist 11 Tage')], True)
        self.archive_index('new', plan['next_state']['records'])
        result = attention.summary(PID)
        self.assertEqual(result['attention_count'], 1)
        self.assertTrue(result['pending_changes'][0]['number_changed'])
        attention.acknowledge(PID, result['review_through'], self.tick())
        self.assertEqual(attention.summary(PID)['attention_count'], 0)
        self.assertEqual(len(store.rows('SELECT * FROM changes')), 1)
        self.assertEqual(len(store.rows('SELECT * FROM change_reviews')), 1)

    def test_05_v5_archive_roundtrip_preserves_index_and_configuration_excludes_history(self):
        records = [record('a'), record('b')]
        self.archive_index('baseline', records)
        state = self.persist(records)
        configuration = self.base / 'configuration.json'
        config = transfer.export_to(configuration)
        self.assertEqual(config['version'], 5)
        self.assertIsNone(config['history'])
        self.assertFalse(config.get('collection_indexes'))
        self.assertNotIn(STATE_KEY.encode(), configuration.read_bytes())
        archive = self.base / 'archive.zip'
        exported = transfer.export_to(archive, history=True)
        self.assertEqual(exported['collection_indexes'],
                         [{'key': STATE_KEY, 'value': json.dumps(state, ensure_ascii=False)}])
        payload = transfer.read_package(archive)
        self.select('restored')
        transfer.import_package(archive, payload, apply_settings=False)
        self.assertEqual(json.loads(store.one('SELECT value FROM state WHERE key=?', (STATE_KEY,))['value']), state)
        self.assertEqual(len(store.rows('SELECT * FROM snapshots')), 1)
        for relative, metadata in payload['files'].items():
            self.assertEqual(transfer.file_hash(store.DATA / relative), metadata['sha256'])
        again = transfer.import_package(archive, payload, apply_settings=False)
        self.assertTrue(all(count == 0 for count in again['added'].values()))
        self.assertEqual(again['files'], 0)
        with sqlite3.connect(store.DATA / 'monitor.db') as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_06_actual_v4_configuration_shape_without_index_field_remains_valid(self):
        self.archive_index('baseline', [record('a')]); self.persist([record('a')])
        path = self.base / 'configuration-v4.json'
        payload = transfer.export_to(path)
        payload['version'] = 4
        payload.pop('collection_indexes')
        # All remaining configuration fields have the real pre-index v4 shape.
        self.assertIsNone(payload['history']); self.assertEqual(payload['files'], {})
        path.write_bytes(transfer.dump(payload))
        legacy = transfer.read_package(path)
        self.select('legacy-v4')
        transfer.import_package(path, legacy, apply_settings=False)
        self.assertIsNotNone(store.one('SELECT id FROM projects WHERE id=?', (PID,)))
        self.assertFalse(store.rows("SELECT * FROM state WHERE key LIKE 'collection_index:%'"))

    def test_07_project_delete_removes_own_indexes_and_preserves_other_projects(self):
        self.archive_index('baseline', [record('a')]); self.persist([record('a')])
        self.seed('other-project')
        other_key = 'collection_index:other-project:' + FINGERPRINT
        other_state = index.plan_update(None, [record('other')], True)['next_state']
        store.execute('INSERT INTO state VALUES(?,?)', (other_key, json.dumps(other_state)))
        # Both identifiers are valid portable IDs. SQL LIKE would treat the
        # underscore in proj_a as a wildcard and delete projXa's index too.
        for extra_pid in ('proj_a', 'projXa'):
            self.seed(extra_pid)
            extra_key = 'collection_index:' + extra_pid + ':' + FINGERPRINT
            store.execute('INSERT INTO state VALUES(?,?)', (extra_key, json.dumps(other_state)))
        project_lifecycle.remove('proj_a')
        self.assertIsNone(store.one('SELECT id FROM projects WHERE id=?', ('proj_a',)))
        self.assertIsNone(store.one('SELECT value FROM state WHERE key=?',
                                    ('collection_index:proj_a:' + FINGERPRINT,)))
        self.assertIsNotNone(store.one('SELECT value FROM state WHERE key=?',
                                       ('collection_index:projXa:' + FINGERPRINT,)))
        self.assertIsNotNone(store.one('SELECT id FROM projects WHERE id=?', ('projXa',)))
        project_lifecycle.remove(PID)
        self.assertIsNone(store.one('SELECT id FROM projects WHERE id=?', (PID,)))
        self.assertIsNone(store.one('SELECT value FROM state WHERE key=?', (STATE_KEY,)))
        self.assertIsNotNone(store.one('SELECT value FROM state WHERE key=?', (other_key,)))
        self.assertIsNotNone(store.one('SELECT id FROM projects WHERE id=?', ('other-project',)))
        self.assertFalse(store.rows('SELECT * FROM runs'))
        self.assertFalse(store.rows('SELECT * FROM snapshots'))

    def test_08_raw_capture_cannot_clobber_completed_collection_or_its_changes(self):
        original = [record('a')]
        self.archive_index('baseline', original)
        plan = index.plan_update(self.persist(original), [record('a'), record('new')], True)
        sid = self.archive_index('changed', plan['next_state']['records'])
        store.execute('UPDATE state SET value=? WHERE key=?',
                      (json.dumps(plan['next_state'], ensure_ascii=False), STATE_KEY))
        before_snapshot = store.one('SELECT * FROM snapshots WHERE id=?', (sid,))
        before_changes = store.rows('SELECT * FROM changes WHERE run_id=? ORDER BY id', ('changed',))
        before_state = store.one('SELECT value FROM state WHERE key=?', (STATE_KEY,))
        before_html_hash = transfer.file_hash(store.DATA / before_snapshot['html'])
        self.assertEqual(len(before_changes), 1)
        relative = Path('artifacts/changed/attempted-raw.html.txt')
        raw = '<main data-kiwm-scope="main-content-v2"><p>Nur ein roher Teilausschnitt</p></main>'
        (store.DATA / relative).write_text(raw, encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Sammlungsvergleich'):
            with patch('engine.now', return_value=self.tick()):
                engine.archive({'id': PID}, 'changed', SOURCE, 'page', raw.encode('utf-8'),
                               'Nur ein roher Teilausschnitt', [], html=relative.as_posix(),
                               scope='main-content-v2')
        self.assertEqual(store.one('SELECT * FROM snapshots WHERE id=?', (sid,)), before_snapshot)
        self.assertEqual(store.rows('SELECT * FROM changes WHERE run_id=? ORDER BY id', ('changed',)), before_changes)
        self.assertEqual(store.one('SELECT value FROM state WHERE key=?', (STATE_KEY,)), before_state)
        self.assertEqual(transfer.file_hash(store.DATA / before_snapshot['html']), before_html_hash)
        self.assertEqual(attention.summary(PID)['attention_count'], 1)


if __name__ == '__main__': unittest.main(verbosity=2)
