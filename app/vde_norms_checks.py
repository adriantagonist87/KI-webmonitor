"""Test the project opt-in and the observed VDE controls in isolated Brave."""
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

import browser_mode
import vde_norms
from visual_browser import Desktop, mobile_window


class VdeFlowConfiguration(unittest.TestCase):
    def test_flow_is_opt_in_and_restricted_to_visual_vde_projects(self):
        project = {'url': vde_norms.URL, 'domains': 'www.normenbibliothek.de'}
        value = {'mode': 'visual_mobile', 'entry_url': '', 'flow': 'vde_norms'}
        self.assertEqual(browser_mode.validate(value, project), value)
        self.assertEqual(browser_mode.validate({'mode': 'visual_mobile', 'entry_url': ''}, project),
                         {'mode': 'visual_mobile', 'entry_url': ''})
        with self.assertRaises(ValueError):
            browser_mode.validate(value, {**project, 'url': 'https://example.org'})
        with self.assertRaises(ValueError):
            browser_mode.validate({**value, 'mode': 'standard'}, project)

    def test_retains_one_library_tab_only_for_an_opted_in_project(self):
        older = SimpleNamespace(url=vde_norms.URL)
        current = SimpleNamespace(url=vde_norms.URL+'?nonce=synthetic#top')
        entry = SimpleNamespace(url='https://www.normenbibliothek.de/')
        foreign = SimpleNamespace(url='https://example.org/xopp/normenbibliothek/start.xav')
        pages = [older, current, entry, foreign]
        with patch('vde_norms.store.one', return_value={'enabled': 1}) as lookup:
            self.assertIs(vde_norms.retained_tab(pages), current)
            self.assertEqual(lookup.call_args.args[1], (vde_norms.URL,))
        with patch('vde_norms.store.one', return_value=None):
            self.assertIsNone(vde_norms.retained_tab(pages))
        with patch('vde_norms.store.one') as lookup:
            self.assertIsNone(vde_norms.retained_tab([entry, foreign]))
            lookup.assert_not_called()


PAGE = '''<!doctype html><html><head><meta charset="utf-8"><style>
body{font:20px sans-serif;margin:20px}button{padding:12px}.icon__menu{display:none}
@media(max-width:600px){#aside__main{display:none}.icon__menu{display:block}}
</style></head><body>
<header class="header__main"><button class="icon__menu" onclick="document.getElementById('dialog_mainmenu').showModal()"><span hidden>Menü</span>☰</button></header>
<aside id="aside__main"><button role="tab" aria-label="Normen" onclick="choose()">Normen</button></aside>
<dialog id="dialog_mainmenu"><header class="dialogHeader"><button class="close" onclick="this.closest('dialog').close()">×</button></header>
<button role="tab" aria-label="Normen" onclick="choose()">Normen</button>
<button role="tab" aria-label="IEC Normen" onclick="window.iec=true">IEC Normen</button></dialog>
<main id="normenuebersicht" hidden><header><span class="mainSwitchContainerChild__header__meta__title">Normen - 162 Dokumente</span></header></main>
<script>window.clicks=0;function choose(){window.clicks++;document.getElementById('normenuebersicht').hidden=false;}</script>
</body></html>'''


