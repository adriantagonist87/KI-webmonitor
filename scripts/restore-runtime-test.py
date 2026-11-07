#!/usr/bin/env python3
"""Restore-Kopie ohne Netz, Ports oder Zugriff auf produktive Container starten."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from backup_archive import BackupError, real_directory, verify_restored
from backup import configured_runtime_root, command as local_command


def command(args):
    return local_command(args)


def ready(args):
    for attempt in range(40):
        try:
            return command(args)
        except BackupError:
            if attempt == 39:
                raise BackupError('Wiederhergestellter Testdienst wurde nicht bereit.') from None
            time.sleep(1)


def persistent_cookies(profile):
    """Nur Anzahl lesen, niemals Namen oder Cookie-Inhalte protokollieren."""
    chrome_now = int((time.time() + 11644473600) * 1000000)
    for candidate in (profile / 'Default/Network/Cookies', profile / 'Default/Cookies'):
        if candidate.is_file():
            connection = sqlite3.connect(candidate.as_uri() + '?mode=ro', uri=True)
            try:
                return connection.execute('SELECT count(*) FROM cookies WHERE expires_utc>?', (chrome_now,)).fetchone()[0]
            finally:
                connection.close()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('restored', type=Path)
    parser.add_argument('--root', type=Path, help='Laufzeitwurzel mit backups/, normalerweise KIWM_ROOT')
    parser.add_argument('--compose-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--app-image')
    parser.add_argument('--browser-image')
    args = parser.parse_args()
    os.umask(0o077)
    compose_root = real_directory(args.compose_root)
    root = real_directory(args.root) if args.root else configured_runtime_root(compose_root)
    restored = args.restored if args.restored.is_absolute() else root / args.restored
    restored = real_directory(restored)
    if restored.parent != root / 'backups' or not restored.name.startswith('restore-test.'):
        raise SystemExit('Nur eine isolierte Restore-Testkopie direkt im Backupverzeichnis ist erlaubt.')
    if any(path.is_symlink() for path in restored.rglob('*')):
        raise SystemExit('Restore-Testkopie enthaelt Links; Laufzeittest abgebrochen.')
    proof = verify_restored(restored)
    # Ausschliesslich Kopie aendern. Automatische Pruefungen in der Kopie sperren.
    connection = sqlite3.connect(restored / 'data/monitor.db')
    try:
        connection.execute("INSERT OR REPLACE INTO state(key,value) VALUES('manual','1')")
        connection.commit()
    finally:
        connection.close()
    if os.name == 'posix' and os.geteuid() == 0:
        for path in [restored / 'data', *(restored / 'data').rglob('*')]:
            os.chown(path, 1000, 1000)
    expected_cookies = persistent_cookies(restored / 'data/browser/profile')
    config = json.loads(command(['docker', 'compose', '--project-directory', str(compose_root), '-f', str(compose_root / 'compose.yaml'), 'config', '--format', 'json']))
    app_tag = args.app_image or config['services']['app']['image']
    browser_tag = args.browser_image or config['services']['browser']['image']
    # Nur lokal vorhandene, fest aufgeloeste Images verwenden; kein Pull.
    app_image = command(['docker', 'image', 'inspect', app_tag, '--format', '{{.Id}}'])
    browser_image = command(['docker', 'image', 'inspect', browser_tag, '--format', '{{.Id}}'])
    suffix = uuid.uuid4().hex[:12]
    app_name, browser_name = 'kiwm-restore-app-' + suffix, 'kiwm-restore-browser-' + suffix
    created = []
    common = ['--network', 'none', '--user', '1000:1000', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
              '--read-only', '--init', '--tmpfs', '/tmp:size=512m,uid=1000,gid=1000,mode=1777']
    try:
        created.append(app_name)
        command(['docker', 'run', '-d', '--name', app_name, *common,
                 '-e', 'ROLE=app', '-e', 'HOME=/tmp/home', '-e', 'ANONYMIZED_TELEMETRY=false',
                 '-v', str(restored / 'data') + ':/data', '-v', str(restored / 'config') + ':/config:ro',
                 '-v', str(restored / 'secrets') + ':/secrets:ro', app_image])
        ready(['docker', 'exec', app_name, 'python', '-c',
               "import sqlite3,urllib.request; from bs4 import BeautifulSoup; c=sqlite3.connect('/data/monitor.db'); names=[r[0] for r in c.execute('SELECT name FROM projects')]; r=urllib.request.urlopen('http://localhost:8000/',timeout=5); soup=BeautifulSoup(r.read(),'html.parser'); actual=[e.get_text() for e in soup.select('[data-project] h2')]; assert sorted(names)==sorted(actual) and r.status==200; print('RESTORED_APP_UI_OK')"])
        document_script = """import sqlite3,urllib.request,hashlib
