#!/usr/bin/env python3
"""Konsistente private Sicherung; laufende/wartende Pruefungen niemals abbrechen."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys

from backup_archive import BackupError, create_archive, real_directory


def command(args):
    environment = dict(os.environ)
    if args and args[0] == 'docker':
        # Lokale Datenbanksperren duerfen niemals einen Remote-Docker steuern.
        # Weder Benutzer-Context noch DOCKER_HOST/TLS-Umgebung uebernehmen.
        args = ['docker', '--host', 'unix:///var/run/docker.sock', *args[1:]]
        for key in [name for name in environment if name.startswith('DOCKER_')]:
            environment.pop(key, None)
    try:
        result = subprocess.run(args, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError as exc:
        raise BackupError('Betriebsbefehl konnte nicht gestartet werden.') from exc
    if result.returncode:
        raise BackupError('Betriebsbefehl fehlgeschlagen; Containerstatus separat pruefen.')
    return result.stdout.strip()


def own_running_containers(root, project=None, *, run=command, allowed_retired=frozenset()):
    config = json.loads(run(['docker', 'compose', '--project-directory', str(root), '-f', str(root / 'compose.yaml'), 'config', '--format', 'json']))
    project = project or config.get('name')
    if not isinstance(project, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', project):
        raise BackupError('Ungueltiger Compose-Projektname.')
    identifiers = run(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project]).split()
    if not identifiers:
        raise BackupError('Keine Container dieses Compose-Projekts gefunden; nichts gestoppt.')
    if any(not re.fullmatch(r'[a-f0-9]{12,64}', value) for value in identifiers):
        raise BackupError('Ungueltige Containerkennung.')
    inspected = json.loads(run(['docker', 'inspect', *identifiers]))
    running = []
    for item in inspected:
        labels = item.get('Config', {}).get('Labels') or {}
        service = labels.get('com.docker.compose.service')
        if labels.get('com.docker.compose.project') != project or service not in set(config['services']) | set(allowed_retired):
            raise BackupError('Abweichende/alte Projektcontainer gefunden; Bereinigung vor Sicherung pruefen.')
        if item['State']['Running']:
            if labels.get('com.docker.compose.oneoff', 'False').lower() == 'true':
                raise BackupError('Einmaliger Projektcontainer aktiv; Sicherung spaeter starten.')
            running.append({'id': item['Id'], 'service': service})
    return running


def configured_runtime_root(root, *, run=command):
    config = json.loads(run(['docker', 'compose', '--project-directory', str(root), '-f', str(root / 'compose.yaml'), 'config', '--format', 'json']))
    volumes = config['services']['app']['volumes']
    mounts = {entry.get('target'): entry for entry in volumes if isinstance(entry, dict)}
    roots = set()
    for directory in ('data', 'config', 'secrets'):
        mount = mounts.get('/' + directory, {})
        source = Path(mount.get('source', ''))
        if mount.get('type') != 'bind' or not source.is_absolute() or source.name != directory:
            raise BackupError('Unerwartete Laufzeit-Mounts; Datenpfad vor Sicherung pruefen.')
        roots.add(source.parent)
    if len(roots) != 1:
        raise BackupError('Daten, Konfiguration und Zugangsdaten haben unterschiedliche Wurzelpfade.')
    return real_directory(roots.pop())


@contextmanager
def quiesced(root, running, *, run=command):
    stopped = []
    order = {'ingress': 0, 'app': 1, 'browser': 2, 'gateway': 3, 'fixture': 4, 'mailpit': 5, 'proxy': 6}
    ordered = sorted(running, key=lambda item: order.get(item['service'], 3))
    connection = sqlite3.connect((root / 'data/monitor.db').as_uri() + '?mode=rw', uri=True, timeout=2)
    try:
        # begin_attempt/enqueue brauchen ebenfalls BEGIN IMMEDIATE. Zwischen
        # dieser Pruefung und Stop der App kann daher keine neue Pruefung starten.
        connection.execute('BEGIN IMMEDIATE')
        if connection.execute("SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1").fetchone():
            raise BackupError('Laufende oder wartende Pruefung vorhanden. Sicherung spaeter erneut starten; nichts gestoppt.')
        for item in ordered:
            if item['service'] in {'ingress', 'app'}:
                stopped.append(item)
                run(['docker', 'stop', '--time', '45', item['id']])
        connection.rollback()
        connection.close()
        connection = None
        for item in ordered:
            if item['service'] not in {'ingress', 'app'}:
                stopped.append(item)
                run(['docker', 'stop', '--time', '45', item['id']])
        yield
    finally:
        if connection is not None:
            connection.rollback()
            connection.close()
        restart_errors = False
        for item in reversed(stopped):
            try:
                run(['docker', 'start', item['id']])
            except BackupError:
                restart_errors = True
        if restart_errors:
            raise BackupError('Mindestens ein zuvor laufender Projektcontainer konnte nicht erneut gestartet werden; Status pruefen.')


def main():
    import fcntl  # Linux-LXC; Archiv- und Logiktests bleiben plattformunabhaengig.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--project', help='Tatsaechlicher Portainer-/Compose-Stackname, falls abweichend')
    args = parser.parse_args()
    os.umask(0o077)
    def interrupted(_signum, _frame):
        raise BackupError('Sicherung unterbrochen; zuvor laufende Container werden erneut gestartet.')
    signal.signal(signal.SIGTERM, interrupted)
    rotation_started = False
    try:
        root = real_directory(args.root)
        if os.geteuid() != 0:
            raise BackupError('Als Administrator/root innerhalb der LXC ausfuehren.')
        runtime_root = configured_runtime_root(root)
        directory = runtime_root / 'backups'
        directory.mkdir(mode=0o700, exist_ok=True)
        real_directory(directory)
        os.chmod(directory, 0o700)
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            running = own_running_containers(root, args.project)
            archive = directory / ('ki-webmonitor-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.tar.gz')
            with quiesced(runtime_root, running):
                result = create_archive(root, archive, runtime_root=runtime_root)
            print(json.dumps(result, ensure_ascii=False))
        finally:
            os.close(fd)
        rotation_started = True
        rotation = command([sys.executable, str(Path(__file__).resolve().with_name('backup-retention.py')),
                            '--directory', str(directory)])
        print(rotation)
        print('Sicherung enthaelt Browser-Sitzungen und Zugangsdaten. Extern verschluesselt aufbewahren.')
    except KeyboardInterrupt:
        raise SystemExit('Sicherung durch Bediener abgebrochen; Containerstatus pruefen.') from None
    except (BackupError, OSError, ValueError, KeyError, sqlite3.Error) as exc:
        prefix = ('Sicherung und Wiederanlauf erfolgreich; Rotation fehlgeschlagen, Archiv bleibt erhalten: '
                  if rotation_started else 'Sicherung abgebrochen: ')
        raise SystemExit(prefix + (str(exc) if isinstance(exc, BackupError) else type(exc).__name__))


if __name__ == '__main__':
    main()
