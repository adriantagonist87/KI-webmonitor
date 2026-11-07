"""Real-Brave synthetic checks. Fresh incognito context, no persistent profile."""
import os
import unittest
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from visible_content import visible_html


class VisibleContentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.connect_over_cdp(
            os.environ.get('BROWSER_CDP_URL', 'http://browser:9223'))
        self.context = await self.browser.new_context(viewport={'width': 1100, 'height': 750},
                                                      service_workers='block')
        self.page = await self.context.new_page()

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.pw.stop()

    async def load(self, body, style=''):
        await self.page.set_content('<!doctype html><html lang="de"><head><title>Normen – Testportal</title>'
                                    '<style>' + style + '</style></head><body>' + body + '</body></html>')

    async def test_01_hidden_account_modules_and_nested_descendants_are_removed(self):
        await self.load('''<main><h1>Normen</h1><p>Fachlicher Inhalt 2026</p></main>
            <div class="billing"><section><p>PRIVATE_RECHNUNG_8472</p></section></div>
            <div style="visibility:hidden"><p style="visibility:visible">PRIVATE_KONTO_9753</p></div>
            <div style="visibility:collapse"><p>PRIVATE_BESTELLUNG_6421</p></div>
            <div hidden><p>PRIVATE_CHAT_5318</p></div>
            <div aria-hidden="true"><p>PRIVATE_ANGEBOT_1492</p></div>''', '.billing {display:none}')
        html = await visible_html(self.page)
        self.assertNotIn('PRIVATE_', html)
        self.assertIn('Fachlicher Inhalt 2026', html)

    async def test_02_offscreen_outer_and_nested_scroller_records_remain(self):
        await self.load('''<main><article>Norm oben</article>
            <div style="height:1600px"></div><article id="outer">Norm weiter unten</article>
            <div class="scroller"><article>Innere Norm oben</article>
            <div style="height:1800px"></div><article id="inner">Norm im inneren Scrollbereich</article></div></main>''',
                        '.scroller {height:100px;overflow:auto}')
        self.assertGreater(await self.page.locator('#outer').evaluate('e=>e.getBoundingClientRect().top'), 750)
        html = await visible_html(self.page)
        self.assertIn('Norm weiter unten', html)
        self.assertIn('Norm im inneren Scrollbereich', html)
        self.assertIn('Innere Norm oben', html)

    async def test_03_title_and_visible_navigation_are_retained_exactly(self):
        await self.load('''<nav><a href="/normen">Normen</a><a href="/iec">IEC Normen</a>
            <button type="button" aria-label="Filter einblenden">Filter einblenden</button></nav>
            <main><h1>Neuerscheinungen</h1></main>''')
        soup = BeautifulSoup(await visible_html(self.page), 'html.parser')
        self.assertEqual(soup.title.get_text(), 'Normen – Testportal')
        self.assertEqual([a.get_text() for a in soup.select('nav a')], ['Normen', 'IEC Normen'])
        self.assertEqual(soup.select_one('nav button')['aria-label'], 'Filter einblenden')
        self.assertEqual(soup.html['lang'], 'de')

    async def test_04_script_styles_templates_and_control_values_are_absent(self):
        await self.load('''<main><h1>Normen</h1><label for="login">E-Mail</label>
            <input id="login" value="PRIVATE_INPUT_DEFAULT"><textarea>PRIVATE_TEXTAREA</textarea>
            <select><option value="PRIVATE_OPTION_ID">PRIVATE_SELECT_LABEL</option></select>
            <script type="application/json">{"account":"PRIVATE_SCRIPT"}</script>
            <template><p>PRIVATE_TEMPLATE</p></template><noscript>PRIVATE_NOSCRIPT</noscript></main>''',
                        'body::before {content:"PRIVATE_STYLE"}')
        await self.page.locator('#login').fill('PRIVATE_INPUT_LIVE')
        html = await visible_html(self.page)
        self.assertNotIn('PRIVATE_', html)
        soup = BeautifulSoup(html, 'html.parser')
        self.assertFalse(soup.select('script,style,noscript,template,input,textarea,select'))
        self.assertEqual(soup.label.get_text(), 'E-Mail')

    async def test_05_closed_details_body_hidden_open_details_preserved(self):
        await self.load('''<main><h1>Normen</h1>
            <details><summary>Kontoinformationen</summary><p>PRIVATE_CLOSED_BILLING</p>
            <details open><summary>Verschachtelt</summary><p>PRIVATE_NESTED_OPEN</p></details></details>
            <details open><summary>Normdetails</summary><p>Öffentliche Normerläuterung</p></details></main>''')
        html = await visible_html(self.page)
        self.assertNotIn('PRIVATE_', html)
        self.assertIn('Kontoinformationen', html)
        self.assertIn('Öffentliche Normerläuterung', html)

    async def test_06_no_live_dom_mutation_or_custom_element_cloning(self):
        await self.load('''<main><h1>Normen</h1><x-norm id="custom">Normaler Eintrag</x-norm>
            <p hidden>PRIVATE_HIDDEN</p><input value="PRIVATE_INPUT"></main>''')
        await self.page.evaluate('''() => {
            window.constructed=0;
            customElements.define('x-norm', class extends HTMLElement {
                constructor(){super();window.constructed++}
            });
            window.mutations=0;
            window.observer=new MutationObserver(records=>window.mutations+=records.length);
            window.observer.observe(document.documentElement,{attributes:true,childList:true,subtree:true,characterData:true});
        }''')
        before = await self.page.content()
        constructed = await self.page.evaluate('window.constructed')
        html = await visible_html(self.page)
        self.assertEqual(await self.page.content(), before)
        self.assertEqual(await self.page.evaluate('window.constructed'), constructed)
        self.assertEqual(await self.page.evaluate('window.mutations'), 0)
        self.assertIn('Normaler Eintrag', html)

    async def test_07_fully_hidden_content_states_are_removed_without_box_tests(self):
        await self.load('''<main><h1>Normen</h1><div style="display:contents"><p>Norm mit contents</p></div>
            <div style="opacity:0"><p>PRIVATE_TRANSPARENT</p></div>
            <div style="content-visibility:hidden"><p>PRIVATE_CONTENT_HIDDEN</p></div>
            <div style="content-visibility:auto;margin-top:3000px"><p>Norm mit Auto-Sichtbarkeit</p></div></main>''')
        html = await visible_html(self.page)
        self.assertNotIn('PRIVATE_', html)
        self.assertIn('Norm mit contents', html)
        self.assertIn('Norm mit Auto-Sichtbarkeit', html)

    async def test_08_serialization_escapes_source_text_and_never_loads_resources(self):
        await self.load('<main><h1>Normen &amp; Hinweise</h1><p>&lt;script&gt;Nur Text&lt;/script&gt;</p></main>')
        requests = []
        async def observe(route):
            requests.append(route.request.url)
            await route.abort()
        await self.context.route('**/*', observe)
        # Add an inert image template and a hidden frame; neither may be cloned
        # into a new document or resource loader by the reader.
        await self.page.evaluate('''() => {
            const t=document.createElement('template');t.innerHTML='<img src="https://never.test/from-template.png">';document.body.append(t);
            document.querySelector('p').setAttribute('onclick','window.unwanted=true');
        }''')
        html = await visible_html(self.page)
        self.assertIn('&lt;script&gt;Nur Text&lt;/script&gt;', html)
        self.assertNotIn('onclick=', html)
        self.assertNotIn('never.test', html)
        self.assertEqual(requests, [])


if __name__ == '__main__': unittest.main(verbosity=2)
