"""Real Brave acceptance checks against the private fixture, with fake values only."""
import asyncio,json,os,tempfile,unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
from site_login import SubscriptionLogin,LoginPolicy,LoginError,open_readonly_section
import site_privacy

ORIGIN='http://fixture:8000'
POLICY=LoginPolicy(origin=ORIGIN,login_path='/auth-test/login',form_selector='form[action="/auth-test/login"]',consent_origin=ORIGIN,close_selector='.advertisement button[aria-label="Werbung schließen"]',logout_paths=('/auth-test/logout',))
SECRET={'provider':'elektro_subscription','origin':ORIGIN,'abonummer':'987654321','postcode':'54321'}

class LoginBrowserTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.p=await async_playwright().start()
  self.browser=await self.p.chromium.connect_over_cdp(os.environ.get('BROWSER_CDP_URL','http://172.29.133.20:9223'))
  self.context=await self.browser.new_context(service_workers='block')
  self.messages=[]
  self.broker=SubscriptionLogin(self.context,ORIGIN+'/auth-test/target',SECRET,self.messages.append,POLICY)
  await self.context.route('**/*',self.broker.guard)
 async def asyncTearDown(self):
  await self.context.close();await self.browser.close();await self.p.stop()
 async def test_01_cookie_ads_login_redirect_and_reused_session(self):
  self.assertEqual(await self.broker.ensure(),'signed_in')
  page=await self.context.new_page();await page.goto(ORIGIN+'/auth-test/target')
  self.assertIn('/auth-test/target',page.url)
  self.assertGreaterEqual(self.messages.count('Werbehinweis geschlossen.'),2);self.assertEqual(self.messages.count('Cookie-Hinweis bestätigt.'),1)
  self.assertTrue(await page.get_by_role('link',name='Abmelden',exact=True).is_visible())
  self.assertEqual(await self.broker.ensure(force=True),'reused')
  self.assertEqual(self.broker.attempts,1)
  self.assertIsNone(self.broker.page);self.assertEqual(self.broker.login_until,0)
  self.assertNotIn(SECRET['abonummer'],' '.join(self.messages));self.assertNotIn(SECRET['postcode'],' '.join(self.messages))
 async def test_02_expired_session_gets_reauthenticated(self):
  await self.broker.ensure()
  await self.context.clear_cookies(name='kiwm_auth_test')
  second=SubscriptionLogin(self.context,ORIGIN+'/auth-test/target',SECRET,self.messages.append,POLICY)
  await self.context.unroute('**/*',self.broker.guard);await self.context.route('**/*',second.guard)
  self.assertEqual(await second.ensure(),'signed_in')
  self.assertEqual(second.attempts,1)
 async def test_03_wrong_credentials_fail_once_without_disclosure(self):
  self.broker.secret={**SECRET,'postcode':'11111'}
  with self.assertRaises(LoginError) as caught:await self.broker.ensure()
  self.assertEqual(self.broker.attempts,1)
  self.assertNotIn('11111',str(caught.exception));self.assertNotIn(SECRET['abonummer'],str(caught.exception))
  self.assertIsNone(self.broker.page);self.assertEqual(self.broker.login_until,0)
  with self.assertRaises(LoginError):await self.broker.ensure(force=True)
  self.assertEqual(self.broker.attempts,1)
 async def test_04_login_form_and_account_values_never_become_model_inputs(self):
  await self.broker.ensure()
  token=site_privacy.bind(SECRET)
  try:
   page=await self.context.new_page();await page.goto(ORIGIN+'/auth-test/target')
   clean=site_privacy.sanitize_html(await page.content())
   self.assertNotIn(SECRET['abonummer'],clean)
   self.assertIn('fachliche Zahlenwert 54321',clean)
   self.assertNotIn('PLZ 54321',clean)
   self.assertFalse(await site_privacy.contains_auth_form(page))
   # Expire only this private test session, then verify the text-type password is protected.
   await self.context.clear_cookies(name='kiwm_auth_test')
   await page.goto(ORIGIN+'/auth-test/login')
   self.assertTrue(await site_privacy.contains_auth_form(page))
   from engine import safe_screenshot
   with self.assertRaises(RuntimeError):await safe_screenshot(page)
   await page.set_content('<main><p>Unerwarteter Identifikator 987654321</p></main>')
   with self.assertRaises(RuntimeError):await safe_screenshot(page)
  finally:site_privacy.reset(token)
 async def test_05_answer_tab_really_opens_and_open_questions_are_excluded(self):
  await self.broker.ensure()
  page=await self.context.new_page();await page.goto(ORIGIN+'/auth-test/target')
  self.assertFalse(await page.locator('#answer').is_visible())
  result=await open_readonly_section(page,'Fragen mit Antwort')
  self.assertEqual(result['selected_section'],'answer')
  self.assertTrue(await page.locator('#answer').is_visible());self.assertFalse(await page.locator('#open').is_visible())
  from page_content import editorial_content
  body=editorial_content(await page.content(),page.url,'#answer')
  self.assertEqual(len(body['metadata']['headings']),4)
  self.assertNotIn('Unpassender Titel',body['text']);self.assertIn('54321',body['text'])
 async def test_06_foreign_form_action_and_other_posts_remain_blocked(self):
  page=await self.context.new_page();await page.goto(ORIGIN+'/auth-test/login')
  self.broker.page=page;self.broker.login_until=__import__('time').monotonic()+10
  class FakeRequest:
   method='POST';url='https://evil.invalid/user/login';post_data=urlencode({'login[csrfToken]':'test','login[userIdentifier]':SECRET['abonummer'],'login[password]':SECRET['postcode']})
   frame=page.main_frame
   def is_navigation_request(self):return True
  self.assertFalse(await self.broker.permits(FakeRequest()))
  FakeRequest.url=ORIGIN+'/mutate'
  self.assertFalse(await self.broker.permits(FakeRequest()))
  self.broker.page=None;self.broker.login_until=0
  result=await page.evaluate("async()=>{try{return (await fetch('/mutate',{method:'POST',body:'probe'})).status}catch{return 'blocked'}}")
  self.assertEqual(result,'blocked')
 async def test_07_cancelled_auth_revokes_lease_and_closes_private_page(self):
  original=self.broker.dismiss
  entered=asyncio.Event()
  async def blocked(page):
   entered.set();await asyncio.Event().wait()
  self.broker.dismiss=blocked
  task=asyncio.create_task(self.broker.ensure())
  await asyncio.wait_for(entered.wait(),20)
  task.cancel()
  with self.assertRaises(asyncio.CancelledError):await task
  self.assertIsNone(self.broker.page);self.assertEqual(self.broker.login_until,0);self.assertEqual(self.broker.consent_until,0)
 async def test_08_production_policy_cannot_rebind_credentials(self):
  self.assertEqual(self.broker._origin('https://WWW.ELEKTRO.NET:443/example'),'https://www.elektro.net')
  self.assertEqual(self.broker._origin('http://fixture:8000/example'),ORIGIN)
  broker=SubscriptionLogin(self.context,'https://evil.invalid/',{**SECRET,'origin':'https://www.elektro.net'})
  with self.assertRaises(LoginError):await broker.ensure()
  self.assertEqual(broker.attempts,0);self.assertIsNone(broker.page)
 async def test_09_hidden_account_logout_is_a_server_state_marker(self):
  page=await self.context.new_page();await page.goto(ORIGIN+'/auth-test/login')
  self.assertFalse(await self.broker._logged_in(page))
  await page.set_content('<nav hidden><a href="/auth-test/logout">Abmelden</a></nav><main>Angemeldete Inhalte</main>')
  self.assertTrue(await self.broker._logged_in(page))
  await page.set_content('<nav hidden><a href="/auth-test/logout">Abmelden</a></nav><form action="/auth-test/login"><input name="login[password]"></form>')
  self.assertFalse(await self.broker._logged_in(page))
 async def test_10_late_ad_on_content_page_can_be_closed_without_form_access(self):
  await self.broker.ensure()
  page=await self.context.new_page();await page.goto(ORIGIN+'/auth-test/target')
  await page.set_content('<main>Fachinhalt</main><div class="advertisement" role="dialog"><button type="button" aria-label="Werbung schließen" onclick="this.parentElement.remove()">X</button></div>')
  await self.broker.dismiss(page)
  self.assertEqual(await page.locator('.advertisement').count(),0)
  self.assertEqual(self.broker.attempts,1)

if __name__=='__main__':unittest.main(verbosity=2)
