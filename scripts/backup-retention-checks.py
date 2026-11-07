#!/usr/bin/env python3
"""Linux fixtures for backup rotation; no Docker, model or network calls."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).with_name('backup-retention.py')
spec = importlib.util.spec_from_file_location('backup_retention', SCRIPT)
rotation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rotation)


class BackupRetention(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='kiwm-backup-rotation-')
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.directory.chmod(0o700)

    def backup(self, day, *, corrupt=False, checksum=True):
        name = f'ki-webmonitor-202611{day:02d}T060000Z.tar.gz'
        path = self.directory / name
        raw = f'fixture backup day {day}'.encode()
        path.write_bytes(raw)
        if checksum:
            expected = '0' * 64 if corrupt else hashlib.sha256(raw).hexdigest()
            path.with_name(name + '.sha256').write_text(expected + '  backups/' + name + '\n')
        return path

    def archive_names(self):
        return sorted(p.name for p in self.directory.iterdir() if p.name.endswith('.tar.gz'))

    def test_01_two_or_three_backups_are_never_deleted(self):
        for day in (1, 2):
            self.backup(day)
        before = self.archive_names()
        self.assertEqual(rotation.prune(self.directory)['removed']['archives'], 0)
        self.assertEqual(self.archive_names(), before)
        self.backup(3)
        self.assertEqual(rotation.prune(self.directory)['removed']['archives'], 0)
        self.assertEqual(len(self.archive_names()), 3)

    def test_02_only_verified_old_pairs_removed_with_three_newer_anchors(self):
        paths = [self.backup(day) for day in range(1, 7)]
        result = rotation.prune(self.directory)
        self.assertEqual(result['status'], 'cleaned')
        self.assertEqual(result['removed']['archives'], 3)
        self.assertEqual(result['removed']['files'], 6)
        self.assertGreater(result['removed']['bytes'], 0)
        self.assertEqual(self.archive_names(), [path.name for path in paths[3:]])
        self.assertEqual(result['kept'], [path.name for path in reversed(paths[3:])])
        self.assertTrue(all(path.with_name(path.name + '.sha256').is_file() for path in paths[3:]))
        self.assertTrue(all(not path.with_name(path.name + '.sha256').exists() for path in paths[:3]))

    def test_03_dry_run_changes_no_entry_or_bytes(self):
        for day in range(1, 5):
            self.backup(day)
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        result = rotation.prune(self.directory, dry_run=True)
        self.assertEqual(result['status'], 'dry-run')
        self.assertEqual(result['candidates']['archives'], 1)
        self.assertEqual(result['removed']['archives'], 0)
        self.assertEqual({path.name: path.read_bytes() for path in self.directory.iterdir()}, before)

    def test_04_invalid_newest_anchor_blocks_all_deletions(self):
        for day in range(1, 5):
            self.backup(day, corrupt=day == 4)
        before = self.archive_names()
        result = rotation.prune(self.directory)
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(result['removed']['archives'], 0)
        self.assertEqual(self.archive_names(), before)

    def test_05_invalid_or_unchecked_old_pairs_remain(self):
        paths = [self.backup(day, corrupt=day == 1, checksum=day != 2) for day in range(1, 7)]
        result = rotation.prune(self.directory)
        self.assertEqual(result['deleted'], [paths[2].name])
        self.assertTrue(paths[0].exists() and paths[1].exists())
        self.assertEqual(len(result['skipped']), 2)

    def test_06_unknown_nested_malformed_and_symlink_files_stay(self):
        for day in range(2, 6):
            self.backup(day)
        unknown = self.directory / 'personal-backup.tar.gz'
        unknown.write_bytes(b'unknown')
        malformed = self.directory / 'ki-webmonitor-20260231T000000Z.tar.gz'
        malformed.write_bytes(b'invalid calendar timestamp')
        clone = self.directory / 'restore-test.abcdef'
        clone.mkdir()
        nested = clone / 'ki-webmonitor-20260101T000000Z.tar.gz'
        nested.write_bytes(b'never recursive')
        outside = self.directory / 'outside-source'
        outside.write_bytes(b'do not alter')
        link = self.directory / 'ki-webmonitor-20261101T060000Z.tar.gz'
        link.symlink_to(outside)
        sidecar = link.with_name(link.name + '.sha256')
        sidecar.write_text(hashlib.sha256(outside.read_bytes()).hexdigest() + '  ' + link.name + '\n')
        rotation.prune(self.directory)
        self.assertTrue(link.is_symlink() and sidecar.exists())
        self.assertEqual(outside.read_bytes(), b'do not alter')
        self.assertTrue(unknown.exists() and malformed.exists() and nested.exists())

    def test_07_symlink_checksum_and_newest_archive_are_not_followed(self):
        paths = [self.backup(day) for day in range(1, 5)]
        sidecar = paths[0].with_name(paths[0].name + '.sha256')
        saved = self.directory / 'saved-checksum'
        sidecar.rename(saved)
        sidecar.symlink_to(saved)
        result = rotation.prune(self.directory)
        self.assertEqual(result['removed']['archives'], 0)
        self.assertTrue(sidecar.is_symlink() and paths[0].exists())
        newest = paths[-1]
        newest.unlink()
        newest.symlink_to(paths[0])
        result = rotation.prune(self.directory)
        self.assertEqual(result['status'], 'skipped')
        self.assertTrue(newest.is_symlink())

    def test_08_lock_idempotence_private_directory_and_minimum_count(self):
        for day in range(1, 5):
            self.backup(day)
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            rotation.fcntl.flock(fd, rotation.fcntl.LOCK_EX | rotation.fcntl.LOCK_NB)
            result = rotation.prune(self.directory)
            self.assertEqual(result['status'], 'skipped')
            self.assertEqual(result['removed']['archives'], 0)
        finally:
            os.close(fd)
        self.assertEqual(rotation.prune(self.directory)['removed']['archives'], 1)
        self.assertEqual(rotation.prune(self.directory)['removed']['archives'], 0)
        self.directory.chmod(0o755)
        self.assertEqual(rotation.prune(self.directory)['status'], 'skipped')
        self.directory.chmod(0o700)
        link = self.directory / 'directory-link'
        link.symlink_to(self.directory, target_is_directory=True)
        self.assertEqual(rotation.prune(link)['status'], 'skipped')
        with self.assertRaises(ValueError):
            rotation.prune(self.directory, keep=2)

    def test_09_cli_rotates_only_explicit_private_owned_directory(self):
        for day in range(1, 5):
            self.backup(day)
        foreign = self.directory / 'foreign-root'
        foreign.mkdir(mode=0o700)
        for path in self.directory.iterdir():
            if path.is_file():
                (foreign / path.name).write_bytes(path.read_bytes())
        before = {path.name: path.read_bytes() for path in foreign.iterdir()}
        if os.geteuid() == 0:
            os.chown(foreign, 1001, 1001)
        else:
            foreign.chmod(0o755)
        response = subprocess.run([sys.executable, str(SCRIPT), '--directory', str(self.directory)],
                                  capture_output=True, text=True)
        self.assertEqual(response.returncode, 0)
        self.assertEqual(json.loads(response.stdout)['removed']['archives'], 1)
        self.assertEqual(len(self.archive_names()), 3)
        try:
            protected = subprocess.run([sys.executable, str(SCRIPT), '--directory', str(foreign)],
                                       capture_output=True, text=True)
            self.assertEqual(protected.returncode, 0)
            result = json.loads(protected.stdout)
            self.assertEqual(result['status'], 'skipped')
            self.assertEqual(result['removed']['files'], 0)
            self.assertEqual({path.name: path.read_bytes() for path in foreign.iterdir()}, before)
        finally:
            if os.geteuid() == 0:
                os.chown(foreign, os.geteuid(), os.getegid())
            foreign.chmod(0o700)

    def test_10_sidecar_unlink_failure_receipt_counts_the_removed_archive(self):
        paths = [self.backup(day) for day in range(1, 5)]
        original = os.unlink

        def unlink(name, *args, **kwargs):
            if name == paths[0].name + '.sha256':
                raise OSError('fixture sidecar failure')
            return original(name, *args, **kwargs)

        size = paths[0].stat().st_size
        with patch.object(rotation.os, 'unlink', side_effect=unlink):
            result = rotation.prune(self.directory)
        self.assertEqual(result['removed'], {'archives': 1, 'files': 1, 'bytes': size})
        self.assertFalse(paths[0].exists())
        self.assertTrue(paths[0].with_name(paths[0].name + '.sha256').exists())
        self.assertEqual(len(self.archive_names()), 3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
