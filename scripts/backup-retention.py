#!/usr/bin/env python3
"""Rotate only verified own backups, keeping the newest three.

Unknown files, invalid checksum pairs and restore-test directories stay intact.
A dry run reads and locks the existing private directory only; it does not
create a lock file or change archives. Linux directory flock serializes rotation.
"""
import argparse
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat

DIRECTORY = Path('/opt/ki-webmonitor/backups')
NAME = re.compile(r'ki-webmonitor-(\d{8}T\d{6}Z)\.tar\.gz\Z')
SUM = re.compile(r'([0-9a-fA-F]{64}) [ *](.+)\Z')


def _filename(name):
    match = NAME.fullmatch(name)
    if not match:
        return False
    try:
        datetime.strptime(match[1], '%Y%m%dT%H%M%SZ')
    except ValueError:
        return False
    return True


def _identity(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns)


def _regular(fd, name):
    metadata = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError('not_regular_file')
    return metadata


def _read_sidecar(fd, name, directory):
    sidecar = name + '.sha256'
    expected_metadata = _regular(fd, sidecar)
    if expected_metadata.st_size > 1024:
        raise ValueError('invalid_checksum_file')
    handle = os.open(sidecar, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    with os.fdopen(handle, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if _identity(before) != _identity(expected_metadata):
            raise ValueError('checksum_file_changed')
        raw = stream.read(1025)
        after = os.fstat(stream.fileno())
    if _identity(before) != _identity(after) or len(raw) > 1024:
        raise ValueError('checksum_file_changed')
    try:
        lines = raw.decode('ascii').splitlines()
        if len(lines) != 1:
            raise ValueError()
        match = SUM.fullmatch(lines[0])
        if not match or match[2] not in (name, 'backups/' + name, str(directory / name)):
            raise ValueError()
    except (UnicodeError, ValueError):
        raise ValueError('invalid_checksum_file') from None
    return match[1].lower(), after


def _verify(fd, name, directory):
    expected_hash, checksum_metadata = _read_sidecar(fd, name, directory)
    expected_metadata = _regular(fd, name)
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    with os.fdopen(handle, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if _identity(before) != _identity(expected_metadata):
            raise ValueError('archive_changed')
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        after = os.fstat(stream.fileno())
    if _identity(before) != _identity(after):
        raise ValueError('archive_changed')
    if actual != expected_hash:
        raise ValueError('checksum_mismatch')
    if _identity(_regular(fd, name + '.sha256')) != _identity(checksum_metadata):
        raise ValueError('checksum_file_changed')
    return {'archive': after, 'checksum': checksum_metadata,
            'bytes': after.st_size + checksum_metadata.st_size}


def prune(directory, keep=3, dry_run=False):
    """Delete verified older direct pairs after all newest retention anchors pass.

    ``keep`` may increase the minimum of three, but cannot weaken it. Failure of
    any newest anchor prevents ALL deletions. Older candidates are each hashed
    independently. Invalid older files are preserved.
    """
    if type(keep) is not int or not 3 <= keep <= 100:
        raise ValueError('keep must be an integer from 3 to 100')
    if type(dry_run) is not bool:
        raise ValueError('dry_run must be boolean')
    directory = Path(directory).absolute()
    result = {'status': 'skipped', 'keep': keep, 'kept': [], 'verified': 0,
              'candidates': {'archives': 0, 'files': 0, 'bytes': 0},
              'removed': {'archives': 0, 'files': 0, 'bytes': 0},
              'deleted': [], 'skipped': []}
    try:
        if directory.resolve(strict=True) != directory or directory.is_symlink():
            raise ValueError('unsafe_directory')
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except (OSError, ValueError):
        result['skipped'].append({'reason': 'unsafe_directory'})
        return result
    try:
        metadata = os.fstat(directory_fd)
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077
                or metadata.st_uid != os.geteuid()):
            result['skipped'].append({'reason': 'directory_must_be_private_and_owned'})
            return result
        try:
            fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result['skipped'].append({'reason': 'rotation_already_running'})
            return result
        # Matching symlink/nonregular names fail anchor verification. They are
        # not skipped silently in favor of deleting genuinely old backups.
        names = sorted((name for name in os.listdir(directory_fd) if _filename(name)), reverse=True)
        if len(names) <= keep:
            result['kept'] = names
            result['skipped'].append({'reason': 'no_backup_older_than_retention_anchors'})
            return result
        anchors = names[:keep]
        result['kept'] = anchors
        for name in anchors:
            try:
                _verify(directory_fd, name, directory)
                result['verified'] += 1
            except (OSError, ValueError):
                result['skipped'].append({'archive': name, 'reason': 'retention_anchor_not_verified'})
                return result
        verified = []
        for name in names[keep:]:
            try:
                proof = _verify(directory_fd, name, directory)
                result['verified'] += 1
                verified.append((name, proof))
            except (OSError, ValueError):
                result['skipped'].append({'archive': name, 'reason': 'older_backup_not_verified'})
        result['candidates'] = {'archives': len(verified), 'files': len(verified) * 2,
                                'bytes': sum(proof['bytes'] for _, proof in verified)}
        result['status'] = 'dry-run' if dry_run else 'cleaned'
        if dry_run:
            return result
        for name, proof in verified:
            current = os.stat(directory, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                result['status'] = 'skipped'
                result['skipped'].append({'reason': 'directory_changed'})
                break
            try:
                if _identity(_regular(directory_fd, name)) != _identity(proof['archive']):
                    raise ValueError('archive_changed')
                if _identity(_regular(directory_fd, name + '.sha256')) != _identity(proof['checksum']):
                    raise ValueError('checksum_file_changed')
                os.unlink(name, dir_fd=directory_fd)
                result['deleted'].append(name)
                result['removed']['archives'] += 1
                result['removed']['files'] += 1
                result['removed']['bytes'] += proof['archive'].st_size
                os.unlink(name + '.sha256', dir_fd=directory_fd)
                result['removed']['files'] += 1
                result['removed']['bytes'] += proof['checksum'].st_size
            except (OSError, ValueError):
                result['skipped'].append({'archive': name, 'reason': 'candidate_changed_or_unlink_failed'})
                continue
        if result['removed']['archives']:
            os.fsync(directory_fd)
        return result
    finally:
        os.close(directory_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--directory', type=Path, default=DIRECTORY)
    arguments = parser.parse_args()
    print(json.dumps(prune(arguments.directory, dry_run=arguments.dry_run), ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
