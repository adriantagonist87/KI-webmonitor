"""ENV overrides stay read-only and never enter saved UI files or exports.

Run in the app image. Synthetic values only; no external services or scheduler.
"""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

scratch = tempfile.TemporaryDirectory(prefix='.configuration-ui-', dir='/data')
os.environ['DATA_DIR'] = scratch.name
import configuration
import store
store.CONFIG = Path(scratch.name) / 'settings.json'
store.SECRETS = Path(scratch.name) / 'credentials.json'
store.init()
import engine
import retention
import web
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient


class ConfigurationUI(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=scratch.name)
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        store.DATA = web.DATA = engine.DATA = root
        store.CONFIG = web.CONFIG = root / 'settings.json'
        store.SECRETS = web.SECRETS = root / 'credentials.json'
        store.init()
        self.saved = {'omlx_url': 'http://saved-model.test:8000/v1', 'agent_model': 'Saved Agent',
                      'analysis_model': 'Saved Analysis', 'vision_model': 'Saved Vision',
                      'smtp_host': 'saved-mail.test', 'smtp_port': 587, 'smtp_enabled': False}
        self.saved_secrets = {'omlx_key': 'SYNTHETIC-SAVED-TOKEN', 'smtp_password': 'SYNTHETIC-SMTP'}
        store.save_json(store.CONFIG, self.saved)
        store.save_json(store.SECRETS, self.saved_secrets)
        clean = patch.dict(os.environ, dict.fromkeys(configuration.ENVIRONMENT_NAMES, ''))
        clean.start()
        self.addCleanup(clean.stop)
        self.client = TestClient(web.app)
        self.addCleanup(self.client.close)
        self.client.get('/')
        self.csrf = self.client.cookies['kiwm_csrf']

    def overrides(self, **extra):
        return patch.dict(os.environ, {'KIWM_OMLX_URL': 'http://environment-model.test:9000/v1',
                                     'KIWM_OMLX_API_KEY': 'SYNTHETIC-ENV-TOKEN-NEVER-SAVED',
                                     'KIWM_AGENT_MODEL': 'Environment Agent', **extra})

    def post(self, url, **fields):
        return self.client.post(url, data={'csrf': self.csrf, **fields}, follow_redirects=False)

    def assert_environment_not_persisted(self):
        raw = store.CONFIG.read_bytes() + store.SECRETS.read_bytes()
        for value in (b'environment-model.test', b'SYNTHETIC-ENV-TOKEN-NEVER-SAVED',
                      b'Environment Agent', b'Environment Fallback'):
            self.assertNotIn(value, raw)

    def test_01_source_labels_readonly_values_and_token_redaction(self):
        with self.overrides(), patch.object(retention, 'inspect', return_value={}):
            response = self.client.get('/settings')
        self.assertEqual(response.status_code, 200)
        page = BeautifulSoup(response.text, 'html.parser')
        for field in ('omlx_url', 'omlx_key', 'agent_model'):
            control = page.select_one('[name="' + field + '"]')
            self.assertTrue(control.has_attr('readonly'))
            self.assertEqual(control['data-config-source'], 'environment')
        for field in ('analysis_model', 'vision_model'):
            self.assertFalse(page.select_one('[name="' + field + '"]').has_attr('readonly'))
        self.assertEqual(page.select_one('[name=omlx_url]')['value'], 'http://environment-model.test:9000/v1')
        self.assertFalse(page.select_one('[name=omlx_key]').get('value'))
        self.assertTrue(page.select_one('[name=clear_key]').has_attr('disabled'))
        self.assertIsNotNone(page.select_one('[data-env-configuration]'))
        for variable in ('KIWM_OMLX_URL', 'KIWM_OMLX_API_KEY', 'KIWM_AGENT_MODEL'):
            self.assertIn(variable, page.get_text())
        self.assertNotIn('SYNTHETIC-ENV-TOKEN-NEVER-SAVED', response.text)
        self.assertNotIn('SYNTHETIC-SAVED-TOKEN', response.text)

    def test_02_forged_locked_fields_are_ignored_while_ui_fields_save(self):
        with self.overrides():
            response = self.post('/settings/omlx', omlx_url='https://forged.test',
                                 agent_model='Forged Agent', analysis_model='New Analysis',
                                 vision_model='New Vision', omlx_key='FORGED-TOKEN', clear_key='on')
            self.assertEqual(response.status_code, 303)
            self.assertEqual(store.settings()['agent_model'], 'Environment Agent')
            self.assertEqual(store.credentials()['omlx_key'], 'SYNTHETIC-ENV-TOKEN-NEVER-SAVED')
        self.assertEqual(store.persisted_settings(), {**self.saved, 'analysis_model': 'New Analysis',
                                                     'vision_model': 'New Vision'})
        self.assertEqual(store.persisted_credentials(), self.saved_secrets)
        self.assert_environment_not_persisted()

    def test_03_other_settings_writes_do_not_copy_effective_environment_values(self):
        with self.overrides(KIWM_MODEL='Environment Fallback'):
            for url, fields in (
                    ('/settings/storage', {'document_versions': '2', 'detail_days': '30', 'cache_days': '7'}),
                    ('/settings/schedule', {'schedule_mode': 'daily', 'schedule_time': '07:45'}),
                    ('/settings/smtp', {'smtp_host': 'new-mail.test', 'smtp_port': '2525',
                                        'smtp_tls': 'starttls', 'smtp_password': 'SYNTHETIC-NEW-SMTP'})):
                with self.subTest(url=url):
                    self.assertEqual(self.post(url, **fields).status_code, 303)
                    self.assert_environment_not_persisted()
                    saved = store.persisted_settings()
                    for field in ('omlx_url', 'agent_model', 'analysis_model', 'vision_model'):
                        self.assertEqual(saved[field], self.saved[field])
                    self.assertEqual(store.persisted_credentials()['omlx_key'], self.saved_secrets['omlx_key'])

    def test_04_discovery_only_fills_uncontrolled_empty_roles(self):
        store.save_json(store.CONFIG, {**self.saved, 'analysis_model': '', 'vision_model': ''})
        original_save = store.save_json
        cache = {}

        def save(target, value):
            if target in (store.CONFIG, store.SECRETS):
                return original_save(target, value)
            cache[target.name] = value

        with self.overrides(), patch.object(engine, 'discover_models', new=AsyncMock(
                return_value=[{'id': 'Discovered Model'}])), patch.object(web, 'save_json', side_effect=save):
            self.assertEqual(self.post('/settings/models').status_code, 303)
        saved = store.persisted_settings()
        self.assertEqual(saved['agent_model'], 'Saved Agent')
        self.assertEqual(saved['analysis_model'], 'Discovered Model')
        self.assertEqual(saved['vision_model'], 'Discovered Model')
        self.assertIn('models.json', cache)
        self.assert_environment_not_persisted()

    def test_05_invalid_ui_configuration_is_atomic_and_errors_do_not_echo_values(self):
        before = (store.CONFIG.read_bytes(), store.SECRETS.read_bytes())
        response = self.post('/settings/omlx', omlx_url='https://user:password@example.org/v1',
                             agent_model='New Agent', analysis_model='New Analysis', vision_model='New Vision',
                             omlx_key='SYNTHETIC NEW TOKEN')
        self.assertEqual(response.status_code, 400)
        for private in ('FAKE-PASSWORD', 'fake-user', 'SYNTHETIC NEW TOKEN'):
            self.assertNotIn(private, response.text)
        self.assertEqual((store.CONFIG.read_bytes(), store.SECRETS.read_bytes()), before)

    def test_06_without_environment_fields_are_editable_and_saved(self):
        with patch.object(retention, 'inspect', return_value={}):
            page = BeautifulSoup(self.client.get('/settings').text, 'html.parser')
        for field in ('omlx_url', 'omlx_key', 'agent_model', 'analysis_model', 'vision_model'):
            self.assertFalse(page.select_one('[name="' + field + '"]').has_attr('readonly'))
        self.assertIsNone(page.select_one('[data-env-configuration]'))
        response = self.post('/settings/omlx', omlx_url='http://new-model.test:8000/v1',
                             agent_model='New Agent', analysis_model='New Analysis', vision_model='New Vision',
                             omlx_key='SYNTHETIC-NEW-TOKEN')
        self.assertEqual(response.status_code, 303)
        self.assertEqual(store.persisted_settings()['agent_model'], 'New Agent')
        self.assertEqual(store.persisted_credentials()['omlx_key'], 'SYNTHETIC-NEW-TOKEN')

    def test_07_common_model_override_labels_all_roles_without_materializing_it(self):
        with patch.dict(os.environ, {'KIWM_MODEL': 'Environment Fallback'}), \
             patch.object(retention, 'inspect', return_value={}):
            page = BeautifulSoup(self.client.get('/settings').text, 'html.parser')
            for field in configuration.MODEL_FIELDS:
                control = page.select_one('[name="' + field + '"]')
                self.assertTrue(control.has_attr('readonly'))
                self.assertEqual(control['value'], 'Environment Fallback')
                self.assertIn('KIWM_MODEL', control.parent.get_text())
            self.assertEqual(self.post('/settings/omlx', omlx_url=self.saved['omlx_url']).status_code, 303)
        self.assertEqual(store.persisted_settings(), self.saved)
        self.assert_environment_not_persisted()


if __name__ == '__main__':
    unittest.main(verbosity=2)
