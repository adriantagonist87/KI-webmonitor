"""LAN origin, secure-cookie and CSRF checks with no live archive or scheduler."""
import os
import tempfile
import unittest
from urllib.parse import urlunsplit

os.environ['KIWM_LAN_URL'] = 'https://192.168.50.10:18443'
scratch = tempfile.TemporaryDirectory(prefix='.lan-check-', dir='/data')
os.environ['DATA_DIR'] = scratch.name
os.environ['KIWM_ADMIN_USERNAME'] = 'synthetic-admin'
os.environ['KIWM_ADMIN_PASSWORD'] = 'SYNTHETIC-LAN-PASSWORD'
import lan_access
import store
import web
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from fastapi import Request


@web.app.api_route('/__lan-check', methods=['GET', 'POST'])
async def probe(request: Request):
    return {'browser': lan_access.browser_url(request, web.LAN_URL, 'http://localhost:8766')}


class LanAccess(unittest.TestCase):
    def test_private_http_or_https_origin_only(self):
        for value in ('ftp://192.168.50.10:8443', 'https://8.8.8.8:8443',
                      'https://127.0.0.1:8443', 'https://host.example:8443',
                      'https://192.168.50.10', 'https://192.168.50.10:443',
                      urlunsplit(('https', 'synthetic-user:synthetic-password@192.168.50.10:8443', '', '', '')),
                      'https://192.168.50.10:8443/path', 'https://192.168.50.10:8443?x=1',
                      'https://192.168.50.10:8443#fragment', 'https://192.168.50.10:8443\nInjected: yes'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                lan_access.lan_origin(value)

    def test_disabled_origin_preserves_tunnel(self):
        self.assertIsNone(lan_access.lan_origin(''))
        self.assertEqual(lan_access.lan_origin('https://10.1.2.3:8443/'), 'https://10.1.2.3:8443')
        self.assertEqual(lan_access.lan_origin('http://192.168.50.10:8765/'), 'http://192.168.50.10:8765')

    def test_lan_browser_uses_same_origin_and_secure_cookie(self):
        client = self.client()
        response = client.get('/__lan-check')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['browser'].startswith('/browser/vnc.html?'))
        self.assertIn('&path=browser/websockify', response.json()['browser'])
        cookie = next(c for c in client.cookies.jar if c.name == 'kiwm_csrf')
        self.assertTrue(cookie.secure)
        self.assertIn('HttpOnly', cookie._rest)
        self.assertEqual(cookie._rest['SameSite'], 'strict')

    def test_tunnel_browser_url_and_cookie_unchanged(self):
        client = self.client('http://localhost:8765')
        response = client.get('/__lan-check')
        self.assertTrue(response.json()['browser'].startswith('http://localhost:8766/'))
        self.assertFalse(next(c for c in client.cookies.jar if c.name == 'kiwm_csrf').secure)

    def test_http_lan_cookie_and_browser_link(self):
        from unittest.mock import patch
        client = self.client('http://192.168.50.10:8765')
        with patch('web.LAN_URL', 'http://192.168.50.10:8765'):
            response = client.get('/__lan-check')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(next(c for c in client.cookies.jar if c.name == 'kiwm_csrf').secure)
        self.assertIn('/browser/vnc.html?', response.json()['browser'])

    def test_macro_picker_uses_browser_page_outside_novnc_namespace(self):
        client = self.client()
        store.init()
        pid = 'lan-macro-project'
        store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created) VALUES(?,?,?,?,?,?,0,?,?)',
                      (pid, 'LAN test project', 'https://example.org/', 'Inspect page', 'example.org',
                       '0 6 * * *', store.now(), store.now()))
        self.addCleanup(store.execute, 'DELETE FROM projects WHERE id=?', (pid,))
        page = BeautifulSoup(client.get('/browser').text, 'html.parser')
        picker = page.select_one('#macro-project')
        self.assertEqual(picker.find_parent('form')['action'], '/browser')
        response = client.get('/browser', params={picker['name']: pid})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], f'/projects/{pid}/macros')
        self.assertIn('Aufnahme starten', client.get(response.headers['location']).text)

    def test_unknown_host_and_forwarded_host_not_trusted(self):
        client = self.client()
        response = client.get('/__lan-check', headers={'Host': 'attacker.example',
                                                      'X-Forwarded-Host': '192.168.50.10:18443'})
        self.assertEqual(response.status_code, 400)

    def test_same_origin_post_requires_csrf(self):
        client = self.client()
        client.get('/__lan-check')
        self.assertEqual(client.post('/__lan-check', headers={'Origin': web.LAN_URL}).status_code, 403)
        response = client.post('/__lan-check', data={'csrf': client.cookies['kiwm_csrf']},
                               headers={'Origin': web.LAN_URL})
        self.assertEqual(response.status_code, 200)

    def test_foreign_origin_post_rejected_even_with_valid_csrf(self):
        client = self.client()
        client.get('/__lan-check')
        response = client.post('/__lan-check', data={'csrf': client.cookies['kiwm_csrf']},
                               headers={'Origin': 'https://attacker.example'})
        self.assertEqual(response.status_code, 403)

    def test_different_port_does_not_select_browser_proxy(self):
        client = self.client('https://192.168.50.10:8444')
        self.assertTrue(client.get('/__lan-check').json()['browser'].startswith('http://localhost:8766/'))

    def test_raw_authority_must_match(self):
        from starlette.requests import Request
        request = Request({'type': 'http', 'headers': [(b'host', b'192.168.50.10:18443.attacker')]})
        self.assertFalse(lan_access.is_lan_request(request, web.LAN_URL))

    def client(self, base_url=None):
        # Do not enter TestClient's lifespan: never start a scheduler in checks.
        client = TestClient(web.app, base_url=base_url or web.LAN_URL, follow_redirects=False)
        self.addCleanup(client.close)
        client.get('/login')
        response = client.post('/login', data={'username':'synthetic-admin', 'password':'SYNTHETIC-LAN-PASSWORD', 'csrf':client.cookies['kiwm_csrf']}, headers={'Origin':base_url or web.LAN_URL})
        self.assertEqual(response.status_code, 303)
        return client
