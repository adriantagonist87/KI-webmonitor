#!/usr/bin/env python3
"""Private Vollsicherungen erstellen und ausschliesslich isoliert wiederherstellen."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile

FORMAT = 'ki-webmonitor-backup/v2'
MAX_MEMBERS = 150000
MAX_EXPANDED = 20 * 1024 ** 3
MANIFEST = 'backup-manifest.json'
EXCLUDED = {'backups', 'research', 'work', 'outputs', '.git', '__pycache__', '.pytest_cache', '.venv'}
TRANSIENT = {'SingletonLock', 'SingletonCookie', 'SingletonSocket', 'DevToolsActivePort'}
HASH = re.compile(r'[0-9a-f]{64}\Z')
NAME = re.compile(r'ki-webmonitor-\d{8}T\d{6}Z\.tar\.gz\Z')


class BackupError(Exception):
    pass


def digest(stream):
    value = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        value.update(block)
    return value.hexdigest()


def relative_name(value, *, root_allowed=False):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise BackupError('Ungueltiger Archivpfad.')
    while value.startswith('./'):
        value = value[2:]
    if value in ('', '.') and root_allowed:
        return ''
    if value.startswith('/') or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise BackupError('Ungueltiger Archivpfad.')
    parts = PurePosixPath(value).parts
    if not parts or '..' in parts or any(part.endswith((' ', '.')) for part in parts):
        raise BackupError('Ungueltiger Archivpfad.')
    # Keine alternativen Windows-Zielnamen bei einem spaeteren Transfer.
    reserved = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))}
    if any(part.split('.')[0].casefold() in reserved for part in parts):
        raise BackupError('Ungueltiger Archivpfad.')
    return '/'.join(parts)


def real_directory(path):
    path = Path(path).absolute()
    if path.resolve() != path or path.is_symlink() or not path.is_dir():
        raise BackupError('Ein echtes absolutes Verzeichnis ist erforderlich.')
    return path


def create_archive(root, archive, *, runtime_root=None):
    """Aufruf nur nach Stilllegung aller schreibenden Projektcontainer."""
    root = real_directory(root)
    runtime_root = real_directory(runtime_root or root)
    archive = Path(archive)
    backup_dir = real_directory(runtime_root / 'backups')
    if archive.parent != backup_dir or not NAME.fullmatch(archive.name) or archive.exists():
        raise BackupError('Ungueltiges oder bereits vorhandenes Sicherungsziel.')
    files, directories = {}, {}
    sources = [(root, '', True)]
    for directory in ('data', 'config', 'secrets'):
        sources.append((real_directory(runtime_root / directory), directory, False))
    # Auch bei getrenntem KIWM_ROOT bleiben sichere Bootstrapvorlagen erhalten.
    if runtime_root != root and (root / 'config').is_dir():
        for path in (root / 'config').rglob('*'):
            if path.is_file() and ('.template' in path.name or '.example' in path.name):
                if path.is_symlink():
                    raise BackupError('Link in der Konfigurationsvorlage.')
                files[path.relative_to(root).as_posix()] = path
    for source, prefix, is_source in sources:
        if prefix:
            directories[prefix] = source
        for parent, names, filenames in os.walk(source, followlinks=False):
            base = Path(parent)
            names[:] = sorted(n for n in names if n not in EXCLUDED
                              and not (is_source and base == root and n in {'data', 'config', 'secrets'})
                              and not (is_source and runtime_root != root and base / n == runtime_root))
            for name in list(names):
                child = base / name
                if child.is_symlink():
                    raise BackupError('Symlink-Verzeichnis im Sicherungsbestand; keine Sicherung erstellt.')
                logical = '/'.join(p for p in (prefix, child.relative_to(source).as_posix()) if p)
                directories[relative_name(logical)] = child
            for name in sorted(filenames):
                if name in TRANSIENT or name == MANIFEST or name.endswith(('.pyc', '.pyo')):
                    continue
                path = base / name
                metadata = path.lstat()
                if not stat.S_ISREG(metadata.st_mode):
                    raise BackupError('Link oder Spezialdatei im Sicherungsbestand; keine Sicherung erstellt.')
                logical = '/'.join(p for p in (prefix, path.relative_to(source).as_posix()) if p)
                files[relative_name(logical)] = path
    manifest = {}
    temporary = archive.with_name(archive.name + '.partial')
    try:
        with temporary.open('xb') as output, tarfile.open(fileobj=output, mode='w:gz', dereference=True) as tar:
            os.chmod(temporary, 0o600)
            for logical, path in sorted(directories.items()):
                entry = tar.gettarinfo(str(path), logical)
                entry.mode &= 0o777
                tar.addfile(entry)
            for logical, path in sorted(files.items()):
                flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                with os.fdopen(os.open(path, flags), 'rb') as stream:
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode):
                        raise BackupError('Sicherungsdatei wurde ersetzt.')
                    entry = tar.gettarinfo(str(path), logical, fileobj=stream)
                    manifest[entry.name] = digest(stream)
                    stream.seek(0)
                    tar.addfile(entry, stream)
                    after = os.fstat(stream.fileno())
                    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                        raise BackupError('Datei waehrend Sicherung veraendert.')
            raw = json.dumps({'format': FORMAT, 'files': manifest}, sort_keys=True).encode()
            entry = tarfile.TarInfo(MANIFEST)
            entry.size, entry.mode = len(raw), 0o600
            tar.addfile(entry, io.BytesIO(raw))
        with temporary.open('rb') as stream:
            sha = digest(stream)
        temporary.replace(archive)
        sidecar = archive.with_name(archive.name + '.sha256')
        with sidecar.open('x', encoding='ascii') as stream:
            stream.write(f'{sha}  {archive.name}\n')
        os.chmod(sidecar, 0o600)
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise
    return {'files': len(files), 'archive': str(archive), 'sha256': sha}


def validated_members(tar, *, max_bytes=MAX_EXPANDED):
    members, kinds, folded, total = [], {}, set(), 0
    for entry in tar:
        if len(members) >= MAX_MEMBERS:
            raise BackupError('Zu viele Archiveintraege.')
        if not entry.isdir() and not entry.isfile():
            raise BackupError('Links und Spezialdateien sind im Archiv nicht erlaubt.')
        name = relative_name(entry.name, root_allowed=entry.isdir())
        if name in kinds or name.casefold() in folded:
            raise BackupError('Doppelte oder kollidierende Archivpfade.')
        if name and PurePosixPath(name).parts[0] in {'backups', '.git'}:
            raise BackupError('Unzulaessiges verschachteltes Sicherungs-/Git-Verzeichnis.')
        kinds[name] = 'directory' if entry.isdir() else 'file'
        folded.add(name.casefold())
        if entry.size < 0:
            raise BackupError('Ungueltige Dateigroesse.')
        total += entry.size
        if total > max_bytes:
            raise BackupError('Entpackte Sicherung ueberschreitet die Groessenbegrenzung.')
        members.append((name, entry))
    for name in kinds:
        for parent in PurePosixPath(name).parents:
            if parent.as_posix() != '.' and kinds.get(parent.as_posix()) == 'file':
                raise BackupError('Datei-/Verzeichniskollision im Archiv.')
    if kinds.get(MANIFEST) != 'file':
        raise BackupError('Sicherungsmanifest fehlt.')
    manifest_entry = next(entry for name, entry in members if name == MANIFEST)
    if manifest_entry.size > 32 * 1024 ** 2:
        raise BackupError('Sicherungsmanifest zu gross.')
    try:
        with tar.extractfile(manifest_entry) as stream:
            payload = json.load(stream)
    except (ValueError, TypeError) as exc:
        raise BackupError('Ungueltiges Sicherungsmanifest.') from exc
    if isinstance(payload, dict) and payload.get('format') == FORMAT:
        manifest = payload.get('files')
        expected = {name for name, entry in members if entry.isfile() and name != MANIFEST}
    else:
        manifest = payload  # Aeltere eigene Sicherungen: nur Laufzeitdateien gehasht.
        expected = {name for name, entry in members if entry.isfile() and name.split('/')[0] in {'data', 'config', 'secrets'}}
    if not isinstance(manifest, dict) or set(manifest) != expected:
        raise BackupError('Manifest und Archivbestand stimmen nicht ueberein.')
    for name, sha in manifest.items():
        if relative_name(name) != name or not isinstance(sha, str) or not HASH.fullmatch(sha):
            raise BackupError('Ungueltiger Eintrag im Sicherungsmanifest.')
    for name, entry in members:
        if name in manifest:
            with tar.extractfile(entry) as stream:
                if digest(stream) != manifest[name]:
                    raise BackupError('Dateihash im Archiv stimmt nicht mit Manifest ueberein.')
    return members, manifest, total


def verified_archive(archive, backup_dir):
    backup_dir = real_directory(backup_dir)
    archive = Path(archive).absolute()
    if archive.parent != backup_dir or not NAME.fullmatch(archive.name) or archive.is_symlink():
        raise BackupError('Nur eigene Sicherungsdateien direkt im Backupverzeichnis sind erlaubt.')
    sidecar = archive.with_name(archive.name + '.sha256')
    for path in (archive, sidecar):
        if not stat.S_ISREG(path.lstat().st_mode) or path.is_symlink():
            raise BackupError('Sicherung und Pruefsumme muessen regulaere Dateien sein.')
    if sidecar.stat().st_size > 2048:
        raise BackupError('Ungueltige Pruefsummendatei.')
    line = sidecar.read_text(encoding='utf-8').strip()
    match = re.fullmatch(r'([0-9a-fA-F]{64}) [ *](.+)', line)
    allowed = {archive.name, 'backups/' + archive.name, str(archive)}
    if not match or match[2] not in allowed:
        raise BackupError('Pruefsummendatei verweist nicht auf dieses Archiv.')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    stream = os.fdopen(os.open(archive, flags), 'rb')
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or digest(stream) != match[1].lower():
        stream.close()
        raise BackupError('Sicherungspruefsumme stimmt nicht ueberein.')
    stream.seek(0)
    return stream


def restore_archive(archive, backup_dir, *, max_bytes=MAX_EXPANDED):
    backup_dir = real_directory(backup_dir)
    with verified_archive(archive, backup_dir) as stream, tarfile.open(fileobj=stream, mode='r:gz') as tar:
        # Vollstaendige Pruefung vor Erstellung eines einzigen Zieldateipfads.
        members, manifest, expanded = validated_members(tar, max_bytes=max_bytes)
        if shutil.disk_usage(backup_dir).free < expanded + 256 * 1024 ** 2:
            raise BackupError('Nicht genug Platz fuer die isolierte Wiederherstellung.')
        destination = Path(tempfile.mkdtemp(prefix='restore-test.', dir=backup_dir))
        os.chmod(destination, 0o700)
        private_roots = {'data', 'config', 'secrets', 'tests'}
        for name, entry in members:
            target = destination / name
            private = name.split('/')[0] in private_roots or PurePosixPath(name).name == '.env'
            if entry.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700 if private else 0o755)
                if name:
                    target.chmod(0o700 if private else 0o755)
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700 if private else 0o755)
            with tar.extractfile(entry) as source, target.open('xb') as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
            mode = (0o700 if entry.mode & 0o100 else 0o600) if private else (0o755 if entry.mode & 0o100 else 0o644)
            os.chmod(target, mode)
            if name in manifest:
                with target.open('rb') as check:
                    if digest(check) != manifest[name]:
                        raise BackupError('Wiederhergestellte Datei stimmt nicht mit Manifest ueberein.')
        # Container lesen Laufzeitdaten als UID 1000. Keine Archiv-Owner oder
        # setuid-Rechte uebernehmen; Nginx benoetigt seine oeffentliche Einzeldatei.
        if os.name == 'posix' and os.geteuid() == 0:
            for directory in ('data', 'config', 'secrets', 'tests'):
                base = destination / directory
                if base.exists():
                    os.chown(base, 1000, 1000)
                    for path in base.rglob('*'):
                        os.chown(path, 1000, 1000)
        nginx = destination / 'config/nginx.conf'
        if nginx.is_file():
            nginx.chmod(0o644)
    return destination


def verify_restored(destination):
    root = real_directory(destination)
    database = root / 'data/monitor.db'
    if not database.is_file():
        raise BackupError('Wiederhergestellte Anwendungsdatenbank fehlt.')
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise BackupError('Datenbankintegritaet fehlgeschlagen.')
        tables = ('projects', 'runs', 'snapshots', 'changes', 'reports')
        counts = {table: connection.execute('SELECT count(*) FROM ' + table).fetchone()[0] for table in tables}
        checked = 0
        for file, sha in connection.execute('SELECT file,sha256 FROM snapshots WHERE file IS NOT NULL'):
            relative = relative_name(file)
            path = root / 'data' / relative
            if path.is_symlink() or not path.is_file() or not isinstance(sha, str) or not HASH.fullmatch(sha):
                raise BackupError('Archivierte Originaldatei fehlt oder hat ungueltige Metadaten.')
            with path.open('rb') as stream:
                if digest(stream) != sha:
                    raise BackupError('Archiviertes Original stimmt nicht mit der Datenbank ueberein.')
            checked += 1
    if not (root / 'data/browser/profile').is_dir() or not (root / 'secrets/credentials.json').is_file():
        raise BackupError('Browserprofil oder private Konfiguration fehlt.')
    return {'database': 'PASS', 'counts': counts, 'originals_verified': checked}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--max-gib', type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.max_gib <= 100:
        parser.error('--max-gib muss zwischen 1 und 100 liegen')
    root = real_directory(args.root)
    archive = args.archive if args.archive.is_absolute() else root / args.archive
    try:
        destination = restore_archive(archive, root / 'backups', max_bytes=args.max_gib * 1024 ** 3)
        result = verify_restored(destination)
        result['restore_directory'] = str(destination)
        print(json.dumps(result, ensure_ascii=False))
        print('Produktivdaten unveraendert. Testverzeichnis enthaelt sensible Daten.')
    except (BackupError, OSError, sqlite3.Error, tarfile.TarError) as exc:
        raise SystemExit('Wiederherstellung abgebrochen: ' + (str(exc) if isinstance(exc, BackupError) else type(exc).__name__))


if __name__ == '__main__':
    main()
