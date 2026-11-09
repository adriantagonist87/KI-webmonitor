"""Test the project opt-in and the observed VDE controls in isolated Brave."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

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
    async def exercise(self, mobile, *, already_open=False, selected=False):
        cdp = os.environ['BROWSER_CDP_URL']
        if cdp != 'http://172.29.148.20:9223':
            raise RuntimeError('Nur das isolierte VDE-Testnetz ist zugelassen.')
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
