"""Local privacy checks with fake credentials and read-only frame/locator doubles."""
import asyncio
import copy
import json
import os
import unittest
from bs4 import BeautifulSoup
import site_privacy as privacy

NUMBER='987654321'
POSTCODE='54321'
SECRET={'abonummer':NUMBER,'postcode':POSTCODE}


def visible(element):
    visibility=None
    for node in (element,*element.parents):
        if not getattr(node,'attrs',None):
            continue
        declarations={part.split(':',1)[0].strip().lower():part.split(':',1)[1].strip().lower()
                      for part in node.get('style','').split(';') if ':' in part}
        if visibility is None and 'visibility' in declarations:visibility=declarations['visibility']
        if (node.has_attr('hidden') and 'display' not in declarations or node.get('type')=='hidden'
                or declarations.get('display')=='none' or declarations.get('opacity') in ('0','0.0')
                or declarations.get('content-visibility')=='hidden'):
            return False
    return visibility not in ('hidden','collapse')


class Locator:
    def __init__(self,nodes,selector=''):
        self.nodes=nodes
        self.selector=selector
    def __repr__(self):
        return 'Locator('+self.selector+')'
    async def count(self):
        return len(self.nodes)
    async def all(self):
        return [self.nth(i) for i in range(len(self.nodes))]
    async def is_visible(self):
        return bool(self.nodes and visible(self.nodes[0]))
    def nth(self,index):
        return Locator([self.nodes[index]],self.selector)
    def locator(self,selector):
        nodes=[]
        # Each selector arm has its own visibility condition, like Playwright.
        for arm in selector.split(','):
            css=arm.strip().replace(':visible','')
            for root in self.nodes:
                for node in root.select(css):
                    if ':visible' not in arm or visible(node):
                        if node not in nodes:nodes.append(node)
        return Locator(nodes,selector)


class Frame(Locator):
    def __init__(self,html):
        self.original=html
        self.evaluation_calls=0
        super().__init__([BeautifulSoup(html,'html.parser')])
    async def evaluate(self,script,args):
        self.evaluation_calls+=1
        if ('cloneNode' in script or 'getComputedStyle' not in script
                or any(value in script for value in args['subscribers'])):
            raise AssertionError('The privacy scan must read visible nodes and pass secrets separately.')
        # Mirror the browser operation on a separately parsed fixture only.
        # Real CSS/rendering and callback behavior are exercised below in Brave.
        copy=BeautifulSoup(self.original,'html.parser')
        body=copy.body
        if body is None:return False
        selected=copy.select(args['private_selectors'])
        if body in selected:return False
        for node in list(body.select(args['private_selectors'])):
            if node.parent is not None:node.decompose()
        for node in list(body.find_all(True)):
            if node.parent is not None and (not visible(node) or node.name in ('script','style','noscript','template')):
                node.decompose()
        return any(value in body.get_text('',strip=False) for value in args['subscribers'])