from bs4 import BeautifulSoup
c=sqlite3.connect('/data/monitor.db')
row=c.execute("SELECT id,sha256 FROM snapshots WHERE kind='document' AND file IS NOT NULL ORDER BY captured DESC LIMIT 1").fetchone()
if row:
 text=urllib.request.urlopen('http://localhost:8000/documents/'+row[0],timeout=5).read().decode()
 body=' '.join(BeautifulSoup(text,'html.parser').get_text(' ',strip=True).split())
 assert row[1] in body and 'Originaldatei herunterladen' in body
 raw=urllib.request.urlopen('http://localhost:8000/files/'+row[0]+'/download',timeout=30).read()
 assert raw and hashlib.sha256(raw).hexdigest()==row[1]
 print('PASS')
else: print('NOT_APPLICABLE_NO_ARCHIVED_DOCUMENT')
"""
        document_result = ready(['docker', 'exec', app_name, 'python', '-c', document_script])
        created.append(browser_name)
        command(['docker', 'run', '-d', '--name', browser_name, *common,
                 '--tmpfs', '/run:size=16m,uid=1000,gid=1000', '--shm-size', '1g', '-e', 'DISPLAY=:99', '-e', 'HOME=/data/home',
                 '-v', str(restored / 'data/browser') + ':/data', browser_image])
        cookie_script = """import json,urllib.request,websocket
targets=json.load(urllib.request.urlopen('http://localhost:9222/json/version',timeout=5))
ws=websocket.create_connection(targets['webSocketDebuggerUrl'],origin='http://localhost:9222',timeout=8)
ws.send(json.dumps({'id':1,'method':'Storage.getCookies'}))
while True:
 response=json.loads(ws.recv())
 if response.get('id')==1:break
assert 'error' not in response
cookies=response.get('result',{}).get('cookies',[])
print(len(cookies))
ws.close()
"""
        loaded = int(ready(['docker', 'exec', browser_name, 'python3', '-c', cookie_script]))
        if expected_cookies and not loaded:
            raise BackupError('Kopiertes Browserprofil gestartet, aber keine gespeicherten Cookies geladen.')
        proof.update({'restored_app_ui': 'PASS', 'restored_document_view': document_result,
                      'restored_browser_cdp': 'PASS', 'persistent_cookies_before': expected_cookies,
                      'cookies_loaded': loaded, 'network': 'none', 'published_ports': 0,
                      'restore_directory': str(restored)})
        print(json.dumps(proof, ensure_ascii=False))
        print('Website-Anmeldungen wurden offline nicht gegen die jeweiligen Anbieter validiert.')
    finally:
        for name in reversed(created):
            try:
                command(['docker', 'rm', '-f', name])
            except BackupError:
                pass


if __name__ == '__main__':
    try:
        main()
    except (BackupError, OSError, ValueError, KeyError, sqlite3.Error) as exc:
        raise SystemExit('Laufzeittest abgebrochen: ' + (str(exc) if isinstance(exc, BackupError) else type(exc).__name__))
