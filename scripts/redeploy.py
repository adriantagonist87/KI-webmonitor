#!/usr/bin/env python3
"""Update one verified local Portainer Standalone stack while its agent is idle."""
from __future__ import annotations
import argparse
from contextlib import closing, contextmanager
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

import backup
import bootstrap
from backup_archive import BackupError, real_directory


class DeployError(Exception):
    pass


def command(args, *, env=None):
    if not args or args[0] != 'docker':
        raise DeployError('Nur lokale Docker-Betriebsbefehle sind erlaubt.')
    environment = dict(os.environ if env is None else env)
    for key in list(environment):
        if key.startswith('DOCKER_'):
            environment.pop(key)
    # Do not inherit a remote Docker context: SQLite and bind paths are local.
    local_args = ['docker', '--host', 'unix:///var/run/docker.sock', *args[1:]]
    result = subprocess.run(local_args, env=environment, capture_output=True, text=True, check=False)
    if result.returncode:
        raise DeployError('Lokaler Betriebsbefehl fehlgeschlagen; keine Ausgabe mit privaten Daten übernommen.')
    return result.stdout.strip()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise DeployError('Portainer-Weiterleitung nicht erlaubt.')


class Portainer:
    def __init__(self, token_file):
        descriptor = os.open(token_file, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'r', encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 16384:
                raise DeployError('Token-Datei muss eine private reguläre Datei mit Modus 0600 sein.')
            self.token = stream.read().strip()
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', self.token):
            raise DeployError('Token-Datei enthält keinen gültigen Bearer-Token.')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path, *, payload=None):
        if not path.startswith('/api/') or '..' in path or '\n' in path:
            raise DeployError('Ungültiger lokaler Portainer-Endpunkt.')
        request = urllib.request.Request('http://127.0.0.1:9000' + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            method='PUT' if payload is not None else 'GET',
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=240 if payload is not None else 15) as response:
                return json.loads(response.read(2 * 1024 * 1024))
        except urllib.error.HTTPError as error:
            raise DeployError('Portainer HTTP ' + str(error.code) + '; Antwortinhalt wird nicht protokolliert.') from None
        except (OSError, ValueError) as error:
            raise DeployError('Portainer-Anfrage fehlgeschlagen (' + type(error).__name__ + ').') from None


def identity(stack, stack_id, endpoint_id, name):
    if (stack.get('Id'), stack.get('EndpointId'), stack.get('Name'), stack.get('Type')) != (stack_id, endpoint_id, name, 2):
        raise DeployError('Stack-ID, Name, Umgebung oder Standalone-Typ stimmen nicht überein.')
    if stack.get('GitConfig') or stack.get('Status') != 1:
        raise DeployError('Nur ein aktiver, dateibasierter Standalone-Stack darf aktualisiert werden.')


def merged_env(existing, local):
    result = {}
    for pair in existing or []:
        name, value = pair.get('name'), pair.get('value')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) or not isinstance(value, str) or name in result:
            raise DeployError('Gespeicherte Stack-Variablen sind ungültig oder doppelt.')
        result[name] = value
    fixed = {'KIWM_ROOT', 'MONITOR_SUBNET', 'BROWSER_IP', 'FIXTURE_IP',
             'WEB_PORT', 'BROWSER_PORT', 'MAILPIT_PORT', 'BROWSER_UI_URL', 'MAILPIT_UI_URL',
             'KIWM_LAN_IP', 'KIWM_LAN_CONFIG'}
    for name, value in local.items():
        if name in fixed and name in result and result[name] != value:
            raise DeployError('Deploymentpfade, Ports oder Netzwerte widersprechen den vorhandenen Stack-Variablen.')
        result[name] = value
    return result


def release_env(values, retire_legacy=False):
    """Remove only known obsolete stack variables during the explicit migration."""
    obsolete={'APP_IMAGE','BROWSER_IMAGE','PROXY_IMAGE','IMAGE_PULL_POLICY',
              'BROWSER_IP','FIXTURE_IP','MAILPIT_PORT','BROWSER_UI_URL','MAILPIT_UI_URL',
              'COMPOSE_PROJECT_NAME','KIWM_ROOT'} if retire_legacy else set()
    optional={'KIWM_AGENT_MODEL','KIWM_ANALYSIS_MODEL','KIWM_VISION_MODEL'}
    return {k:v for k,v in values.items() if k not in obsolete and (k not in optional or v)}