class Page:
    def __init__(self,*documents):
        self.frames=[Frame(html) for html in documents]


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self.empty_token=privacy.bind(None)
    def tearDown(self):
        privacy.reset(self.empty_token)
    def active(self,secret=None):
        token=privacy.bind(SECRET if secret is None else secret)
        self.addCleanup(privacy.reset,token)
    def test_01_inactive_is_unchanged(self):
        text=NUMBER+' PLZ: '+POSTCODE+' 54321 Watt'
        html='<header>'+text+'</header><form><input value="'+NUMBER+'"></form>'
        self.assertEqual(privacy.redact_text(text),text)
        self.assertEqual(privacy.sanitize_html(html),html)
        value={'text':text}
        self.assertEqual(privacy.redact_payload(value),value)
    def test_02_unique_number_removed_but_free_postcode_preserved(self):
        self.active()
        source=NUMBER+' A'+NUMBER+'Z / '+POSTCODE+' Watt / Menge '+POSTCODE
        clean=privacy.redact_text(source)
        self.assertNotIn(NUMBER,clean)
        self.assertIn(POSTCODE+' Watt',clean)
        self.assertIn('Menge '+POSTCODE,clean)
    def test_03_only_labelled_postcodes_are_redacted(self):
        self.active()
        for labelled in ('PLZ: '+POSTCODE,'postleitzahl='+POSTCODE,
                         '"PLZ": "'+POSTCODE+'"','postcode = '+POSTCODE,
                         'login[password]='+POSTCODE,'login%5Bpassword%5D='+POSTCODE):
            self.assertNotIn(POSTCODE,privacy.redact_text(labelled),labelled)
        for public in (POSTCODE+' Watt','PLZ '+POSTCODE,'PLZ: '+POSTCODE+'0',
                       'Fachliche Ziffer '+POSTCODE):
            self.assertEqual(privacy.redact_text(public),public)
    def test_04_private_containers_removed_but_article_header_kept(self):
        self.active()
        html='''<html><body><header>SITE-HEADER</header>
        <div class="page-header">PAGE-HEADER</div><nav>NAV-PRIVATE</nav>
        <footer>FOOTER-PRIVATE</footer><form>FORM-PRIVATE<input></form>
        <div class="account">ACCOUNT-PRIVATE</div><div class="user-account">USER-PRIVATE</div>
        <div data-account="x">DATA-PRIVATE</div><div class="nav-account">NAV-ACCOUNT</div>
        <div class="login-form">LOGIN-PRIVATE</div>
        <input autocomplete="postal-code" value="54321">
        <article><header><h2>Fachliche Überschrift</h2></header>
        <nav class="tabs">Fragen mit Antwort</nav><div role="navigation">Fachliche Unterseiten</div>
        <p>Leistung 54321 Watt bleibt Fachinhalt.</p></article></body></html>'''
        clean=privacy.sanitize_html(html)
        for marker in ('SITE-HEADER','PAGE-HEADER','NAV-PRIVATE','FOOTER-PRIVATE',
                       'FORM-PRIVATE','ACCOUNT-PRIVATE','USER-PRIVATE','DATA-PRIVATE',
                       'NAV-ACCOUNT','LOGIN-PRIVATE'):
            self.assertNotIn(marker,clean)
        soup=BeautifulSoup(clean,'html.parser')
        self.assertEqual(soup.select_one('article > header h2').get_text(),'Fachliche Überschrift')
        self.assertEqual(soup.select_one('article > nav.tabs').get_text(),'Fragen mit Antwort')
        self.assertEqual(soup.select_one('article > [role="navigation"]').get_text(),'Fachliche Unterseiten')
        self.assertIn('54321 Watt',clean)
        self.assertIsNone(soup.find('input'))
    def test_05_all_attributes_and_text_use_the_scrubber(self):
        self.active()
        html='<article><p title="'+NUMBER+'" data-owner="'+NUMBER+'" data-plz="'+POSTCODE+'" data-public="'+POSTCODE+'">Kunde '+NUMBER+'</p><a href="/x?plz='+POSTCODE+'&abo='+NUMBER+'">Quelle</a></article>'
        clean=privacy.sanitize_html(html)
        self.assertNotIn(NUMBER,clean)
        soup=BeautifulSoup(clean,'html.parser')
        self.assertNotIn(POSTCODE,soup.p['data-plz'])
        self.assertEqual(soup.p['data-public'],POSTCODE)
        self.assertNotIn(POSTCODE,soup.a['href'])
    def test_06_rich_labels_do_not_change_other_numeric_content(self):
        self.active()
        html='<article><p>PLZ: <span><b>54</b></span><em>321</em></p><p>54321 Watt</p><p>Postleitzahl = <span>54321</span>; fachliche Zahl 54321 Watt</p></article>'
        clean=privacy.sanitize_html(html)
        soup=BeautifulSoup(clean,'html.parser')
        paragraphs=soup.find_all('p')
        self.assertNotIn(POSTCODE,paragraphs[0].get_text())
        self.assertEqual(paragraphs[1].get_text(),'54321 Watt')
        self.assertIn('fachliche Zahl 54321 Watt',paragraphs[2].get_text())
        self.assertNotIn('Postleitzahl = 54321',paragraphs[2].get_text())
    def test_07_recursive_json_is_copied_and_scrubbed(self):
        self.active()
        value={NUMBER:[NUMBER,{'postcode':POSTCODE,'plz':int(POSTCODE),'text':'54321 Watt',
                              'url':'/x?PLZ='+POSTCODE+'&abo='+NUMBER}],
               'nested':({'login[password]':POSTCODE,'amount':54321,'number':int(NUMBER)},),
               int(NUMBER):NUMBER,'decimal':987654321.5}
        before=copy.deepcopy(value)
        clean=privacy.redact_payload(value)
        raw=json.dumps(clean)
        self.assertNotIn(NUMBER,raw)
        self.assertIn('54321 Watt',raw)
        self.assertEqual(clean['nested'][0]['amount'],54321)
        self.assertNotEqual(clean['nested'][0]['login[password]'],POSTCODE)
        self.assertEqual(value,before)
    def test_08_context_reset_and_nested_projects(self):
        source=NUMBER+' / 123456789'
        first=privacy.bind(SECRET)
        try:
            self.assertNotIn(NUMBER,privacy.redact_text(source))
            second=privacy.bind({'abonummer':'123456789','postcode':'11111'})
            try:
                clean=privacy.redact_text(source)
                self.assertIn(NUMBER,clean);self.assertNotIn('123456789',clean)
            finally:privacy.reset(second)
            self.assertNotIn(NUMBER,privacy.redact_text(source))
        finally:privacy.reset(first)
        self.assertEqual(privacy.redact_text(source),source)
    def test_09_async_tasks_have_isolated_contexts(self):
        async def one(number):
            token=privacy.bind({'abonummer':number,'postcode':POSTCODE})
            try:
                await asyncio.sleep(0)
                return privacy.redact_text(NUMBER+' / 123456789')
            finally:privacy.reset(token)
        async def both():return await asyncio.gather(one(NUMBER),one('123456789'))
        a,b=asyncio.run(both())
        self.assertNotIn(NUMBER,a);self.assertIn('123456789',a)
        self.assertIn(NUMBER,b);self.assertNotIn('123456789',b)
        self.assertEqual(privacy.redact_text(NUMBER),NUMBER)
    def test_10_auth_fields_in_any_frame_and_hidden_modals(self):
        for field in ('<input type="password">',
                      '<input type="text" autocomplete="one-time-code">',
                      '<input type="text" autocomplete="section-login current-password">'):
            page=Page('<article>Content</article>',field)
            self.assertTrue(asyncio.run(privacy.contains_auth_form(page)))
        hidden=Page('<form hidden><input type="password"></form>')
        self.assertFalse(asyncio.run(privacy.contains_auth_form(hidden)))
    def test_11_plain_text_subscription_form_is_detected(self):
        pair='<form><input name="login[userIdentifier]" type="text"><input name="login[password]" type="text"></form>'
        self.assertTrue(asyncio.run(privacy.contains_auth_form(Page('<p>Content</p>',pair))))
        self.assertFalse(asyncio.run(privacy.contains_auth_form(Page('<div hidden>'+pair+'</div>'))))
        half='<form><input name="login[userIdentifier]" type="hidden"><input name="login[password]" type="text"></form>'
        self.assertTrue(asyncio.run(privacy.contains_auth_form(Page(half))))
        unrelated='<form><input name="search" type="text"></form>'
        self.assertFalse(asyncio.run(privacy.contains_auth_form(Page(unrelated))))
    def test_12_screenshot_masks_all_frames_without_dynamic_secret_selectors(self):
        page=Page('<header>x</header><input>', '<form><textarea></textarea></form>')
        before=[frame.original for frame in page.frames]
        inactive=asyncio.run(privacy.screenshot_masks(page))
        self.assertEqual(len(inactive),2)
        self.active()
        active=asyncio.run(privacy.screenshot_masks(page))
        self.assertEqual(len(active),4)
        for locator in active:
            self.assertNotIn(NUMBER,repr(locator));self.assertNotIn(POSTCODE,repr(locator))
        self.assertIn('.page-header',repr(active))
        self.assertIn('body > header',repr(active))
        self.assertEqual([frame.original for frame in page.frames],before)
    def test_13_secret_is_copied_and_errors_have_no_submitted_value(self):
        value=dict(SECRET);token=privacy.bind(value)
        try:
            value['abonummer']='123456789'
            self.assertNotIn(NUMBER,privacy.redact_text(NUMBER))
            self.assertNotIn(NUMBER,repr(token))
        finally:privacy.reset(token)
        for malformed in ({'abonummer':'PRIVATE-INVALID','postcode':POSTCODE},
                          {'abonummer':987654321,'postcode':POSTCODE}):
            with self.assertRaises(ValueError) as error:privacy.bind(malformed)
            self.assertNotIn('PRIVATE-INVALID',str(error.exception))
            self.assertNotIn(NUMBER,str(error.exception))
    def test_14_script_comment_and_sensitive_autocomplete_elements_removed(self):
        self.active()
        html='<article><!-- '+NUMBER+' --><script>window.secret="'+NUMBER+'"</script><span autocomplete="postal-code">'+POSTCODE+'</span><p>54321 Watt</p></article>'
        clean=privacy.sanitize_html(html)
        self.assertNotIn(NUMBER,clean);self.assertNotIn('<script',clean)
        self.assertNotIn('<span',clean);self.assertIn('54321 Watt',clean)
    def test_15_static_masks_protect_header_account_and_form_subscriber(self):
        self.active()
        main='<html><body><header>'+NUMBER+'</header><div class="account">'+NUMBER+'</div><form><span>'+NUMBER+'</span></form><article>54321 Watt</article></body></html>'
        child='<html><body><div data-account="yes">'+NUMBER+'</div><p>Public content</p></body></html>'
        page=Page(main,child)
        before=[frame.original for frame in page.frames]
        self.assertFalse(asyncio.run(privacy.contains_exposed_subscriber(page)))
        self.assertEqual([frame.evaluation_calls for frame in page.frames],[1,1])
        self.assertEqual([frame.original for frame in page.frames],before)
    def test_16_exposed_subscriber_in_body_or_child_frame_blocks_image(self):
        self.active()
        clean='<html><body><article>54321 Watt</article></body></html>'
        exposed='<html><body><article><p>Customer '+NUMBER+'</p></article></body></html>'
        self.assertTrue(asyncio.run(privacy.contains_exposed_subscriber(Page(exposed))))
        self.assertTrue(asyncio.run(privacy.contains_exposed_subscriber(Page(clean,exposed))))
    def test_17_unbound_or_numberless_context_never_evaluates_page(self):
        page=Page('<html><body>'+NUMBER+'</body></html>')
        self.assertFalse(asyncio.run(privacy.contains_exposed_subscriber(page)))
        self.active({})
        self.assertFalse(asyncio.run(privacy.contains_exposed_subscriber(page)))
        self.assertEqual(page.frames[0].evaluation_calls,0)
    def test_18_fully_masked_body_does_not_expose_subscriber(self):
        self.active()
        page=Page('<html><body class="account">'+NUMBER+'</body></html>')
        self.assertFalse(asyncio.run(privacy.contains_exposed_subscriber(page)))

    def test_19_generic_credentials_are_redacted_without_affecting_legacy_rules(self):
        secret={'provider':'generic_form','username':'reader@example.test','password':'Synthetic-Only-Secret!'}
        config={'provider':'generic_form','form_selector':'#login','username_selector':'#user','password_selector':'#pass'}
        token=privacy.bind(secret,config)
        try:
            text='reader@example.test reader%40example.test Synthetic-Only-Secret!'
            clean=privacy.redact_text(text)
            for value in (secret['username'],secret['password'],'reader%40example.test'):self.assertNotIn(value,clean)
            body='<html><body><article>'+secret['password']+'</article></body></html>'
            self.assertTrue(asyncio.run(privacy.contains_exposed_subscriber(Page(body))))
            private='<html><body><form id="login"><input type="text" id="user"><input type="text" id="pass"></form></body></html>'
            self.assertTrue(asyncio.run(privacy.contains_auth_form(Page(private))))
            self.assertFalse(asyncio.run(privacy.contains_auth_form(Page(private.replace('id="login"','id="login" hidden')))))
            self.assertNotIn('<form',privacy.sanitize_html(private))
        finally:privacy.reset(token)

    def test_20_generic_disabled_empty_secret_still_protects_configured_text_form(self):
        token=privacy.bind({}, {'provider':'generic_form','form_selector':'#one,#two','username_selector':'#user','password_selector':'#pass'})
        try:
            page=Page('<html><body><form id="one" hidden></form><form id="two"><input type="text"></form></body></html>')
            self.assertTrue(asyncio.run(privacy.contains_auth_form(page)))
            hidden=Page('<html><body><form id="one" hidden></form><form id="two" hidden></form></body></html>')
            self.assertFalse(asyncio.run(privacy.contains_auth_form(hidden)))
        finally:privacy.reset(token)


class RealBraveExposureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        self.token=privacy.bind({'provider':'generic_form','username':'reader@example.test',
                                 'password':'Synthetic-Only-Secret!'})
        self.pw=await async_playwright().start()
        self.browser=await self.pw.chromium.connect_over_cdp(
            os.environ.get('BROWSER_CDP_URL','http://browser:9223'))
        self.context=await self.browser.new_context(viewport={'width':1100,'height':750},service_workers='block')
        self.page=await self.context.new_page()

    async def asyncTearDown(self):
        await self.context.close();await self.browser.close();await self.pw.stop()
        privacy.reset(self.token)

    async def load(self,body):
        await self.page.set_content('<!doctype html><html><head><title>Synthetic privacy test</title></head><body>'+body+'</body></html>')

    async def test_21_hidden_billing_dd_and_templates_do_not_block(self):
        await self.load('''<main><h1>Normen</h1><p>Öffentlicher Fachinhalt</p>
            <dl style="visibility:hidden"><dt>Benutzer</dt><dd>reader@example.test</dd></dl>
            <div style="display:none"><p>Synthetic-Only-Secret!</p></div>
            <div hidden>reader@example.test</div><div aria-hidden="true" style="display:none">Synthetic-Only-Secret!</div>
            <div style="opacity:0">reader@example.test</div>
            <div style="content-visibility:hidden">Synthetic-Only-Secret!</div>
            <details><summary>Weitere Informationen</summary><p>reader@example.test</p></details>
            <template>reader@example.test</template><script type="application/json">"Synthetic-Only-Secret!"</script></main>''')
        self.assertFalse(await privacy.contains_auth_form(self.page))
        self.assertFalse(await privacy.contains_exposed_subscriber(self.page))

    async def test_22_visible_username_and_password_still_block(self):
        for value in ('reader@example.test','Synthetic-Only-Secret!'):
            with self.subTest(value_type='username' if '@' in value else 'password'):
                await self.load('<main><h1>Normen</h1><p>'+value+'</p></main>')
                self.assertTrue(await privacy.contains_exposed_subscriber(self.page))

    async def test_23_static_private_masks_and_standalone_inputs_are_excluded(self):
        await self.load('''<header>reader@example.test</header>
            <div class="account">Synthetic-Only-Secret!</div><form><span>reader@example.test</span></form>
            <input value="Synthetic-Only-Secret!"><textarea>reader@example.test</textarea>
            <div contenteditable="true">Synthetic-Only-Secret!</div><main>Fachlicher Inhalt</main>''')
        self.assertFalse(await privacy.contains_exposed_subscriber(self.page))
        masks=await privacy.screenshot_masks(self.page)
        for value in ('reader@example.test','Synthetic-Only-Secret!'):
            self.assertNotIn(value,repr(masks))

    async def test_24_visible_split_inline_credentials_and_legacy_number_block(self):
        await self.load('<main><p><span>reader</span><b>@example</b><em>.test</em></p></main>')
        self.assertTrue(await privacy.contains_exposed_subscriber(self.page))
        await self.load('<main><p><span>Synthetic-</span><b>Only-</b><em>Secret!</em></p></main>')
        self.assertTrue(await privacy.contains_exposed_subscriber(self.page))
        token=privacy.bind(SECRET)
        try:
            await self.load('<main><p><span>987</span><b>654</b><em>321</em></p></main>')
            self.assertTrue(await privacy.contains_exposed_subscriber(self.page))
        finally:privacy.reset(token)

    async def test_25_no_dom_mutation_or_custom_element_construction(self):
        await self.load('''<main><h1>Normen</h1><x-norm>Fachlicher Eintrag</x-norm>
            <dl hidden><dd>reader@example.test</dd></dl></main>''')
        await self.page.evaluate('''() => {
            window.constructed=0;
            customElements.define('x-norm',class extends HTMLElement{constructor(){super();window.constructed++}});
            window.mutations=0;
            window.observer=new MutationObserver(records=>window.mutations+=records.length);
            window.observer.observe(document.documentElement,{attributes:true,childList:true,subtree:true,characterData:true});
        }''')
        before=await self.page.content();count=await self.page.evaluate('window.constructed')
        self.assertFalse(await privacy.contains_exposed_subscriber(self.page))
        self.assertEqual(await self.page.content(),before)
        self.assertEqual(await self.page.evaluate('window.constructed'),count)
        self.assertEqual(await self.page.evaluate('window.mutations'),0)

    async def test_26_frame_embedding_visibility_and_private_masks_are_honored(self):
        from html import escape
        child='<html><body><article>reader@example.test</article></body></html>'
        await self.load('<main>Normen</main><iframe hidden srcdoc="'+escape(child,quote=True)+'"></iframe>')
        await self.page.wait_for_timeout(100)
        self.assertFalse(await privacy.contains_exposed_subscriber(self.page))
        await self.load('<header><iframe srcdoc="'+escape(child,quote=True)+'"></iframe></header><main>Normen</main>')
        await self.page.wait_for_timeout(100)
        self.assertFalse(await privacy.contains_exposed_subscriber(self.page))
        await self.load('<main>Normen</main><iframe srcdoc="'+escape(child,quote=True)+'"></iframe>')
        await self.page.wait_for_timeout(100)
        self.assertTrue(await privacy.contains_exposed_subscriber(self.page))
        await self.load('<div style="visibility:hidden"><iframe style="visibility:visible" srcdoc="'+escape(child,quote=True)+'"></iframe></div>')
        await self.page.wait_for_timeout(100)
        self.assertTrue(await privacy.contains_exposed_subscriber(self.page))

    async def test_27_accessibility_and_overridden_hiding_do_not_approve_visible_secrets(self):
        cases=[
            ('aria-only','<main aria-hidden="true">reader@example.test</main>',True),
            ('hidden-css-override','<main hidden style="display:block">reader@example.test</main>',True),
            ('visible-child-override','<main style="visibility:hidden"><span style="visibility:visible">reader@example.test</span></main>',True),
            ('invisible-parent-text','<main style="visibility:hidden">reader@example.test<span style="visibility:visible">Öffentlicher Text</span></main>',False),
            ('document-visibility-override','<style>html{visibility:hidden}body{visibility:visible}</style><main>reader@example.test</main>',True),
        ]
        for name,body,expected in cases:
            with self.subTest(case=name):
                await self.load(body)
                self.assertEqual(await privacy.contains_exposed_subscriber(self.page),expected)


if __name__=='__main__':unittest.main(verbosity=2)
