"""Isolated native desktop checks. Synthetic origin and credentials only."""
import os
import unittest
from urllib.parse import parse_qs
from playwright.async_api import async_playwright
from visual_browser import Desktop, VisualLogin, agent_options
from native_input import endpoint
from site_login import LoginError
from site_access import GENERIC_DEFAULTS
import browser_mode
import site_privacy

ORIGIN='https://native-fixture.example.org'
CONFIG={**GENERIC_DEFAULTS,'provider':'generic_form','origin':ORIGIN,'enabled':True,
        'login_url':ORIGIN+'/login','submit_url':ORIGIN+'/login',
        'form_selector':'form','username_selector':'#username','password_selector':'#password',
        'submit_selector':'button[type=submit]','success_selector':'[data-authenticated=true]'}
SECRET={'provider':'generic_form','origin':ORIGIN,'username':'synthetic@example.invalid','password':'Only-QA!9'}
FORM='''<style>body{font:20px sans-serif;margin:80px}input,button{display:block;margin:20px;padding:12px}</style>
<form action="/login" method="post"><label>Benutzername<input id="username" name="username"></label><label>Passwort<input id="password" name="password" type="password"></label><button type="submit">Anmelden</button></form>
<script>sessionStorage.setItem('same-tab','preserved')</script>'''
TARGET='''<style>body{font:22px sans-serif;margin:40px}button{padding:20px;margin:20px}</style><header data-authenticated="true">Angemeldet</header><nav><button id="norms" onclick="document.querySelector('main').textContent='Normen ausgewählt'">Normen</button><button id="iec">IEC Normen</button><button id="buy">Kaufen</button></nav><main>Auswahl</main><div style="height:2000px"></div>'''


class VisualConfiguration(unittest.TestCase):
    def test_entry_origin_is_explicit(self):
        p={'domains':'example.org'}
        self.assertEqual(browser_mode.validate(dict(browser_mode.DEFAULT),p)['mode'],'standard')
        for url in ('https://evil.test','http://example.org','https://user:password@example.org','https://example.org/?token=a'):
            with self.assertRaises(ValueError):browser_mode.validate({'mode':'visual','entry_url':url},p)

    def test_transport_and_tools_are_bounded(self):
        self.assertEqual(endpoint('http://qa-browser:9223'),'ws://qa-browser:6080/websockify')
        with self.assertRaises(ValueError):endpoint('file:///tmp/socket')
        names={t['function']['name'] for t in agent_options()['tools']}
        self.assertIn('visual_click',names);self.assertNotIn('shell',names);self.assertNotIn('type_password',names)