@unittest.skipUnless(os.environ.get('KIWM_VDE_QA_BROWSER') == '1', 'Isolierter Brave-Browser erforderlich')
class VdeFlowBrowser(unittest.IsolatedAsyncioTestCase):
    def qa_cdp(self):
        cdp = os.environ['BROWSER_CDP_URL']
        if cdp != 'http://172.29.148.20:9223':
            raise RuntimeError('Nur das isolierte VDE-Testnetz ist zugelassen.')
        return cdp

    async def exercise(self, mobile, *, already_open=False, selected=False):
        cdp = self.qa_cdp()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(cdp)
            context = browser.contexts[0]
            page = await mobile_window(browser, context) if mobile else await context.new_page()
            try:
                await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body=PAGE))
                await page.goto(vde_norms.URL)
                if already_open:
                    await page.locator('dialog').evaluate('dialog => dialog.showModal()')
                if selected:
                    await page.evaluate("document.getElementById('normenuebersicht').hidden=false")
                async with Desktop(page, cdp, vde_norms.matches) as desktop:
                    await vde_norms.select_norms(page, desktop, mobile=mobile)
                self.assertTrue(await page.locator('#normenuebersicht').is_visible())
                self.assertFalse(await page.locator('dialog').is_visible())
                self.assertEqual(await page.evaluate('window.clicks'), 0 if selected else 1)
                self.assertFalse(await page.evaluate('Boolean(window.iec)'))
            finally:
                await page.close()
                await browser.close()

    async def test_desktop_norms(self):
        await self.exercise(False)

    async def test_mobile_opens_menu_and_closes_it_after_selection(self):
        await self.exercise(True)

    async def test_mobile_menu_already_open(self):
        await self.exercise(True, already_open=True)

    async def test_mobile_norms_already_selected(self):
        await self.exercise(True, selected=True)

    async def test_standard_run_preserves_the_opted_in_vde_tab_and_session(self):
        import engine
        from relevance_test_model import review_only_model
        import store

        cdp = self.qa_cdp()
        standard_url = 'https://monitor-fixture.example.test/archive'
        standard_text = 'Ein eigenständiger Prüftext für das Standardprojekt.'
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(cdp)
            context = browser.contexts[0]
            vde_page = await context.new_page()
            stale_page = await context.new_page()
            await vde_page.route(vde_norms.URL, lambda route: route.fulfill(content_type='text/html', body=PAGE))
            await vde_page.goto(vde_norms.URL)
            await vde_page.evaluate("sessionStorage.setItem('qa-session', 'retained')")

            class FixtureContext:
                def __getattr__(self, name):
                    return getattr(context, name)

                async def new_page(self):
                    page = await context.new_page()
                    await page.route(standard_url, lambda route: route.fulfill(
                        content_type='text/html; charset=utf-8',
                        body=f'<main><h1>Projektarchiv</h1><p>{standard_text}</p></main>'))
                    return page

            # Share the isolated CDP connection so interception stays attached.
            proxy = SimpleNamespace(contexts=[FixtureContext()], close=AsyncMock())

            class ConnectedPlaywright:
                async def __aenter__(self):
                    return SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=proxy)))

                async def __aexit__(self, *_):
                    pass

            async def agent(task, initial, *_args, **_kwargs):
                self.assertEqual(initial['url'], standard_url)
                self.assertIn(standard_text, initial['text'])
                return {'complete': True, 'pending': [], 'checked': ['Projektarchiv'], 'actions': []}

            try:
                with tempfile.TemporaryDirectory(dir='/data') as tmp:
                    folder = Path(tmp)
                    with (patch.multiple(store, DATA=folder, CONFIG=folder/'settings.json', SECRETS=folder/'credentials.json'),
                          patch.multiple(engine, DATA=folder, BROWSER_CDP_URL=cdp, async_playwright=ConnectedPlaywright,
                                         run_agent=agent, create_summary=AsyncMock(return_value='QA erfolgreich'),
                                         analyze_visual_changes=AsyncMock(return_value=[]), llm=AsyncMock(side_effect=review_only_model))):
                        store.init()
                        store.save_json(store.CONFIG, {key: 'synthetic-no-model' for key in ('agent_model', 'analysis_model', 'vision_model')})
                        for pid, url, host in (('vde', vde_norms.URL, 'www.normenbibliothek.de'),
                                               ('standard', standard_url, 'monitor-fixture.example.test')):
                            store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created) VALUES(?,?,?,?,?,?,?,?,?)',
                                          (pid, pid, url, 'Projektarchiv lesen.', host, '0 6 * * *', 0, store.now(), store.now()))
                        browser_mode.save(store.one("SELECT * FROM projects WHERE id='vde'"),
                                          {'mode': 'visual', 'entry_url': '', 'flow': 'vde_norms'})
                        store.execute('INSERT INTO runs(id,project_id,started,status,origin) VALUES(?,?,?,?,?)',
                                      ('standard-run', 'standard', store.now(), 'running', 'test'))
                        await engine.monitor(store.one("SELECT * FROM projects WHERE id='standard'"), 'standard-run')
                        run = store.one("SELECT status,error FROM runs WHERE id='standard-run'")
                        self.assertEqual(run['status'], 'success', run['error'])
                        self.assertEqual(store.rows("SELECT DISTINCT url FROM snapshots WHERE run_id='standard-run'"),
                                         [{'url': standard_url}])
                    self.assertTrue(stale_page.is_closed())
                    self.assertFalse(vde_page.is_closed())
                    self.assertEqual(await vde_page.evaluate("sessionStorage.getItem('qa-session')"), 'retained')
            finally:
                await vde_page.close()
                await browser.close()
