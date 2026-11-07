"""Local configuration resolution checks: synthetic values, no network/model calls."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import configuration


UI_SETTINGS = {'omlx_url': 'http://inference.test:8000/v1', 'agent_model': 'UI-Agent',
               'analysis_model': 'UI-Analysis', 'vision_model': 'UI-Vision',
               'smtp_enabled': False, 'retention': {'artifact_days': 14}}
UI_KEY = 'SYNTHETIC-UI-KEY'
ENV_KEY = 'SYNTHETIC-ENV-KEY-CANARY'


class ConfigurationResolutionTests(unittest.TestCase):
    def test_01_without_environment_ui_values_remain_and_inputs_are_not_mutated(self):
        saved = copy.deepcopy(UI_SETTINGS)
        effective = configuration.resolve_settings(saved, {})
        self.assertEqual(effective, saved)
        effective['retention']['artifact_days'] = 7
        self.assertEqual(saved, UI_SETTINGS)
        self.assertTrue(all(item == {'source': 'ui', 'environment': None, 'readonly': False}
                            for item in configuration.sources({}).values()))

    def test_02_explicit_environment_wins_and_secrets_stay_private(self):
        environment = {'KIWM_OMLX_URL': 'http://deployment.test:9000',
                       'KIWM_AGENT_MODEL': 'Deployment-Agent', 'KIWM_ANALYSIS_MODEL': 'Deployment-Text',
                       'KIWM_VISION_MODEL': 'Deployment-Vision', 'KIWM_OMLX_API_KEY': ENV_KEY}
        settings = configuration.resolve_settings(UI_SETTINGS, environment)
        private = configuration.resolve_credentials({'omlx_key': UI_KEY, 'smtp_password': 'SYNTHETIC-SMTP'}, environment)
        self.assertEqual(settings['omlx_url'], environment['KIWM_OMLX_URL'])
        self.assertEqual([settings[field] for field in configuration.MODEL_FIELDS],
                         ['Deployment-Agent', 'Deployment-Text', 'Deployment-Vision'])
        self.assertEqual(private['omlx_key'], ENV_KEY)
        self.assertEqual(private['smtp_password'], 'SYNTHETIC-SMTP')
        self.assertNotIn(ENV_KEY, json.dumps(settings))
        self.assertNotIn(ENV_KEY, json.dumps(configuration.sources(environment)))
        self.assertEqual(configuration.sources(environment)['omlx_key'],
                         {'source': 'environment', 'environment': 'KIWM_OMLX_API_KEY', 'readonly': True})

    def test_03_role_model_wins_over_common_model_which_wins_over_ui(self):
        environment = {'KIWM_MODEL': 'Common-Vision-Model', 'KIWM_ANALYSIS_MODEL': 'Text-Specialist'}
        settings = configuration.resolve_settings(UI_SETTINGS, environment)
        self.assertEqual(settings['agent_model'], 'Common-Vision-Model')
        self.assertEqual(settings['vision_model'], 'Common-Vision-Model')
        self.assertEqual(settings['analysis_model'], 'Text-Specialist')
        sources = configuration.sources(environment)
        self.assertEqual(sources['agent_model']['environment'], 'KIWM_MODEL')
        self.assertEqual(sources['vision_model']['environment'], 'KIWM_MODEL')
        self.assertEqual(sources['analysis_model']['environment'], 'KIWM_ANALYSIS_MODEL')

    def test_04_empty_environment_does_not_clear_saved_configuration_or_token(self):
        environment = {name: '  ' for name in configuration.ENVIRONMENT_NAMES}
        environment['KIWM_MODEL'] = ''
        self.assertEqual(configuration.resolve_settings(UI_SETTINGS, environment), UI_SETTINGS)
        self.assertEqual(configuration.resolve_credentials({'omlx_key': UI_KEY}, environment), {'omlx_key': UI_KEY})
        self.assertTrue(all(not source['readonly'] for source in configuration.sources(environment).values()))

    def test_05_environment_is_resolved_per_call_without_caching(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(configuration.resolve_settings(UI_SETTINGS)['agent_model'], 'UI-Agent')
            os.environ['KIWM_MODEL'] = 'First-Model'
            self.assertEqual(configuration.resolve_settings(UI_SETTINGS)['agent_model'], 'First-Model')
            os.environ['KIWM_MODEL'] = 'Second-Model'
            self.assertEqual(configuration.resolve_settings(UI_SETTINGS)['agent_model'], 'Second-Model')
            del os.environ['KIWM_MODEL']
            self.assertEqual(configuration.resolve_settings(UI_SETTINGS)['agent_model'], 'UI-Agent')

    def test_06_invalid_urls_fail_without_echoing_the_supplied_value(self):
        bad = ['ftp://host.test', 'http://', 'http://user:'+ENV_KEY+'@host.test',
               'http://host.test?token='+ENV_KEY, 'http://host.test/#'+ENV_KEY,
               'http://host.test:65536', 'http://host.test:0', 'http://host.test:abc',
               'http://host.test:', 'http://[broken', 'http://host.test/\n'+ENV_KEY,
               'http://host.test/with space', 'http://host.test\\other']
        for value in bad:
            with self.subTest(value_type=value.split(':', 1)[0]),self.assertRaises(configuration.ConfigurationError) as caught:
                configuration.resolve_settings(UI_SETTINGS, {'KIWM_OMLX_URL': value})
            self.assertIn('KIWM_OMLX_URL', str(caught.exception))
            self.assertNotIn(ENV_KEY, str(caught.exception))
        for value in ('http://inference.test', 'https://inference.test:443/base/v1', 'http://[fd00::123]:8000/v1'):
            self.assertEqual(configuration.validate_url(value), value)

    def test_07_invalid_models_do_not_fall_back_to_the_saved_model(self):
        for variable in ('KIWM_MODEL', 'KIWM_AGENT_MODEL', 'KIWM_ANALYSIS_MODEL', 'KIWM_VISION_MODEL'):
            for value in (ENV_KEY+'\nmodel', ENV_KEY+'\x00model', 'x'*513, 123):
                with self.subTest(variable=variable),self.assertRaises(configuration.ConfigurationError) as caught:
                    configuration.resolve_settings(UI_SETTINGS, {variable: value})
                self.assertIn(variable, str(caught.exception))
                self.assertNotIn(ENV_KEY, str(caught.exception))
        self.assertEqual(configuration.validate_model('namespace/Local Vision:4bit'), 'namespace/Local Vision:4bit')

    def test_08_invalid_tokens_fail_privately_and_do_not_use_old_key(self):
        for value in (ENV_KEY+'\r\nInjected: value', ENV_KEY+' middle', 'ü'+ENV_KEY, 'x'*4097, 123):
            with self.subTest(value_type=type(value).__name__),self.assertRaises(configuration.ConfigurationError) as caught:
                configuration.resolve_credentials({'omlx_key': UI_KEY}, {'KIWM_OMLX_API_KEY': value})
            self.assertIn('KIWM_OMLX_API_KEY', str(caught.exception))
            self.assertNotIn(ENV_KEY, str(caught.exception));self.assertNotIn(UI_KEY, str(caught.exception))

    def test_09_bad_saved_values_also_fail_clearly_without_network_calls(self):
        for field, value in [('omlx_url', 'not-a-url'), ('agent_model', None), ('vision_model', 'bad\nmodel')]:
            saved = {**UI_SETTINGS, field: value}
            with self.subTest(field=field),self.assertRaises(configuration.ConfigurationError):
                configuration.resolve_settings(saved, {})
        with self.assertRaises(configuration.ConfigurationError):
            configuration.resolve_credentials({'omlx_key': 'not a token'}, {})
        self.assertEqual(configuration.resolve_settings({}, {}), {})

    def test_10_secret_fields_are_never_accepted_in_public_settings(self):
        for field in ('omlx_key', 'omlx_api_key', 'api_key', 'smtp_password', 'KIWM_OMLX_API_KEY'):
            with self.subTest(field=field),self.assertRaises(configuration.ConfigurationError) as caught:
                configuration.resolve_settings({**UI_SETTINGS, field: ENV_KEY}, {})
            self.assertNotIn(ENV_KEY, str(caught.exception))

    def test_11_no_implicit_copy_between_roles_without_common_model(self):
        settings = configuration.resolve_settings({}, {'KIWM_AGENT_MODEL': 'Agent-Only'})
        self.assertEqual(settings, {'agent_model': 'Agent-Only'})
        self.assertFalse(configuration.sources({'KIWM_AGENT_MODEL': 'Agent-Only'})['vision_model']['readonly'])
        self.assertEqual(set(configuration.ENVIRONMENT_NAMES),
                         {'KIWM_OMLX_URL', 'KIWM_OMLX_API_KEY', 'KIWM_MODEL', 'KIWM_AGENT_MODEL',
                          'KIWM_ANALYSIS_MODEL', 'KIWM_VISION_MODEL'})

    def test_12_nested_private_values_are_preserved_without_mutating_the_saved_object(self):
        saved = {'omlx_key': UI_KEY, 'site_logins': {'project': {'username': 'synthetic'}}}
        before = copy.deepcopy(saved)
        effective = configuration.resolve_credentials(saved, {'KIWM_OMLX_API_KEY': ENV_KEY})
        effective['site_logins']['project']['username'] = 'changed'
        self.assertEqual(saved, before)
        self.assertEqual(configuration.resolve_settings(UI_SETTINGS, {'KIWM_OMLX_API_KEY': ENV_KEY}), UI_SETTINGS)


class StoreConfigurationTests(unittest.TestCase):
    def setUp(self):
        import store
        self.store = store
        self.scratch = tempfile.TemporaryDirectory()
        self.root = Path(self.scratch.name)
        self.patches = [patch.object(store, 'CONFIG', self.root/'settings.json'),
                        patch.object(store, 'SECRETS', self.root/'credentials.json'),
                        patch.dict(os.environ, {}, clear=True)]
        for item in self.patches:item.start()
        store.save_json(store.CONFIG, UI_SETTINGS)
        store.save_json(store.SECRETS, {'omlx_key': UI_KEY, 'smtp_password': 'SYNTHETIC-SMTP'})

    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        self.scratch.cleanup()

    def test_13_effective_reads_do_not_persist_environment_and_raw_reads_remain_available(self):
        store = self.store
        before = (store.CONFIG.read_bytes(), store.SECRETS.read_bytes())
        os.environ.update(KIWM_OMLX_URL='http://deployment.test:9000', KIWM_MODEL='Deployment-Model',
                          KIWM_OMLX_API_KEY=ENV_KEY)
        self.assertEqual(store.settings()['agent_model'], 'Deployment-Model')
        self.assertEqual(store.credentials()['omlx_key'], ENV_KEY)
        self.assertEqual(store.persisted_settings(), UI_SETTINGS)
        self.assertEqual(store.persisted_credentials()['omlx_key'], UI_KEY)
        self.assertEqual((store.CONFIG.read_bytes(), store.SECRETS.read_bytes()), before)
        self.assertNotIn(ENV_KEY, json.dumps(store.configuration_sources()))
        os.environ.clear()
        self.assertEqual(store.settings(), UI_SETTINGS)
        self.assertEqual(store.credentials()['omlx_key'], UI_KEY)

    def test_14_saving_unrelated_ui_fields_never_copies_environment_token_or_models(self):
        store = self.store
        os.environ.update(KIWM_MODEL='Deployment-Model', KIWM_OMLX_API_KEY=ENV_KEY)
        saved = store.persisted_settings();saved['smtp_enabled'] = True
        private = store.persisted_credentials();private['smtp_password'] = 'SYNTHETIC-NEW-SMTP'
        store.save_json(store.CONFIG, saved);store.save_json(store.SECRETS, private)
        self.assertEqual(store.persisted_settings()['agent_model'], UI_SETTINGS['agent_model'])
        self.assertEqual(store.persisted_credentials()['omlx_key'], UI_KEY)
        self.assertNotIn(ENV_KEY, store.SECRETS.read_text())
        self.assertNotIn('Deployment-Model', store.CONFIG.read_text())
        self.assertEqual(store.settings()['agent_model'], 'Deployment-Model')

    def test_15_invalid_environment_fails_without_changing_saved_files(self):
        store = self.store
        before = (store.CONFIG.read_bytes(), store.SECRETS.read_bytes())
        os.environ.update(KIWM_OMLX_URL='http://user:'+ENV_KEY+'@host.test')
        with self.assertRaises(configuration.ConfigurationError) as caught:store.settings()
        self.assertNotIn(ENV_KEY, str(caught.exception))
        self.assertEqual((store.CONFIG.read_bytes(), store.SECRETS.read_bytes()), before)


if __name__ == '__main__':unittest.main(verbosity=2)
