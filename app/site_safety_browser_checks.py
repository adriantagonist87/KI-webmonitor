"""Real Brave against synthetic routed pages only; never contact a publisher."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from playwright.async_api import Error as BrowserError
from playwright.async_api import async_playwright
import site_safety as safety
from site_login import GenericFormLogin, LoginPolicy

ORIGIN='https://source.test'
WARNING='Unsere Systeme haben ungewöhnlichen Datenverkehr aus Ihrem Computernetzwerk festgestellt. Bitte klicken Sie hier, wenn Sie kein Roboter sind.'


@unittest.skipUnless(os.environ.get('BROWSER_CDP_URL'),'Isolated QA browser required')
class SafetyBrowser(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        endpoint=os.environ['BROWSER_CDP_URL']
        if endpoint in ('http://browser:9223','http://172.29.133.20:9223'):
            raise AssertionError('Do not use the production browser')
        self.pw=await async_playwright().start()
        self.browser=await self.pw.chromium.connect_over_cdp(endpoint)
        self.ctx=await self.browser.new_context()
        self.requests=[]
        self.html='<h1>Content</h1>'
        self.status=200
        async def route(request):
            self.requests.append((request.request.method,request.request.url))
            if request.request.url.startswith(ORIGIN):
                await request.fulfill(status=429 if request.request.url.endswith('/api') else self.status,
                                     content_type='text/html',body='<link rel="icon" href="data:,">'+
                                     ('' if request.request.url.endswith('/captcha-widget') else self.html))
            else:
                await request.abort()
        await self.ctx.route('**/*',route)
        self.page=await self.ctx.new_page()
        self.guard=safety.Guard('qa-project',ORIGIN,0)
        self.token=safety._CURRENT.set(self.guard)

    async def asyncTearDown(self):
        safety.reset(self.token)
        await self.ctx.close();await self.browser.close();await self.pw.stop()

    async def test_01_security_page_at_original_url_stops(self):
        self.html='<title>Security</title><h1>Security</h1><p>'+WARNING+'</p>'
        with self.assertRaises(safety.ProtectionDetected):
            await safety.goto(self.page,ORIGIN+'/start',wait_until='domcontentloaded')
        self.assertEqual(self.requests,[('GET',ORIGIN+'/start')])

    async def test_02_captcha_target_is_not_visited_or_solved(self):
        with self.assertRaises(safety.ProtectionDetected):
            await safety.goto(self.page,ORIGIN+'/myracloud-captcha/?q=target')
        self.assertEqual(self.requests,[])

    async def test_03_regular_article_and_invisible_footer_widget_remain_readable(self):
        self.html='<title>Security</title><article><h1>Security</h1>'+WARNING+'</article><iframe style="display:none" src="/captcha-widget"></iframe>'
        await safety.goto(self.page,ORIGIN+'/article',wait_until='domcontentloaded')
        self.assertIsNone(self.guard.pending)

    async def test_04_xhr_rate_limit_stops_following_actions(self):
        self.ctx.on('response',self.guard.note_response)
        self.html='<button onclick="fetch(\'/api\')">Lesen</button>'
        await safety.goto(self.page,ORIGIN+'/start',wait_until='domcontentloaded')
        async with self.page.expect_response(lambda r:r.url.endswith('/api')):
            await self.page.get_by_role('button',name='Lesen').click()
        with self.assertRaises(safety.ProtectionDetected):
            await safety.check_page(self.page)
        self.assertEqual(len(self.requests),2)

    async def test_06_failed_navigation_with_rendered_protection_is_not_generic_error(self):
        self.html='<title>Security</title><h1>Security</h1>'+WARNING
        goto=self.page.goto
        async def interrupted(url,**options):
            await goto(url,**options)
            raise BrowserError('Page.goto: net::ERR_ABORTED at https://source.test/start')
        with patch.object(self.page,'goto',side_effect=interrupted):
            with self.assertRaises(safety.ProtectionDetected):
                await safety.goto(self.page,ORIGIN+'/start',wait_until='domcontentloaded')
        self.assertEqual(self.requests,[('GET',ORIGIN+'/start')])

    async def test_07_ordinary_navigation_failure_is_not_claimed_as_protection(self):
        from browser_errors import describe
        error=BrowserError('net::ERR_ABORTED at https://source.test/?token=PRIVATE')
        with patch.object(self.page,'goto',side_effect=error):
            with self.assertRaises(BrowserError) as raised:
                await safety.goto(self.page,ORIGIN+'/start')
        self.assertIs(raised.exception,error)
        self.assertIsNone(self.guard.pending)
        self.assertEqual(self.requests,[])
        self.assertNotIn('PRIVATE',describe(error))
        self.assertNotIn('https://',describe(error))

    async def test_05_login_broker_never_fills_or_submits_after_security_page(self):
        self.status=403;self.html='<title>Security</title><h1>Security</h1>'+WARNING
        config={'origin':ORIGIN,'login_url':ORIGIN+'/login','form_selector':'form'}
        policy=LoginPolicy(origin=ORIGIN,login_path='/login',form_selector='form',consent_origin=ORIGIN)
        secret={'provider':'generic_form','origin':ORIGIN,'username':'synthetic-user','password':'synthetic-password'}
        broker=GenericFormLogin(self.ctx,ORIGIN+'/start',secret,config,policy=policy)
        with self.assertRaises(safety.ProtectionDetected):await broker.ensure()
        self.assertEqual(broker.attempts,0)
        self.assertEqual(self.requests,[('GET',ORIGIN+'/start')])


if __name__=='__main__':unittest.main(verbosity=2)
