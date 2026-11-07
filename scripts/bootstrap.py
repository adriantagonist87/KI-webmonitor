#!/usr/bin/env python3
"""Prepare private bind mounts for a fresh Linux Compose or Portainer installation."""
from __future__ import annotations
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit

SOURCE = Path(__file__).resolve().parents[1]
DEFAULTS = {
    'MONITOR_SUBNET': '172.29.133.0/24', 'BROWSER_IP': '172.29.133.20',
    'FIXTURE_IP': '172.29.133.40', 'WEB_PORT': '8765', 'BROWSER_PORT': '8766',
    'MAILPIT_PORT': '8767', 'APP_IMAGE': 'ki-webmonitor-app:1.0.0',
    'BROWSER_IMAGE': 'ki-webmonitor-browser:1.0.0', 'PROXY_IMAGE': 'ki-webmonitor-proxy:1.0.0', 'IMAGE_PULL_POLICY': 'missing',
}


def linked(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def plain_ancestors(path):
    for candidate in (path, *path.parents):
        if linked(candidate):
            raise ValueError('Symlinks/Junctions sind fuer Installationspfade nicht erlaubt: ' + str(candidate))


def env_file(path):
    plain_ancestors(path)
    result = {}
    if path.exists() and not path.is_file():
        raise ValueError('.env muss eine regulaere Datei sein.')
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, sep, value = line.partition('=')
            if not sep or not re.fullmatch(r'[A-Z][A-Z0-9_]*', key):
                raise ValueError('Ungueltige .env-Zeile; KEY=VALUE ohne Shell-Ausdruecke verwenden.')
            if key in result:
                raise ValueError('Doppelter Schluessel in .env: ' + key)
            result[key] = value.strip().strip('"').strip("'")
    return result


def private_file(path, value):
    # O_EXCL makes a concurrent file or symlink creation fail rather than follow
    # or overwrite it. Private permissions apply from the first byte written.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as out:
        if hasattr(os, 'geteuid') and os.geteuid() == 0:
            os.fchown(out.fileno(), 1000, 1000)
        out.write(value)


def prepare(root: Path, values, *, source=SOURCE, refresh_proxy=False):
    root = Path(root)
    if not root.is_absolute():
        raise ValueError('Ein eigenes absolutes Projektverzeichnis ist erforderlich.')
    plain_ancestors(root)
    root = root.resolve()
    if root == Path(root.anchor):
        raise ValueError('Ein eigenes absolutes Projektverzeichnis ist erforderlich.')
    if any(c in str(root) for c in '\n\r$#'):
        raise ValueError('Projektpfad enthaelt ungueltige Zeichen.')
    if values.get('KIWM_ROOT') and Path(values['KIWM_ROOT']).resolve() != root:
        raise ValueError('--root und KIWM_ROOT widersprechen sich. Bestehende .env bewusst anpassen.')
    settings = {**DEFAULTS, **values, 'KIWM_ROOT': str(root)}
    # Legacy/explicit bind installations only; fresh release installs need no
    # host preparation and use named volumes directly from compose.yaml.
    settings.setdefault('VERSION','1.1.0')
    for key,directory in (('KIWM_DATA','data'),('KIWM_CONFIG','config'),('KIWM_SECRETS','secrets'),('KIWM_BROWSER_DATA','data/browser')):
        settings.setdefault(key,str(root/directory))
    if settings['IMAGE_PULL_POLICY'] not in ('never','missing','always'):
        raise ValueError('IMAGE_PULL_POLICY muss missing, always oder never sein.')
    ports = []
    for key in ('WEB_PORT', 'BROWSER_PORT', 'MAILPIT_PORT'):
        value = str(settings[key])
        if not value.isascii() or not value.isdigit() or not 1024 <= int(value) <= 65535:
            raise ValueError(key + ': Port muss zwischen 1024 und 65535 liegen.')
        settings[key] = str(int(value))
        ports.append(int(value))
    if len(set(ports)) != 3:
        raise ValueError('Die drei lokalen Ports muessen unterschiedlich sein.')
    for key, port_key in (('BROWSER_UI_URL', 'BROWSER_PORT'), ('MAILPIT_UI_URL', 'MAILPIT_PORT')):
        if key not in settings:
            continue
        try:
            url = urlsplit(str(settings[key]))
            valid = (url.scheme == 'http' and url.hostname in ('localhost', '127.0.0.1')
                     and url.port == int(settings[port_key]) and not url.username and not url.password
                     and url.path in ('', '/') and not url.query and not url.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(key + ' muss die lokale HTTP-Adresse mit dem konfigurierten ' + port_key + ' sein.')
    network = ipaddress.ip_network(settings['MONITOR_SUBNET'])
    private_ranges = tuple(ipaddress.ip_network(value) for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
    if network.version != 4 or not any(network.subnet_of(block) for block in private_ranges):
        raise ValueError('Privates IPv4-Docker-Subnetz nach RFC 1918 erforderlich.')
    addresses = []
    for key in ('BROWSER_IP', 'FIXTURE_IP'):
        ip = ipaddress.ip_address(settings[key])
        if ip not in network or ip in (network.network_address, network.network_address + 1, network.broadcast_address):
            raise ValueError(key + ' muss im Monitor-Subnetz liegen und darf nicht Netzwerk, Gateway oder Broadcast belegen.')
        addresses.append(ip)
    if addresses[0] == addresses[1]:
        raise ValueError('Browser und Fixture brauchen verschiedene IPs.')

    # All expected failures are checked before the first directory/file mutation.
    directories = ('data', 'data/browser', 'data/mailpit', 'config', 'secrets', 'tests')
    if root.exists() and not root.is_dir():
        raise ValueError('Projektverzeichnis erwartet.')
    for name in directories:
        path = root / name
        plain_ancestors(path)
        if path.exists() and not path.is_dir():
            raise ValueError('Verzeichnis erwartet: ' + name)
        if path.exists() and hasattr(os, 'geteuid') and os.geteuid() != 0:
            if path.stat().st_uid != os.geteuid() or not os.access(path, os.W_OK | os.X_OK):
                raise ValueError('Bind-Verzeichnis muss fuer UID 1000 vorbereitet werden; Bootstrap als root ausfuehren: ' + name)
    if hasattr(os, 'geteuid') and os.geteuid() not in (0, 1000):
        raise ValueError('Bootstrap als root oder Runtime-UID 1000 ausfuehren.')
    private_paths = list(dict.fromkeys([root / 'config/settings.json', root / 'secrets/credentials.json',
                                      *(root / 'secrets').glob('*.json')]))
    for path in (*private_paths, root / 'config/nginx.conf'):
        name = str(path.relative_to(root))
        plain_ancestors(path)
        if path.exists() and not path.is_file():
            raise ValueError('Regulaere Datei erwartet: ' + name)
        if path in private_paths and path.exists() and hasattr(os, 'geteuid') and os.geteuid() != 0:
            if path.stat().st_uid != os.geteuid() or not os.access(path, os.R_OK | os.W_OK):
                raise ValueError('Private Runtime-Datei muss fuer UID 1000 vorbereitet werden; Bootstrap als root ausfuehren: ' + name)
    template_path = Path(source) / 'config/nginx.conf.template'
    plain_ancestors(template_path)
    template = template_path.read_text(encoding='utf-8')
    if '__BROWSER_PORT__' not in template:
        raise ValueError('Proxy-Vorlage enthaelt keinen Browser-Port.')
    rendered = template.replace('__BROWSER_PORT__', settings['BROWSER_PORT'])
    proxy = root / 'config/nginx.conf'
    if proxy.exists() and not refresh_proxy and proxy.read_text(encoding='utf-8') != rendered:
        raise ValueError('nginx.conf weicht von der Vorlage ab. Pruefen und nur bewusst mit --refresh-proxy erneuern.')

    created = []
    root.mkdir(parents=True, exist_ok=True)
    for name in directories:
        path = root / name
        if not path.exists():
            path.mkdir(parents=True)
            created.append(name)
        # Git clones already contain config/. Correct only the explicitly named
        # bind directories, never the source tree or archived files recursively.
        if hasattr(os, 'geteuid') and os.geteuid() == 0:
            os.chown(path, 1000, 1000)
        path.chmod(0o700 if name != 'tests' else 0o755)
    initial = {
        'config/settings.json': {'omlx_url': '', 'analysis_model': '', 'agent_model': '', 'vision_model': '',
            'smtp_enabled': False, 'global_schedule': {'mode': 'daily', 'time': '06:00', 'weekday': 0, 'monthday': 1, 'interval': 6}},
        'secrets/credentials.json': {},
    }
    for name, value in initial.items():
        path = root / name
        if not path.exists():
            private_file(path, json.dumps(value, indent=2))
            created.append(name)
    for path in private_paths:
        if path.exists() and path.relative_to(root).as_posix() not in created:
            if hasattr(os, 'geteuid') and os.geteuid() == 0:
                os.chown(path, 1000, 1000)
            path.chmod(0o600)
    if not proxy.exists() or refresh_proxy:
        descriptor, temporary = tempfile.mkstemp(prefix='.nginx-', dir=proxy.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as out:
                out.write(rendered)
                if hasattr(os, 'fchmod'):
                    os.fchmod(out.fileno(), 0o644)
            os.replace(temporary, proxy)
        finally:
            Path(temporary).unlink(missing_ok=True)
        created.append('config/nginx.conf')
    return settings, created


def configure(*, source=SOURCE, root=None, environment=None, refresh_proxy=False):
    source = Path(source)
    path = source / '.env'
    file_values = env_file(path)
    environment = os.environ if environment is None else environment
    overrides = {k: v for k, v in environment.items() if k in DEFAULTS or k in ('KIWM_ROOT', 'BROWSER_UI_URL', 'MAILPIT_UI_URL')}
    values = {**file_values, **overrides}
    root = Path(root) if root is not None else Path(values.get('KIWM_ROOT', str(source)))
    if path.exists():
        # Preparing B while the persisted Compose file points at A is dangerous.
        # Neither existing dotenv values nor runtime data are rewritten implicitly.
        file_root = Path(file_values.get('KIWM_ROOT', str(source)))
        if root.resolve() != file_root.resolve():
            raise ValueError('Vorhandene .env verweist auf ein anderes KIWM_ROOT. .env bewusst anpassen, dann erneut starten.')
        for key in DEFAULTS:
            if key in overrides and overrides[key] != file_values.get(key, DEFAULTS[key]):
                raise ValueError('Umgebungsvariable widerspricht vorhandener .env: ' + key + '. .env bewusst anpassen.')
    settings, created = prepare(root, values, source=source, refresh_proxy=refresh_proxy)
    if not path.exists():
        content = '# Private installation paths and ports; no credentials.\n'
        content += ''.join(key + '=' + str(value) + '\n' for key, value in settings.items())
        private_file(path, content)
    return settings, created


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, help='Runtime-Verzeichnis; Standard: KIWM_ROOT oder Checkout')
    parser.add_argument('--refresh-proxy', action='store_true', help='Gepruefte nginx-Vorlage bewusst neu rendern')
    args = parser.parse_args()
    try:
        settings, created = configure(root=args.root, refresh_proxy=args.refresh_proxy)
        print('Vorbereitet: ' + settings['KIWM_ROOT'])
        print('Neu angelegt: ' + (', '.join(created) or 'nichts; vorhandene Daten erhalten'))
        print('Weiter: docker compose config --quiet && docker compose build && docker compose up -d --wait')
    except (OSError, ValueError) as exc:
        print('Einrichtung abgebrochen: ' + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
