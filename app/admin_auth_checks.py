"""Real session storage, login/logout, CSRF, expiry and ENV-rotation checks."""
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

scratch = tempfile.TemporaryDirectory(prefix='.admin-check-', dir='/data')
os.environ['DATA_DIR'] = scratch.name
import admin_auth
import web
from fastapi.testclient import TestClient

VALUES = {'KIWM_ADMIN_USERNAME': 'synthetic-admin', 'KIWM_ADMIN_PASSWORD': 'SYNTHETIC-ADMIN-PASSWORD',
          'KIWM_SESSION_HOURS': '2'}


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=scratch.name)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.time = [1700000000]
        self.auth = admin_auth.AdminAuth(self.root, VALUES, lambda: self.time[0])

    def login(self):
        token, error = self.auth.login(VALUES['KIWM_ADMIN_USERNAME'], VALUES['KIWM_ADMIN_PASSWORD'], 'client')
        self.assertIsNone(error)
        return token

    def test_01_missing_config_is_disabled_but_partial_config_fails_closed(self):
        self.assertFalse(admin_auth.AdminAuth(self.root, {}).enabled)
        auth = admin_auth.AdminAuth(self.root, {'KIWM_ADMIN_USERNAME': 'admin'})
        self.assertTrue(auth.enabled)
        self.assertTrue(auth.error)
        self.assertFalse(auth.authorized('x' * 43))

    def test_02_password_and_duration_validation(self):
        for override in ({'KIWM_ADMIN_PASSWORD': 'short'}, {'KIWM_SESSION_HOURS': '0'}, {'KIWM_SESSION_HOURS': '721'}):
            self.assertTrue(admin_auth.AdminAuth(self.root, {**VALUES, **override}).error)

    def test_03_valid_login_and_wrong_credentials(self):
        self.assertTrue(self.auth.authorized(self.login()))
        self.assertEqual(self.auth.login('wrong-user', VALUES['KIWM_ADMIN_PASSWORD'], 'client')[1], 'credentials')
        self.assertEqual(self.auth.login(VALUES['KIWM_ADMIN_USERNAME'], 'wrong-password', 'client')[1], 'credentials')

    def test_04_database_does_not_contain_plain_password_or_session(self):
        token = self.login()
        raw = (self.root / 'auth/sessions.db').read_bytes()
        self.assertNotIn(token.encode(), raw)
        self.assertNotIn(VALUES['KIWM_ADMIN_PASSWORD'].encode(), raw)
        self.assertEqual((self.root / 'auth/session.key').stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / 'auth/sessions.db').stat().st_mode & 0o777, 0o600)

    def test_05_session_survives_restart(self):
        token = self.login()
        restarted = admin_auth.AdminAuth(self.root, VALUES, lambda: self.time[0])
        self.assertTrue(restarted.authorized(token))

    def test_06_session_expires(self):
        token = self.login()
        self.time[0] += 7200
        self.assertFalse(self.auth.authorized(token))

    def test_07_logout_revokes_copied_session(self):
        token = self.login()
        self.auth.logout(token)
        self.assertFalse(self.auth.authorized(token))

    def test_08_env_password_or_username_change_revokes_old_sessions(self):
        for change in ({'KIWM_ADMIN_PASSWORD': 'SYNTHETIC-NEW-PASSWORD'}, {'KIWM_ADMIN_USERNAME': 'new-admin'}):
            token = self.login()
            changed = admin_auth.AdminAuth(self.root, {**VALUES, **change}, lambda: self.time[0])
            self.assertFalse(changed.authorized(token))

    def test_09_login_rotates_existing_session(self):
        old = self.login()
        new, _ = self.auth.login(VALUES['KIWM_ADMIN_USERNAME'], VALUES['KIWM_ADMIN_PASSWORD'], 'client', old)
        self.assertNotEqual(old, new)
        self.assertFalse(self.auth.authorized(old))
        self.assertTrue(self.auth.authorized(new))

    def test_10_bruteforce_limit_and_recovery(self):
        for _ in range(5):
            self.assertEqual(self.auth.login('bad', 'bad', 'limited')[1], 'credentials')
        self.assertEqual(self.auth.login('bad', 'bad', 'limited')[1], 'rate')
        self.time[0] += 61
        self.assertEqual(self.auth.login('bad', 'bad', 'limited')[1], 'credentials')

    def test_11_malformed_tokens_never_authorize(self):
        self.login()
        for value in ('', None, 'x' * 42, 'x' * 44, 'x' * 42 + '/', "' OR 1=1 --"):
            self.assertFalse(self.auth.authorized(value))

    def test_12_redirect_validation(self):
        for value in ('//evil.example', '/%2Fevil.example', '/%252Fevil.example', '/\\evil.example',
                      'https://evil.example', '/%0aLocation:evil', '/login', '/logout'):
            self.assertEqual(admin_auth.safe_next(value), '/')
        self.assertEqual(admin_auth.safe_next('/projects/example?view=history'), '/projects/example?view=history')

    def test_13_session_count_is_bounded(self):
        for _ in range(35):
            self.login()
        with sqlite3.connect(self.root / 'auth/sessions.db') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0], 32)


