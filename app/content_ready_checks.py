"""Synthetic state-machine checks and an isolated real-Brave readiness check."""
import asyncio
import json
import os
import unittest
from unittest.mock import patch

from content_ready import ContentNotReady, wait_content_ready


class FakePage:
    def __init__(self, states):
        self.states, self.index, self.clock, self.waits = states, 0, 0.0, []
    async def evaluate(self, script, selector):
        result = self.states[min(self.index, len(self.states) - 1)]
        self.index += 1
        return result
    async def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)
        self.clock += milliseconds / 1000


class ContentReadyTests(unittest.TestCase):
    def check(self, page, selector='#content', **options):
        with patch('content_ready.monotonic', side_effect=lambda: page.clock):
            return asyncio.run(wait_content_ready(page, selector, timeout_ms=1000,
                                                settle_ms=200, poll_ms=100, **options))

    def test_01_loader_then_changing_text_must_settle_before_return(self):
        page = FakePage([{'state': 'loading'}, {'state': 'ready', 'signature': 'first'},
                         {'state': 'ready', 'signature': 'final'}])
        self.assertEqual(self.check(page), {'state': 'ready'})
        self.assertGreaterEqual(page.clock, 0.4)

    def test_02_visible_busy_or_permanent_placeholder_times_out(self):
        page = FakePage([{'state': 'loading'}])
        with self.assertRaisesRegex(ContentNotReady, 'Quelle nicht archiviert'):
            self.check(page)
        self.assertLessEqual(page.clock, 1.001)
        self.assertGreaterEqual(page.clock, 1.0)

    def test_03_hidden_or_missing_selection_is_deferred_for_agent_navigation(self):
        for kind in ('hidden', 'missing'):
            page = FakePage([{'state': kind}])
            with self.subTest(kind=kind):
                self.assertEqual(self.check(page, '#answer'), {'state': 'deferred_' + kind})
                self.assertEqual(page.waits, [])

    def test_04_empty_content_waits_then_accepts_real_text(self):
        page = FakePage([{'state': 'empty'}, {'state': 'ready', 'signature': 'complete'}])
        self.assertEqual(self.check(page), {'state': 'ready'})
        self.assertGreaterEqual(page.clock, 0.3)
        with self.assertRaises(ContentNotReady):
            self.check(FakePage([{'state': 'empty'}]))

    def test_05_unstable_text_cannot_be_archived_as_ready(self):
        states = [{'state': 'ready', 'signature': str(number)} for number in range(30)]
        with self.assertRaises(ContentNotReady):
            self.check(FakePage(states))

    def test_06_result_never_returns_source_text_or_dom_exception_details(self):
        page = FakePage([{'state': 'ready', 'signature': 'opaque', 'text': 'SYNTHETIC_SOURCE_CANARY'}])
        self.assertNotIn('SYNTHETIC_SOURCE_CANARY', json.dumps(self.check(page)))
        async def fail(script, selector):
            raise RuntimeError('SYNTHETIC_SOURCE_CANARY')
        page.evaluate = fail
        with self.assertRaises(ContentNotReady) as raised:
            self.check(page)
        self.assertNotIn('SYNTHETIC_SOURCE_CANARY', str(raised.exception))


class RealBraveContentReadyTests(unittest.IsolatedAsyncioTestCase):
    async def test_07_real_loading_markers_settling_and_hidden_tab(self):
        from playwright.async_api import async_playwright
        endpoint = os.environ.get('BROWSER_CDP_URL', 'http://172.29.133.20:9223')
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(endpoint)
            context = await browser.new_context(viewport={'width': 640, 'height': 360})
            try:
                page = await context.new_page()
                await page.set_content('''<!doctype html><html><head>
                    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'">
                    </head><body><main>
                    <article id="content" aria-busy="true"><h1>Synthetic 3868 wird geladen ...</h1></article>
                    <section id="answer" hidden>Hidden tab content.</section></main>
                    <script>
                    setTimeout(() => { document.querySelector('#content').removeAttribute('aria-busy'); }, 150);
                    setTimeout(() => { document.querySelector('#content').innerHTML = '<h1>First content.</h1>'; }, 260);
                    setTimeout(() => { document.querySelector('#content').innerHTML = '<h1>Final content.</h1><p>Complete synthetic description.</p>'; }, 330);
                    </script></body></html>''', wait_until='load')
                result = await wait_content_ready(page, '#content', timeout_ms=5000, settle_ms=160, poll_ms=40)
                self.assertEqual(result, {'state': 'ready'})
                self.assertIn('Final content.', await page.locator('#content').inner_text())
                self.assertEqual(await wait_content_ready(page, '#answer'), {'state': 'deferred_hidden'})
                self.assertEqual(await wait_content_ready(page, '#not-present'), {'state': 'deferred_missing'})
                await page.locator('#content').evaluate("e => e.innerHTML = '<p>Loading...</p>'")
                with self.assertRaises(ContentNotReady):
                    await wait_content_ready(page, '#content', timeout_ms=240, settle_ms=80, poll_ms=30)
                await page.locator('#content').evaluate("e => e.innerHTML = '<h1>Loading of documents is described here.</h1><p>Ordinary article text.</p>'")
                self.assertEqual(await wait_content_ready(page, '#content', timeout_ms=1000, settle_ms=80, poll_ms=30), {'state': 'ready'})
            finally:
                # Only this fresh incognito context is closed; default pages,
                # profiles, cookies and model endpoints are never accessed.
                await context.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
