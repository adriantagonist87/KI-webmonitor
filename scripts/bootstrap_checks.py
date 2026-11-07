#!/usr/bin/env python3
"""Offline bootstrap acceptance; only disposable directories, standard library."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import bootstrap


def inventory(root):
    if not root.exists():
        return None
    result = {}
    for path in sorted(root.rglob('*')):
        result[str(path.relative_to(root))] = ('link', os.readlink(path)) if path.is_symlink() else ('dir',) if path.is_dir() else ('file', path.read_bytes())
    return result


class BootstrapChecks(unittest.TestCase):
    def setUp(self):
        # The Windows desktop sandbox temp location can deny child creation;
        # keep local checks inside the authorized checkout instead.
        self.directory = tempfile.TemporaryDirectory(prefix='.bootstrap-check-',
            dir=Path(__file__).resolve().parents[1] if os.name == 'nt' else None)
        self.base = Path(self.directory.name).resolve()
        self.source = self.base / 'checkout'
        (self.source / 'config').mkdir(parents=True)
        self.template = self.source / 'config/nginx.conf.template'
        self.template.write_text('origin http://localhost:__BROWSER_PORT__;\n', encoding='utf-8')
        self.root = self.base / 'runtime'

    def tearDown(self):
        self.directory.cleanup()

    def prepare(self, **values):
        return bootstrap.prepare(self.root, values, source=self.source)

    def link(self, path, target, *, directory=False):
        try:
            path.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError):
            self.skipTest('Symlink creation unavailable; Linux runtime must execute this check')

    def assert_preflight_failure(self, values, kind=ValueError):
        before = inventory(self.base)
        with self.assertRaises(kind):
            self.prepare(**values)
        self.assertEqual(inventory(self.base), before)

    def test_fresh_install_creates_expected_private_files_and_model_names(self):
        settings, created = self.prepare(BROWSER_PORT='18766')
        self.assertEqual(settings['KIWM_ROOT'], str(self.root))
        self.assertIn('data/browser', created)
        self.assertIn('secrets/credentials.json', created)
        config = json.loads((self.root / 'config/settings.json').read_text())
        self.assertIn('analysis_model', config)
        self.assertNotIn('text_model', config)
        self.assertFalse(config['smtp_enabled'])
        self.assertEqual(json.loads((self.root / 'secrets/credentials.json').read_text()), {})
        self.assertIn('localhost:18766', (self.root / 'config/nginx.conf').read_text())

    def test_repeated_prepare_preserves_settings_secrets_profiles_and_history_exactly(self):
        self.prepare()
        for name, data in {
            'config/settings.json': b'{"analysis_model":"synthetic-model","custom":true}',
            'secrets/credentials.json': b'{"omlx_key":"SYNTHETIC-PRIVATE"}',
            'data/monitor.db': b'synthetic-database-bytes',
            'data/browser/profile': b'synthetic-private-cookies',
        }.items():
            (self.root / name).write_bytes(data)
        before = inventory(self.root)
        _, created = self.prepare()
        self.assertEqual(created, [])
        self.assertEqual(inventory(self.root), before)

    def test_invalid_ports_fail_before_any_write(self):
        for values in ({'WEB_PORT': '1023'}, {'BROWSER_PORT': '65536'},
                       {'MAILPIT_PORT': 'invalid'}, {'WEB_PORT': '８７６５'},
                       {'WEB_PORT': '8766'}, {'MAILPIT_PORT': '8766'}):
            with self.subTest(values=values):
                self.assert_preflight_failure(values)

    def test_invalid_network_and_static_ips_fail_before_any_write(self):
        for values in ({'MONITOR_SUBNET': '8.8.8.0/24'}, {'MONITOR_SUBNET': '127.0.0.0/8'},
                       {'MONITOR_SUBNET': '2001:db8::/64'}, {'MONITOR_SUBNET': '172.29.133.1/24'},
                       {'BROWSER_IP': '172.29.134.20'}, {'BROWSER_IP': '172.29.133.0'},
                       {'BROWSER_IP': '172.29.133.1'}, {'FIXTURE_IP': '172.29.133.255'},
                       {'FIXTURE_IP': '172.29.133.20'}, {'BROWSER_IP': '::1'}):
            with self.subTest(values=values):
                self.assert_preflight_failure(values)

    def test_companion_urls_must_match_local_ports_before_any_write(self):
        for values in ({'BROWSER_UI_URL': 'http://example.org:8766/'},
                       {'BROWSER_UI_URL': 'https://localhost:8766/'},
                       {'BROWSER_UI_URL': 'http://localhost:18766/'},
                       {'BROWSER_UI_URL': 'http://name:password@localhost:8766/'},
                       {'BROWSER_UI_URL': 'http://localhost:8766/path'},
                       {'MAILPIT_UI_URL': 'http://localhost:8767/?x=1'},
                       {'MAILPIT_UI_URL': 'http://localhost:invalid/'}):
            with self.subTest(values=values):
                self.assert_preflight_failure(values)
        values, _ = self.prepare(BROWSER_PORT='18766', BROWSER_UI_URL='http://127.0.0.1:18766/',
                                 MAILPIT_UI_URL='http://localhost:8767/')
        self.assertEqual(values['BROWSER_PORT'], '18766')

    def test_relative_root_and_filesystem_root_are_rejected(self):
        for root in (Path('relative-runtime'), Path(self.root.anchor)):
            with self.subTest(root=root), self.assertRaises(ValueError):
                bootstrap.prepare(root, {}, source=self.source)
        self.assertFalse(self.root.exists())

    def test_missing_or_invalid_proxy_template_fails_before_writes(self):
        self.template.write_text('no marker', encoding='utf-8')
        self.assert_preflight_failure({})
        self.template.unlink()
        self.assert_preflight_failure({}, FileNotFoundError)

    def test_existing_custom_proxy_is_preserved_until_explicit_refresh(self):
        (self.root / 'config').mkdir(parents=True)
        (self.root / 'config/nginx.conf').write_text('custom-proxy', encoding='utf-8')
        (self.root / 'config/settings.json').write_text('{"keep":true}', encoding='utf-8')
        self.assert_preflight_failure({})
        bootstrap.prepare(self.root, {}, source=self.source, refresh_proxy=True)
        self.assertIn('localhost:8766', (self.root / 'config/nginx.conf').read_text())
        self.assertEqual((self.root / 'config/settings.json').read_text(), '{"keep":true}')

    def test_wrong_directory_or_file_type_fails_without_partial_installation(self):
        self.root.mkdir()
        (self.root / 'tests').write_bytes(b'not-a-directory')
        self.assert_preflight_failure({})
        (self.root / 'tests').unlink()
        (self.root / 'secrets/credentials.json').mkdir(parents=True)
        self.assert_preflight_failure({})

    def test_directory_symlink_is_rejected_before_data_or_settings_creation(self):
        self.root.mkdir()
        outside = self.base / 'outside'
        outside.mkdir()
        self.link(self.root / 'secrets', outside, directory=True)
        self.assert_preflight_failure({})
        self.assertEqual(list(outside.iterdir()), [])

    def test_dangling_private_file_symlink_is_rejected_before_writes(self):
        (self.root / 'config').mkdir(parents=True)
        self.link(self.root / 'config/settings.json', self.base / 'missing-target')
        self.assert_preflight_failure({})
        self.assertFalse((self.base / 'missing-target').exists())

    def test_root_and_ancestor_symlinks_are_rejected(self):
        outside = self.base / 'outside'
        outside.mkdir()
        self.link(self.root, outside, directory=True)
        with self.assertRaises(ValueError):
            bootstrap.prepare(self.root / 'nested-runtime', {}, source=self.source)
        self.assertEqual(list(outside.iterdir()), [])

    def test_env_symlink_is_rejected_before_runtime_creation(self):
        outside = self.base / 'outside.env'
        outside.write_text('KIWM_ROOT=' + str(self.root), encoding='utf-8')
        self.link(self.source / '.env', outside)
        before = outside.read_bytes()
        with self.assertRaises(ValueError):
            bootstrap.configure(source=self.source, environment={})
        self.assertFalse(self.root.exists())
        self.assertEqual(outside.read_bytes(), before)

    def test_new_env_is_private_and_repeated_configure_preserves_it_exactly(self):
        bootstrap.configure(source=self.source, root=self.root, environment={'BROWSER_PORT': '18766'})
        path = self.source / '.env'
        values = bootstrap.env_file(path)
        self.assertEqual(values['KIWM_ROOT'], str(self.root))
        self.assertEqual(values['BROWSER_PORT'], '18766')
        self.assertNotIn('PASSWORD', path.read_text())
        before = inventory(self.base)
        bootstrap.configure(source=self.source, environment={})
        self.assertEqual(inventory(self.base), before)

    def test_existing_env_root_cannot_be_silently_overridden(self):
        first = self.base / 'first-runtime'
        env = self.source / '.env'
        env.write_text('KIWM_ROOT=' + str(first) + '\n', encoding='utf-8')
        before = inventory(self.base)
        for options in ({'root': self.root}, {'environment': {'KIWM_ROOT': str(self.root)}}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                bootstrap.configure(source=self.source, environment=options.get('environment', {}),
                                    root=options.get('root'))
        self.assertEqual(inventory(self.base), before)
        self.assertFalse(first.exists())
        self.assertFalse(self.root.exists())

    def test_existing_env_default_root_and_ports_cannot_drift(self):
        env = self.source / '.env'
        env.write_text('WEB_PORT=8765\n', encoding='utf-8')
        before = inventory(self.base)
        with self.assertRaises(ValueError):
            bootstrap.configure(source=self.source, root=self.root, environment={})
        with self.assertRaises(ValueError):
            bootstrap.configure(source=self.source, environment={'WEB_PORT': '18765'})
        self.assertEqual(inventory(self.base), before)

    def test_duplicate_env_keys_are_rejected(self):
        (self.source / '.env').write_text('WEB_PORT=8765\nWEB_PORT=18765\n', encoding='utf-8')
        before = inventory(self.base)
        with self.assertRaises(ValueError):
            bootstrap.configure(source=self.source, root=self.root, environment={})
        self.assertEqual(inventory(self.base), before)

    def test_private_exclusive_create_never_overwrites_a_file(self):
        path = self.base / 'secret'
        path.write_bytes(b'SYNTHETIC-ORIGINAL')
        with self.assertRaises(FileExistsError):
            bootstrap.private_file(path, 'SYNTHETIC-REPLACEMENT')
        self.assertEqual(path.read_bytes(), b'SYNTHETIC-ORIGINAL')

    def test_root_installer_assigns_runtime_uid_1000(self):
        with patch.object(bootstrap.os, 'geteuid', return_value=0, create=True), \
             patch.object(bootstrap.os, 'chown', create=True) as chown, \
             patch.object(bootstrap.os, 'fchown', create=True) as fchown:
            self.prepare()
        self.assertEqual(chown.call_count, 6)
        self.assertEqual(fchown.call_count, 2)
        for call in [*chown.call_args_list, *fchown.call_args_list]:
            self.assertEqual(call.args[-2:], (1000, 1000))

    @unittest.skipUnless(os.name == 'posix' and hasattr(os, 'geteuid') and os.geteuid() == 0,
                         'Requires the real Linux root installer')
    def test_git_clone_existing_root_owned_config_becomes_runtime_writable(self):
        # The fixture's TemporaryDirectory is root-private by default; the host
        # path check below intentionally gives the runtime UID traversal access.
        self.base.chmod(0o755)
        self.assertEqual((self.source / 'config').stat().st_uid, 0)
        (self.source / 'config').chmod(0o755)
        original = self.template.read_bytes()
        bootstrap.configure(source=self.source, root=self.source, environment={})
        config = self.source / 'config'
        self.assertEqual((config.stat().st_uid, config.stat().st_gid), (1000, 1000))
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o700)
        self.assertEqual(self.template.read_bytes(), original)
        self.assertEqual(self.template.stat().st_uid, 0, 'Do not recursively chown source files')
        # Test actual UID 1000 access, including atomic settings-file replacement.
        import subprocess
        subprocess.run([__import__('sys').executable, '-c',
            'import os,pathlib; os.setgroups([]); os.setgid(1000); os.setuid(1000); '
            'p=pathlib.Path("."); q=p/"settings.tmp"; '
            'q.write_text("synthetic replacement"); os.replace(q,p/"settings.json")'],
            # Enter the fixture before dropping UID, like a bind-mounted config
            # directory. An outer runner-owned TMPDIR may legitimately be 0700;
            # it must not weaken this permission check or be chmodded by a test.
            cwd=config, check=True)
        self.assertEqual((config / 'settings.json').read_text(), 'synthetic replacement')
        self.assertEqual(json.loads((self.source / 'secrets/credentials.json').read_text()), {})

    @unittest.skipUnless(os.name == 'posix' and hasattr(os, 'geteuid') and os.geteuid() == 0,
                         'Requires the real Linux root installer')
    def test_existing_root_private_jsons_are_repaired_without_changing_bytes(self):
        self.base.chmod(0o755)
        (self.root / 'config').mkdir(parents=True)
        (self.root / 'secrets').mkdir()
        records = {'config/settings.json': b'{"custom":"preserve exactly"}',
                   'secrets/credentials.json': b'{"api":"SYNTHETIC"}',
                   'secrets/site-access.json': b'{"login":"SYNTHETIC"}'}
        for name, value in records.items():
            path = self.root / name
            path.write_bytes(value)
            path.chmod(0o600)
            self.assertEqual(path.stat().st_uid, 0)
        bootstrap.prepare(self.root, {}, source=self.source)
        for name, value in records.items():
            path = self.root / name
            self.assertEqual(path.read_bytes(), value)
            self.assertEqual((path.stat().st_uid, path.stat().st_gid), (1000, 1000))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_additional_secret_json_symlink_is_rejected_before_directory_repairs(self):
        (self.root / 'secrets').mkdir(parents=True)
        self.link(self.root / 'secrets/other.json', self.base / 'missing-private-target')
        self.assert_preflight_failure({})

    @unittest.skipUnless(os.name == 'posix', 'POSIX ownership preflight')
    def test_unprivileged_installer_rejects_foreign_bind_directory_before_writes(self):
        (self.root / 'config').mkdir(parents=True)
        before = inventory(self.base)
        foreign_uid = 1000 if (self.root / 'config').stat().st_uid != 1000 else 2000
        with patch.object(bootstrap.os, 'geteuid', return_value=foreign_uid):
            with self.assertRaisesRegex(ValueError, 'Bind-Verzeichnis'):
                self.prepare()
        self.assertEqual(inventory(self.base), before)

    @unittest.skipUnless(os.name == 'posix', 'POSIX private permissions are verified on Linux')
    def test_actual_linux_permissions_and_ownership(self):
        bootstrap.configure(source=self.source, root=self.root, environment={})
        for name in ('data', 'data/browser', 'data/mailpit', 'config', 'secrets'):
            path = self.root / name
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            self.assertEqual(path.stat().st_uid, 1000 if os.geteuid() == 0 else os.geteuid())
        for path in (self.root / 'config/settings.json', self.root / 'secrets/credentials.json', self.source / '.env'):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.stat().st_uid, 1000 if os.geteuid() == 0 else os.geteuid())
        self.assertEqual(stat.S_IMODE((self.root / 'tests').stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((self.root / 'config/nginx.conf').stat().st_mode), 0o644)


if __name__ == '__main__':
    unittest.main(verbosity=2)
