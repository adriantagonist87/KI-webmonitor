"""Isolated usability acceptance: real routes/templates, no scheduler or network.

Uses a disposable database and credentials. Optional Brave tests only use a new
incognito context whose requests are fulfilled from this private TestClient.
"""
import asyncio
import json
import mimetypes
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

scratch = tempfile.TemporaryDirectory(prefix='.usability-check-', dir='/data' if Path('/data').is_dir() else None)
os.environ['DATA_DIR'] = str(Path(scratch.name) / 'bootstrap')
import store
store.CONFIG = Path(scratch.name) / 'bootstrap-settings.json'
store.SECRETS = Path(scratch.name) / 'bootstrap-secrets.json'
import engine
import scheduling
import tag_registry
import web
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from jinja2 import FileSystemLoader

ROOT = Path(__file__).parent
HTML = {'Accept': 'text/html'}
PID = 'usability-project'
STAMP = '2026-11-06T08:00:00+00:00'
ORIGIN = 'https://usability.test'


class Fixture:
    def setup_fixture(self):
        self.directory = tempfile.TemporaryDirectory(dir=scratch.name)
        self.root = Path(self.directory.name)
        store.DATA = web.DATA = engine.DATA = self.root / 'data'
        store.CONFIG = web.CONFIG = self.root / 'settings.json'
        store.SECRETS = web.SECRETS = self.root / 'credentials.json'
        store.init()
        store.save_json(store.CONFIG, {'omlx_url': 'http://192.0.2.2:8000/v1',
                                      'global_schedule': dict(scheduling.DEFAULT)})
        store.save_json(store.SECRETS, {'smtp_password': 'PRIVATE_USABILITY_SENTINEL'})
        web.env.loader = FileSystemLoader(ROOT / 'templates')
        self.client = TestClient(web.app, follow_redirects=False)
        self.client.get('/')
        self.csrf = self.client.cookies['kiwm_csrf']

    def cleanup_fixture(self):
        self.client.close()
        self.directory.cleanup()

    def project(self, *, enabled=1):
        store.execute('''INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created)
                         VALUES(?,?,?,?,?,?,?,?,?)''',
                      (PID, 'Synthetisches Projekt', 'https://example.org/monitor',
                       'Ursprünglicher Auftrag', 'example.org', '0 6 * * *',
                       enabled, '2099-01-01T00:00:00+00:00', STAMP))

    def form(self, **extra):
        value = {'id': '', 'name': 'Neuer Alias', 'url': 'https://example.org/monitor',
                 'prompt': 'Behalte diesen ausführlichen eigenen Analyseauftrag.',
                 'domains': 'example.org', 'color': '#8b5cf6', 'enabled': 'on',
                 'use_global_schedule': 'on', 'schedule_mode': 'weekly',
                 'schedule_time': '14:30', 'schedule_weekday': '3', 'tags_present': '1'}
        value.update(extra)
        return value

    def post(self, path, data=None, *, html=True):
        return self.client.post(path, data={'csrf': self.csrf, **(data or {})}, headers=HTML if html else {})

    def soup(self, path):
        response = self.client.get(path, headers=HTML)
        self.assertEqual(response.status_code, 200)
        return BeautifulSoup(response.text, 'html.parser')