def preflight(source, values, name, *, run=command):
    spec = importlib.util.spec_from_file_location('kiwm_validate', source / 'scripts/validate-compose.py')
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    cli_source = (source / 'compose.yaml').read_text(encoding='utf-8')
    standard_stack = (source / 'portainer-stack.yaml').read_text(encoding='utf-8')
    validator.require(validator.portainer_source(cli_source) == standard_stack, 'Stack-YAML ist nicht synchron mit Compose.')
    lan = bool(values.get('KIWM_LAN_IP'))
    stack_name = 'portainer-lan-stack.yaml' if lan else 'portainer-stack.yaml'
    stack_source = (source / stack_name).read_text(encoding='utf-8')
    environment = dict(os.environ)
    variables = set(re.findall(r'\$\{([A-Za-z_][A-Za-z0-9_]*)', cli_source + stack_source))
    for key in variables:
        environment.pop(key, None)
    # Keep unknown Stack Env in Portainer, but never let it replace the host's
    # PATH, loader variables or Docker connection while running preflight.
    environment.update({key: value for key, value in values.items() if key in variables})
    environment['COMPOSE_PROJECT_NAME'] = name  # Portainer also supplies its own project name.
    models = []
    for files in ((['compose.yaml', 'compose.lan.yaml'] if lan else ['compose.yaml']), [stack_name]):
        raw = run(['docker', 'compose', '--project-directory', str(source), '--env-file', os.devnull,
                   *[part for filename in files for part in ('-f', str(source / filename))],
                   'config', '--format', 'json'], env=environment)
        model = json.loads(raw)
        validator.check_runtime(model, image_only=True)
        models.append(model)
    normalized = copy.deepcopy(models[0])
    for service in normalized['services'].values():
        service.pop('build', None)
    validator.require(normalized == models[1], 'Compose- und Portainer-Laufzeit unterscheiden sich.')
    mounts = {v['target']: Path(v['source']) for v in models[1]['services']['app']['volumes']}
    runtime = mounts['/data'].parent
    bootstrap.plain_ancestors(runtime)
    if any(mounts.get('/' + directory) != runtime / directory for directory in ('data', 'config', 'secrets')):
        raise DeployError('Die Laufzeit-Mounts haben keine gemeinsame Datenwurzel.')
    return real_directory(runtime), models[1], stack_source


def probe_assets(model, *, run=command):
    """Catch unreadable image assets before stopping services or touching SQLite."""
    checks = (
        ('app', '1000:1000', 'test -r /app/main.py && test -r /app/gateway.py && test -r /app/templates/index.html'),
        ('browser', '1000:1000', 'test -r /etc/brave/policies/managed/monitor.json && test -r /usr/local/bin/close-browser.py && test -x /usr/local/bin/start-browser'),
        ('proxy', 'proxy', 'test -r /etc/squid/squid.conf && test -x /usr/local/bin/start-proxy && test -x /usr/local/bin/check-proxy'),
    )
    for service, user, script in checks:
        try:
            run(['docker', 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                 '--security-opt', 'no-new-privileges:true', '--user', user, '--entrypoint', '/bin/sh',
                 model['services'][service]['image'], '-eu', '-c', script])
        except (DeployError, OSError):
            raise DeployError('Image-Asset-Prüfung für ' + service + ' fehlgeschlagen; keine App gestoppt und kein Update gestartet.') from None
    if 'ingress' in model['services']:
        run(['docker','run','--rm','--network','none','--read-only','--cap-drop','ALL',
             '--security-opt','no-new-privileges:true','--user','101:101','--entrypoint','/bin/sh',
             model['services']['ingress']['image'],'-eu','-c','test -r /etc/kiwm/nginx.conf && test -x /usr/local/bin/start-ingress'])


