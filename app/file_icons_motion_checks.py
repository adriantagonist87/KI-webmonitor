"""Actual icon/layout and motion behavior in isolated Brave or Firefox."""
import os,re,unittest
from pathlib import Path
from urllib.parse import urlsplit
from playwright.async_api import async_playwright
import document_files_ui_checks as fixtures
from document_types import MEDIA
import store,web

@unittest.skipUnless(os.environ.get('BROWSER_CDP_URL'),'Isolated QA browser required')
class FileIconsMotionBrowser(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=fixtures.GlobalFilesUI();self.fixture.setUp()
        for ext in MEDIA:self.fixture.document('alpha','https://example.org/sample'+ext,('ICON-FIXTURE-'+ext).encode(),8,extension=ext[1:])
        store.execute("UPDATE projects SET enabled=1 WHERE id='alpha'")
        for rid,status,started in [('icon-notice','success','2026-11-06T10:00:00+00:00'),('icon-error','failed','2026-11-07T10:00:00+00:00')]:
            store.execute('INSERT INTO runs(id,project_id,started,finished,status,error,origin) VALUES(?,?,?,?,?,?,?)',(rid,'alpha',started,started,status,'Synthetic timeout' if status=='failed' else None,'test'))
        store.execute('INSERT INTO changes VALUES(?,?,?,?,?,?,?,?,?,?)',('icon-hint','alpha','icon-notice','https://example.org','Visuelle Abweichung','2026-11-06T10:00:00+00:00','previous','current','{"ai_assessment":{"relevance":"uncertain","reason":"Synthetischer Hinweis"}}','icon-hint-1'))
        self.pw=await async_playwright().start()
        if os.environ.get('KIWM_UI_TEST_ENGINE')=='firefox':self.browser=await self.pw.firefox.launch(headless=False)
        else:self.browser=await self.pw.chromium.connect_over_cdp(os.environ['BROWSER_CDP_URL'])
        self.context=await self.browser.new_context(viewport={'width':1440,'height':1050},reduced_motion='reduce')
        await self.context.route('**/*',self.serve)
        self.page=await self.context.new_page()
    async def asyncTearDown(self):
        if hasattr(self,'context'):await self.context.close()
        if hasattr(self,'browser'):await self.browser.close()
        if hasattr(self,'pw'):await self.pw.stop()
        self.fixture.doCleanups()
    async def serve(self,route):
        u=urlsplit(route.request.url)
        if u.hostname!='appearance.test':return await route.abort()
        response=self.fixture.client.get(u.path+('?' +u.query if u.query else ''))
        await route.fulfill(status=response.status_code,body=response.content,headers={'content-type':response.headers.get('content-type','text/html')})
    async def open(self,path='/'):
        await self.page.goto('https://appearance.test'+path,wait_until='networkidle')
    async def animations(self,on):
        for selector in ('[data-filter=error]','.notice-button'):
            name=await self.page.locator(selector).first.evaluate('e=>getComputedStyle(e).animationName')
            self.assertEqual(name!='none',on,(selector,name))
    async def hover(self,on):
        button=self.page.locator('.analyze-button').first;await button.hover();await self.page.wait_for_timeout(260)
        matrix=await button.evaluate('e=>getComputedStyle(e).transform')
        if on:self.assertAlmostEqual(float(re.findall(r'-?[0-9.]+',matrix)[0]),1.08,delta=.005)
        else:self.assertEqual(matrix,'none')
    async def test_01_default_animations_ignore_reduced_motion(self):
        await self.open()
        await self.page.emulate_media(reduced_motion='reduce')
        self.assertTrue(await self.page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches"))
        self.assertEqual(await self.page.locator('html').get_attribute('data-motion'),'on')
        await self.animations(True);await self.hover(True)
    async def test_02_manual_choice_switches_all_effects_and_persists(self):
        await self.open();await self.page.locator('[data-motion-toggle]').first.click()
        await self.animations(False);await self.hover(False)
        await self.page.reload(wait_until='networkidle');await self.animations(False)
        await self.page.locator('[data-motion-toggle]').first.click();await self.animations(True);await self.hover(True)
    async def test_03_system_changes_never_override_app_choice(self):
        await self.open()
        for pref in ('no-preference','reduce'):await self.page.emulate_media(reduced_motion=pref);await self.animations(True)
        await self.page.locator('[data-motion-toggle]').first.click()
        for pref in ('no-preference','reduce'):await self.page.emulate_media(reduced_motion=pref);await self.animations(False)
    async def test_04_blocked_storage_keeps_controls_functional(self):
        await self.context.add_init_script("Storage.prototype.getItem=function(){throw new DOMException('Blocked','SecurityError')};Storage.prototype.setItem=function(){throw new DOMException('Blocked','SecurityError')}")
        await self.open();await self.animations(True)
        await self.page.locator('[data-motion-toggle]').first.click();await self.animations(False)
        await self.page.locator('[data-motion-toggle]').first.click();await self.animations(True)
    async def test_05_all_types_have_readable_badges_and_distinct_colors(self):
        await self.open('/files')
        for ext in MEDIA:
            icon=self.page.locator('.file-type-icon[data-file="'+ext[1:]+'"]').first
            self.assertTrue(await icon.is_visible());self.assertEqual(await icon.get_attribute('aria-label'),ext[1:].upper()+'-Datei')
            content=await icon.evaluate("e=>getComputedStyle(e,'::after').content")
            # Firefox retains attr() in the computed value; both engines draw the same attribute.
            if content!='attr(data-file)':self.assertIn(ext[1:],content)
        colors=[]
        for ext in ('pdf','docx','xlsx','pptx'):
            colors.append(await self.page.locator('[data-file='+ext+']').first.evaluate("e=>getComputedStyle(e,'::after').backgroundColor"))
        self.assertEqual(len(set(colors)),4)
        for theme in ('light','dark'):
            await self.page.evaluate('(theme)=>document.documentElement.dataset.theme=theme',theme)
            self.assertEqual(await self.page.locator('[data-file=pdf]').first.evaluate("e=>getComputedStyle(e,'::after').color"),'rgb(255, 255, 255)')
        out=os.environ.get('KIWM_UI_SCREENSHOT_DIR')
        if out:
            path=Path(out);path.mkdir(parents=True,exist_ok=True)
            await self.page.evaluate("document.documentElement.dataset.theme='light'");await self.page.screenshot(path=str(path/'file-icons-light.png'),full_page=True)
            await self.page.evaluate("document.documentElement.dataset.theme='dark'");await self.page.screenshot(path=str(path/'file-icons-dark.png'),full_page=True)
    async def test_06_badges_fit_the_icon_column_at_all_font_sizes(self):
        await self.open('/files')
        for size in ('50','100','150'):
            await self.page.locator('[data-ui-size]').first.select_option(size)
            icon=self.page.locator('[data-file=xlsx]').first
            metrics=await icon.evaluate("e=>{const r=e.getBoundingClientRect(),c=e.parentElement.getBoundingClientRect(),s=getComputedStyle(e,'::after');return {left:r.left+parseFloat(s.left),right:r.left+parseFloat(s.left)+parseFloat(s.width),start:c.left,end:c.right,font:parseFloat(s.fontSize)}}")
            self.assertGreaterEqual(metrics['left'],metrics['start']);self.assertLessEqual(metrics['right'],metrics['end']);self.assertGreaterEqual(metrics['font'],5)
            self.assertLessEqual((await icon.bounding_box())['height'],20)
            self.assertLessEqual((await icon.bounding_box())['width'],15)

    async def test_07_document_names_filter_and_sort_in_the_real_table(self):
        import document_names
        row=store.one('SELECT id,run_id,project_id,url,sha256,file FROM snapshots WHERE id=?',(self.fixture.b_repeat,))
        document_names._persist(row,{'source':'first_page','text':'Prüfprotokoll Technische Anlagen'})
        await self.open('/files?q=prüfprotokoll')
        self.assertEqual(await self.page.locator('[data-file-row]').count(),1)
        self.assertEqual(await self.page.locator('.file-document-name').inner_text(),'Prüfprotokoll Technische Anlagen')
        self.assertEqual(await self.page.locator('[data-files-table] thead th').count(),8)
        await self.page.locator('[data-files-sort-key=document_name]').click()
        await self.page.wait_for_function("document.querySelector('th.files-document-name-column').getAttribute('aria-sort') === 'ascending'")
        self.assertEqual(await self.page.locator('[data-files-query]').input_value(),'prüfprotokoll')

    async def test_08_table_fills_the_workspace_and_centers_downloads(self):
        await self.page.set_viewport_size({'width': 2300, 'height': 900})
        await self.open('/files')
        project = self.page.locator('.file-project-link[href="/projects/alpha"]').first
        icon = project.locator('img')
        await self.page.wait_for_function('img => img.naturalWidth > 0', arg=await icon.element_handle())
        icon_box = await icon.bounding_box()
        name_box = await project.locator('span').bounding_box()
        self.assertLessEqual(icon_box['height'], 16)
        self.assertLess(icon_box['x'] + icon_box['width'], name_box['x'])
        workspace = await self.page.locator('.workspace').bounding_box()
        table = await self.page.locator('[data-files-table]').bounding_box()
        self.assertLess(workspace['width'] - table['width'], 50)
        header = await self.page.locator('th.files-download-column').bounding_box()
        button = await self.page.locator('td.files-download-column a').first.bounding_box()
        self.assertAlmostEqual(header['x'] + header['width']/2, button['x'] + button['width']/2, delta=1)
        self.assertLess(header['width'], 100)
        await self.page.set_viewport_size({'width': 390, 'height': 844})
        self.assertTrue(await self.page.locator('.files-table-scroll').evaluate('e => e.scrollWidth > e.clientWidth'))
        page_widths = await self.page.evaluate('''() => ({
            content: document.documentElement.scrollWidth,
            viewport: document.documentElement.clientWidth,
        })''')
        self.assertLessEqual(page_widths['content'], page_widths['viewport'])

if __name__=='__main__':unittest.main(verbosity=2)