class LoginRoutes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=scratch.name)
        self.addCleanup(self.temp.cleanup)
        self.auth = admin_auth.AdminAuth(Path(self.temp.name), VALUES)
        patcher = patch('admin_auth.current', return_value=self.auth)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(web.app, base_url='http://localhost:8765', follow_redirects=False)
        self.addCleanup(self.client.close)
        self.client.get('/login')

    def post(self, **values):
        data = {'username': VALUES['KIWM_ADMIN_USERNAME'], 'password': VALUES['KIWM_ADMIN_PASSWORD'],
                'csrf': self.client.cookies['kiwm_csrf'], 'next': '/browser', **values}
        return self.client.post('/login', data=data, headers={'Origin': 'http://localhost:8765'})

    def test_14_anonymous_page_redirects_to_login_without_browser_challenge(self):
        response = self.client.get('/projects/example')
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers['location'].startswith('/login?next='))
        self.assertNotIn('www-authenticate', response.headers)

    def test_15_anonymous_api_is_401(self):
        self.assertEqual(self.client.get('/api/project-status').status_code, 401)

    def test_16_login_page_is_accessible_and_has_password_manager_fields(self):
        response = self.client.get('/login')
        self.assertEqual(response.status_code, 200)
        for value in ('autocomplete="username"', 'autocomplete="current-password"', 'autocapitalize="none"'):
            self.assertIn(value, response.text)

    def test_17_success_sets_opaque_http_only_cookie_and_rotates_csrf(self):
        before = self.client.cookies['kiwm_csrf']
        response = self.post()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/browser')
        self.assertIn('HttpOnly', response.headers.get_list('set-cookie')[0])
        self.assertIn('SameSite=lax', response.headers.get_list('set-cookie')[0])
        self.assertNotEqual(before, self.client.cookies['kiwm_csrf'])
        self.assertEqual(self.client.get('/auth/check').status_code, 204)

    def test_18_wrong_password_is_friendly_and_not_echoed(self):
        response = self.post(password='SYNTHETIC-WRONG-PASSWORD')
        self.assertEqual(response.status_code, 401)
        self.assertIn('Benutzername oder Passwort', response.text)
        self.assertNotIn('SYNTHETIC-WRONG-PASSWORD', response.text)
        self.assertNotIn('www-authenticate', response.headers)

    def test_19_login_requires_csrf(self):
        self.assertEqual(self.post(csrf='incorrect-token').status_code, 403)

    def test_20_foreign_login_origin_is_rejected(self):
        response = self.client.post('/login', data={'csrf': self.client.cookies['kiwm_csrf']}, headers={'Origin': 'https://evil.example'})
        self.assertEqual(response.status_code, 403)

    def test_21_logout_revokes_session_and_requires_csrf(self):
        self.post()
        token = self.client.cookies[admin_auth.COOKIE]
        self.assertEqual(self.client.post('/logout').status_code, 403)
        response = self.client.post('/logout', data={'csrf': self.client.cookies['kiwm_csrf']})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/login')
        self.assertFalse(self.auth.authorized(token))
        self.assertEqual(self.client.get('/auth/check').status_code, 401)

    def test_22_safe_return_path(self):
        self.assertEqual(self.post(next='//evil.example').headers['location'], '/')

    def test_23_basic_header_does_not_bypass_form_authentication(self):
        response = self.client.get('/auth/check', headers={'Authorization': 'Basic c3ludGhldGljOmNyZWRlbnRpYWxz'})
        self.assertEqual(response.status_code, 401)

    def test_24_static_login_assets_remain_public(self):
        for path in ('/static/login.css', '/static/login.js', '/static/app-logo.png', '/favicon.ico'):
            self.assertEqual(self.client.get(path).status_code, 200)
        self.client.cookies.clear()
        for path in ('/favicon.ico', '/static/favicon.ico?v=old'):
            for method in ('GET', 'HEAD'):
                with self.subTest(path=path, method=method):
                    response = self.client.request(method, path)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers['content-type'], 'image/vnd.microsoft.icon')
                    self.assertEqual(response.headers['cache-control'], 'public, max-age=86400')
                    self.assertNotIn('set-cookie', response.headers)
                    self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
        login = self.client.get('/login')
        self.assertIn('href="/favicon.ico"', login.text)
        self.assertEqual(login.headers['cache-control'], 'no-store')
        self.assertEqual(self.client.get('/static/login.css').headers['cache-control'], 'no-store')

    def test_25_missing_admin_env_keeps_lan_closed(self):
        disabled = admin_auth.AdminAuth(Path(self.temp.name), {})
        # Keep this authority in TrustedHostMiddleware's existing allowlist;
        # host validation itself is covered independently by lan_access_checks.
        with patch('admin_auth.current', return_value=disabled), patch('web.LAN_URL', 'http://localhost:8765'):
            client = TestClient(web.app, base_url='http://localhost:8765', follow_redirects=False)
            try:
                self.assertEqual(client.get('/').status_code, 503)
                self.assertEqual(client.get('/auth/check').status_code, 401)
            finally:
                client.close()
