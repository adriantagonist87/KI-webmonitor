"""Generic credential storage validation and private real-Brave login checks."""
import asyncio,json,os,tempfile,time,unittest
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch
from site_login import GenericFormLogin,LoginPolicy,LoginError
import site_access,site_privacy,store

ORIGIN='http://fixture:8000'
CONFIG={'provider':'generic_form','origin':ORIGIN,'enabled':True,'login_url':ORIGIN+'/auth-test/generic/login','submit_url':ORIGIN+'/auth-test/generic/login','form_selector':'#generic-login','username_selector':'#generic-user','password_selector':'#generic-pass','submit_selector':'#generic-submit','success_selector':'#generic-logout','consent_selector':'#generic-cookie-accept','close_selector':'#generic-ad-close'}
POLICY=LoginPolicy(origin=ORIGIN,login_path='/auth-test/generic/login',form_selector='#generic-login',consent_origin=ORIGIN)
SECRET={'provider':'generic_form','origin':ORIGIN,'username':'test-reader@example.test','password':'fake-only-C0de!'}


class GenericConfigTests(unittest.TestCase):
 def test_00_same_origin_return_url_is_exact_and_bounded(self):
  from urllib.parse import quote
  origin='https://example.test'
  base={**CONFIG,'origin':origin,'login_url':origin+'/login','submit_url':origin+'/login'}
  url=origin+'/login?return_url='+quote(origin+'/library/login',safe='')
  cfg={**base,'login_url':url,'submit_url':url}
  self.assertEqual(site_access.validate_public_config(origin,cfg),cfg)
  for query in ('return_url=https%3A%2F%2Fevil.test%2F','next=%2Fcatalog','return_url='+quote(origin+'/admin',safe=''),'return_url='+quote(origin+'/catalog?next=/admin',safe=''),'return_url='+quote(origin+'/catalog',safe='')+'&extra=1'):
   with self.subTest(query=query),self.assertRaises(site_access.AccessError):site_access.validate_public_config(origin,{**base,'submit_url':origin+'/login?'+query})
 def test_01_public_binding_rejects_other_origin_values_and_unsafe_success(self):
  cfg={**CONFIG,'origin':'https://example.test','login_url':'https://example.test/login','submit_url':'https://example.test/login'}
  self.assertEqual(site_access.validate_public_config('https://example.test/articles',cfg),cfg)
  for patchdata in ({'origin':'https://evil.invalid'},{'submit_url':'https://evil.invalid/login'},{'success_selector':'body'},{'form_selector':'input[value="credential"]'},{'password':'not-public'},{'submit_url':'https://example.test/account/delete'}):
   with self.subTest(patchdata=patchdata),self.assertRaises(site_access.AccessError):site_access.validate_public_config('https://example.test/articles',{**cfg,**patchdata})
  with self.assertRaises(site_access.AccessError):site_access.validate_public_config('http://example.test',cfg)
 def test_02_private_generic_storage_preserves_other_values_and_origin_binding(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);before=(store.DATA,store.CONFIG,store.SECRETS);store.DATA=root;store.CONFIG=root/'config.json';store.SECRETS=root/'credentials.json'
   try:
    store.init();store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,next_due,created) VALUES(?,?,?,?,?,?,?,?)',('generic','Demo','https://example.test/articles','Read','example.test','0 6 * * *',store.next_due('0 6 * * *'),store.now()))
    cfg={**CONFIG,'origin':'https://example.test','login_url':'https://example.test/login','submit_url':'https://example.test/login'}
    site_access.save_generic('generic',True,cfg,SECRET['username'],SECRET['password']);self.assertEqual(site_access.get_secret('generic')['password'],SECRET['password'])
    public=json.dumps(store.rows('SELECT * FROM state'));self.assertNotIn(SECRET['username'],public);self.assertNotIn(SECRET['password'],public)
    site_access.save_generic('generic',False,cfg);self.assertEqual(site_access.get_secret('generic')['password'],SECRET['password']);self.assertFalse(site_access.public_config('generic')['enabled'])
    store.execute('UPDATE projects SET url=? WHERE id=?',('https://evil.invalid/articles','generic'));self.assertIsNone(site_access.get_secret('generic'))
    store.execute('UPDATE projects SET url=? WHERE id=?',('https://example.test/articles','generic'))
    original=site_access.vault_path().read_bytes()
    with self.assertRaises(RuntimeError):
     with site_access.secret_removal('generic'):raise RuntimeError('synthetic transaction failure')
    self.assertEqual(site_access.vault_path().read_bytes(),original)
    with site_access.secret_removal('generic'):pass
    self.assertIsNone(site_access.get_secret('generic'))
   finally:store.DATA,store.CONFIG,store.SECRETS=before
 def test_03_generic_privacy_masks_raw_encoded_and_json_numeric_values(self):
  secret={**SECRET,'username':'reader@example.test','password':'78123456'};token=site_privacy.bind(secret,CONFIG)
  try:
   data={'reader@example.test':'reader%40example.test','password':78123456}
   clean=json.dumps(site_privacy.redact_payload(data));self.assertNotIn('reader@example.test',clean);self.assertNotIn('reader%40example.test',clean);self.assertNotIn('78123456',clean)
   html=site_privacy.sanitize_html('<form id="generic-login"><input type="text" name="pass" value="78123456"></form><main>reader@example.test</main>');self.assertNotIn('78123456',html);self.assertNotIn('reader@example.test',html);self.assertNotIn('<form',html)
  finally:site_privacy.reset(token)


class GenericBrowserTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  from playwright.async_api import async_playwright
  self.p=await async_playwright().start();self.browser=await self.p.chromium.connect_over_cdp(os.environ.get('BROWSER_CDP_URL','http://172.29.133.20:9223'));self.context=await self.browser.new_context(service_workers='block');self.logs=[]
  self.broker=GenericFormLogin(self.context,ORIGIN+'/auth-test/generic/target',SECRET,CONFIG,self.logs.append,POLICY);await self.context.route('**/*',self.broker.guard)
 async def asyncTearDown(self):
  await self.context.close();await self.browser.close();await self.p.stop()
 async def test_04_real_form_login_ads_cookie_reuse_and_expiry(self):
  self.assertEqual(await self.broker.ensure(),'signed_in');self.assertGreaterEqual(self.logs.count('Werbehinweis geschlossen.'),2);self.assertIn('Cookie-Hinweis bestätigt.',self.logs)
  self.assertIsNone(self.broker.page);self.assertIsNone(self.broker.expected);self.assertEqual(await self.broker.ensure(force=True),'reused')
  await self.context.clear_cookies(name='kiwm_generic_auth_test');second=GenericFormLogin(self.context,ORIGIN+'/auth-test/generic/target',SECRET,CONFIG,self.logs.append,POLICY);await self.context.unroute('**/*',self.broker.guard);await self.context.route('**/*',second.guard);self.assertEqual(await second.ensure(),'signed_in')
  self.assertNotIn(SECRET['username'],' '.join(self.logs));self.assertNotIn(SECRET['password'],' '.join(self.logs))
 async def test_05_wrong_password_fails_once_without_disclosure(self):
  self.broker.secret={**SECRET,'password':'wrong-fake-password'}
  with self.assertRaises(LoginError) as caught:await self.broker.ensure()
  self.assertEqual(self.broker.attempts,1);self.assertNotIn('wrong-fake-password',str(caught.exception));self.assertNotIn(SECRET['username'],str(caught.exception));self.assertIsNone(self.broker.page);self.assertIsNone(self.broker.expected)
  with self.assertRaises(LoginError):await self.broker.ensure(force=True)
  self.assertEqual(self.broker.attempts,1)
 async def test_06_configured_text_password_form_and_exposed_values_block_screenshot(self):
  from engine import safe_screenshot
  token=site_privacy.bind(SECRET,CONFIG)
  try:
   q=await self.context.new_page();await q.goto(ORIGIN+'/auth-test/generic/login');await q.locator('#generic-pass').evaluate("e=>e.type='text'")
   self.assertTrue(await site_privacy.contains_auth_form(q))
   with self.assertRaises(RuntimeError):await safe_screenshot(q)
   await q.set_content('<main>Unexpected '+SECRET['password']+'</main>')
   with self.assertRaises(RuntimeError):await safe_screenshot(q)
  finally:site_privacy.reset(token)
 async def test_07_forged_form_destination_is_rejected_before_fill(self):
  async def forge(route):
   response=await route.fetch();body=(await response.text()).replace('action="/auth-test/generic/login"','action="https://evil.invalid/steal"');await route.fulfill(response=response,body=body)
  await self.context.route('**/auth-test/generic/login',forge)
  # Exercise the forged form itself, independently of redirect interception.
  self.broker.target=CONFIG['login_url']
  with self.assertRaises(LoginError):await self.broker.ensure()
  self.assertEqual(self.broker.attempts,0);self.assertIsNone(self.broker.expected)
 async def test_08_only_exact_form_post_is_permitted_once(self):
  q=await self.context.new_page();await q.goto(ORIGIN+'/auth-test/generic/login');self.broker.page=q;expected={'csrf':['test'],'email':[SECRET['username']],'pass':[SECRET['password']]};self.broker.expected=expected;self.broker.login_until=time.monotonic()+10
  class FakeRequest:
   method='POST';url=CONFIG['submit_url'];post_data=urlencode({k:v[0] for k,v in expected.items()});headers={'content-type':'application/x-www-form-urlencoded'};frame=q.main_frame;resource_type='document'
   def is_navigation_request(self):return True
  request=FakeRequest();request.url='https://evil.invalid/steal';self.assertFalse(await self.broker.permits(request));request.url=CONFIG['submit_url'];request.post_data+='&admin=1';self.assertFalse(await self.broker.permits(request));request.post_data=urlencode({k:v[0] for k,v in expected.items()});self.assertTrue(await self.broker.permits(request));self.assertFalse(await self.broker.permits(request));await q.close();self.broker.page=None


if __name__=='__main__':unittest.main(verbosity=2)