class UsabilityRoutes(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup_fixture()

    def tearDown(self):
        self.cleanup_fixture()

    def test_rejected_new_project_preserves_input_without_creating_data(self):
        tag_registry.save('Arbeit', '#1967d2')
        tid = tag_registry.all_tags()[0]['id']
        form = self.form(domains='wrong.example', tag_ids=tid)
        response = self.post('/projects/save', form)
        self.assertEqual(response.status_code, 400)
        page = BeautifulSoup(response.text, 'html.parser')
        self.assertEqual(page.h1.get_text(), 'Neues Projekt')
        self.assertIn('Projekt noch nicht gespeichert', page.get_text())
        self.assertEqual(page.select_one('#prompt').get_text(), form['prompt'])
        self.assertEqual(page.select_one('#name')['value'], form['name'])
        self.assertEqual(page.select_one('#color')['value'], form['color'])
        self.assertEqual(page.select_one('#domains')['value'], 'wrong.example')
        self.assertTrue(page.select_one('#domains').find_parent('details').has_attr('open'))
        self.assertEqual(page.select_one('#project-schedule-time')['value'], '14:30')
        self.assertEqual(page.select_one('input[name=tag_ids][checked]')['value'], tid)
        self.assertEqual(page.select_one('input[name=tag_ids][checked]')['aria-label'], 'Arbeit')
        self.assertIsNotNone(page.select_one('form[data-restored-input]'))
        self.assertIsNone(page.select_one('.danger-zone'))
        self.assertEqual(store.rows('SELECT * FROM projects'), [])

    def test_rejected_edit_leaves_existing_archive_unchanged(self):
        self.project()
        before = store.one('SELECT * FROM projects WHERE id=?', (PID,))
        response = self.post('/projects/save', self.form(id=PID, domains='bad.example'))
        self.assertEqual(response.status_code, 400)
        self.assertIn('Projekt bearbeiten', response.text)
        self.assertEqual(store.one('SELECT * FROM projects WHERE id=?', (PID,)), before)

    def test_corrected_form_can_be_saved(self):
        with patch('web.project_icons.refresh', new=AsyncMock()):
            response = self.post('/projects/save', self.form())
        self.assertEqual(response.status_code, 303)
        record = store.one('SELECT * FROM projects')
        self.assertEqual(record['name'], 'Neuer Alias')
        self.assertEqual(record['prompt'], self.form()['prompt'])

    def test_malformed_url_is_validation_error_not_internal_error(self):
        response = self.post('/projects/save', self.form(url='https://[invalid'))
        self.assertEqual(response.status_code, 400)
        self.assertIn('Projekt noch nicht gespeichert', response.text)

    def test_machine_error_stays_json_and_no_credentials_are_exposed(self):
        response = self.post('/projects/save', self.form(domains='bad.example'), html=False)
        self.assertEqual(response.status_code, 400)
        self.assertIn('detail', response.json())
        self.assertNotIn('PRIVATE_USABILITY_SENTINEL', response.text)

    def test_manual_mode_blocks_analysis_visibly_and_server_side(self):
        self.project()
        store.execute("UPDATE state SET value='1' WHERE key='manual'")
        for path in ('/', '/projects/' + PID):
            page = self.soup(path)
            self.assertTrue(page.select_one('form[action$="/run"] button').has_attr('disabled'))
            self.assertIn('Manueller Browsermodus aktiv', page.get_text())
        response = self.post('/projects/' + PID + '/run')
        self.assertEqual(response.status_code, 409)
        self.assertIn('Aktion nicht möglich', response.text)
        self.assertIn('href="/browser"', response.text)
        self.assertEqual(store.rows('SELECT * FROM runs'), [])

    def test_busy_browser_cannot_enter_manual_mode(self):
        asyncio.run(engine.BUSY.acquire())
        try:
            page = self.soup('/browser')
            self.assertTrue(page.select_one('form[action="/manual"] button').has_attr('disabled'))
            response = self.post('/manual', {'mode': 'start'})
            self.assertEqual(response.status_code, 409)
            self.assertIn('Aktive Prüfung zuerst', response.text)
        finally:
            engine.BUSY.release()
        self.assertFalse(store.manual())

    def test_paused_schedule_does_not_announce_a_next_run(self):
        self.project(enabled=0)
        page = self.soup('/projects/' + PID)
        self.assertIn('Automatik: pausiert', page.get_text())
        self.assertNotIn('Nächste Prüfung:', page.get_text())

    def test_failed_run_does_not_claim_successful_unchanged_comparison(self):
        self.project()
        store.execute('INSERT INTO runs(id,project_id,started,finished,status,error,origin) VALUES(?,?,?,?,?,?,?)',
                      ('failed-ui', PID, STAMP, STAMP, 'failed', 'Synthetischer Fehler', 'manual'))
        page = self.soup('/runs/failed-ui')
        self.assertIn('Kein vollständiger Vergleich', page.get_text())
        self.assertNotIn('erste vollständige Erfassung', page.get_text())

    def test_reports_distinguish_preview_test_mail_and_regular_delivery(self):
        self.assertIn('Noch keine Wochenberichte vorhanden.', self.soup('/reports').get_text())
        for report_id, key, status in (
                ('regular-sent', 'week-2026-01', 'sent'), ('sent', 'test-sent', 'sent'),
                ('preview', 'test-preview', 'preview'), ('pending', 'test-pending', 'pending'),
                ('failed', 'test-failed', 'failed')):
            store.execute('INSERT INTO reports(id,period_key,created,start,end,content,mail_status,error) VALUES(?,?,?,?,?,?,?,?)',
                          (report_id, key, STAMP, STAMP, STAMP, 'Testbericht', status,
                           'SMTP: SyntheticError' if status == 'failed' else None))
        page = self.soup('/reports')
        self.assertCountEqual([el.get_text(strip=True) for el in page.select('.report-table .badge')],
                              ['Versandt', 'Testmail versandt', 'Nur Vorschau', 'Versand ausstehend', 'Versand fehlgeschlagen'])
        self.assertNotIn('test-failed', page.get_text())
        self.assertNotIn('SyntheticError', page.get_text())
        regular = self.soup('/reports?regular=1')
        self.assertTrue(regular.select_one('[name=regular]').has_attr('checked'))
        self.assertEqual([el['href'] for el in regular.select('.report-table .report-name')], ['/reports/regular-sent'])
        detail = self.soup('/reports/failed')
        self.assertIn('Versand fehlgeschlagen', detail.select_one('.report-info').get_text())
        self.assertIn('SyntheticError', detail.select_one('.error-banner').get_text())

    def test_smtp_invalid_ports_never_replace_saved_settings(self):
        before = store.CONFIG.read_bytes()
        for port in ('invalid', '0', '65536'):
            response = self.post('/settings/smtp', {'smtp_port': port})
            self.assertEqual(response.status_code, 400)
            self.assertIn('SMTP-Port', response.text)
            self.assertEqual(store.CONFIG.read_bytes(), before)

    def test_companion_ports_are_configurable_and_unsafe_urls_fall_back(self):
        with patch.dict(os.environ, {'BROWSER_UI_URL': 'http://localhost:18866/',
                                     'MAILPIT_UI_URL': 'http://localhost:18867/', 'KIWM_TEST_TOOLS':'1'}):
            self.assertIn('http://localhost:18866/vnc.html?', self.client.get('/browser').text)
            self.assertIn('http://localhost:18867', self.client.get('/smtp').text)
        for value in ('javascript:alert(1)', 'http://example.org:8766',
                      'http://name:password@localhost:8766', 'http://localhost:bad'):
            with patch.dict(os.environ, {'BROWSER_UI_URL': value}):
                self.assertEqual(web.companion_url('BROWSER_UI_URL', 'http://localhost:8766'), 'http://localhost:8766')

    def test_missing_page_has_navigation_and_machine_404_remains_json(self):
        response = self.client.get('/not-a-page', headers=HTML)
        self.assertEqual(response.status_code, 404)
        self.assertIn('Diese Seite oder Datei ist nicht mehr vorhanden.', response.text)
        self.assertIn('aria-label="Navigationspfad"', response.text)
        self.assertEqual(self.client.get('/not-a-page').json(), {'detail': 'Not Found'})

    def test_project_tab_titles_distinguish_views_and_escape_aliases(self):
        self.project()
        alias = 'BIM & „Planung“ </title><script>alert(1)</script>'
        store.execute('UPDATE projects SET name=? WHERE id=?', (alias, PID))
        store.execute('INSERT INTO runs(id,project_id,started,finished,status,origin) VALUES(?,?,?,?,?,?)',
                      ('title-run', PID, STAMP, STAMP, 'success', 'manual'))
        expectations = {
            '/projects/' + PID: alias,
            '/projects/' + PID + '/edit': alias + ' – Bearbeiten',
            '/projects/' + PID + '/access': alias + ' – Webseitenzugang',
            '/runs/title-run': alias + ' – Prüfung',
            '/projects/new': 'Neues Projekt',
            '/': 'Projekte',
            '/tags': 'Tags',
            '/reports': 'Wochenberichte',
        }
        for path, title in expectations.items():
            with self.subTest(path=path):
                response = self.client.get(path, headers=HTML)
                self.assertEqual(response.status_code, 200)
                page = BeautifulSoup(response.text, 'html.parser')
                self.assertEqual(page.title.get_text(), title + ' · KI-Webmonitor')
                self.assertIsNone(page.title.find('script'))
                self.assertNotIn('</title><script>alert(1)', response.text)
                self.assertNotIn('PRIVATE_USABILITY_SENTINEL', page.title.get_text())


@unittest.skipUnless(os.environ.get('BROWSER_CDP_URL'), 'Select an isolated QA browser')
class UsabilityBrowser(Fixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        self.setup_fixture()
        self.project()
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.connect_over_cdp(os.environ['BROWSER_CDP_URL'])
        self.context = await self.browser.new_context(viewport={'width': 1280, 'height': 900}, service_workers='block')
        await self.context.route(ORIGIN + '/**', self.serve)
        self.page = await self.context.new_page()

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.pw.stop()
        self.cleanup_fixture()

    async def serve(self, route):
        request = route.request
        path = urlsplit(request.url).path
        if path.startswith('/static/'):
            asset = (ROOT / path.lstrip('/')).resolve()
            assert asset.is_relative_to((ROOT / 'static').resolve())
            return await route.fulfill(status=200, content_type=mimetypes.guess_type(asset.name)[0] or 'application/octet-stream', body=asset.read_bytes())
        if request.method == 'POST' and path == '/projects/save':
            # Browser origin is synthetic; the TestClient has its own CSRF cookie.
            from urllib.parse import parse_qs
            values = {key: value if len(value) > 1 else value[0] for key, value in parse_qs(request.post_data or '', keep_blank_values=True).items()}
            values['csrf'] = self.csrf
            response = self.client.post(path, data=values, headers=HTML)
        elif request.method == 'GET' and (path in ('/', '/projects/new', '/api/project-status', '/reports') or path == '/projects/' + PID):
            response = self.client.get(path, params=urlsplit(request.url).query, headers=HTML)
        else:
            return await route.fulfill(status=404, body='Synthetic origin only')
        return await route.fulfill(status=response.status_code,
                                   content_type=response.headers.get('content-type', 'text/html'), body=response.content)

    async def test_rejected_native_form_keeps_prompt_tags_and_unsaved_guard(self):
        await self.page.goto(ORIGIN + '/projects/new')
        await self.page.locator('#name').fill('Browser Test')
        await self.page.locator('#url').fill('https://example.org/monitor')
        await self.page.locator('#prompt').fill('Eigener langer Auftrag, der nach dem Fehler erhalten bleiben muss.')
        await self.page.locator('summary', has_text='Erlaubte Hostnamen').click()
        await self.page.locator('#domains').fill('wrong.example')
        await self.page.locator('.project-form button[type=submit], .project-form .form-footer button').click()
        await self.page.wait_for_selector('form[data-restored-input]')
        self.assertEqual(await self.page.locator('#prompt').input_value(), 'Eigener langer Auftrag, der nach dem Fehler erhalten bleiben muss.')
        self.assertIn('Projekt noch nicht gespeichert', await self.page.locator('.warning').last.inner_text())
        # Evaluate a cancelable event; no real navigation, dialogs or production routes.
        prevented = await self.page.evaluate("(() => { const event=new Event('beforeunload',{cancelable:true}); window.dispatchEvent(event); return event.defaultPrevented; })()")
        self.assertTrue(prevented)

    async def test_manual_controls_are_disabled_in_actual_browser(self):
        store.execute("UPDATE state SET value='1' WHERE key='manual'")
        await self.page.goto(ORIGIN)
        self.assertTrue(await self.page.locator('.analyze-button').is_disabled())
        self.assertIn('Manuellen Browsermodus zuerst beenden', await self.page.locator('.analyze-button').get_attribute('title'))
        await self.page.locator('[data-ui-size]').select_option('150')
        dimensions = await self.page.evaluate('''() => {
          const word = document.querySelector('a[href="/files"] .nav-link-label');
          const range = document.createRange(); range.selectNodeContents(word);
          range.setStart(word.firstChild, word.textContent.indexOf('Dateiverzeichnis'));
          const brand = document.querySelector('.brand-name');
          return {sidebar:document.querySelector('.sidebar').getBoundingClientRect().width,
            wordLines:range.getClientRects().length,brandHeight:brand.getBoundingClientRect().height,
            brandFont:parseFloat(getComputedStyle(brand).fontSize)};
        }''')
        self.assertAlmostEqual(dimensions['sidebar'], 315, delta=1)
        self.assertEqual(dimensions['wordLines'], 1, 'Do not split Dateiverzeichnis in the middle of the word')
        self.assertLess(dimensions['brandHeight'], dimensions['brandFont'] * 2)
        await self.page.locator('[data-ui-size]').select_option('100')
        self.assertAlmostEqual(await self.page.locator('.sidebar').evaluate('(node) => node.getBoundingClientRect().width'), 210, delta=1)
        self.assertEqual(await self.page.title(), 'Projekte · KI-Webmonitor')
        await self.page.goto(ORIGIN + '/projects/' + PID)
        self.assertEqual(await self.page.title(), 'Synthetisches Projekt · KI-Webmonitor')

    async def test_report_table_keeps_recipient_readable_and_filter_usable(self):
        store.execute('''INSERT INTO reports(id,period_key,created,start,end,content,mail_status,
                         audience,distribution_alias,recipient_email) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                      ('preview', 'test-preview', STAMP, '2025-12-27T08:00:00+00:00', STAMP, 'Vorschau',
                       'preview', 'distribution', 'Verteiler Arbeit', 'recipient@example.test'))
        store.execute('INSERT INTO reports(id,period_key,created,start,end,content,mail_status) VALUES(?,?,?,?,?,?,?)',
                      ('regular', 'week-2026-01', STAMP, STAMP, STAMP, 'Bericht', 'sent'))
        await self.page.goto(ORIGIN + '/reports')
        table = self.page.locator('.report-table')
        self.assertEqual(await table.locator('th').all_text_contents(), ['Bericht', 'Versand', 'Berichtszeitraum', 'Erstellt'])
        preview = table.locator('tr', has_text='Nur Vorschau')
        name = await preview.locator('.report-name').bounding_box()
        recipient = await preview.locator('.report-recipient').bounding_box()
        self.assertGreater(recipient['y'], name['y'])
        self.assertNotIn(':', await preview.locator('.report-date').first.inner_text())
        await self.page.set_viewport_size({'width':390,'height':844})
        self.assertTrue(await self.page.locator('.report-table-wrap').evaluate('e => e.scrollWidth > e.clientWidth'))
        self.assertTrue(await self.page.evaluate('document.documentElement.scrollWidth === document.documentElement.clientWidth'))
        await self.page.get_by_label('Nur reguläre Berichte').check()
        await self.page.get_by_role('button', name='Filtern', exact=True).click()
        await self.page.wait_for_url(ORIGIN + '/reports?*regular=1')
        self.assertEqual(await table.locator('.report-name').count(), 1)
        self.assertEqual(await table.locator('.report-name').get_attribute('href'), '/reports/regular')


if __name__ == '__main__':
    unittest.main(verbosity=2)