@unittest.skipUnless(os.environ.get('KIWM_NATIVE_TEST')=='1','Explicit isolated native desktop required')
class NativeBrowser(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.url=os.environ['BROWSER_CDP_URL']
        if 'browser:9223' in self.url or '172.29.133.' in self.url:raise RuntimeError('Production browser forbidden')
        self.pw=await async_playwright().start()
        self.browser=await self.pw.chromium.connect_over_cdp(self.url)
        self.context=self.browser.contexts[0]
        old_pages=list(self.context.pages)
        self.page=await self.context.new_page()
        for old in old_pages:await old.close()
        self.posts=[];self.logs=[];self.requests=[];self.signed_in=False
        async def fixture(route):
            request=route.request
            self.requests.append(request.url)
            if request.method=='POST':
                self.posts.append(parse_qs(request.post_data or ''))
                self.signed_in=True
                await route.fulfill(status=303,headers={'Location':ORIGIN+'/target'})
            else:await route.fulfill(content_type='text/html; charset=utf-8',body=FORM if request.url.endswith('/login') or request.url.endswith('/start') and not self.signed_in else TARGET)
        self.fixture=fixture
        await self.context.route(ORIGIN+'/**',fixture)
        await self.page.goto(ORIGIN+'/target')
        await self.page.bring_to_front()
        self.desktop=Desktop(self.page,self.url,lambda u:u.startswith(ORIGIN+'/'))
        await self.desktop.__aenter__()

    async def asyncTearDown(self):
        await self.desktop.__aexit__(None,None,None)
        await self.context.new_page()  # Keep the disposable browser process alive.
        await self.page.close()
        await self.context.unroute(ORIGIN+'/**',self.fixture)
        await self.browser.close();await self.pw.stop()

    async def test_native_coordinate_click_and_scroll(self):
        x,y=await self.desktop.point(self.page.locator('#norms'))
        await self.desktop.click(x,y,'Normen')
        self.assertEqual(await self.page.locator('main').inner_text(),'Normen ausgewählt')
        result=await self.desktop.scroll('down')
        self.assertTrue(result['progress']);self.assertGreater(result['after'],0)

    async def test_wrong_label_and_purchase_are_rejected(self):
        x,y=await self.desktop.point(self.page.locator('#iec'))
        with self.assertRaises(ValueError):await self.desktop.click(x,y,'Normen')
        x,y=await self.desktop.point(self.page.locator('#buy'))
        with self.assertRaises(ValueError):await self.desktop.click(x,y,'Kaufen')
        self.assertEqual(await self.page.locator('main').inner_text(),'Auswahl')

    async def test_observed_collection_uses_native_nested_scroll(self):
        from visual_browser import collection_structure
        from collection_scan import scan_collection
        def card(number,date):
            return f'<article class="publication"><div><h3><a href="{ORIGIN}/entry/{number}">Titel {number}</a></h3></div><aside><dl><dt>Stand:</dt><dd>{date}</dd></dl></aside></article>'
        old=card(3,'2025-12')
        import json
        await self.page.set_content('<style>body{margin:30px}main{height:250px;overflow-y:auto}article{height:230px}</style><main hidden>'+old+'</main><main>'+card(1,'2026-11')+card(2,'2026-10')+'</main><script>let box=document.querySelector("main:not([hidden])");box.addEventListener("wheel",e=>window.nativeWheel=e.isTrusted);box.addEventListener("scroll",()=>{if(!window.added&&box.scrollTop>10){window.added=true;box.insertAdjacentHTML("beforeend",'+json.dumps(old)+')}})</script>')
        structure=await collection_structure(self.page,'Stand')
        self.assertEqual(structure['item_selector'],'article')
        self.assertEqual(structure['container_selector'],'main')
        scan=await scan_collection(self.page,**structure,year=2026,scroll_fn=lambda p,d,s:self.desktop.scroll(d,s))
        self.assertTrue(scan['complete']);self.assertEqual(scan['reason'],'year_boundary')
        self.assertEqual(scan['observed_count'],2);self.assertTrue(await self.page.evaluate('window.nativeWheel'))

    async def test_collection_without_evidence_rejects_instead_of_guessing(self):
        from visual_browser import collection_structure
        await self.page.set_content('<article><h2><a href="https://example.org">Titel</a></h2><p>2026-11</p></article>')
        with self.assertRaises(ValueError):await collection_structure(self.page,'Ausgabedatum')
        await self.page.set_content('<article><h2><a href="https://example.org">Titel</a></h2><dl><dt>Stand:</dt><dd>2026-11</dd><dt>Stand:</dt><dd>2026-10</dd></dl></article>')
        with self.assertRaises(ValueError):await collection_structure(self.page,'Stand')

    async def test_same_tab_native_login_and_session_reuse(self):
        await self.desktop.goto(ORIGIN+'/login')
        token=site_privacy.bind(SECRET,CONFIG)
        try:
            from engine import safe_screenshot
            with self.assertRaises(RuntimeError):await safe_screenshot(self.page)
            broker=VisualLogin(self.context,ORIGIN+'/target',SECRET,CONFIG,self.desktop,self.logs.append)
            tabs=len(self.context.pages)
            self.assertEqual(await broker.ensure(),'signed_in')
            self.assertEqual(len(self.context.pages),tabs)
            self.assertFalse(self.page.is_closed())
            self.assertEqual(await self.page.evaluate("sessionStorage.getItem('same-tab')"),'preserved')
            self.assertEqual(len(self.posts),1)
            self.assertEqual(self.posts[0],{'username':[SECRET['username']],'password':[SECRET['password']]})
            self.assertEqual(await broker.ensure(force=True),'reused')
            self.assertEqual(len(self.posts),1)
            self.assertTrue(await safe_screenshot(self.page))
            for value in SECRET.values():self.assertNotIn(value,' '.join(self.logs))
        finally:site_privacy.reset(token)

    async def test_forged_login_target_stops_before_typing(self):
        await self.page.goto(ORIGIN+'/login')
        await self.page.locator('form').evaluate("e=>e.action='https://evil.invalid/login'")
        broker=VisualLogin(self.context,ORIGIN+'/target',SECRET,CONFIG,self.desktop,self.logs.append)
        with self.assertRaises(LoginError):await broker.ensure()
        self.assertEqual(await self.page.locator('#username').input_value(),'')
        self.assertEqual(self.posts,[])

    async def test_one_lost_focus_click_is_retried_without_a_second_submission(self):
        await self.desktop.goto(ORIGIN+'/login')
        await self.page.locator('#username').evaluate('''e => e.addEventListener('pointerdown', event => {
            event.preventDefault();
            sessionStorage.setItem('first-click-blocked', String(event.isTrusted));
        }, {once: true})''')
        broker=VisualLogin(self.context,ORIGIN+'/target',SECRET,CONFIG,self.desktop,self.logs.append)
        self.assertEqual(await broker.ensure(),'signed_in')
        self.assertEqual(await self.page.evaluate("sessionStorage.getItem('first-click-blocked')"),'true')
        self.assertEqual(len(self.posts),1)
        self.assertEqual(self.posts[0],{'username':[SECRET['username']],'password':[SECRET['password']]})

    async def test_covered_field_never_receives_credentials_or_a_click_on_the_overlay(self):
        await self.desktop.goto(ORIGIN+'/login')
        await self.page.evaluate('''() => {
            const overlay = document.createElement('div');
            overlay.style = 'position:fixed;inset:0;z-index:10;background:#fff3';
            overlay.addEventListener('click', () => { window.overlayClicked = true; });
            document.body.append(overlay);
        }''')
        broker=VisualLogin(self.context,ORIGIN+'/target',SECRET,CONFIG,self.desktop,self.logs.append)
        with self.assertRaisesRegex(LoginError,'konnte nicht fokussiert werden'):
            await broker.ensure()
        self.assertEqual(await self.page.locator('#username').input_value(),'')
        self.assertFalse(await self.page.evaluate('Boolean(window.overlayClicked)'))
        self.assertEqual(self.posts,[])

    async def test_password_typing_stops_if_focus_moves_outside_the_field(self):
        await self.desktop.goto(ORIGIN+'/login')
        await self.page.evaluate('''() => {
            const distractor = document.createElement('input');
            distractor.id = 'distractor';
            document.body.append(distractor);
            document.querySelector('#password').addEventListener('input', () => distractor.focus(), {once: true});
        }''')
        broker=VisualLogin(self.context,ORIGIN+'/target',SECRET,CONFIG,self.desktop,self.logs.append)
        with self.assertRaisesRegex(LoginError,'Fokus verloren'):
            await broker.ensure()
        self.assertEqual(await self.page.locator('#password').input_value(),SECRET['password'][:1])
        self.assertEqual(await self.page.locator('#distractor').input_value(),'')
        self.assertEqual(self.posts,[])

    async def test_protection_stops_without_login_or_retry(self):
        await self.page.set_content('<title>Security</title><h1>Sicherheitsüberprüfung</h1><p>Ungewöhnlichen Datenverkehr. Bitte klicken Sie hier, wenn Sie kein Roboter sind.</p>')
        from site_safety import ProtectionDetected
        broker=VisualLogin(self.context,ORIGIN+'/target',SECRET,CONFIG,self.desktop,self.logs.append)
        with self.assertRaises(ProtectionDetected):await broker.ensure()
        self.assertEqual(self.posts,[])

    async def test_notice_close_and_contract_rejection(self):
        for contract in (True,False):
            text='Hiermit akzeptiere ich den kostenpflichtigen Vertrag.' if contract else 'Neue Informationen sind verfügbar.'
            await self.page.set_content('<div role="dialog"><p>'+text+'</p><button type="button" onclick="this.parentElement.hidden=true">Verstanden</button></div>')
            x,y=await self.desktop.point(self.page.get_by_role('button',name='Verstanden'))
            if contract:
                with self.assertRaises(ValueError):await self.desktop.click(x,y,'Verstanden')
                self.assertTrue(await self.page.get_by_role('dialog').is_visible())
            else:
                await self.desktop.click(x,y,'Verstanden')
                self.assertFalse(await self.page.get_by_role('dialog').is_visible())

    async def test_monitor_uses_native_mode_without_closing_other_tabs(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch,AsyncMock
        import engine,store
        from relevance_test_model import review_only_model
        await self.page.goto(ORIGIN+'/login')
        sentinel=await self.context.new_page()
        try:
            with tempfile.TemporaryDirectory(dir='/data') as tmp:
                folder=Path(tmp)
                with patch.object(store,'DATA',folder),patch.object(engine,'DATA',folder),patch.object(store,'CONFIG',folder/'settings.json'),patch.object(store,'SECRETS',folder/'credentials.json'):
                    store.init()
                    store.save_json(store.CONFIG,{k:'synthetic-no-model' for k in ('agent_model','analysis_model','vision_model')})
                    store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created) VALUES(?,?,?,?,?,?,?,?,?)',('native','Native QA',ORIGIN+'/target','Select Normen.','native-fixture.example.org','0 6 * * *',0,store.now(),store.now()))
                    project=store.one("SELECT * FROM projects WHERE id='native'")
                    browser_mode.save(project,{'mode':'visual','entry_url':ORIGIN+'/start'})
                    store.execute('INSERT INTO runs(id,project_id,started,status,origin) VALUES(?,?,?,?,?)',('native-run','native',store.now(),'running','test'))
                    async def agent(task,initial,model,llm,dispatch,screen,log,**options):
                        self.assertIn('visual_click',{t['function']['name'] for t in options['tools']})
                        result=await dispatch('visual_click',{'label':'Normen'})
                        self.assertIn('Normen ausgewählt',result['source']['text'])
                        self.assertEqual((await screen())['type'],'image_url')
                        return {'complete':True,'pending':[],'checked':['Normen'],'findings':'QA','actions':[]}
                    # Keep the fixture's routing attached to one CDP connection;
                    # a second client resets interception for this synthetic origin.
                    browser_proxy=SimpleNamespace(contexts=[self.context],close=AsyncMock())
                    class ConnectedPlaywright:
                        async def __aenter__(self):return SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser_proxy)))
                        async def __aexit__(self,*args):pass
                    with patch('engine.async_playwright',ConnectedPlaywright),patch('engine.site_access.public_config',return_value=CONFIG),patch('engine.site_access.get_secret',return_value=SECRET),patch('engine.run_agent',side_effect=agent),patch('engine.create_summary',new=AsyncMock(return_value='QA erfolgreich')),patch('engine.analyze_visual_changes',new=AsyncMock(return_value=[])),patch('engine.llm',new=AsyncMock(side_effect=review_only_model)):
                        await engine.monitor(project,'native-run')
                        store.execute('INSERT INTO runs(id,project_id,started,status,origin) VALUES(?,?,?,?,?)',('native-reused','native',store.now(),'running','test'))
                        await engine.monitor(project,'native-reused')
                    run=store.one("SELECT status,error FROM runs WHERE id='native-run'")
                    self.assertEqual(run['status'],'success',run['error'])
                    self.assertTrue(store.rows("SELECT id FROM snapshots WHERE run_id='native-run'"))
                    self.assertFalse(sentinel.is_closed());self.assertFalse(self.page.is_closed())
                    self.assertEqual(len(self.posts),1)
                    self.assertEqual(self.requests.count(ORIGIN+'/start'),2,'Each run must refresh the source despite a valid session.')
                    self.assertEqual(store.one("SELECT status FROM runs WHERE id='native-reused'")['status'],'success')
                    self.assertEqual(browser_mode.get({**project,'id':'untouched'})['mode'],'standard')
        finally:await sentinel.close()


if __name__=='__main__':unittest.main()
