"""Real-Brave collection checks using only a routed synthetic HTTPS origin.

The new incognito context does not read or alter a persistent browser profile.
Set BROWSER_CDP_URL to the isolated QA browser. No real website is contacted.
"""
from html import escape
import json
import os
import unittest

from playwright.async_api import async_playwright
from collection_scan import scan_collection


ORIGIN = 'https://source.test'
ITEMS = '#content .norm-card'
TITLES = 'h2 a'
CONTAINER = '#content'
DATE_LABEL = 'Ausgabedatum'


def record(ident, date, title=None, date_label=DATE_LABEL):
    return {'id': ident, 'date': date, 'title': title or 'Norm ' + ident,
            'date_label': date_label}


class CollectionScanTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.connect_over_cdp(
            os.environ.get('BROWSER_CDP_URL', 'http://browser:9223'))
        self.context = await self.browser.new_context(
            viewport={'width': 1100, 'height': 800}, service_workers='block')
        self.page = await self.context.new_page()
        self.html = ''
        self.external_requests = []

        async def serve(route):
            if route.request.url == ORIGIN + '/collection':
                return await route.fulfill(status=200, content_type='text/html',
                                           body=self.html,
                                           headers={'Cache-Control': 'no-store'})
            if route.request.url == ORIGIN + '/favicon.ico':
                return await route.fulfill(status=204, body='')
            self.external_requests.append(route.request.url)
            return await route.abort()
        await self.context.route('**/*', serve)

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.pw.stop()

    async def load(self, initial, lazy_batches=None):
        def card(item):
            return ('<article class="norm-card"><h2><a href="/item/' + escape(item['id'], quote=True)
                    + '">' + escape(item['title']) + '</a></h2><p>Normbeschreibung 10</p>'
                    + '<dl><dt>' + escape(item['date_label']) + ':</dt><dd>' + escape(item['date'])
                    + '</dd><dt>Erscheinungsdatum:</dt><dd>2027-02-01</dd>'
                    + '<dt>Zurückgezogen:</dt><dd>2030-12</dd></dl></article>')
        initial_html = ''.join(card(item) for item in initial)
        # JSON contains only this test's synthetic markup, not website text.
        batches = json.dumps([''.join(card(item) for item in batch)
                              for batch in (lazy_batches or [])])
        self.html = '''<!doctype html><html lang="de"><head><title>Normen-Sammlung – Test</title>
            <style>
            html,body{margin:0;height:100%;overflow:hidden;font:16px Arial}
            #layout{display:flex;gap:20px;padding:10px}
            #sidebar{width:260px;height:680px;overflow-y:auto;flex-shrink:0}
            #content{width:650px;height:180px;overflow-y:auto;border:1px solid #444}
            .norm-card{box-sizing:border-box;min-height:150px;padding:8px;border-bottom:1px solid #aaa}
            .norm-card h2{font-size:17px;margin:0 0 8px}.norm-card p{margin:4px 0}
            dl{display:grid;grid-template-columns:180px 1fr;margin:0}dd{margin:0}
            </style></head><body><div id="layout"><aside id="sidebar">
            <h2>Navigation</h2><p>Normen</p><p>IEC Normen</p>
            <article class="norm-card"><h2><a href="/item/sidebar">NICHT IM INHALT</a></h2>
            <dl><dt>Ausgabedatum:</dt><dd>2020-01</dd></dl></article>
            <div style="height:2000px">Lange unveränderte Sidebar</div></aside>
            <main><h1>Normen</h1><section id="content">''' + initial_html + '''</section></main></div>
            <script>
            const content=document.getElementById('content');
            const batches=''' + batches + ''';
            window.appendBatches=0;let pending=false;
            content.addEventListener('scroll',()=>{
              if(pending || !batches.length || content.scrollTop+content.clientHeight<content.scrollHeight-3)return;
              pending=true;content.setAttribute('aria-busy','true');
              setTimeout(()=>{
                content.insertAdjacentHTML('beforeend',batches.shift());
                window.appendBatches++;pending=false;content.removeAttribute('aria-busy');
              },40);
            });
            document.getElementById('sidebar').scrollTop=37;
            </script></body></html>'''
        await self.page.goto(ORIGIN + '/collection', wait_until='domcontentloaded')
        await self.page.wait_for_timeout(60)

    async def scan(self, **options):
        return await scan_collection(self.page, ITEMS, TITLES, DATE_LABEL, CONTAINER,
                                     year=2026, clean_url=lambda value: value, **options)

    async def test_01_lazy_nested_list_stops_at_selected_date_year_boundary(self):
        await self.load(
            [record('a', '2026-12', 'DIN VDE 0100:2018-06'),
             record('b', '2026-11', 'Norm mit Titeljahr 2031')],
            [[record('c', '2026-10'), record('d', '2026-08')],
             [record('old-a', '2025-12', 'Norm mit Titeljahr 2026'), record('old-b', '2025-11')],
             [record('ancient-a', '2001-01'), record('ancient-b', '1990-01')]])
        sidebar_before = await self.page.locator('#sidebar').evaluate('e=>e.scrollTop')
        self.assertGreater(sidebar_before, 0)
        result = await self.scan(max_scrolls=30, timeout=30)
        self.assertTrue(result['complete'])
        self.assertEqual(result['reason'], 'year_boundary')
        self.assertEqual(result['date_label'], DATE_LABEL)
        self.assertEqual(result['boundary_date'], '2025-12')
        self.assertEqual([r['url'] for r in result['records']],
                         [ORIGIN + '/item/' + ident for ident in ('a','b','c','d')])
        self.assertIn('2018-06', result['records'][0]['title'])
        self.assertIn('2031', result['records'][1]['title'])
        self.assertTrue(all(r['date'].startswith('2026-') for r in result['records']))
        self.assertEqual(await self.page.evaluate('window.appendBatches'), 2)
        self.assertEqual(await self.page.locator('#content .norm-card').count(), 6)
        self.assertEqual(await self.page.locator('#sidebar').evaluate('e=>e.scrollTop'), sidebar_before)
        self.assertGreater(await self.page.locator('#content').evaluate('e=>e.scrollTop'), 0)
        self.assertEqual(await self.page.evaluate('window.scrollY'), 0)
        self.assertFalse(self.external_requests)

    async def test_02_real_list_end_without_older_record_is_complete(self):
        await self.load([record('a','2026-10-15'), record('b','2026-08-01'), record('c','2026-01-01')])
        result = await self.scan(max_scrolls=30, timeout=30)
        self.assertTrue(result['complete'])
        self.assertEqual(result['reason'], 'list_end')
        self.assertIsNone(result['boundary_date'])
        self.assertEqual(result['observed_count'], 3)
        self.assertEqual(await self.page.evaluate('window.appendBatches'), 0)

    async def test_03_unsorted_selected_dates_are_rejected(self):
        await self.load([record('a','2026-09'), record('b','2026-11')])
        with self.assertRaisesRegex(ValueError, 'absteigend'):
            await self.scan(max_scrolls=10, timeout=20)

    async def test_04_missing_selected_date_label_is_rejected(self):
        await self.load([record('a','2026-09',date_label='Eingangsdatum'),
                         record('b','2026-08',date_label='Eingangsdatum')])
        with self.assertRaisesRegex(ValueError, 'Datumsfeld'):
            await self.scan(max_scrolls=10, timeout=20)

    async def test_05_scroll_limit_never_claims_full_coverage(self):
        months = [f'2026-{month:02}' for month in range(12, 0, -1)]
        values = [record('m' + str(i), value) for i,value in enumerate(months)]
        batches = [values[i:i+2] for i in range(2, len(values), 2)]
        batches.append([record('old', '2025-12')])
        await self.load(values[:2], batches)
        result = await self.scan(max_scrolls=1, timeout=30)
        self.assertFalse(result['complete'])
        self.assertEqual(result['reason'], 'scroll_limit')
        self.assertIsNone(result['boundary_date'])
        self.assertLess(result['observed_count'], len(values))
        self.assertLessEqual(result['scrolls'], 1)

    async def test_06_time_limit_never_claims_full_coverage(self):
        await self.load([record('a','2026-12'),record('b','2026-11')],
                        [[record('c','2026-10'),record('d','2026-09')]])
        # The real initial position check itself waits for a stable browser
        # state, making a one-millisecond deadline deterministic here.
        result = await self.scan(max_scrolls=30, timeout=.001)
        self.assertFalse(result['complete'])
        self.assertEqual(result['reason'], 'time_limit')
        self.assertIsNone(result['boundary_date'])


if __name__ == '__main__': unittest.main(verbosity=2)
