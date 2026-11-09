"""Real file-directory routes against isolated original files and database rows.

No application lifespan, scheduler, model calls or external HTTP are started.
"""
from io import BytesIO
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, quote, urlsplit
import zipfile

module_scratch = tempfile.TemporaryDirectory(prefix='.files-ui-module-', dir='/data')
os.environ['DATA_DIR'] = module_scratch.name
import store
store.CONFIG = Path(module_scratch.name) / 'settings.json'
store.SECRETS = Path(module_scratch.name) / 'credentials.json'
store.init()
import engine
import web
import tag_registry
import project_icons
import retention
import document_files
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from pypdf import PdfWriter


def pdf(title, author='Fachautor Nord', suffix='A'):
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({'/Title': title, '/Author': author,
                         '/Subject': 'Technische Unterlagen', '/Keywords': 'Prüfung Merkblatt',
                         '/ModDate': 'D:20251201080000Z', '/Creator': 'Fixture ' + suffix})
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


class GlobalFilesUI(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='.files-ui-', dir='/data')
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        store.DATA = web.DATA = engine.DATA = self.root
        store.CONFIG = web.CONFIG = self.root / 'settings.json'
        store.SECRETS = web.SECRETS = self.root / 'credentials.json'
        store.init()
        store.save_json(store.CONFIG, {'omlx_url': 'http://local.test/v1', 'smtp_enabled': False})
        store.save_json(store.SECRETS, {'omlx_key': 'PRIVATE-KEY-NOT-CATALOGUED-912'})
        tag_registry.save('Arbeit', '#1967d2')
        tag_registry.save('Privat', '#8b5cf6')
        self.project('alpha', 'Planungsbüro Alpha', ['Arbeit'])
        self.project('beta', 'Privat Bau', ['Privat'])
        self.alpha_icon = project_icons.record(b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6" fill="blue"/></svg>')
        project_icons.save('alpha', self.alpha_icon)
        self.counter = 0
        self.raw_a = pdf('Brandschutz Richtlinie', suffix='A')
        self.raw_b = pdf('Brandschutz Richtlinie Ausgabe 2', suffix='B')
        self.raw_csv = b'field,value\nprivate,CSV-CONTENT-ONLY-714\n'
        self.a = self.document('alpha', 'https://example.org/Richtlinie%20Berlin%202026.pdf', self.raw_a, 1)
        self.shared = self.document('beta', 'https://example.org/gemeinsam.pdf', self.raw_a, 2)
        self.b = self.document('alpha', 'https://example.org/Richtlinie%20Berlin%202026.pdf', self.raw_b, 3)
        self.csv = self.document('alpha', 'https://example.org/Wartung.csv', self.raw_csv, 4, extension='csv')
        self.b_repeat = self.document('alpha', 'https://example.org/Richtlinie%20Berlin%202026.pdf', self.raw_b, 5)
        self.expired = self.document('alpha', 'https://example.org/expired.pdf', b'gone', 6, expired=True)
        self.missing = self.document('alpha', 'https://example.org/missing.pdf', b'missing', 7, missing=True)
        self.client = TestClient(web.app)
        self.addCleanup(self.client.close)

    def project(self, pid, name, tags):
        store.execute('''INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created,tags)
                         VALUES(?,?,?,?,?,?,?,?,?,?)''',
                      (pid, name, 'https://example.org', 'PROMPT-ONLY-NOT-CATALOGUED-813',
                       'example.org', '0 6 * * *', 0, '2099-01-01T00:00:00+00:00',
                       '2026-01-01T00:00:00+00:00', json.dumps(tags)))

    def document(self, pid, url, raw, day, extension='pdf', expired=False, missing=False, path=None):
        self.counter += 1
        rid, sid = 'files-run-' + str(self.counter), 'files-doc-' + str(self.counter)
        captured = f'2026-01-{day:02d}T08:00:00+00:00'
        digest = store.digest(raw)
        relative = path or ('artifacts/objects/' + digest + '.' + extension)
        if not expired and not missing and path is None:
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            # Match the real content-addressed writer: unchanged bytes are not
            # rewritten or touched during another capture/project association.
            if not target.exists():
                target.write_bytes(raw)
                timestamp = datetime.fromisoformat(captured).timestamp()
                os.utime(target, (timestamp, timestamp))
        text = 'SNAPSHOT-CONTENT-ONLY-997 ' + str(self.counter)
        store.execute('INSERT INTO runs(id,project_id,started,finished,status,origin) VALUES(?,?,?,?,?,?)',
                      (rid, pid, captured, captured, 'success', 'test'))
        store.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (sid, rid, pid, url, 'document', captured, digest, store.digest(text),
                       text, '[]', None if expired else relative, None, None))
        return sid

    def page(self, **params):
        response = self.client.get('/files', params=params)
        self.assertEqual(response.status_code, 200, response.text[:300])
        return BeautifulSoup(response.text, 'html.parser')

    def file_rows(self, page):
        table = page.select_one('table[data-files-table]')
        if table is None:
            # An accessible empty-result view may omit the otherwise empty table.
            self.assertIsNotNone(page.select_one('[data-files-results]'))
            return []
        return [row for row in table.select('tbody tr')
                if any('/files/' in link.get('href', '') and link['href'].endswith('/download')
                       for link in row.find_all('a'))]

    def row_source(self, row):
        link = next(link for link in row.find_all('a')
                    if '/files/' in link.get('href', '') and link['href'].endswith('/download'))
        return store.one('SELECT * FROM snapshots WHERE id=?', (link['href'].split('/')[-2],))

    def test_01_sidebar_semantic_columns_search_and_global_zip(self):
        page = self.page()
        nav = page.select_one('aside a[href="/files"]')
        self.assertIsNotNone(nav)
        self.assertEqual(nav.get_text(' ', strip=True), 'Globales Dateiverzeichnis')
        self.assertEqual(nav.get('aria-current'), 'page')
        self.assertIsNotNone(nav.select_one('svg'))
        table = page.select_one('table[data-files-table]')
        project_link = table.select_one('.files-project-column a[href="/projects/alpha"]')
        favicon = project_link.select_one('img.project-favicon')
        self.assertEqual(favicon['alt'], '')
        self.assertEqual(favicon['src'], '/projects/alpha/icon?v=' + self.alpha_icon['sha256'][:16])
        self.assertEqual(self.client.get(favicon['src']).headers['content-type'], 'image/svg+xml')
        self.assertIsNotNone(table.select_one('.files-project-column a[href="/projects/beta"] .favicon-placeholder'))
        headers = table.select('thead th')
        self.assertEqual(len(headers), 8)
        text = ' '.join(header.get_text(' ', strip=True) for header in headers)
        for label in ('Tags', 'Projekt', 'Dateityp', 'Dokumentname', 'Dateiname', 'Größe', 'Zuletzt geändert', 'Download'):
            self.assertIn(label, text)
        type_cell = table.select_one('td.files-type-column')
        self.assertIsNotNone(type_cell.select_one('[role=img][aria-label]'))
        self.assertEqual(type_cell.get_text(strip=True), '')
        self.assertEqual(len(table.select('thead th[aria-sort="descending"]')), 1)
        sort_keys = {parse_qs(urlsplit(link['href']).query)['sort'][0]
                     for link in table.select('thead a[data-files-sort]')}
        self.assertEqual(sort_keys, {'tags', 'project', 'type', 'document_name', 'name', 'size', 'changed_at'})
        form = page.select_one('form[data-files-search]')
        self.assertEqual(form.get('method', '').lower(), 'get')
        self.assertEqual(form.get('action'), '/files')
        self.assertIsNotNone(form.select_one('input[name=q]'))
        self.assertEqual(page.select_one('[data-files-zip]')['href'], '/files/download-all.zip')

    def test_02_default_order_uses_observed_version_not_latest_download(self):
        rows = self.file_rows(self.page())
        self.assertEqual(len(rows), 4, 'Repeated captures must not add another offline-file row')
        sources = [self.row_source(row) for row in rows]
        self.assertEqual(sources[0]['sha256'], store.digest(self.raw_csv))
        self.assertEqual(sources[1]['sha256'], store.digest(self.raw_b))
        b_row = rows[1]
        self.assertIn('03.01.2026', b_row.get_text(' ', strip=True))
        times = b_row.find_all('time')
        if times:
            self.assertTrue(times[0]['datetime'].startswith('2026-01-03'))
        self.assertNotIn('05.01.2026', b_row.get_text(' ', strip=True))

    def test_03_search_combines_casefolded_project_metadata_and_wildcards(self):
        page = self.page(q='pLaNuNgSbÜrO NORd BrAnD*RiChT')
        rows = self.file_rows(page)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(self.row_source(row)['project_id'] == 'alpha' for row in rows))
        self.assertEqual(len(self.file_rows(self.page(q='Fachaut?r'))), 3)
        self.assertEqual(len(self.file_rows(self.page(q='RICHTLINIE*2026.PDF'))), 2)

    def test_04_search_excludes_contents_prompts_and_credentials(self):
        for phrase in ('SNAPSHOT-CONTENT-ONLY-997', 'CSV-CONTENT-ONLY-714',
                       'PROMPT-ONLY-NOT-CATALOGUED-813', 'PRIVATE-KEY-NOT-CATALOGUED-912'):
            with self.subTest(phrase=phrase):
                page = self.page(q=phrase)
                self.assertEqual(self.file_rows(page), [])
        page = self.page()
        for phrase in ('SNAPSHOT-CONTENT-ONLY-997', 'PROMPT-ONLY-NOT-CATALOGUED-813',
                       'PRIVATE-KEY-NOT-CATALOGUED-912'):
            self.assertNotIn(phrase, str(page))

    def test_05_sort_links_preserve_filter_and_project_order_is_reversible(self):
        page = self.page(q='richt*', sort='project', direction='asc')
        header = next(header for header in page.select('thead th')
                      if 'Projekt' in header.get_text())
        self.assertEqual(header.get('aria-sort'), 'ascending')
        for link in page.select('thead a[data-files-sort]'):
            self.assertEqual(parse_qs(urlsplit(link['href']).query).get('q'), ['richt*'])
        asc = [self.row_source(row)['project_id'] for row in self.file_rows(self.page(sort='project', direction='asc'))]
        desc = [self.row_source(row)['project_id'] for row in self.file_rows(self.page(sort='project', direction='desc'))]
        self.assertEqual(asc, ['alpha', 'alpha', 'alpha', 'beta'])
        self.assertEqual(desc, ['beta', 'alpha', 'alpha', 'alpha'])

    def test_20_size_shows_original_bytes_in_mb_and_sorts_numerically(self):
        for name, size in (('mittel', 1_250_000), ('gross', 12_500_000)):
            self.document('alpha', f'https://example.org/{name}.csv', b'x' * size, 8, extension='csv')
        for direction, expected in (('asc', ['<0,01 MB', '1,25 MB', '12,50 MB']),
                                    ('desc', ['12,50 MB', '1,25 MB', '<0,01 MB'])):
            with self.subTest(direction=direction):
                page = self.page(q='csv', sort='size', direction=direction)
                cells = [row.select_one('.files-size-column').get_text(strip=True) for row in self.file_rows(page)]
                self.assertEqual(cells, expected)
                header = page.select_one('th.files-size-column')
                self.assertEqual(header['aria-sort'], 'ascending' if direction == 'asc' else 'descending')
                link = parse_qs(urlsplit(header.a['href']).query)
                self.assertEqual(link, {'q': ['csv'], 'sort': ['size'],
                                        'direction': ['desc' if direction == 'asc' else 'asc']})

    def test_06_expired_missing_and_unsafe_originals_are_not_listed_or_downloaded(self):
        secret = store.SECRETS.read_bytes()
        unsafe = self.document('alpha', 'https://example.org/unsafe.pdf', secret, 8,
                               path='../credentials.json')
        page = self.page()
        text = page.select_one('table[data-files-table]').get_text(' ', strip=True)
        for filename in ('expired.pdf', 'missing.pdf', 'unsafe.pdf'):
            self.assertNotIn(filename, text)
        for sid in (self.expired, self.missing, unsafe, 'unknown-file'):
            response = self.client.get('/files/' + sid + '/download')
            self.assertEqual(response.status_code, 404)
            self.assertNotIn(b'PRIVATE-KEY-NOT-CATALOGUED', response.content)

    def test_07_individual_download_is_the_exact_original_with_a_friendly_name(self):
        response = self.client.get('/files/' + self.b_repeat + '/download')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.raw_b)
        self.assertEqual(store.digest(response.content), store.digest(self.raw_b))
        self.assertIn('application/pdf', response.headers['content-type'])
        disposition = response.headers['content-disposition']
        self.assertIn('attachment', disposition)
        self.assertIn('Richtlinie', disposition)
        self.assertNotIn(store.digest(self.raw_b), disposition)
        detail = self.client.get('/documents/' + self.b_repeat, params={'from':'files'})
        self.assertEqual(detail.status_code, 200)
        links = BeautifulSoup(detail.text, 'html.parser').select('.breadcrumbs a')
        self.assertEqual([link['href'] for link in links], ['/', '/files', '/documents/' + self.b_repeat + '?from=files'])
        self.assertEqual(links[-1]['aria-current'], 'page')

    def test_08_zip_ignores_filter_deduplicates_shared_originals_and_keeps_other_types(self):
        response = self.client.get('/files/download-all.zip', params={'q': 'NO-MATCH-THIS-MUST-NOT-FILTER-ZIP'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b'PK'))
        self.assertIn('attachment', response.headers['content-disposition'])
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn('dateien.json', archive.namelist())
            originals = [name for name in archive.namelist() if name != 'dateien.json']
            self.assertEqual(len(originals), 4)
            self.assertEqual({PurePosixPath(name).parts[0] for name in originals},
                             {'Planungsbüro Alpha','Privat Bau'})
            self.assertEqual({store.digest(archive.read(name)) for name in originals},
                             {store.digest(raw) for raw in (self.raw_a, self.raw_b, self.raw_csv)})
            for name in archive.namelist():
                path = PurePosixPath(name)
                self.assertFalse(path.is_absolute())
                self.assertNotIn('..', path.parts)
                self.assertNotIn('\\', name)
            payload = json.loads(archive.read('dateien.json'))
            self.assertEqual(payload['archive_files'],4)
            for row in payload['documents']:
                self.assertEqual(store.digest(archive.read(row['filename'])),row['sha256'])
            manifest = json.dumps(payload, ensure_ascii=False)
            self.assertIn('Planungsbüro Alpha', manifest)
            self.assertIn('Privat Bau', manifest)
            self.assertNotIn('SNAPSHOT-CONTENT-ONLY-997', manifest)
            self.assertNotIn('PROMPT-ONLY-NOT-CATALOGUED-813', manifest)

    def test_09_metadata_is_escaped_and_reading_routes_do_not_modify_history(self):
        title = '<img src=x onerror="document.attack=1">'
        self.document('alpha', 'https://example.org/harmless.pdf', pdf(title, suffix='escape'), 8)
        tables = ('projects', 'runs', 'snapshots', 'changes', 'reports', 'events')
        before = {table:store.rows('SELECT * FROM ' + table) for table in tables}
        settings, secret = store.CONFIG.read_bytes(), store.SECRETS.read_bytes()
        page = self.page(q='document.attack')
        self.assertEqual(len(self.file_rows(page)), 1)
        self.assertIsNone(page.select_one('img[onerror]'))
        self.assertIsNone(page.select_one('script:not([src])'))
        self.assertIn(title, page.get_text(' ', strip=True))
        response = self.client.get('/files/download-all.zip')
        self.assertEqual(response.status_code, 200)
        self.assertEqual({table:store.rows('SELECT * FROM ' + table) for table in tables}, before)
        self.assertEqual(store.CONFIG.read_bytes(), settings)
        self.assertEqual(store.SECRETS.read_bytes(), secret)

    def test_10_invalid_queries_and_sort_fields_are_rejected(self):
        for params in ({'q': 'a'*513}, {'sort':'text'}, {'direction':'sideways'}):
            with self.subTest(params=params):
                response = self.client.get('/files', params=params)
                self.assertEqual(response.status_code, 400)
        response = self.client.get('/files/' + quote('../credentials.json', safe='') + '/download')
        self.assertEqual(response.status_code, 404)

    def test_11_client_cancellation_always_closes_the_download(self):
        class Download:
            filename = 'original.pdf'
            media_type = 'application/pdf'
            closed = False

            def iterator(self):
                yield b'first original bytes'
                yield b'second original bytes'

            def close(self):
                self.closed = True

        download = Download()
        response = web.DocumentDownloadResponse(download)
        scope = {'type':'http', 'asgi':{'version':'3.0','spec_version':'2.4'},
                 'http_version':'1.1', 'method':'GET', 'scheme':'http',
                 'path':'/files/example/download', 'raw_path':b'/files/example/download',
                 'query_string':b'', 'root_path':'', 'headers':[],
                 'client':('127.0.0.1',1234), 'server':('testserver',80)}
        messages = []

        async def receive():
            return {'type':'http.request', 'body':b'', 'more_body':False}

        async def send(message):
            messages.append(message['type'])
            if message['type'] == 'http.response.body':
                raise asyncio.CancelledError('Synthetic client disconnected')

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(response(scope, receive, send))
        self.assertIn('http.response.start', messages)
        self.assertIn('http.response.body', messages)
        self.assertTrue(download.closed, 'Cancellation must release the archive read lock')

    def test_12_retention_compaction_does_not_make_unchanged_original_newer(self):
        self.document('alpha', 'https://example.org/Richtlinie%20Berlin%202026.pdf', self.raw_a, 2)

        def first_version_date():
            rows = self.file_rows(self.page(sort='changed_at', direction='desc'))
            row = next(row for row in rows if self.row_source(row)['project_id'] == 'alpha'
                       and self.row_source(row)['sha256'] == store.digest(self.raw_a))
            return next(cell.get_text(' ', strip=True) for cell in row.find_all('td')
                        if '01.01.2026' in cell.get_text(' ', strip=True))

        before = first_version_date()
        result = retention.cleanup(at=datetime(2026,3,1,tzinfo=timezone.utc))
        self.assertEqual(result['status'], 'cleaned')
        self.assertIsNone(store.one('SELECT id FROM snapshots WHERE id=?', (self.a,)),
                          'The test must actually compact the earliest redundant snapshot')
        self.assertEqual(first_version_date(), before)

    def test_13_catalog_remains_available_during_a_browser_analysis(self):
        rid = 'files-concurrent-run'
        store.execute('INSERT INTO runs(id,project_id,started,status,origin) VALUES(?,?,?,?,?)',
                      (rid, 'alpha', store.now(), 'queued', 'test'))
        project = store.one('SELECT * FROM projects WHERE id=?', ('alpha',))

        async def exercise():
            started, release = asyncio.Event(), asyncio.Event()

            async def monitor(current, run_id):
                started.set()
                await release.wait()
                store.execute('UPDATE runs SET status=?,finished=? WHERE id=?',
                              ('success', store.now(), run_id))

            with patch.object(engine, 'monitor', side_effect=monitor), \
                 patch.object(engine.project_icons, 'refresh', new=AsyncMock()), \
                 patch.object(engine.site_access, 'get_secret', return_value=None), \
                 patch.object(engine.site_access, 'public_config', return_value=None):
                job = asyncio.create_task(engine.run_job(project, rid))
                try:
                    await asyncio.wait_for(started.wait(), timeout=3)
                    # This executes on another thread while monitor is still
                    # waiting, so an exclusive monitor flock cannot slip by.
                    catalog = await asyncio.wait_for(
                        asyncio.to_thread(document_files.list_files), timeout=3)
                    self.assertEqual(catalog['total'], 4)
                    self.assertFalse(job.done())
                finally:
                    release.set()
                    await asyncio.wait_for(job, timeout=3)

        asyncio.run(exercise())
        self.assertEqual(store.one('SELECT status FROM runs WHERE id=?', (rid,))['status'], 'success')
        self.assertFalse(engine.BUSY.locked())

    def hash_record(self, sid, status, previous=None):
        current = store.one('SELECT * FROM snapshots WHERE id=?', (sid,))
        prior = store.one('SELECT * FROM snapshots WHERE id=?', (previous,)) if previous else None
        store.execute('UPDATE snapshots SET text=?,text_sha256=? WHERE id=?', ('', store.digest(''), sid))
        target = self.root / 'artifacts' / current['run_id'] / (sid + '.extraction.json')
        store.save_json(target, {'method': 'hash_only', 'comparison_mode': 'sha256',
                                'content_analysis_performed': False, 'original_sha256_verified': True,
                                'source_sha256': current['sha256'], 'comparison_status': status,
                                'previous_snapshot_id': previous,
                                'previous_sha256': prior['sha256'] if prior else None})
        return target, current

    def test_14_hash_only_view_has_honest_comparison_source_hash_and_exact_download(self):
        for status, previous, label in (
                ('baseline', None, 'Ausgangsstand gespeichert'),
                ('unchanged', self.b, 'Datei unverändert'),
                ('changed', self.a, 'Datei geändert')):
            with self.subTest(status=status):
                _, current = self.hash_record(self.b_repeat, status, previous)
                response = self.client.get('/documents/' + self.b_repeat, params={'from': 'files'})
                self.assertEqual(response.status_code, 200)
                page = BeautifulSoup(response.text, 'html.parser')
                result = page.select_one('[data-document-comparison]')
                self.assertEqual(result['data-document-comparison'], status)
                self.assertEqual(result.get_text(strip=True), label)
                self.assertIn(current['sha256'], page.select_one('[data-document-sha256]').get_text())
                self.assertIsNotNone(page.select_one('a[href="' + current['url'] + '"]'))
                link = page.select_one('[data-document-download]')['href']
                self.assertEqual(self.client.get(link).content, self.raw_b)
                self.assertIsNone(page.select_one('[data-historical-document-text]'))
                for misleading in ('Extrahierter Dokumenttext', 'Unvollständig', 'OCR', 'unbekannte Extraktion'):
                    self.assertNotIn(misleading, page.get_text(' ', strip=True))
                if previous:
                    self.assertIsNotNone(page.select_one('a[href="/documents/' + previous + '?from=files"]'))
                    prior = store.one('SELECT sha256 FROM snapshots WHERE id=?', (previous,))
                    self.assertIn(prior['sha256'], page.get_text())
                archived = BeautifulSoup(self.client.get('/runs/' + current['run_id']).text, 'html.parser')
                self.assertIn('Dateinachweis öffnen', archived.get_text())
                self.assertNotIn('Archivierter Text und technische Angaben', archived.get_text())

    def test_15_historical_text_remains_collapsed_and_marked_as_historical(self):
        original = store.one('SELECT text FROM snapshots WHERE id=?', (self.a,))['text']
        page = BeautifulSoup(self.client.get('/documents/' + self.a).text, 'html.parser')
        historical = page.select_one('details[data-historical-document-text]')
        self.assertIsNotNone(historical)
        self.assertFalse(historical.has_attr('open'))
        self.assertIn('Historisch archivierter Dokumenttext', historical.get_text())
        self.assertIn('früheren Erfassung', historical.get_text())
        self.assertIn(original, historical.get_text())
        self.assertIsNone(page.select_one('[data-document-comparison]'))
        self.assertEqual(store.one('SELECT text FROM snapshots WHERE id=?', (self.a,))['text'], original)

    def test_16_foreign_or_damaged_hash_record_cannot_claim_verified_comparison(self):
        target, _ = self.hash_record(self.b_repeat, 'unchanged', self.b)
        record = store.read_json(target)
        record['source_sha256'] = store.digest(b'another original')
        store.save_json(target, record)
        page = BeautifulSoup(self.client.get('/documents/' + self.b_repeat).text, 'html.parser')
        self.assertIsNone(page.select_one('[data-document-comparison]'))
        target.write_text('{broken record', encoding='utf-8')
        self.assertEqual(self.client.get('/documents/' + self.b_repeat).status_code, 200)

    def test_17_examples_use_hash_only_files_and_preserve_html_article_analysis(self):
        from prompt_examples import EXAMPLES, FILE_CHECK
        for example in EXAMPLES:
            with self.subTest(example=example['title']):
                self.assertTrue(example['prompt'].endswith(FILE_CHECK))
                for obsolete in ('text_complete', 'unreadable_pages', 'ocr_budget_limited',
                                 'Vergleiche extrahierte Texte', 'Textvergleich eingeschränkt'):
                    self.assertNotIn(obsolete, example['prompt'])
        iww = next(e for e in EXAMPLES if e['url'] == 'https://www.iww.de/pbp/archiv')
        self.assertIn('Öffne anschließend alle Artikel', iww['prompt'])
        self.assertIn('kurzer fachlicher Absatz', iww['prompt'])
        custom = BeautifulSoup(self.client.get('/projects/new').text, 'html.parser')
        self.assertIn('ausschließlich per SHA-256', custom.select_one('textarea[name=prompt]').get_text())

    def test_18_hash_bound_metadata_distinguishes_pdf_dates_from_http_server_dates(self):
        from document_metadata import extract_document_metadata
        target, _ = self.hash_record(self.b_repeat, 'unchanged', self.b)
        record = store.read_json(target)
        record['document_metadata'] = extract_document_metadata(self.raw_b, '.pdf', {
            'Last-Modified': 'Mon, 15 Jun 2026 13:34:39 GMT', 'ETag': '"original-2"',
            'Content-Type': 'application/pdf', 'Content-Length': str(len(self.raw_b))})
        store.save_json(target, record)
        page = BeautifulSoup(self.client.get('/documents/' + self.b_repeat).text, 'html.parser')
        block = page.select_one('[data-document-metadata]')
        self.assertIsNotNone(block)
        self.assertIn('PDF-Metadaten', block.get_text())
        self.assertIn('PDF ModDate', block.get_text())
        self.assertIn('01.12.2025 · 09:00:00 · Europe/Berlin', block.get_text())
        self.assertIn('HTTP-Antwort des Servers', block.get_text())
        self.assertIn('HTTP Last-Modified', block.get_text())
        self.assertIn('15.06.2026 · 15:34:39 · Europe/Berlin', block.get_text())
        self.assertIn('HTTP ETag', block.get_text())
        self.assertEqual(page.select_one('[data-document-comparison]')['data-document-comparison'], 'unchanged')
        self.assertIsNone(page.select_one('[data-historical-document-text]'))
        record['source_sha256'] = store.digest(b'wrong original')
        store.save_json(target, record)
        foreign = BeautifulSoup(self.client.get('/documents/' + self.b_repeat).text, 'html.parser')
        self.assertIsNone(foreign.select_one('[data-document-metadata]'))


    def test_19_every_supported_type_has_visible_colored_extension_icon(self):
        from document_types import MEDIA
        for ext in MEDIA:self.document('alpha','https://example.org/sample'+ext,('ICON-FIXTURE-'+ext).encode(),8,extension=ext[1:])
        page=self.page();icons=page.select('.file-type-icon.file-icon')
        self.assertTrue({ext[1:] for ext in MEDIA}<={i['data-file'] for i in icons})
        for item in icons:
            self.assertEqual(item['role'],'img');self.assertTrue(item['aria-label'].endswith('-Datei'))
        self.assertIsNotNone(page.select_one('link[href^="/static/vendor/file-icons/style.css"]'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
