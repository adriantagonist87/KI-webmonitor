"""Dashboard actions, scheduling races and real Brave layout; isolated synthetic data."""
import json,os,tempfile,unittest
from pathlib import Path
from datetime import datetime,timezone
from unittest.mock import patch
from urllib.parse import urlsplit,parse_qsl
from fastapi.testclient import TestClient
from playwright.async_api import async_playwright
import store,web,engine,retry_jobs,dashboard_controls

ORIGIN='https://dashboard-controls.test'


def setup(test):
    test.temp=tempfile.TemporaryDirectory(dir='/data');test.root=Path(test.temp.name)
    test.saved=store.DATA,web.DATA,engine.DATA,store.CONFIG,web.CONFIG,store.SECRETS,web.SECRETS
    store.DATA=web.DATA=engine.DATA=test.root
    store.CONFIG=web.CONFIG=test.root/'settings.json';store.SECRETS=web.SECRETS=test.root/'credentials.json'
    store.init();store.save_json(store.CONFIG,{'omlx_url':'http://model.test:8000/v1'})
    for pid,name,enabled,tags in [('active','Dashboard Test',1,['Arbeit']),('paused','Pausiertes Projekt',0,['Privat'])]:
        store.execute('''INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created,last_success,tags)
          VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(pid,name,'https://example.test/','PRIVATE_PROMPT','example.test','0 6 * * *',enabled,'2000-01-01T00:00:00+00:00',store.now(),'2026-11-06T20:00:00+00:00',json.dumps(tags)))
    store.init()
    store.execute("INSERT INTO runs(id,project_id,started,finished,status,origin) VALUES('failed','active',?,?, 'failed','manual')",(store.now(),store.now()))
    test.client=TestClient(web.app);test.client.get('/');test.csrf=test.client.cookies['kiwm_csrf']


def teardown(test):
    test.client.close()
    store.DATA,web.DATA,engine.DATA,store.CONFIG,web.CONFIG,store.SECRETS,web.SECRETS=test.saved
    test.temp.cleanup()


def add_notice():
    stamp='2026-11-06T19:00:00+00:00'
    store.execute("INSERT INTO runs(id,project_id,started,finished,status,origin) VALUES('notice','active',?,?,'success','manual')",(stamp,stamp))
    store.execute('INSERT INTO changes VALUES(?,?,?,?,?,?,?,?,?,?)',('notice-change','active','notice','https://example.test/','Visueller Hinweis',stamp,'previous','current','{}','notice-fingerprint'))


class AutomationBackend(unittest.TestCase):
    def setUp(self):setup(self)
    def tearDown(self):teardown(self)
    def post(self,pid='active',value='0',**extra):
        return self.client.post('/projects/'+pid+'/automation',data={'csrf':self.csrf,'enabled':value,**extra},headers={'Accept':'application/json'})

    def test_01_pause_preserves_project_definition_and_next_due(self):
        old=store.one("SELECT * FROM projects WHERE id='active'");result=self.post()
        self.assertEqual(result.json(),{'id':'active','enabled':0})
        new=store.one("SELECT * FROM projects WHERE id='active'")
        self.assertEqual({**old,'enabled':0},new)

    def test_02_resume_schedules_future_without_catching_up(self):
        self.assertEqual(self.post('paused','1').status_code,200)
        p=store.one("SELECT * FROM projects WHERE id='paused'")
        self.assertEqual(p['enabled'],1);self.assertGreater(datetime.fromisoformat(p['next_due']),datetime.now(timezone.utc))

    def test_03_repeated_set_does_not_flip_or_reschedule(self):
        self.post();self.post();self.assertEqual(store.one("SELECT enabled FROM projects WHERE id='active'")['enabled'],0)
        self.post(value='1');due=store.one("SELECT next_due FROM projects WHERE id='active'")['next_due']
        self.post(value='1');self.assertEqual(store.one("SELECT next_due FROM projects WHERE id='active'")['next_due'],due)

    def test_04_csrf_foreign_origin_and_invalid_values_rejected(self):
        self.assertEqual(self.client.post('/projects/active/automation',data={'enabled':'0'}).status_code,403)
        self.assertEqual(self.client.post('/projects/active/automation',data={'csrf':self.csrf,'enabled':'0'},headers={'Origin':'https://foreign.test'}).status_code,403)
        for value in ('','2','true','-1'):self.assertEqual(self.post(value=value).status_code,400)
        self.assertEqual(self.post('missing').status_code,404)

    def test_05_html_fallback_redirects(self):
        r=self.client.post('/projects/active/automation',data={'csrf':self.csrf,'enabled':'0'},follow_redirects=False)
        self.assertEqual(r.status_code,303);self.assertEqual(r.headers['location'],'/')

    def test_06_pause_cancels_waiting_scheduled_but_keeps_manual(self):
        for pid,origin in [('active','scheduled'),('paused','manual')]:
            if origin=='manual':store.execute("UPDATE projects SET enabled=1 WHERE id='paused'")
            store.enqueue(pid,origin)
        self.post();self.post('paused')
        self.assertEqual(store.one("SELECT status FROM runs WHERE origin='scheduled'")['status'],'cancelled')
        self.assertEqual(store.one("SELECT status FROM runs WHERE origin='manual' AND id<>'failed'")['status'],'queued')

    def test_07_running_analysis_continues(self):
        rid=store.enqueue('active','scheduled');self.assertTrue(retry_jobs.begin_attempt(rid));self.post()
        self.assertEqual(store.one('SELECT status FROM runs WHERE id=?',(rid,))['status'],'running')

    def test_08_late_scheduler_snapshot_cannot_enqueue_paused_project(self):
        self.post();self.assertIsNone(store.enqueue('active','scheduled'))
        self.assertIsNotNone(store.enqueue('active','manual'))

    def test_09_already_claimed_scheduled_job_rechecks_enabled(self):
        rid=store.enqueue('active','scheduled');store.execute("UPDATE projects SET enabled=0 WHERE id='active'")
        self.assertFalse(retry_jobs.begin_attempt(rid));self.assertEqual(store.one('SELECT status FROM runs WHERE id=?',(rid,))['status'],'cancelled')

    def test_10_pause_cancels_planned_retry(self):
        retry_jobs.register_result('failed');self.assertEqual(store.one('SELECT state FROM run_retries')['state'],'pending')
        self.post();self.assertEqual(store.one('SELECT state FROM run_retries')['state'],'cancelled')

    def test_11_configuration_and_other_projects_unchanged(self):
        old=store.one("SELECT * FROM projects WHERE id='paused'");raw=store.CONFIG.read_bytes()
        self.post();self.assertEqual(store.CONFIG.read_bytes(),raw);self.assertEqual(store.one("SELECT * FROM projects WHERE id='paused'"),old)


@unittest.skipUnless(os.environ.get('BROWSER_CDP_URL'),'Isolated QA browser required')
class DashboardBrowser(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        setup(self);self.posts=[];self.fail_post=False;self.stale=False
        self.pw=await async_playwright().start();self.browser=await self.pw.chromium.connect_over_cdp(os.environ['BROWSER_CDP_URL'])
        self.ctx=await self.browser.new_context(viewport={'width':1440,'height':1000},reduced_motion='no-preference')
        await self.ctx.route('**/*',self.serve);self.page=await self.ctx.new_page()
        await self.page.goto(ORIGIN,wait_until='networkidle');await self.page.evaluate('document.fonts.ready')

    async def asyncTearDown(self):
        await self.ctx.close();await self.browser.close();await self.pw.stop();teardown(self)

    async def serve(self,route):
        req=route.request
        if not req.url.startswith(ORIGIN):return await route.abort()
        path=req.url.removeprefix(ORIGIN) or '/'
        if req.method=='POST':
            self.posts.append(path)
            if self.fail_post:return await route.fulfill(status=503,body='synthetic failure')
            response=self.client.post(path,data=dict(parse_qsl(req.post_data or '')),headers={'Accept':'application/json'})
        else:
            if path=='/api/project-status' and self.stale:
                data=self.client.get(path).json()
                import asyncio
                self.stale=False;await asyncio.sleep(.5)
                return await route.fulfill(status=200,content_type='application/json',body=json.dumps(data))
            response=self.client.get(path)
        await route.fulfill(status=response.status_code,content_type=response.headers.get('content-type','text/html'),body=response.content)

    def active(self):return self.page.locator('[data-project-id=active]')

    async def test_12_real_pause_resume_and_filter_counts_without_navigation(self):
        button=self.active().locator('.automation-toggle');url=self.page.url
        self.assertEqual(await button.locator('use').get_attribute('href'),'#i-play')
        await button.click();await self.page.wait_for_function("document.querySelector('[data-project-id=active]').dataset.enabled==='0'")
        self.assertEqual(self.page.url,url);self.assertIn('Pausiert',await button.inner_text())
        self.assertEqual(await button.locator('use').get_attribute('href'),'#i-pause')
        self.assertEqual(await self.page.locator('[data-filter=paused] b').inner_text(),'2')
        self.assertIn('has-paused',await self.page.locator('[data-filter=paused]').get_attribute('class'))
        await button.click();await self.page.wait_for_function("document.querySelector('[data-project-id=active]').dataset.enabled==='1'")
        self.assertEqual(await self.page.locator('[data-filter=paused] b').inner_text(),'1')
        self.assertEqual(store.one("SELECT enabled FROM projects WHERE id='active'")['enabled'],1)

    async def test_13_paused_filter_and_search_keep_counts_consistent(self):
        await self.page.locator('[data-filter=paused]').click()
        self.assertFalse(await self.active().is_visible());self.assertTrue(await self.page.locator('[data-project-id=paused]').is_visible())
        await self.page.locator('[data-project-search]').fill('Dashboard')
        self.assertEqual(await self.page.locator('[data-filter=paused] b').inner_text(),'0')
        self.assertNotIn('has-paused',await self.page.locator('[data-filter=paused]').get_attribute('class'))
        self.assertEqual(await self.page.locator('[data-filter=error] b').inner_text(),'1')
        self.assertIn('has-errors',await self.page.locator('[data-filter=error]').get_attribute('class'))

    async def test_14_error_fades_once_per_second_and_stops_when_zero(self):
        button=self.page.locator('[data-filter=error]');self.assertIn('has-errors',await button.get_attribute('class'))
        self.assertEqual(await button.evaluate('e=>getComputedStyle(e).animationDuration'),'1s')
        colors=await button.evaluate("e=>{const a=e.getAnimations()[0];a.pause();a.currentTime=0;const first=getComputedStyle(e).backgroundColor;a.currentTime=500;return [first,getComputedStyle(e).backgroundColor]}")
        self.assertNotEqual(*colors)
        await self.page.locator('[data-project-search]').fill('Pausiertes')
        self.assertEqual(await button.evaluate('e=>getComputedStyle(e).animationName'),'none')

    async def test_15_app_motion_choice_overrides_system_and_manual_off_stops_fade(self):
        button=self.page.locator('[data-filter=error]')
        await self.page.emulate_media(reduced_motion='reduce')
        self.assertNotEqual(await button.evaluate('e=>getComputedStyle(e).animationName'),'none')
        await self.page.emulate_media(reduced_motion='no-preference')
        await self.page.wait_for_function("document.documentElement.dataset.motion==='on'")
        await self.page.locator('[data-motion-toggle]').click()
        self.assertEqual(await button.evaluate('e=>getComputedStyle(e).animationName'),'none')

    async def test_16_notices_share_time_row_without_changing_card_height_at_any_zoom(self):
        for size in ('50','100','150'):
            await self.page.locator('[data-ui-size]').first.select_option(size)
            old=await self.active().evaluate('e=>e.offsetHeight')
            if not store.one("SELECT id FROM runs WHERE id='notice'"):add_notice()
            await self.page.reload(wait_until='networkidle')
            card=self.active();self.assertAlmostEqual(await card.evaluate('e=>e.offsetHeight'),old,delta=1)
            row=card.locator('.project-facts');self.assertEqual(await row.locator('.change-pill.subtle').count(),1)
            span=await row.locator('span[title="Letzte erfolgreiche Prüfung"]').bounding_box();pill=await row.locator('.change-pill.subtle').bounding_box()
            self.assertAlmostEqual(span['y']+span['height']/2,pill['y']+pill['height']/2,delta=2)
            store.execute("DELETE FROM changes WHERE id='notice-change'");store.execute("DELETE FROM runs WHERE id='notice'")
            await self.page.reload(wait_until='networkidle')

    async def test_19_notice_filter_link_and_mobile_label(self):
        add_notice();await self.page.reload(wait_until='networkidle')
        await self.page.locator('[data-filter=notices]').click()
        self.assertEqual(await self.page.locator('[data-filter=notices] b').inner_text(),'1')
        self.assertTrue(await self.active().is_visible())
        self.assertFalse(await self.page.locator('[data-project-id=paused]').is_visible())
        await self.page.set_viewport_size({'width':377,'height':850})
        link=self.active().locator('.notice-button')
        self.assertIn('1 Hinweis',await link.inner_text())
        self.assertEqual(await link.evaluate('e=>getComputedStyle(e).animationDuration'),'1s')
        await link.click()
        await self.page.locator('#change-notices').wait_for()
        self.assertTrue(await self.page.locator('#change-notices').evaluate('e=>e.open'))
        self.assertTrue(await self.page.locator('#change-notices > summary').evaluate('e=>{const r=e.getBoundingClientRect();return r.top>=0 && r.bottom<innerHeight}'))

    async def test_20_paused_title_and_accessible_notice_animation(self):
        add_notice();await self.page.reload(wait_until='networkidle')
        active=self.active().locator('h2 a')
        paused=self.page.locator('[data-project-id=paused] h2 a')
        self.assertNotEqual(await active.evaluate('e=>getComputedStyle(e).color'),await paused.evaluate('e=>getComputedStyle(e).color'))
        await self.active().locator('.automation-toggle').click()
        await self.page.wait_for_function("document.querySelector('[data-project-id=active]').dataset.enabled==='0'")
        self.assertEqual(await active.evaluate('e=>getComputedStyle(e).color'),await paused.evaluate('e=>getComputedStyle(e).color'))
        await self.page.emulate_media(reduced_motion='reduce')
        self.assertNotEqual(await self.active().locator('.notice-button').evaluate('e=>getComputedStyle(e).animationName'),'none')

    async def test_21_dashboard_review_confirms_visible_changes_and_keeps_a_newer_one(self):
        def add_findings(run_id, findings):
            stamp = store.now()
            store.execute('INSERT INTO runs(id,project_id,started,finished,status,origin) VALUES(?,?,?,?,?,?)',
                          (run_id, 'active', stamp, stamp, 'success', 'test'))
            for finding in findings:
                store.execute('INSERT INTO changes VALUES(?,?,?,?,?,?,?,?,?,?)',
                              (finding, 'active', run_id, 'https://example.test/', 'Inhalt geändert',
                               stamp, 'previous', 'current', '{}', finding))

        add_findings('review-visible', ['review-one', 'review-two'])
        await self.page.set_viewport_size({'width': 377, 'height': 850})
        await self.page.reload(wait_until='networkidle')
        card = self.active()
        review = card.get_by_role('button', name='Alle Änderungen für Dashboard Test bestätigen')
        self.assertTrue(await review.is_visible())
        self.assertEqual(await review.locator('use').get_attribute('href'), '#i-check')
        edit_box = await card.get_by_role('link', name='Dashboard Test bearbeiten').bounding_box()
        review_box = await review.bounding_box()
        self.assertGreater(review_box['x'], edit_box['x'])
        self.assertAlmostEqual(review_box['y'], edit_box['y'], delta=2)
        self.assertFalse(await self.page.locator('[data-project-id=paused] [data-review-form]').is_visible())
        add_findings('review-newer', ['review-three'])

        changes_filter = self.page.locator('[data-filter=attention]')
        await changes_filter.click()
        await review.click()
        await card.get_by_role('link', name='1 Änderung erkannt').wait_for()
        self.assertEqual({row['change_id'] for row in store.rows('SELECT change_id FROM change_reviews')},
                         {'review-one', 'review-two'})
        await review.click()
        await card.wait_for(state='hidden')
        self.assertEqual(await changes_filter.get_attribute('aria-pressed'), 'true')
        self.assertEqual(await changes_filter.locator('b').inner_text(), '0')
        await changes_filter.click()
        self.assertTrue(await card.is_visible())
        self.assertFalse(await review.is_visible())
        self.assertEqual(await card.get_attribute('data-attention'), '0')
        self.assertEqual(len(store.rows('SELECT change_id FROM change_reviews')), 3)

    async def test_17_request_failure_retains_last_confirmed_state(self):
        self.fail_post=True;await self.active().locator('.automation-toggle').click()
        await self.page.locator('.toast').wait_for();self.assertEqual(await self.active().get_attribute('data-enabled'),'1')
        self.assertFalse(await self.active().locator('.automation-toggle').is_disabled())

    async def test_18_stale_status_response_cannot_undo_pause(self):
        self.stale=True;await self.page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        await self.page.wait_for_timeout(100)
        await self.active().locator('.automation-toggle').click()
        await self.page.wait_for_function("document.querySelector('[data-project-id=active]').dataset.enabled==='0'")
        await self.page.wait_for_timeout(600)
        self.assertEqual(await self.active().get_attribute('data-enabled'),'0')


if __name__=='__main__':unittest.main()
