"""Opt-in browser input mode. Existing projects keep their original driver."""
import json
from urllib.parse import urlsplit
import store
import vde_norms

KEY = 'browser_mode:'
FLOW_KEY = 'browser_flow:'
DEFAULT = {'mode': 'standard', 'entry_url': ''}


def validate(value, project):
    if (not isinstance(value, dict) or set(value) not in ({'mode', 'entry_url'}, {'mode', 'entry_url', 'flow'})
            or value['mode'] not in ('standard', 'visual', 'visual_mobile')):
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
    result = {'mode': value['mode'], 'entry_url': entry}
    flow = value.get('flow', '')
    if flow not in ('', 'vde_norms'):
        raise ValueError('Ungültiger Browserablauf.')
    if flow:
        if value['mode'] == 'standard' or not vde_norms.matches(project.get('url')):
            raise ValueError('Der VDE-Ablauf benötigt die NormenBibliothek und einen visuellen Browsermodus.')
        result['flow'] = flow
    return result


def get(project, connection=None):
    mode_key, flow_key = KEY + project['id'], FLOW_KEY + project['id']
    sql = 'SELECT key,value FROM state WHERE key IN (?,?)'
    rows = connection.execute(sql, (mode_key, flow_key)) if connection else store.rows(sql, (mode_key, flow_key))
    saved = {row['key']: row['value'] for row in rows}
    if mode_key in saved:
        try:
            value = validate(json.loads(saved[mode_key]), project)
            if flow_key in saved and vde_norms.matches(project.get('url')) and value['mode'] != 'standard':
                value = validate({**value, 'flow': saved[flow_key]}, project)
            return value
        except (ValueError, TypeError, KeyError): pass
    return dict(DEFAULT)


def save(project, value, connection=None):
    if connection is None:
        with store.db() as connection:
            return save(project, value, connection)
    value = validate(value, project)
    flow = value.pop('flow', '')
    sql = 'INSERT OR REPLACE INTO state VALUES(?,?)'
    connection.execute(sql, (KEY+project['id'], json.dumps(value)))
    if flow:
        connection.execute(sql, (FLOW_KEY+project['id'], flow))
    else:
        connection.execute('DELETE FROM state WHERE key=?', (FLOW_KEY+project['id'],))
