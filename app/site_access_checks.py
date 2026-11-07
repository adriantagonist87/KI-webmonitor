"""Isolated origin-bound access storage and HTTP checks; only fake credentials."""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

scratch = tempfile.TemporaryDirectory(prefix='.site-access-check-', dir='/data')
os.environ['DATA_DIR'] = scratch.name
import store
store.init()
import web
import site_access
from fastapi.testclient import TestClient
from bs4 import BeautifulSoup

if not any(r.path == '/projects/{pid}/access' for r in web.app.routes):
    site_access.register(web.app, web.render, web.redirect)

NUMBER = '812345679'
POSTCODE = '54321'
SECOND = '987654321'


class SiteAccessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=scratch.name)
        self.root = Path(self.directory.name)
        self.original = (store.DATA, store.CONFIG, store.SECRETS, web.DATA, web.CONFIG, web.SECRETS)
        store.DATA = web.DATA = self.root
        store.CONFIG = web.CONFIG = self.root / 'config/settings.json'
        store.SECRETS = web.SECRETS = self.root / 'secrets/credentials.json'
        store.init()
        store.save_json(store.CONFIG, {'agent_model': 'local-test'})
        store.save_json(store.SECRETS, {'omlx_key': 'FAKE-API-KEEP'})
        for pid, url in [('access-one', 'https://www.elektro.net/praxisprobleme/'),
                         ('access-two', 'https://www.elektro.net/praxisprobleme/#answer'),
                         ('other', 'https://example.org')]:
            store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created) VALUES(?,?,?,?,?,?,?,?,?)',
                          (pid, 'Alias ' + pid, url, 'Read titles only', url.split('/')[2], '0 6 * * *', 0,
                           store.next_due('0 6 * * *'), store.now()))
        self.client = TestClient(web.app)
        self.client.get('/projects/access-one/access')
        self.csrf = self.client.cookies['kiwm_csrf']

    def tearDown(self):
        store.DATA, store.CONFIG, store.SECRETS, web.DATA, web.CONFIG, web.SECRETS = self.original
        self.directory.cleanup()

    def post(self, pid='access-one', **extra):
        data = {'csrf': self.csrf, 'enabled': 'on', 'abonummer': NUMBER, 'postcode': POSTCODE}
        data.update(extra)
        return self.client.post('/projects/' + pid + '/access', data=data)

    def test_01_private_storage_metadata_and_no_secret_echo(self):
        response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(NUMBER, response.text)
        self.assertNotIn(POSTCODE, response.text)
        self.assertEqual(site_access.status('access-one'), {'supported': True, 'configured': True, 'enabled': True})
        self.assertEqual(site_access.get_secret('access-one'), {'provider': site_access.PROVIDER,
                         'origin': site_access.ORIGIN, 'abonummer': NUMBER, 'postcode': POSTCODE})
        if os.name == 'posix':
            self.assertEqual(site_access.vault_path().stat().st_mode & 0o777, 0o600)
        config = site_access.public_config('access-one')
        self.assertEqual(set(config), {'provider', 'origin', 'enabled'})
        state = json.dumps(store.rows('SELECT * FROM state'))
        projects = json.dumps(store.rows('SELECT * FROM projects'))
        for serialized in (state, projects):
            self.assertNotIn(NUMBER, serialized)
            self.assertNotIn(POSTCODE, serialized)
        self.assertEqual(store.credentials(), {'omlx_key': 'FAKE-API-KEEP'})

    def test_02_empty_preserves_and_partial_update_is_bound(self):
        self.post()
        response = self.post(abonummer='', postcode='', enabled='')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(site_access.public_config('access-one')['enabled'])
        self.assertEqual(site_access.get_secret('access-one')['abonummer'], NUMBER)
        self.assertEqual(self.post(abonummer=SECOND, postcode='').status_code, 200)
        self.assertEqual(site_access.get_secret('access-one')['abonummer'], SECOND)
        self.assertEqual(site_access.get_secret('access-one')['postcode'], POSTCODE)

    def test_03_validation_never_echoes_or_persists_invalid_values(self):
        for extra in ({'abonummer': 'SECRET-INVALID'}, {'postcode': 'BAD-PC'},
                      {'abonummer': '１２３４５６７８９'}, {'abonummer': '', 'postcode': ''}):
            response = self.post(**extra)
            self.assertEqual(response.status_code, 400, response.text)
            self.assertNotIn(extra.get('abonummer', 'SECRET-INVALID') or 'SECRET-INVALID', response.text)
            self.assertNotIn('BAD-PC', response.text)
            self.assertIsNone(site_access.public_config('access-one'))
            self.assertFalse(site_access.vault_path().exists())

    def test_04_origin_change_and_tampered_binding_fail_closed(self):
        self.post()
        store.execute('UPDATE projects SET url=? WHERE id=?', ('https://other.example/praxisprobleme/', 'access-one'))
        self.assertIsNone(site_access.get_secret('access-one'))
        self.assertIsNone(site_access.public_config('access-one'))
        self.assertTrue(site_access.status('access-one')['supported'])
        self.assertEqual(self.post().status_code, 400)
        store.execute('UPDATE projects SET url=? WHERE id=?', ('https://www.elektro.net/praxisprobleme/', 'access-one'))
        for origin in ('http://www.elektro.net', 'https://evil.example'):
            vault = json.loads(site_access.vault_path().read_text())
            vault['logins']['access-one']['origin'] = origin
            store.save_json(site_access.vault_path(), vault)
            self.assertIsNone(site_access.get_secret('access-one'))

    def test_05_clear_one_preserves_other_project_and_deactivates(self):
        self.post()
        self.assertEqual(self.post('access-two', abonummer=SECOND).status_code, 200)
        response = self.post(clear='on')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(site_access.get_secret('access-one'))
        self.assertFalse(site_access.public_config('access-one')['enabled'])
        self.assertEqual(site_access.get_secret('access-two')['abonummer'], SECOND)
        self.assertNotIn(NUMBER, site_access.vault_path().read_text())

    def test_06_protected_forms_and_no_existing_values_in_inputs(self):
        self.assertEqual(self.post(csrf='bad').status_code, 403)
        response = self.client.post('/projects/access-one/access',
            data={'csrf': self.csrf, 'enabled': 'on', 'abonummer': NUMBER, 'postcode': POSTCODE},
            headers={'Origin': 'http://evil.invalid'})
        self.assertEqual(response.status_code, 403)
        self.post()
        page = BeautifulSoup(self.client.get('/projects/access-one/access').text, 'html.parser')
        for name in ('abonummer', 'postcode'):
            field = page.select_one('input[name=' + name + ']')
            self.assertEqual(field['type'], 'password')
            self.assertEqual(field['autocomplete'], 'off')
            self.assertNotIn('value', field.attrs)
        self.assertIsNotNone(page.select_one('input[name=clear]'))
        self.assertEqual(self.client.get('/projects/missing/access').status_code, 404)
        self.assertIsNone(site_access.get_secret('../access-one'))

    def test_07_only_https_projects_support_credentials_and_legacy_stays_exact(self):
        for url in ('http://www.elektro.net/praxisprobleme/', 'https://elektro.net/praxisprobleme/',
                    'https://www.elektro.net:444/praxisprobleme/', 'https://www.elektro.net.evil.invalid/',
                    'https://person@www.elektro.net/praxisprobleme/'):
            store.execute('UPDATE projects SET url=? WHERE id=?', (url, 'access-one'))
            self.assertEqual(site_access.status('access-one')['supported'],url.startswith('https://') and 'person@' not in url)
            self.assertEqual(self.post().status_code, 400)
            self.assertFalse(site_access.vault_path().exists())
        self.assertEqual(self.post('other').status_code, 400)

    def test_08_failed_metadata_write_restores_previous_vault(self):
        self.post()
        original = site_access.vault_path().read_bytes()
        with patch('store.execute', side_effect=sqlite3.OperationalError('simulated write failure')):
            with self.assertRaises(sqlite3.OperationalError):
                site_access.save('access-one', True, SECOND, POSTCODE)
        self.assertEqual(json.loads(site_access.vault_path().read_bytes()), json.loads(original))
        self.assertEqual(site_access.get_secret('access-one')['abonummer'], NUMBER)

    def test_09_corrupt_vault_is_not_overwritten_or_echoed(self):
        path = site_access.vault_path()
        path.write_text('{"secret":"BROKEN-STORAGE-SENTINEL"')
        if os.name == 'posix':
            path.chmod(0o600)
        response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('BROKEN-STORAGE-SENTINEL', response.text)
        self.assertIn('BROKEN-STORAGE-SENTINEL', path.read_text())

    def test_10_links_available_on_custom_https_projects(self):
        page = BeautifulSoup(self.client.get('/projects/access-one/edit').text, 'html.parser')
        self.assertIsNotNone(page.select_one('a[href="/projects/access-one/access"]'))
        page = BeautifulSoup(self.client.get('/projects/other/edit').text, 'html.parser')
        self.assertIsNotNone(page.select_one('a[href="/projects/other/access"]'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
