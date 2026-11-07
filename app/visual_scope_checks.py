"""Synthetic scoped screenshots and archive comparison transitions."""
import asyncio
from io import BytesIO
import os
from pathlib import Path
import tempfile
import unittest
from PIL import Image

from visual_scope import capture_scoped


def png(color, size=(16, 10)):
    out = BytesIO()
    Image.new('RGB', size, color).save(out, format='PNG')
    return out.getvalue()


class Node:
    def __init__(self, color='blue', width=16, height=10, visible=True, parent=None, raw=None, error=False):
        self.width, self.height, self.visible, self.parent = width, height, visible, parent
        self.raw, self.error, self.calls = raw or png(color), error, []
    async def screenshot(self, **options):
        self.calls.append(options)
        self.page.scroll = {'x': 0, 'y': 999}
        if self.error:
            raise RuntimeError('Synthetic screenshot failure')
        return self.raw


class Locator:
    def __init__(self, page):
        self.page = page
    async def count(self):
        return len(self.page.nodes)
    async def evaluate_all(self, script):
        nodes = self.page.nodes
        visible = {index for index, node in enumerate(nodes) if node.visible and node.width > 0 and node.height > 0}
        boxes = []
        for index in sorted(visible):
            parent = nodes[index].parent
            duplicate = False
            while parent is not None:
                if parent in visible:
                    duplicate = True
                    break
                parent = nodes[parent].parent
            if not duplicate:
                boxes.append({'index': index, 'width': nodes[index].width, 'height': nodes[index].height})
        return boxes
    def nth(self, index):
        return self.page.nodes[index]


class Page:
    def __init__(self, nodes=(), header='red'):
        self.nodes = list(nodes)
        for node in self.nodes:
            node.page = self
        self.header, self.scroll, self.viewport_calls = header, {'x': 11, 'y': 87}, []
    def locator(self, selector):
        return Locator(self)
    async def evaluate(self, script, arg=None):
        if arg is None:
            return dict(self.scroll)
        self.scroll = dict(arg)
    async def screenshot(self, **options):
        self.viewport_calls.append(options)
        return png(self.header)


class CaptureScopeTests(unittest.TestCase):
    def capture(self, page, selector='#content', masks=None):
        return asyncio.run(capture_scoped(page, selector, [] if masks is None else masks))

    def test_01_changed_outside_header_preserves_content_image_and_scroll(self):
        first, second = Page([Node()], 'red'), Page([Node()], 'green')
        masks = [object()]
        a, tag = self.capture(first, masks=masks)
        b, other_tag = self.capture(second)
        self.assertEqual(a, b)
        self.assertEqual((tag, other_tag), ('content-strips-v1', 'content-strips-v1'))
        self.assertEqual(first.viewport_calls, [])
        self.assertEqual(first.nodes[0].calls, [{'mask': masks, 'animations': 'disabled', 'scale': 'css'}])
        self.assertEqual(first.scroll, {'x': 11, 'y': 87})

    def test_02_inside_change_changes_scoped_image(self):
        a, _ = self.capture(Page([Node('blue')]))
        b, _ = self.capture(Page([Node('black')]))
        self.assertNotEqual(a, b)

    def test_03_multiple_nodes_exclude_intervening_ad_and_selected_descendant(self):
        page = Page([Node('blue'), Node('red', parent=0), Node('green')], header='red')
        raw, tag = self.capture(page)
        with Image.open(BytesIO(raw)) as result:
            self.assertEqual(result.size, (16, 28))
            self.assertEqual(result.getpixel((0, 0)), (0, 0, 255))
            self.assertEqual(result.getpixel((0, 12)), (255, 255, 255))
            self.assertEqual(result.getpixel((0, 20)), (0, 128, 0))
        self.assertEqual(tag, 'content-strips-v1')
        self.assertEqual(page.nodes[1].calls, [])

    def test_04_empty_hidden_and_none_use_viewport_fallback(self):
        for page, selector in ((Page(), '#answer'), (Page([Node(visible=False)]), '#answer'), (Page([Node()]), None)):
            with self.subTest(selector=selector, nodes=len(page.nodes)):
                raw, tag = self.capture(page, selector)
                self.assertEqual((raw, tag), (png('red'), 'viewport-v1'))
                self.assertEqual(page.viewport_calls, [{'mask': [], 'animations': 'disabled'}])
                self.assertEqual(page.scroll, {'x': 11, 'y': 87})

    def test_05_node_and_geometry_and_actual_pixel_budgets_fall_back(self):
        cases = [[Node() for _ in range(101)], [Node(width=2401)], [Node(height=16001)],
                 [Node(height=8000), Node(height=8000)], [Node(raw=png('blue', (2401, 1)))]]
        for nodes in cases:
            page = Page(nodes)
            with self.subTest(nodes=len(nodes), width=nodes[0].width, height=nodes[0].height):
                raw, tag = self.capture(page)
                self.assertEqual((raw, tag), (png('red'), 'viewport-v1'))
                self.assertEqual(page.scroll, {'x': 11, 'y': 87})

    def test_06_other_errors_propagate_and_restore_scroll(self):
        page = Page([Node(error=True)])
        with self.assertRaisesRegex(RuntimeError, 'Synthetic screenshot failure'):
            self.capture(page)
        self.assertEqual(page.scroll, {'x': 11, 'y': 87})
        self.assertEqual(page.viewport_calls, [])


class ArchiveVisualScopeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='.visual-scope-check-', dir='/data')
        os.environ['DATA_DIR'] = self.directory.name
        import store
        import engine
        self.store, self.engine = store, engine
        store.DATA = engine.DATA = Path(self.directory.name)
        store.CONFIG, store.SECRETS = store.DATA / 'settings.json', store.DATA / 'credentials.json'
        store.init()
        self.project = {'id': 'visual-scope-project'}
        self.url = 'https://example.org/norm/'
        store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created) VALUES(?,?,?,?,?,?,?,?,?)',
                      (self.project['id'], 'Visual scope', self.url, 'Synthetic visual check', 'example.org',
                       '0 6 * * *', 0, store.next_due('0 6 * * *'), store.now()))
    def tearDown(self):
        self.directory.cleanup()
    def archive(self, rid, color, visual_tag=None):
        store, engine = self.store, self.engine
        store.execute('INSERT INTO runs(id,project_id,started,finished,status,origin) VALUES(?,?,?,?,?,?)',
                      (rid, self.project['id'], store.now(), store.now(), 'success', 'test'))
        image = Path(rid + '.png')
        html = Path(rid + '.html.txt')
        attr = '' if visual_tag is None else ' data-kiwm-visual-scope="' + visual_tag + '"'
        source = '<div id="content" data-kiwm-scope="css:#content"' + attr + '><h1>Norm</h1><p>Same content.</p></div>'
        (store.DATA / image).write_bytes(png(color))
        (store.DATA / html).write_text(source)
        engine.archive(self.project, rid, self.url, 'page', source.encode(), 'Norm\nSame content.',
                       screenshot=str(image), html=str(html), scope='css:#content')

    def test_07_legacy_viewport_to_content_strips_does_not_claim_visual_change(self):
        self.archive('viewport', 'blue')
        self.archive('strips', 'red', 'content-strips-v1')
        self.assertEqual(self.store.rows('SELECT * FROM changes WHERE run_id=?', ('strips',)), [])

    def test_08_same_content_strips_tag_detects_real_visual_change(self):
        self.archive('strips-one', 'blue', 'content-strips-v1')
        self.archive('strips-two', 'red', 'content-strips-v1')
        change = self.store.one('SELECT * FROM changes WHERE run_id=?', ('strips-two',))
        self.assertIsNotNone(change)
        self.assertIn('Visuelle Abweichung', change['kind'])


class RealBraveVisualScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_09_real_locator_strips_ignore_header_and_ad_but_detect_inside_change(self):
        from playwright.async_api import async_playwright
        endpoint = os.environ.get('BROWSER_CDP_URL', 'http://172.29.133.20:9223')

        def synthetic_html(header, ad):
            return '''<!doctype html><html><head>
                <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
                <style>
                html,body{margin:0;padding:0;background:white;scroll-behavior:auto}
                .banner{height:80px;background:''' + header + '''}
                .content{position:relative;width:420px;height:120px;margin:24px;background:#123456}
                .maskable{position:absolute;left:12px;top:12px;width:30px;height:18px;background:#cccccc}
                .ad{height:900px;background:''' + ad + '''}
                .tail{height:1000px}
                </style></head><body>
                <header class="banner"></header>
                <section class="content"><span class="maskable"></span></section>
                <aside class="ad"></aside>
                <section class="content"><span class="maskable"></span></section>
                <div class="tail"></div></body></html>'''

        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(endpoint)
            # A fresh incognito context: never inspect cookies or default pages.
            context = await browser.new_context(viewport={'width': 640, 'height': 360}, device_scale_factor=1)
            try:
                page = await context.new_page()
                masks = [page.locator('.maskable')]
                strips, viewports = [], []
                for header, ad in (('#ff0000', '#ffaa00'), ('#0000ff', '#aa00ff')):
                    # Only inline synthetic content; no external URL is opened.
                    await page.set_content(synthetic_html(header, ad), wait_until='load')
                    await page.evaluate('window.scrollTo(0,0)')
                    viewport, viewport_tag = await capture_scoped(page, None, masks)
                    self.assertEqual(viewport_tag, 'viewport-v1')
                    viewports.append(viewport)
                    await page.evaluate('window.scrollTo(0,document.documentElement.scrollHeight)')
                    before = await page.evaluate('window.scrollY')
                    self.assertGreater(before, 0)
                    shot, tag = await capture_scoped(page, '.content', masks)
                    self.assertEqual(tag, 'content-strips-v1')
                    self.assertEqual(await page.evaluate('window.scrollY'), before)
                    strips.append(shot)
                self.assertNotEqual(viewports[0], viewports[1])
                self.assertEqual(strips[0], strips[1])
                with Image.open(BytesIO(strips[0])) as image:
                    self.assertEqual(image.size, (420, 248))
                    self.assertEqual(image.getpixel((0, 124)), (255, 255, 255))
                    self.assertEqual(image.getpixel((15, 15)), (255, 0, 255))
                await page.locator('.content').first.evaluate("e => e.style.backgroundColor = '#000000'")
                before = await page.evaluate('window.scrollY')
                changed, tag = await capture_scoped(page, '.content', masks)
                self.assertEqual(tag, 'content-strips-v1')
                self.assertNotEqual(changed, strips[1])
                self.assertEqual(await page.evaluate('window.scrollY'), before)
            finally:
                # Close only this newly created context. Stopping Playwright
                # disconnects CDP without closing the shared default context.
                await context.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
