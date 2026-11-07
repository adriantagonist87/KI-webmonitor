"""Retry presentation checks in an isolated DB and synthetic Brave origin.

No production DB/profile, external requests, scheduler or model calls. Set
BROWSER_CDP_URL to an isolated QA browser to run the actual JavaScript checks.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import mimetypes
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

scratch = tempfile.TemporaryDirectory(prefix='.retry-ui-check-', dir='/data')
os.environ['DATA_DIR'] = scratch.name
import store
import retry_jobs
store.init()
from web import app
from fastapi.testclient import TestClient
from bs4 import BeautifulSoup

PID = 'retry-ui'
ORIGIN = 'https://retry-ui.test'
ROOT = Path(__file__).parent


def reset():
    with store.db() as c:
        for table in ('run_retries', 'events', 'change_reviews', 'changes', 'snapshots', 'runs', 'projects'):
            c.execute('DELETE FROM ' + table)
        c.execute("INSERT OR REPLACE INTO state VALUES('manual','0')")
    stamp = store.now()
    store.execute('''INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created)
                     VALUES(?,?,?,?,?,?,?,?,?)''',
                  (PID, 'Retry Projekt', 'https://example.org/monitor', 'PRIVATE_PROMPT_SENTINEL',
                   'example.org', '0 6 * * *', 1, '2099-01-01T00:00:00+00:00', stamp))


def failed(status='failed', register=True, rid='original', stamp=None):
    stamp = stamp or store.now()
    store.execute('INSERT INTO runs(id,project_id,started,finished,status,error,summary,origin) VALUES(?,?,?,?,?,?,?,?)',
                  (rid, PID, stamp, stamp, status, 'PRIVATE_ERROR_SENTINEL', 'Originale Prüfung fehlgeschlagen.', 'manual'))
    if register:
        retry_jobs.register_result(rid)
    return store.one('SELECT * FROM runs WHERE id=?', (rid,))


def card(client):
    page = BeautifulSoup(client.get('/').text, 'html.parser')
    return page, page.select_one('[data-project-id="' + PID + '"]')


class RetryViews(unittest.TestCase):
    def setUp(self):
        reset()
        self.client = TestClient(app)
        self.client.get('/')

    def tearDown(self):
        self.client.close()

    def save(self, **extra):
        value = {'id': PID, 'name': 'Retry Projekt', 'url': 'https://example.org/monitor',
                 'domains': 'example.org', 'prompt': 'PRIVATE_PROMPT_SENTINEL',
                 'csrf': self.client.cookies['kiwm_csrf']}
        value.update(extra)
        return self.client.post('/projects/save', data=value, follow_redirects=False)

    def test_historical_failure_has_no_invented_countdown(self):
        failed(register=False)
        page, item = card(self.client)
        self.assertEqual(item.select_one('.badge').get_text(strip=True), 'Fehler')
        self.assertTrue(item.select_one('[data-retry-status]').has_attr('hidden'))
        self.assertNotIn('Wird wiederholt in', item.get_text(' ', strip=True))

    def test_failed_and_partial_cards_have_real_due_time(self):
        for status in ('failed', 'partial'):
            reset(); row = failed(status)
            page, item = card(self.client)
            indicator = item.select_one('[data-retry-status]')
            self.assertFalse(indicator.has_attr('hidden'))
            self.assertEqual(indicator['data-retry-state'], 'pending')
            self.assertEqual(datetime.fromisoformat(indicator['data-retry-due']) - datetime.fromisoformat(row['finished']), timedelta(minutes=30))
            self.assertRegex(indicator.get_text(strip=True), r'^Wird wiederholt in (?:29:[0-5][0-9]|30:00)$')
            self.assertEqual(indicator['aria-live'], 'off')
            self.assertEqual(item['data-error'], '1')
            self.assertIsNotNone(page.body.get('data-server-now'))

    def test_public_status_contains_no_prompt_or_error_or_credentials(self):
        failed()
        response = self.client.get('/api/project-status')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        data = response.json()
        self.assertEqual(set(data), {'server_now', 'projects'})
        self.assertEqual(set(data['projects'][0]),
                         {'id', 'enabled', 'last_success', 'last_checked', 'last_change', 'status', 'retry'})
        self.assertNotIn('PRIVATE_', response.text)
        self.assertIsNotNone(datetime.fromisoformat(data['server_now']).tzinfo)

    def test_pause_cancels_pending_and_alias_edit_does_not(self):
        failed()
        self.assertEqual(self.save(name='Neuer Alias', enabled='on').status_code, 303)
        self.assertEqual(retry_jobs.project_retry(PID)['state'], 'pending')
        self.assertEqual(self.save().status_code, 303)
        self.assertIsNone(retry_jobs.project_retry(PID))
        self.assertTrue(card(self.client)[1].select_one('[data-retry-status]').has_attr('hidden'))

    def test_changed_url_cancels_plan_without_fetching_in_test(self):
        failed()
        with patch('web.project_icons.refresh', new=AsyncMock()) as refresh:
            self.assertEqual(self.save(url='https://example.org/other', enabled='on').status_code, 303)
            self.assertEqual(refresh.await_count, 1)
        self.assertIsNone(retry_jobs.project_retry(PID))

    def test_cancelled_queued_attempt_does_not_mask_original_failure(self):
        failed(stamp=(datetime.now(timezone.utc)-timedelta(minutes=31)).isoformat())
        due = retry_jobs.project_retry(PID)['due_at']
        child = retry_jobs.sweep_due(due)[0]
        self.assertEqual(self.save().status_code, 303)
        self.assertEqual(store.one('SELECT status FROM runs WHERE id=?', (child,))['status'], 'cancelled')
        self.assertEqual(card(self.client)[1].select_one('.badge').get_text(strip=True), 'Fehler')
        self.assertEqual(self.client.get('/api/project-status').json()['projects'][0]['status'], 'failed')
        detail = BeautifulSoup(self.client.get('/projects/' + PID).text, 'html.parser')
        link = detail.select_one('a[href="/runs/' + child + '"]')
        self.assertEqual(link.get_text(strip=True), 'Nicht ausgeführt')
        self.assertIn('Originale Prüfung fehlgeschlagen.', detail.get_text(' ', strip=True))
        run = BeautifulSoup(self.client.get('/runs/' + child).text, 'html.parser')
        self.assertEqual(run.select_one('.page-heading .badge').get_text(strip=True), 'Abgebrochen')
        self.assertIn('nicht ausgeführt', run.select_one('.report').get_text())
        self.assertNotIn('Die erste vollständige Erfassung', run.get_text(' ', strip=True))

    def test_manual_run_supersedes_countdown(self):
        failed()
        response = self.client.post('/projects/' + PID + '/run',
                                    data={'csrf': self.client.cookies['kiwm_csrf']}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIsNone(retry_jobs.project_retry(PID))
        item = card(self.client)[1]
        self.assertEqual(item.select_one('.badge').get_text(strip=True), 'Wartet')
        self.assertTrue(item.select_one('[data-retry-status]').has_attr('hidden'))


@unittest.skipUnless(os.environ.get('BROWSER_CDP_URL'), 'Select an isolated QA browser')
class RetryBrowser(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        reset()
        stamp = (datetime.now(timezone.utc)-timedelta(minutes=31)).isoformat() if self._testMethodName == 'test_retry_transitions_preserve_search_and_update_error_filter' else None
        failed(stamp=stamp)
        self.client = TestClient(app)
        self.client.get('/')
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.connect_over_cdp(os.environ['BROWSER_CDP_URL'])
        self.context = await self.browser.new_context(viewport={'width': 1280, 'height': 900}, service_workers='block')
        self.requests = []
        await self.context.route(ORIGIN + '/**', self.serve)
        await self.context.add_init_script('''(() => {
          const NativeDate=Date, originalNow=performance.now.bind(performance);
          window.__elapsed=0;
          Object.defineProperty(performance,'now',{value:()=>originalNow()+window.__elapsed});
          window.Date=class extends NativeDate {
            constructor(...values){super(...(values.length?values:[NativeDate.now()+172800000]));}
            static now(){return NativeDate.now()+172800000;}
          };
        })();''')
        self.page = await self.context.new_page()
        await self.page.goto(ORIGIN, wait_until='networkidle')

    async def asyncTearDown(self):
        await self.context.close(); await self.browser.close(); await self.pw.stop(); self.client.close()

    async def serve(self, route):
        path = urlsplit(route.request.url).path
        if path.startswith('/static/'):
            asset = (ROOT / path.lstrip('/')).resolve()
            assert asset.is_relative_to((ROOT / 'static').resolve())
            return await route.fulfill(status=200, content_type=mimetypes.guess_type(asset.name)[0] or 'application/octet-stream', body=asset.read_bytes())
        self.requests.append(path)
        if path not in ('/', '/api/project-status'):
            return await route.fulfill(status=404, body='Synthetic origin only')
        response = self.client.get(path)
        return await route.fulfill(status=response.status_code, content_type=response.headers.get('content-type', 'text/html'), body=response.content)

    async def refresh(self):
        before = self.requests.count('/api/project-status')
        await self.page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        for _ in range(50):
            if self.requests.count('/api/project-status') > before:
                await self.page.wait_for_timeout(100)
                return
            await self.page.wait_for_timeout(20)
        self.fail('Visible dashboard did not refresh its own status endpoint')

    async def test_live_countdown_uses_server_clock_and_never_goes_negative(self):
        indicator = self.page.locator('[data-retry-label]')
        first = await indicator.inner_text()
        self.assertRegex(first, r'^Wird wiederholt in (?:29:[0-5][0-9]|30:00)$')
        await self.page.wait_for_timeout(1200)
        self.assertNotEqual(await indicator.inner_text(), first)
        await self.page.evaluate('window.__elapsed=1801000')
        await self.page.wait_for_timeout(1100)
        self.assertEqual(await indicator.inner_text(), 'Wiederholung wartet')
        self.assertEqual(await self.page.locator('[data-retry-status]').get_attribute('aria-live'), 'off')

    async def test_retry_transitions_preserve_search_and_update_error_filter(self):
        await self.page.locator('[data-project-search]').fill('Retry Projekt')
        await self.page.locator('[data-filter=error]').click()
        due = retry_jobs.project_retry(PID)['due_at']
        child = retry_jobs.sweep_due(due)[0]
        await self.refresh()
        self.assertEqual(await self.page.locator('.project-head .badge').inner_text(), 'Wartet')
        self.assertEqual(await self.page.locator('[data-retry-label]').inner_text(), 'Wiederholung wartet')
        self.assertTrue(retry_jobs.begin_attempt(child))
        await self.refresh()
        self.assertEqual(await self.page.locator('[data-retry-label]').inner_text(), 'Wiederholung läuft')
        retry_jobs.finish_run(child, 'success', summary='Synthetisch erfolgreich')
        store.execute('UPDATE projects SET last_success=? WHERE id=?', (store.now(), PID))
        await self.refresh()
        self.assertEqual(await self.page.locator('[data-project-search]').input_value(), 'Retry Projekt')
        self.assertEqual(await self.page.locator('[data-filter=error] b').inner_text(), '0')
        self.assertEqual(await self.page.locator('[data-project]').get_attribute('data-error'), '0')
        self.assertTrue(await self.page.locator('[data-retry-status]').is_hidden())
        self.assertIn('Erfolgreich', await self.page.locator('.project-head .badge').text_content())
        self.assertIsNotNone(await self.page.locator('.project-facts time').get_attribute('datetime'))

    async def test_zoom_dark_mobile_layout_and_pause_poll(self):
        await self.page.set_viewport_size({'width': 390, 'height': 844})
        await self.page.locator('[data-ui-size]').select_option('150')
        await self.page.locator('[data-theme-toggle]').click()
        self.assertEqual(await self.page.locator('html').get_attribute('data-theme'), 'dark')
        bounds = await self.page.locator('[data-project]').bounding_box()
        label = await self.page.locator('[data-retry-status]').bounding_box()
        self.assertGreaterEqual(label['x'], bounds['x'])
        self.assertLessEqual(label['x'] + label['width'], bounds['x'] + bounds['width'])
        self.assertLessEqual(await self.page.evaluate('document.documentElement.scrollWidth'), 390)
        store.execute('UPDATE projects SET enabled=0 WHERE id=?', (PID,)); retry_jobs.cancel_project(PID)
        await self.refresh()
        self.assertTrue(await self.page.locator('[data-retry-status]').is_hidden())
        self.assertEqual(await self.page.locator('[data-project]').get_attribute('data-enabled'), '0')
        self.assertIn('Pausiert', await self.page.locator('.automation-badge').inner_text())


if __name__ == '__main__':
    unittest.main(verbosity=2)
