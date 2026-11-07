"""Opt-in browser input mode. Existing projects keep their original driver."""
import json
from urllib.parse import urlsplit
import store

KEY = 'browser_mode:'
DEFAULT = {'mode': 'standard', 'entry_url': ''}


def validate(value, project):
    if not isinstance(value, dict) or set(value) != {'mode', 'entry_url'} or value['mode'] not in ('standard', 'visual'):
        raise ValueError('Ungültige Browserbedienung.')
    entry = value['entry_url']
    if not isinstance(entry, str) or len(entry) > 2048:
        raise ValueError('Ungültige Browser-Startadresse.')
    entry = entry.strip()
    if entry:
        u = urlsplit(entry)
        domains = {s.strip().lower() for s in project['domains'].split(',')}
        if u.scheme != 'https' or u.hostname not in domains or u.username or u.password or u.query or u.fragment:
            raise ValueError('Browser-Startadresse: HTTPS und ein erlaubter Hostname, ohne Zugangsdaten oder URL-Parameter.')
    return {'mode': value['mode'], 'entry_url': entry}


def get(project, connection=None):
    row = (connection.execute('SELECT value FROM state WHERE key=?', (KEY+project['id'],)).fetchone()
           if connection else store.one('SELECT value FROM state WHERE key=?', (KEY+project['id'],)))
    if row:
        try: return validate(json.loads(row['value']), project)
        except (ValueError, TypeError, KeyError): pass
    return dict(DEFAULT)


def save(project, value, connection=None):
    value = validate(value, project)
    sql, args = 'INSERT OR REPLACE INTO state VALUES(?,?)', (KEY+project['id'], json.dumps(value))
    if connection: connection.execute(sql, args)
    else: store.execute(sql, args)
