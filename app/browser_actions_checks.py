"""Synthetic routed-origin action tests; no real accounts or default profile use."""
import os
import unittest
from playwright.async_api import async_playwright
from browser_actions import normalize_label, safe_navigation_url, dismiss_notice, activate_navigation, scroll_region


class LabelsAndTargets(unittest.TestCase):
    def test_01_exact_normalization_and_validation(self):
        self.assertEqual(normalize_label('  Normen\n '), 'normen')
        self.assertEqual(normalize_label('Schließen'), 'schliessen')
        for value in ('', '   ', 'a\x00b', 42):
            with self.assertRaises(ValueError): normalize_label(value)

    def test_02_only_local_read_navigation_urls(self):
        current='https://actions.test/library'
        for value in ('#normen','/normen?year=2026','https://actions.test/normen'):
            self.assertTrue(safe_navigation_url(current,value))
        for value in ('javascript:alert(1)','https://other.test/normen','//other.test/normen','/logout','/account/settings','/checkout','#payment','https://u:p@actions.test/normen'):
            self.assertFalse(safe_navigation_url(current,value),value)


@unittest.skipUnless(os.environ.get('BROWSER_CDP_URL'), 'BROWSER_CDP_URL must select an isolated QA browser')
class BrowserActions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw=await async_playwright().start()
        self.browser=await self.pw.chromium.connect_over_cdp(os.environ['BROWSER_CDP_URL'])
        self.context=await self.browser.new_context(viewport={'width':1200,'height':800},service_workers='block')
        self.body='';self.requests=[]
        async def serve(route):
            self.requests.append((route.request.method,route.request.url))
            if route.request.url.startswith('https://actions.test/') and route.request.method=='GET':
                await route.fulfill(status=200,content_type='text/html; charset=utf-8',body='<!doctype html><html><head><meta charset="utf-8"></head><body>'+self.body+'</body></html>')
            else: await route.abort()
        await self.context.route('**/*',serve)
        self.page=await self.context.new_page()

    async def asyncTearDown(self):
        await self.context.close();await self.browser.close();await self.pw.stop()

    async def open(self,body):
        self.body=body
        await self.page.goto('https://actions.test/',wait_until='domcontentloaded')

    async def test_03_dismiss_information_notice_and_close_icon(self):
        await self.open('<div role="dialog" id="notice"><h2>Hinweis zum neuen Preismodell</h2><p>Informationen zum Abonnement.</p><button type="button" onclick="this.parentElement.hidden=true">Verstanden</button></div>')
        result=await dismiss_notice(self.page,'Verstanden')
        self.assertTrue(result['dismissed']);self.assertFalse(await self.page.locator('#notice').is_visible())
        await self.open('<div class="uk-modal" id="notice"><button type="button" class="uk-modal-close" aria-label="Close" onclick="this.parentElement.hidden=true">×</button></div>')
        self.assertTrue((await dismiss_notice(self.page,'Close'))['dismissed'])
        await self.open('<div role="dialog"><button type="button" onclick="this.parentElement.hidden=true">Schließen</button></div>')
        self.assertTrue((await dismiss_notice(self.page,'Schließen'))['dismissed'])
        await self.open('<dialog open class="xaverDialog2"><button class="close" type="button" onclick="this.parentElement.close()">✖</button><button type="button" onclick="this.parentElement.close()">Verstanden</button></dialog>')
        self.assertTrue((await dismiss_notice(self.page,'✖'))['dismissed'])

    async def test_04_dismiss_rejects_form_link_contract_and_duplicate(self):
        bodies=[
            '<div role="dialog"><form><button>Verstanden</button></form></div>',
            '<div role="dialog"><a href="/checkout" role="button">Verstanden</a></div>',
            '<div role="dialog"><p>Hiermit akzeptiere ich den kostenpflichtigen Vertrag.</p><button type="button">Verstanden</button></div>',
            '<div role="dialog"><button type="button">Verstanden</button><button type="button">Verstanden</button></div>',
            '<button type="button">Verstanden</button>',
        ]
        for body in bodies:
            await self.open(body)
            with self.assertRaises(ValueError):await dismiss_notice(self.page,'Verstanden')
        self.assertTrue(all(method=='GET' for method,url in self.requests))

    async def test_05_exact_normen_navigation_never_selects_iec(self):
        await self.open('<nav class="navigation__main" role="tablist"><button type="submit" onclick="document.body.dataset.choice=\'normen\'">Normen</button><button type="button" onclick="document.body.dataset.choice=\'iec\'">IEC Normen</button><button type="submit" hidden>Normen</button></nav>')
        await activate_navigation(self.page,'Normen')
        self.assertEqual(await self.page.locator('body').get_attribute('data-choice'),'normen')

    async def test_06_navigation_rejects_form_transaction_non_nav_and_duplicates(self):
        for body,label in [
            ('<nav><form><button>Normen</button></form></nav>','Normen'),
            ('<nav><a href="/checkout">Normen</a></nav>','Normen'),
            ('<button type="button">Normen</button>','Normen'),
            ('<nav><button>Normen</button><button>Normen</button></nav>','Normen'),
            ('<aside><button>Abonnement</button></aside>','Abonnement'),
            ('<nav><a href="https://external.test/normen">Normen</a></nav>','Normen'),
        ]:
            await self.open(body)
            with self.assertRaises(ValueError):await activate_navigation(self.page,label)

    async def test_07_scroll_chooses_content_container_instead_of_sidebar(self):
        await self.open('<style>body{margin:0;display:flex;overflow:hidden}aside{width:200px;height:600px;overflow-y:auto}main{width:900px;height:600px;overflow-y:auto}</style><aside><div style="height:3000px">Menü</div></aside><main id="content"><div style="height:3000px">Normenliste</div></main>')
        result=await scroll_region(self.page,'down')
        self.assertEqual(result['selector'],'#content');self.assertTrue(result['progress']);self.assertGreater(result['after'],300)
        self.assertEqual(await self.page.locator('aside').evaluate('e=>e.scrollTop'),0)
        up=await scroll_region(self.page,'up','#content');self.assertTrue(up['at_start'])

    async def test_08_scroll_selected_subcontainer_boundary_and_lazy_growth(self):
        await self.open('<main id="main"><div id="list" style="height:250px;width:700px;overflow-y:auto"><div style="height:550px">Liste</div></div></main><script>let done=false;document.querySelector("#list").addEventListener("scroll",e=>{if(!done&&e.target.scrollTop>100){done=true;setTimeout(()=>{const d=document.createElement("div");d.style.height="600px";e.target.append(d)},150)}})</script>')
        result=await scroll_region(self.page,'down','#main')
        self.assertEqual(result['selector'],'#list');self.assertGreater(result['scroll_height'],result['initial_height'])
        await self.page.locator('#list').evaluate('e=>e.scrollTop=e.scrollHeight')
        result=await scroll_region(self.page,'down','#list');self.assertTrue(result['at_end']);self.assertFalse(result['progress'])

    async def test_09_scroll_blocks_dialog_and_ambiguous_selection(self):
        await self.open('<main><p>Text</p></main><div role="dialog"><button type="button">Verstanden</button></div>')
        with self.assertRaises(ValueError):await scroll_region(self.page,'down')
        await self.open('<main>eins</main><main>zwei</main>')
        with self.assertRaises(ValueError):await scroll_region(self.page,'down','main')
        with self.assertRaises(ValueError):await scroll_region(self.page,'sideways')

    async def test_10_delayed_native_notice_recovers_exactly_once_during_click(self):
        await self.open('''<style>@keyframes moving{from{transform:translateX(0)}to{transform:translateX(20px)}}#normen{animation:moving .7s linear}</style>
          <nav><button id="normen" type="submit" onclick="window.normen++">Normen</button><button type="button" onclick="window.iec++">IEC Normen</button></nav>
          <dialog id="notice" class="xaverDialog2"><p>Hinweis zum neuen Preismodell und Informationen zum Abonnement.</p>
          <button type="button" onclick="window.unsafe++">Abonnement ändern</button>
          <button type="button" onclick="window.noticeClosed++;this.closest('dialog').close()">Verstanden</button></dialog>
          <script>window.normen=0;window.iec=0;window.unsafe=0;window.noticeClosed=0;setTimeout(()=>document.querySelector('#notice').showModal(),100)</script>''')
        result=await activate_navigation(self.page,'Normen')
        counts=await self.page.evaluate('()=>({normen:window.normen,iec:window.iec,unsafe:window.unsafe,noticeClosed:window.noticeClosed})')
        self.assertEqual(counts,{'normen':1,'iec':0,'unsafe':0,'noticeClosed':1})
        self.assertEqual(result['notices_closed'],1)

    async def test_11_unsafe_notice_remains_blocked_without_raw_browser_error(self):
        await self.open('''<nav><button type="submit" onclick="window.normen++">Normen</button></nav>
          <dialog open><p>Hiermit akzeptiere ich den kostenpflichtigen Vertrag.</p><button type="button" onclick="window.unsafe++">Verstanden</button></dialog>
          <script>window.normen=0;window.unsafe=0</script>''')
        with self.assertRaisesRegex(ValueError,'Hinweis') as error:
            await activate_navigation(self.page,'Normen')
        self.assertNotIn('<dialog',str(error.exception))
        self.assertEqual(await self.page.evaluate('()=>[window.normen,window.unsafe]'),[0,0])


if __name__=='__main__':unittest.main(verbosity=2)