@contextmanager
def paused_app(runtime, running, *, run=command):
    """Hold the enqueue write lock until app stops; release before API/healthchecks."""
    stopped, committed = [], False
    outcome = {'put_started': False, 'success': False}
    connection = sqlite3.connect((runtime / 'data/monitor.db').as_uri() + '?mode=rw', uri=True, timeout=2)
    previous = None
    try:
        connection.execute('BEGIN IMMEDIATE')
        if connection.execute("SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1").fetchone():
            raise DeployError('Laufende oder wartende Prüfung vorhanden; kein Update ausgeführt.')
        row = connection.execute("SELECT value FROM state WHERE key='manual'").fetchone()
        if row is None or row[0] not in ('0', '1'):
            raise DeployError('Manueller Modus konnte nicht sicher bestimmt werden.')
        previous = row[0]
        connection.execute("UPDATE state SET value='1' WHERE key='manual'")
        for item in sorted(running, key=lambda item: item['service'] != 'ingress'):
            if item['service'] in ('app', 'ingress'):
                stopped.append(item)
                run(['docker', 'stop', '--time', '45', item['id']])
        connection.commit()
        committed = True
        connection.close()
        connection = None
        yield outcome
    finally:
        if connection is not None:
            connection.rollback()
            connection.close()
        # Old IDs can be removed by the stack update. Never blindly restart them
        # after success; on failure only try the exact previously running IDs.
        if not outcome['success']:
            for item in reversed(stopped):
                try:
                    run(['docker', 'start', item['id']])
                except (DeployError, BackupError, OSError):
                    pass
        if committed and (outcome['success'] or not outcome['put_started']):
            with closing(sqlite3.connect(runtime / 'data/monitor.db', timeout=5)) as database, database:
                database.execute('BEGIN IMMEDIATE')
                database.execute("UPDATE state SET value=? WHERE key='manual'", (previous,))
        # An ambiguous/partial API failure deliberately leaves manual=1. The
        # operator can inspect Portainer and resume through the browser page.


def containers(project, *, run=command):
    ids = run(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project]).split()
    if not ids or any(not re.fullmatch(r'[a-f0-9]{12,64}', value) for value in ids):
        raise DeployError('Eigene Stack-Container konnten nicht eindeutig ermittelt werden.')
    items = json.loads(run(['docker', 'inspect', *ids]))
    services = {}
    for item in items:
        labels = item.get('Config', {}).get('Labels') or {}
        service = labels.get('com.docker.compose.service')
        if labels.get('com.docker.compose.project') != project or service in services or labels.get('com.docker.compose.oneoff', 'False').lower() == 'true':
            raise DeployError('Projektcontainer sind nicht eindeutig zugeordnet.')
        services[service] = item
    return services


