"""Regressions for grounded first-page titles and retained-version scheduling."""
import json
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

import document_files as files
import document_files_checks as fixtures
import document_names as names
import store


def answer(start=1, end=1):
    content = json.dumps({'start': start, 'end': end})
    return {'choices': [{'message': {'content': content}}]}


class DocumentNames(IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.DocumentFiles()
        self.fixture.setUp()
        self.doc = self.fixture.add(url='https://example.test/Datei-2016.pdf')
        self.reader = AsyncMock(return_value={'status': 'text', 'text': 'Brandschutz für öffentliche Gebäude'})
        self.reader_patch = patch.object(names, '_page_text', self.reader)
        self.reader_patch.start()
        self.addCleanup(self.reader_patch.stop)
        self.ai = AsyncMock(return_value=answer())

    def tearDown(self):
        self.fixture.tearDown()

    async def process(self):
        return await names.process_pending(self.ai, 'local-model')

    def sidecar(self, row, **extra):
        path = names._sidecar(row)
        path.parent.mkdir(parents=True, exist_ok=True)
        store.save_json(path, {'source_sha256': row['sha256'], **extra})
        return path

    async def test_01_replace_legacy_guesses_and_keep_original(self):
        self.sidecar(self.doc, document_name={'text': 'Falsche alte Zuordnung',
                     'source': 'source_context_ai', 'document_url': self.doc['url']})
        self.assertEqual(files.list_files()['files'][0]['document_name'], '—')
        await self.process()
        row = files.list_files()['files'][0]
        self.assertEqual(row['document_name'], 'Brandschutz für öffentliche Gebäude')
        self.assertEqual(row['document_name_source'], 'first_page')
        self.assertEqual((store.DATA/self.doc['file']).read_bytes(), self.doc['raw'])

    async def test_02_preserve_opposite_editions_from_adjacent_heading(self):
        heading = 'Entscheidungshilfen der Obersten Bauaufsicht (EHB)'
        for edition in ('EHB bis 12/2016', 'EHB ab 01/2017'):
            with self.subTest(edition=edition):
                self.reader.return_value = {'status': 'text', 'text': f'{heading},\n{edition}\nBeide Ausgaben: EHB bis 12/2016 und EHB ab 01/2017.'}
                record = await names._name(self.doc, self.ai, 'local-model')
                self.assertEqual(record['text'], f'{heading} · {edition}')
        lines = ['Informationen zur Mittelspannung', 'Herausgeber', 'Netzanschluss', 'Nummer 02 vom Oktober 2009']
        for edition in (4, None):
            self.assertEqual(names._selected_title(lines, {'start': 1, 'end': 1, 'edition': edition}),
                             'Informationen zur Mittelspannung · Nummer 02 vom Oktober 2009')

    async def test_03_image_only_document_stays_unnamed(self):
        self.reader.return_value = {'status': 'no_text'}
        await self.process()
        self.assertEqual(names.saved(self.doc)['source'], 'no_text')
        self.assertEqual(files.list_files()['files'][0]['document_name'], '—')
        self.ai.assert_not_awaited()

    async def test_04_invented_title_and_invalid_indices_are_rejected(self):
        self.ai.return_value = {'choices': [{'message': {'content': '{"title":"Fassung ab 2026"}'}}]}
        await self.process()
        self.assertEqual(names.saved(self.doc)['source'], 'model_pending')
        self.assertEqual(files.list_files()['files'][0]['document_name'], '—')

    async def test_05_model_only_sees_bounded_page_and_filename(self):
        await self.process()
        messages = self.ai.await_args.args[0]
        data = json.loads(messages[-1]['content'])
        self.assertEqual(data['filename'], 'Datei-2016.pdf')
        self.assertEqual(data['lines'], [{'line': 1, 'text': 'Brandschutz für öffentliche Gebäude'}])
        for secret in ('PROMPT_ONLY_SECRET', 'CONTENT_ONLY_SECRET', 'example.test', '%PDF-'):
            self.assertNotIn(secret, str(messages))
        self.assertEqual(files.list_files('BRANDSCHUTZ ÖFFENTLICHE')['matched'], 1)

    async def test_06_hash_and_url_bind_the_name(self):
        await self.process()
        self.assertIsNone(names.saved({**self.doc, 'sha256': 'b'*64}))
        self.assertIsNone(names.saved({**self.doc, 'url': 'https://example.test/other.pdf'}))

    async def test_07_unsupported_archive_does_not_enter_reader(self):
        store.execute('UPDATE snapshots SET file=NULL WHERE id=?', (self.doc['id'],))
        archive = self.fixture.add(raw=b'archive', extension='.zip', url='https://example.test/a.zip')
        await self.process()
        self.assertEqual(names.saved(archive)['source'], 'unsupported')
        self.reader.assert_not_awaited()
        self.ai.assert_not_awaited()

    async def test_08_same_version_reuses_verified_title_on_next_run(self):
        await self.process()
        repeat = self.fixture.add(raw=self.doc['raw'], url=self.doc['url'], when='2026-01-02T00:00:00+00:00')
        self.sidecar(repeat, comparison_status='unchanged', previous_sha256=self.doc['sha256'], previous_snapshot_id=self.doc['id'])
        await self.process()
        self.assertEqual(names.saved(repeat)['text'], names.saved(self.doc)['text'])
        self.ai.assert_awaited_once()
        self.reader.assert_awaited_once()

    async def test_09_changed_file_is_read_again(self):
        await self.process()
        new = self.fixture.add(raw=fixtures.pdf('New'), url=self.doc['url'], when='2026-01-03T00:00:00+00:00')
        self.sidecar(new, comparison_status='changed', previous_sha256=self.doc['sha256'], previous_snapshot_id=self.doc['id'])
        self.reader.return_value = {'status': 'text', 'text': 'Brandschutzrichtlinie ab Januar 2017'}
        await self.process()
        self.assertEqual(names.saved(new)['text'], 'Brandschutzrichtlinie ab Januar 2017')
        self.assertEqual(self.ai.await_count, 2)

    async def test_10_same_binary_in_other_project_is_not_a_cache_hit(self):
        await self.process()
        other = self.fixture.add(raw=self.doc['raw'], url=self.doc['url'], pid='p2')
        self.sidecar(other, comparison_status='unchanged', previous_sha256=self.doc['sha256'], previous_snapshot_id=self.doc['id'])
        await self.process()
        self.assertEqual(self.ai.await_count, 2)
        self.assertEqual(names.saved(other)['source'], 'first_page')

    async def test_11_original_hash_mismatch_never_enters_reader(self):
        (store.DATA/self.doc['file']).write_bytes(b'damaged')
        await self.process()
        self.assertEqual(names.saved(self.doc)['source'], 'unreadable')
        self.reader.assert_not_awaited()
        self.ai.assert_not_awaited()

    async def test_12_reader_failure_can_retry_and_active_snapshots_wait(self):
        store.execute("UPDATE runs SET status='running' WHERE id=?", (self.doc['run_id'],))
        self.assertFalse(await self.process())
        self.reader.assert_not_awaited()
        store.execute("UPDATE runs SET status='success' WHERE id=?", (self.doc['run_id'],))
        self.reader.side_effect = ConnectionRefusedError
        await self.process()
        self.assertEqual(names.saved(self.doc)['source'], 'reader_pending')
        self.assertFalse(await self.process())
        names._persist(self.doc, {'source': 'reader_pending', 'text': '—', 'retry_at': '2020-01-01'})
        self.reader.side_effect = None
        await self.process()
        self.assertEqual(names.saved(self.doc)['source'], 'first_page')
