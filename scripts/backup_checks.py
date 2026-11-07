#!/usr/bin/env python3
"""Offline-Regressionstests: sichere Archive und schonende Containerstilllegung."""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import backup
import backup_archive as archive


class BackupChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kiwm-backup-check-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        for relative in ('backups', 'data/browser/profile', 'data/artifacts', 'config', 'secrets'):
            (self.root / relative).mkdir(parents=True, exist_ok=True)
        (self.root / 'config/nginx.conf').write_text('safe nginx configuration\n')
        (self.root / 'secrets/credentials.json').write_text('{}')
        (self.root / 'data/browser/profile/Preferences').write_text('{"test":true}')
        self.database = self.root / 'data/monitor.db'
        connection = sqlite3.connect(self.database)
        connection.executescript('''CREATE TABLE projects(name TEXT);
            CREATE TABLE runs(status TEXT); CREATE TABLE snapshots(file TEXT,sha256 TEXT);
            CREATE TABLE changes(value TEXT); CREATE TABLE reports(value TEXT);''')
        connection.close()
        self.path = self.root / 'backups/ki-webmonitor-20261106T120000Z.tar.gz'

    def sidecar(self):
        with self.path.open('rb') as stream:
            sha = archive.digest(stream)
        self.path.with_name(self.path.name + '.sha256').write_text(sha + '  ' + self.path.name + '\n')

    def malicious(self, entries, *, manifest=None):
        with tarfile.open(self.path, 'w:gz') as tar:
            for name, kind, raw in entries:
                info = tarfile.TarInfo(name)
                info.type = kind
                if kind == tarfile.REGTYPE:
                    info.size = len(raw)
                    tar.addfile(info, io.BytesIO(raw))
                else:
                    info.linkname = '../outside'
                    tar.addfile(info)
            payload = json.dumps(manifest or {'format': archive.FORMAT, 'files': {}}).encode()
            info = tarfile.TarInfo(archive.MANIFEST)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        self.sidecar()

    def assert_rejected_without_destination(self):
        with self.assertRaises(archive.BackupError):
            archive.restore_archive(self.path, self.root / 'backups')
        self.assertEqual(list((self.root / 'backups').glob('restore-test.*')), [])

    def test_normal_roundtrip_and_original_hashes(self):
        raw = b'test document bytes'
        (self.root / 'data/artifacts/example.pdf').write_bytes(raw)
        connection = sqlite3.connect(self.database)
        connection.execute('INSERT INTO snapshots VALUES(?,?)', ('artifacts/example.pdf', hashlib.sha256(raw).hexdigest()))
        connection.commit()
        connection.close()
        original = (self.root / 'data/browser/profile/Preferences').read_bytes()
        result = archive.create_archive(self.root, self.path)
        restored = archive.restore_archive(self.path, self.root / 'backups')
        proof = archive.verify_restored(restored)
        self.assertGreater(result['files'], 3)
        self.assertEqual(proof['originals_verified'], 1)
        self.assertEqual((restored / 'data/artifacts/example.pdf').read_bytes(), raw)
        self.assertEqual((self.root / 'data/browser/profile/Preferences').read_bytes(), original)

    @unittest.skipUnless(os.name == 'posix', 'POSIX-Besitzer und Rechte werden auf der Linux-LXC geprueft')
    def test_restored_source_readable_for_image_build_and_private_runtime_owned(self):
        for relative in ('browser/policies.json', 'browser/close-browser.py', 'proxy/squid.conf', 'tests/version.txt'):
            path = self.root / relative
            path.parent.mkdir(exist_ok=True)
            path.write_text('safe fixture')
        (self.root / '.env').write_text('KIWM_ROOT=/example')
        archive.create_archive(self.root, self.path)
        restored = archive.restore_archive(self.path, self.root / 'backups')
        for relative in ('browser/policies.json', 'browser/close-browser.py', 'proxy/squid.conf'):
            self.assertEqual(stat.S_IMODE((restored / relative).stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(restored.stat().st_mode), 0o700)
        for relative in ('data', 'config', 'secrets', 'tests'):
            self.assertEqual(stat.S_IMODE((restored / relative).stat().st_mode), 0o700)
            if os.geteuid() == 0:
                self.assertEqual((restored / relative).stat().st_uid, 1000)
        for relative in ('.env', 'secrets/credentials.json', 'tests/version.txt'):
            self.assertEqual(stat.S_IMODE((restored / relative).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((restored / 'config/nginx.conf').stat().st_mode), 0o644)

    def test_separate_runtime_root_uses_only_that_installations_private_data(self):
        source = self.root / 'source'
        source.mkdir()
        (source / 'app').mkdir()
        (source / 'app/main.py').write_text('public source')
        (source / 'config').mkdir()
        (source / 'config/nginx.conf.template').write_text('safe template')
        (source / 'config/settings.json').write_text('unrelated-installation')
        (source / 'secrets').mkdir()
        (source / 'secrets/credentials.json').write_text('unrelated-secret')
        archive.create_archive(source, self.path, runtime_root=self.root)
        restored = archive.restore_archive(self.path, self.root / 'backups')
        self.assertEqual((restored / 'app/main.py').read_text(), 'public source')
        self.assertEqual((restored / 'secrets/credentials.json').read_text(), '{}')
        self.assertFalse((restored / 'config/settings.json').exists())
        self.assertTrue((restored / 'config/nginx.conf.template').is_file())

    def test_paths_rejected_before_extracting_even_earlier_safe_entry(self):
        for path in ('../outside', '/outside', 'C:/outside', 'folder\\outside', './folder/../../outside', 'aux.txt'):
            with self.subTest(path=path):
                self.malicious([('safe-first.txt', tarfile.REGTYPE, b'safe'), (path, tarfile.REGTYPE, b'bad')])
                self.assert_rejected_without_destination()
                self.assertFalse((self.root / 'outside').exists())

    def test_links_and_special_members_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE):
            with self.subTest(kind=kind):
                self.malicious([('unsafe', kind, b'')])
                self.assert_rejected_without_destination()

    def test_duplicate_normalized_case_and_prefix_collisions_rejected(self):
        for first, second in (('x.txt', 'x.txt'), ('./x.txt', 'x.txt'), ('X.txt', 'x.txt'), ('folder', 'folder/file')):
            with self.subTest(first=first, second=second):
                self.malicious([(first, tarfile.REGTYPE, b'x'), (second, tarfile.REGTYPE, b'x')])
                self.assert_rejected_without_destination()

    def test_checksum_tampering_rejected(self):
        archive.create_archive(self.root, self.path)
        with self.path.open('ab') as stream:
            stream.write(b'tampered')
        self.assert_rejected_without_destination()

    def test_manifest_wrong_hash_and_unlisted_files_rejected(self):
        self.malicious([('safe.txt', tarfile.REGTYPE, b'hello')], manifest={'format': archive.FORMAT, 'files': {'safe.txt': '0' * 64}})
        self.assert_rejected_without_destination()
        self.malicious([('safe.txt', tarfile.REGTYPE, b'hello')])
        self.assert_rejected_without_destination()

    def test_expansion_limit_rejected(self):
        archive.create_archive(self.root, self.path)
        with self.assertRaises(archive.BackupError):
            archive.restore_archive(self.path, self.root / 'backups', max_bytes=1)
        self.assertEqual(list((self.root / 'backups').glob('restore-test.*')), [])

    def test_queued_or_running_jobs_leave_all_services_untouched(self):
        for status in ('queued', 'running'):
            connection = sqlite3.connect(self.database)
            connection.execute('DELETE FROM runs')
            connection.execute('INSERT INTO runs VALUES(?)', (status,))
            connection.commit()
            connection.close()
            calls = []
            with self.assertRaises(archive.BackupError):
                with backup.quiesced(self.root, [{'id': 'app-id', 'service': 'app'}], run=lambda command: calls.append(command)):
                    self.fail('busy backup entered')
            self.assertEqual(calls, [])

    def test_restart_only_previously_running_ids_even_after_backup_failure(self):
        calls = []
        running = [{'id': 'app-id', 'service': 'app'}, {'id': 'ingress-id', 'service': 'ingress'}, {'id': 'browser-id', 'service': 'browser'}]
        with self.assertRaisesRegex(RuntimeError, 'archive failed'):
            with backup.quiesced(self.root, running, run=lambda command: calls.append(command)):
                raise RuntimeError('archive failed')
        self.assertEqual([cmd[-1] for cmd in calls if cmd[1] == 'stop'], ['ingress-id', 'app-id', 'browser-id'])
        self.assertEqual([cmd[-1] for cmd in calls if cmd[1] == 'start'], ['browser-id', 'app-id', 'ingress-id'])

    def test_job_start_transaction_is_locked_until_app_stopped(self):
        locks = []
        def docker(command):
            if command[1] == 'stop' and command[-1] == 'app-id':
                connection = sqlite3.connect(self.database, timeout=0)
                try:
                    with self.assertRaises(sqlite3.OperationalError):
                        connection.execute('BEGIN IMMEDIATE')
                    locks.append(True)
                finally:
                    connection.close()
        with backup.quiesced(self.root, [{'id': 'app-id', 'service': 'app'}], run=docker):
            pass
        self.assertEqual(locks, [True])

    def test_container_selection_ignores_originally_stopped_services(self):
        project = 'ki-webmonitor'
        ids = ['a' * 64, 'b' * 64]
        config = {'name': project, 'services': {'app': {}, 'browser': {}}}
        inspected = [{'Id': value, 'Config': {'Labels': {'com.docker.compose.project': project, 'com.docker.compose.service': service}}, 'State': {'Running': active}}
                     for value, service, active in ((ids[0], 'app', True), (ids[1], 'browser', False))]
        def docker(command):
            if command[1] == 'compose': return json.dumps(config)
            if command[1] == 'ps': return '\n'.join(ids)
            if command[1] == 'inspect': return json.dumps(inspected)
            self.fail('unexpected docker call')
        self.assertEqual(backup.own_running_containers(self.root, run=docker), [{'id': ids[0], 'service': 'app'}])
        inspected[0]['Config']['Labels']['com.docker.compose.project'] = 'other-stack'
        with self.assertRaises(archive.BackupError):
            backup.own_running_containers(self.root, run=docker)

    def test_runtime_root_is_taken_from_resolved_bind_mounts(self):
        config = {'services': {'app': {'volumes': [
            {'type': 'bind', 'source': str(self.root / name), 'target': '/' + name}
            for name in ('data', 'config', 'secrets')]}}}
        self.assertEqual(backup.configured_runtime_root(self.root, run=lambda command: json.dumps(config)), self.root)
        config['services']['app']['volumes'][0]['source'] = str(self.root / 'other/data')
        with self.assertRaises(archive.BackupError):
            backup.configured_runtime_root(self.root, run=lambda command: json.dumps(config))

    def test_docker_cli_pinned_to_local_socket_and_remote_environment_removed(self):
        with patch.dict(os.environ, {'DOCKER_HOST': 'ssh://other-host', 'DOCKER_CONTEXT': 'remote',
                                     'DOCKER_TLS_VERIFY': '1', 'DOCKER_CERT_PATH': '/other', 'DOCKER_TLS': '1'}), \
                patch.object(backup.subprocess, 'run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = 'local result\n'
            self.assertEqual(backup.command(['docker', 'info']), 'local result')
            self.assertEqual(run.call_args.args[0], ['docker', '--host', 'unix:///var/run/docker.sock', 'info'])
            for key in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH', 'DOCKER_TLS'):
                self.assertNotIn(key, run.call_args.kwargs['env'])

    @unittest.skipUnless(os.name == 'posix', 'Backup-CLI und flock werden auf der Linux-LXC geprueft')
    def test_other_root_uses_own_rotation_helper_and_reports_rotation_failure_separately(self):
        scripts = self.root / 'scripts'
        scripts.mkdir()
        marker = self.root / 'old-helper-ran'
        (scripts / 'backup-retention.py').write_text(
            'from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\nraise SystemExit(2)\n')
        running = [{'id': 'app-id', 'service': 'app'}, {'id': 'browser-id', 'service': 'browser'}]
        real_command, real_quiesced = backup.command, backup.quiesced
        previous_umask = os.umask(0o077)
        try:
            for rotation_fails, second in ((False, '00'), (True, '01')):
                with self.subTest(rotation_fails=rotation_fails):
                    calls = []
                    def rotate(arguments):
                        self.assertEqual(arguments, [sys.executable,
                            str(Path(backup.__file__).resolve().with_name('backup-retention.py')),
                            '--directory', str(self.root / 'backups')])
                        if rotation_fails:
                            raise archive.BackupError('fixture rotation failure')
                        return real_command(arguments)
                    output = io.StringIO()
                    with patch.object(backup, 'configured_runtime_root', return_value=self.root), \
                            patch.object(backup, 'own_running_containers', return_value=running), \
                            patch.object(backup, 'quiesced', side_effect=lambda root, items:
                                real_quiesced(root, items, run=lambda args: calls.append(args))), \
                            patch.object(backup, 'command', side_effect=rotate), \
                            patch.object(backup.os, 'geteuid', return_value=0), \
                            patch.object(backup.signal, 'signal'), \
                            patch.object(backup, 'datetime') as clock, \
                            patch.object(sys, 'argv', ['backup.py', '--root', str(self.root)]), \
                            redirect_stdout(output):
                        clock.now.return_value.strftime.return_value = '20261106T1200' + second + 'Z'
                        if rotation_fails:
                            with self.assertRaisesRegex(SystemExit,
                                    'Sicherung und Wiederanlauf erfolgreich; Rotation fehlgeschlagen, Archiv bleibt erhalten'):
                                backup.main()
                        else:
                            backup.main()
                    result = json.loads(output.getvalue().splitlines()[0])
                    self.assertTrue(Path(result['archive']).is_file())
                    self.assertTrue(Path(result['archive'] + '.sha256').is_file())
                    self.assertEqual([args[-1] for args in calls if args[1] == 'start'], ['browser-id', 'app-id'])
                    self.assertFalse(marker.exists())
        finally:
            os.umask(previous_umask)


if __name__ == '__main__':
    unittest.main(verbosity=2)
