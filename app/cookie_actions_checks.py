"""Real isolated Brave cookie-choice regressions; no live websites/accounts."""
import os
import unittest
from playwright.async_api import async_playwright
from browser_actions import dismiss_cookie_banner


@unittest.skipUnless(os.environ.get('BROWSER_CDP_URL'), 'Select an isolated QA browser with BROWSER_CDP_URL')
class CookieActions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.connect_over_cdp(os.environ['BROWSER_CDP_URL'])
        self.context = await self.browser.new_context(viewport={'width': 1200, 'height': 900},
                                                     service_workers='block')
        self.body = ''
        self.requests = []

        async def serve(route):
            self.requests.append((route.request.method, route.request.url))
            if route.request.method == 'GET' and route.request.url == 'https://cookie.test/':
                await route.fulfill(status=200, content_type='text/html; charset=utf-8',
                                    body='<!doctype html><html><head><meta charset="utf-8"></head><body>'
                                         + self.body + '</body></html>')
            else:
                await route.abort()

        await self.context.route('**/*', serve)
        self.page = await self.context.new_page()

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.pw.stop()

    async def open(self, body):
        self.body = body
        await self.page.goto('https://cookie.test/', wait_until='domcontentloaded')
        if 'id="qa-choice"' in body:
            self.assertTrue(await self.page.locator('#qa-choice').is_visible(),
                            'The synthetic QA dialog must be visible; cosmetic blocking must not turn safety tests into no-ops')
            self.assertTrue(await self.page.locator('#qa-choice h2').is_visible())

    def banner(self, controls, *, heading='Cookie-Einstellungen', extra=''):
        # Common cookie-banner IDs/classes can be hidden by Brave's cosmetic lists.
        # A neutral native dialog exercises the same confirmed-context branch
        # without disabling Shields or altering the production resolver.
        return '<dialog open id="qa-choice"><h2>' + heading + '</h2>' + extra + controls + '</dialog>'

    async def test_01_save_existing_selection_precedes_accept_all_without_form_or_type(self):
        await self.open(self.banner('''<div class="selection-actions">
          <button id="save" onclick="document.body.dataset.choice='save';this.closest('dialog').close()">Auswahl speichern</button></div>
          <div><button id="all" onclick="document.body.dataset.choice='all';this.closest('dialog').close()"><span>Alle auswählen und akzeptieren</span></button></div>''',
          extra='<label><input type="checkbox" checked disabled>Notwendig</label><label><input id="statistics" type="checkbox">Statistik</label>'))
        self.assertEqual(await self.page.locator('#save').evaluate('e=>({type:e.type,form:!!e.form})'),
                         {'type': 'submit', 'form': False})
        result = await dismiss_cookie_banner(self.page)
        self.assertTrue(result['dismissed'])
        self.assertEqual(result['label'], 'Auswahl speichern')
        self.assertEqual(await self.page.locator('body').get_attribute('data-choice'), 'save')
        self.assertFalse(await self.page.locator('#statistics').is_checked())
        self.assertTrue(all(method == 'GET' for method, url in self.requests))

    async def test_02_accept_all_fallback_and_absent_banner_are_explicit(self):
        await self.open(self.banner('''<button onclick="this.closest('dialog').close()">Alle Cookies akzeptieren</button>'''))
        result = await dismiss_cookie_banner(self.page)
        self.assertTrue(result['dismissed'])
        self.assertEqual(result['label'], 'Alle Cookies akzeptieren')
        self.assertFalse((await dismiss_cookie_banner(self.page))['dismissed'])
        await self.open('<main><h1>Öffentlicher Fachinhalt</h1></main>')
        self.assertFalse((await dismiss_cookie_banner(self.page))['dismissed'])

    async def test_03_generic_submit_or_unconfirmed_cookie_word_never_gets_clicked(self):
        for body in [
            '<form action="/account" method="post"><button>Auswahl speichern</button></form>',
            '<h2>Cookie-Einstellungen</h2><button onclick="document.body.dataset.clicked=1">Alle akzeptieren</button>',
            '<div class="cookie"><h2>Benutzerprofil</h2><button onclick="document.body.dataset.clicked=1">Auswahl speichern</button></div>',
            '<div role="dialog"><h2>Bestellung bestätigen</h2><button>Alle akzeptieren</button></div>',
        ]:
            with self.subTest(body=body):
                await self.open(body)
                self.assertFalse((await dismiss_cookie_banner(self.page))['dismissed'])
                self.assertIsNone(await self.page.locator('body').get_attribute('data-clicked'))
        self.assertTrue(all(method == 'GET' for method, url in self.requests))

    async def test_04_cookie_context_does_not_authorize_forms_external_forms_links_or_nested_controls(self):
        fixtures = [
            self.banner('<form action="/account" method="post"><button>Auswahl speichern</button></form>'),
            '<form id="external" action="/account" method="post"></form>' + self.banner('<button form="external">Auswahl speichern</button>'),
            self.banner('<button><a href="/account">Auswahl speichern</a></button>'),
            self.banner('<button><span role="button">Auswahl speichern</span></button>'),
            self.banner('<button href="/account">Auswahl speichern</button>'),
            self.banner('<button formaction="/account">Auswahl speichern</button>'),
            self.banner('<button role="link">Auswahl speichern</button>'),
        ]
        for body in fixtures:
            with self.subTest(body=body):
                await self.open(body)
                with self.assertRaises(ValueError):
                    await dismiss_cookie_banner(self.page)
        self.assertTrue(all(method == 'GET' for method, url in self.requests))

    async def test_05_duplicate_visible_choices_block_but_hidden_copy_is_ignored(self):
        await self.open(self.banner('<button>Auswahl speichern</button><button>Auswahl speichern</button>'))
        with self.assertRaisesRegex(ValueError, 'eindeutig'):
            await dismiss_cookie_banner(self.page)
        await self.open(self.banner('''<button onclick="this.closest('dialog').close()">Auswahl speichern</button>
          <button hidden onclick="document.body.dataset.hiddenClicked=1">Auswahl speichern</button>'''))
        self.assertTrue((await dismiss_cookie_banner(self.page))['dismissed'])
        self.assertIsNone(await self.page.locator('body').get_attribute('data-hidden-clicked'))

    async def test_06_cookie_heading_does_not_authorize_password_fields_or_contracts(self):
        for extra in ['<input type="password" value="synthetic-test">',
                      '<p>Hiermit akzeptiere ich ein kostenpflichtiges Abonnement.</p>']:
            await self.open(self.banner('<button>Auswahl speichern</button>', extra=extra))
            with self.assertRaises(ValueError):
                await dismiss_cookie_banner(self.page)

    async def test_07_choice_with_no_effect_is_never_repeated_or_replaced(self):
        await self.open(self.banner('''<button onclick="document.body.dataset.clicks=String(Number(document.body.dataset.clicks||0)+1)">Auswahl speichern</button>
          <button onclick="document.body.dataset.allClicked=1;this.closest('dialog').close()">Alle akzeptieren</button>'''))
        with self.assertRaisesRegex(ValueError, 'keine Wiederholung'):
            await dismiss_cookie_banner(self.page)
        self.assertEqual(await self.page.locator('body').get_attribute('data-clicks'), '1')
        self.assertIsNone(await self.page.locator('body').get_attribute('data-all-clicked'))

    async def test_08_existing_readonly_network_guard_remains_installed(self):
        await self.open(self.banner('''<button onclick="fetch('/forbidden',{method:'POST',body:'synthetic'}).catch(()=>{});this.closest('dialog').close()">Auswahl speichern</button>'''))
        self.assertTrue((await dismiss_cookie_banner(self.page))['dismissed'])
        await self.page.wait_for_timeout(100)
        self.assertEqual([request for request in self.requests if request[0] == 'POST'],
                         [('POST', 'https://cookie.test/forbidden')])
        with self.assertRaises(Exception):
            await self.page.evaluate("async()=>{await fetch('/still-forbidden',{method:'POST',body:'synthetic'})}")
        self.assertEqual(self.requests[-1], ('POST', 'https://cookie.test/still-forbidden'))

    async def test_09_disabled_preferred_choice_does_not_fall_through_to_other_consent(self):
        await self.open(self.banner('''<button disabled>Auswahl speichern</button>
          <button onclick="document.body.dataset.allClicked=1;this.closest('dialog').close()">Alle akzeptieren</button>'''))
        with self.assertRaises(ValueError):
            await dismiss_cookie_banner(self.page)
        self.assertIsNone(await self.page.locator('body').get_attribute('data-all-clicked'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
