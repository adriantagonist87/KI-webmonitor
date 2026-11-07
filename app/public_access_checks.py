"""Explicit external HTTPS origin with isolated synthetic authentication."""
import os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlunsplit
os.environ['KIWM_PUBLIC_URL']='https://webmonitor.example.org'
os.environ['KIWM_LAN_URL']='http://192.168.50.10:8765'
os.environ['KIWM_ADMIN_USERNAME']='synthetic-admin'
os.environ['KIWM_ADMIN_PASSWORD']='SYNTHETIC-PUBLIC-PASSWORD'
scratch=tempfile.TemporaryDirectory(prefix='.public-check-',dir='/data')
os.environ['DATA_DIR']=scratch.name
import store,web,lan_access,admin_auth
from fastapi.testclient import TestClient
from fastapi import Request
store.init()

@web.app.api_route('/__public-check',methods=['GET','POST'])
async def probe(request:Request):
    return {'url':str(request.url),'browser':lan_access.browser_url(request,web.LAN_URL,'http://localhost:8766',web.PUBLIC_URL)}

class PublicAccess(unittest.TestCase):
    def client(self,origin=None):
        c=TestClient(web.app,base_url=origin or web.PUBLIC_URL,follow_redirects=False)
        self.addCleanup(c.close)
        return c

    def login(self,c):
        self.assertEqual(c.get('/login').status_code,200)
        r=c.post('/login',data={'username':os.environ['KIWM_ADMIN_USERNAME'],'password':os.environ['KIWM_ADMIN_PASSWORD'],'csrf':c.cookies['kiwm_csrf']},headers={'Origin':str(c.base_url).rstrip('/')})
        self.assertEqual(r.status_code,303)

    def test_valid_explicit_public_origin(self):
        self.assertIsNone(lan_access.public_origin(''))
        self.assertEqual(lan_access.public_origin(web.PUBLIC_URL+'/'),web.PUBLIC_URL)

    def test_invalid_public_origins_fail_closed(self):
        invalid=['http://webmonitor.example.org','https://localhost','https://192.168.50.10','https://8.8.8.8','https://host.local','https://host.example:8443','https://host.example/path','https://host.example?x=y','https://host.example#x','https://*.example.org','https://bad_.example.org','https://Webmonitor.example.org','https://host.example\nInjected: yes',urlunsplit(('https','synthetic:synthetic@host.example','','',''))]
        for url in invalid:
            with self.subTest(url=url),self.assertRaises(ValueError):lan_access.public_origin(url)

    def test_public_login_secure_cookies_and_same_origin_browser(self):
        c=self.client();self.login(c)
        self.assertEqual(c.get('/').status_code,200)
        self.assertEqual(c.get('/auth/check').status_code,204)
        self.assertIn('/browser/vnc.html?',c.get('/__public-check').json()['browser'])
        for cookie in c.cookies.jar:
            self.assertTrue(cookie.secure);self.assertIn('HttpOnly',cookie._rest)
        self.assertNotIn('localhost:8766',c.get('/browser').text)

    def test_public_post_rejects_wrong_scheme_and_foreign_origin(self):
        c=self.client();self.login(c)
        for origin in ('http://webmonitor.example.org','https://attacker.example','null'):
            self.assertEqual(c.post('/__public-check',data={'csrf':c.cookies['kiwm_csrf']},headers={'Origin':origin}).status_code,403)
        self.assertEqual(c.post('/__public-check',data={'csrf':c.cookies['kiwm_csrf']},headers={'Origin':web.PUBLIC_URL}).status_code,200)
        self.assertEqual(c.post('/__public-check',headers={'Origin':web.PUBLIC_URL}).status_code,403)

    def test_external_host_is_exact_and_forwarded_headers_not_trusted(self):
        c=self.client()
        for host in ('attacker.example','webmonitor.example.org:443','webmonitor.example.org:8765'):
            self.assertEqual(c.get('/login',headers={'Host':host,'X-Forwarded-Host':'webmonitor.example.org'}).status_code,400)

    def test_proxy_http_is_reconstructed_as_configured_https(self):
        c=self.client();self.login(c)
        cookie='; '.join(x.name+'='+x.value for x in c.cookies.jar)
        r=c.get('http://webmonitor.example.org/__public-check',headers={'Cookie':cookie,'X-Forwarded-Proto':'http'})
        self.assertEqual(r.status_code,200);self.assertTrue(r.json()['url'].startswith(web.PUBLIC_URL+'/'))

    def test_missing_admin_keeps_public_origin_closed(self):
        with patch('admin_auth.current',return_value=admin_auth.AdminAuth(Path(scratch.name),{})):
            c=self.client();self.assertEqual(c.get('/').status_code,503)
            self.assertEqual(c.get('/auth/check').status_code,401)

    def test_lan_and_ssh_access_still_use_http(self):
        for origin,remote in ((web.LAN_URL,True),('http://localhost:8765',False)):
            c=self.client(origin);self.login(c)
            r=c.get('/__public-check',headers={'X-Forwarded-Host':'webmonitor.example.org','X-Forwarded-Proto':'https'})
            self.assertEqual(r.status_code,200);self.assertTrue(r.json()['url'].startswith(origin+'/'))
            self.assertEqual(r.json()['browser'].startswith('/browser/'),remote)
            self.assertFalse(any(cookie.secure for cookie in c.cookies.jar))

    def test_public_logout_revokes_session(self):
        c=self.client();self.login(c)
        self.assertEqual(c.post('/logout',data={'csrf':c.cookies['kiwm_csrf']},headers={'Origin':web.PUBLIC_URL}).status_code,303)
        self.assertEqual(c.get('/').status_code,303)

if __name__=='__main__':unittest.main()