def wait_ready(project, image_ids, *, run=command, timeout=180):
    deadline = time.monotonic() + timeout
    while True:
        try:
            current = containers(project, run=run)
        except DeployError:
            # Newer Portainer versions may acknowledge before Compose has
            # finished replacing old IDs; require the final healthy set below.
            current = {}
        if set(current) == set(image_ids) and all(
                c['State']['Running'] and c['State'].get('Health', {}).get('Status') == 'healthy'
                and c['Image'] == image_ids[service] for service, c in current.items()):
            return
        if time.monotonic() >= deadline:
            raise DeployError('Neue Container nicht vollständig gesund; manueller Modus bleibt aktiv.')
        time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1], help='Quell-/Compose-Verzeichnis')
    parser.add_argument('--stack-id', type=int, required=True)
    parser.add_argument('--endpoint-id', type=int, required=True)
    parser.add_argument('--expected-name', required=True)
    parser.add_argument('--token-file', type=Path, default=Path('/tmp/kiwm-portainer.jwt'))
    parser.add_argument('--retire-test-services',action='store_true',help='Beim Umstieg ausschließlich eigene alte fixture/mailpit-Container entfernen; Daten bleiben erhalten')
    args = parser.parse_args()
    changed = False
    os.umask(0o077)
    def interrupted(_number, _frame):
        raise DeployError('Update unterbrochen; Stack prüfen, bevor Automatik fortgesetzt wird.')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if os.name != 'posix' or os.geteuid() != 0 or args.stack_id < 1 or args.endpoint_id < 1 or not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', args.expected_name):
            raise DeployError('Als root in der LXC ausführen; positive IDs und gültigen Stacknamen angeben.')
        import fcntl
        source = real_directory(args.root)
        local = bootstrap.env_file(source / '.env')
        api = Portainer(args.token_file)
        stack_path = '/api/stacks/' + str(args.stack_id)
        original = api.request(stack_path)
        identity(original, args.stack_id, args.endpoint_id, args.expected_name)
        endpoint = api.request('/api/endpoints/' + str(args.endpoint_id))
        if endpoint.get('Id') != args.endpoint_id:
            raise DeployError('Portainer-Umgebung stimmt nicht überein.')
        info = api.request('/api/endpoints/' + str(args.endpoint_id) + '/docker/info')
        local_info = json.loads(command(['docker', 'info', '--format', '{{json .}}']))
        if not info.get('ID') or info['ID'] != local_info.get('ID') or info.get('Swarm', {}).get('LocalNodeState') != 'inactive':
            raise DeployError('Portainer-Umgebung ist nicht der lokale Standalone-Docker-Daemon.')
        values = merged_env(original.get('Env'), local)
        runtime, model, yaml = preflight(source, values, args.expected_name)
        image_ids = {service: command(['docker', 'image', 'inspect', '--format', '{{.Id}}', item['image']])
                     for service, item in model['services'].items()}
        probe_assets(model)
        retired={'fixture','mailpit'} if args.retire_test_services else set()
        running = backup.own_running_containers(source, args.expected_name, run=command,allowed_retired=retired)
        actual={item['service'] for item in running}
        if not set(model['services'])<=actual<=set(model['services'])|retired:
            raise DeployError('Eigene Dienste stimmen nicht mit dem Updateziel überein; Zustand prüfen.')
        current = containers(args.expected_name)
        actual_mounts = {item['Destination']: item['Source'] for item in current['app']['Mounts']}
        wanted_mounts = {v['target']: v['source'] for v in model['services']['app']['volumes']}
        if any(actual_mounts.get(target) != path for target, path in wanted_mounts.items()):
            raise DeployError('Neue YAML würde andere App-Daten einbinden; kein automatischer Pfadwechsel erlaubt.')
        previous_browser={v['Destination']:v['Source'] for v in current['browser']['Mounts']}
        next_browser={v['target']:v['source'] for v in model['services']['browser']['volumes']}
        if previous_browser!=next_browser:raise DeployError('Browserprofil-Pfadwechsel verweigert.')
        directory = runtime / 'backups'
        directory.mkdir(mode=0o700, exist_ok=True)
        real_directory(directory)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if api.request(stack_path) != original:
                raise DeployError('Stack wurde zwischenzeitlich geändert; erneut prüfen.')
            with paused_app(runtime, running) as outcome:
                if api.request(stack_path) != original:
                    raise DeployError('Stack wurde zwischenzeitlich geändert; erneut prüfen.')
                saved_values=release_env(values,args.retire_test_services)
                payload = {'StackFileContent': yaml, 'Env': [{'name': k, 'value': v} for k, v in saved_values.items()],
                           'Prune': False, 'PullImage': False}
                outcome['put_started'] = changed = True
                updated = api.request(stack_path + '?endpointId=' + str(args.endpoint_id), payload=payload)
                identity(updated, args.stack_id, args.endpoint_id, args.expected_name)
                for item in running:
                    if item['service'] in retired and item['service'] not in model['services']:
                        found=json.loads(command(['docker','inspect',item['id']]))[0]
                        labels=found['Config']['Labels']
                        if labels.get('com.docker.compose.project')!=args.expected_name or labels.get('com.docker.compose.service')!=item['service']:
                            raise DeployError('Alter Testcontainer nicht eindeutig zugeordnet.')
                        command(['docker','stop','--time','20',item['id']])
                        command(['docker','rm',item['id']])
                wait_ready(args.expected_name, image_ids)
                if merged_env(api.request(stack_path).get('Env'), {}) != saved_values:
                    raise DeployError('Stack-Umgebungsvariablen wurden nicht vollständig erhalten.')
                outcome['success'] = True
        finally:
            os.close(descriptor)
        print('PORTAINER_UPDATE_OK: Stackidentität, Datenpfade, Variablen und alle Dienste gesund geprüft.')
        return 0
    except (DeployError, BackupError, OSError, ValueError, KeyError, sqlite3.Error, KeyboardInterrupt) as error:
        message = str(error) if isinstance(error, (DeployError, BackupError)) else type(error).__name__
        print('PORTAINER_UPDATE_ABORTED: ' + message, file=sys.stderr)
        if changed:
            print('Manuellen Modus und Containerstatus prüfen; Automatik erst nach erfolgreicher Prüfung freigeben.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
