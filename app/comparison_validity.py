"""Explicit administrator exclusions for verified bad page comparison captures.

This module is deliberately not an agent tool. A mark excludes only the exact
archived snapshot to which its identifiers and hashes remain bound. It never
deletes an archive row, changes its content, or acknowledges a user change.
"""
from datetime import datetime
from hashlib import sha256
import json
import re
from urllib.parse import urlsplit

from store import db,one,now


POLICY='verified-capture-exclusion-v1'
PREFIX='comparison_excluded:'
_HASH=re.compile(r'[0-9a-f]{64}\Z')
_BOUND={'snapshot_id':'id','project_id':'project_id','url':'url',
        'sha256':'sha256','text_sha256':'text_sha256'}
_FIELDS=set(_BOUND)|{'policy','excluded_at','reason','verification'}


def _reason(value):
    return (isinstance(value,str) and 20<=len(value.strip())<=2000
            and not any(ord(c)<32 and c not in '\n\t' for c in value))


def _verification(value):
    if not isinstance(value,dict) or not value or any(not isinstance(key,str) for key in value):
        return None
    try:
        encoded=json.dumps(value,ensure_ascii=False,allow_nan=False,sort_keys=True)
        if len(encoded.encode('utf-8'))>64*1024:return None
        return json.loads(encoded)
    except (TypeError,ValueError,RecursionError):
        return None


def _snapshot(snapshot):
    if not isinstance(snapshot,dict) or snapshot.get('kind')!='page':return False
    if any(not isinstance(snapshot.get(key),str) or not snapshot[key].strip()
           for key in ('id','project_id','run_id','url','sha256','text_sha256','text')):
        return False
    if any(not _HASH.fullmatch(snapshot[key]) for key in ('sha256','text_sha256')):
        return False
    if sha256(snapshot['text'].encode('utf-8')).hexdigest()!=snapshot['text_sha256']:
        return False
    try:
        u=urlsplit(snapshot['url'])
        if (u.scheme not in ('http','https') or not u.hostname or u.username or u.password
                or any(c.isspace() or ord(c)<32 for c in snapshot['url'])):
            return False
        u.port
    except ValueError:
        return False
    return True


def _valid_mark(mark,snapshot):
    if (not _snapshot(snapshot) or not isinstance(mark,dict) or set(mark)!=_FIELDS
            or mark.get('policy')!=POLICY or not _reason(mark.get('reason'))
            or _verification(mark.get('verification')) is None):
        return False
    if any(mark.get(saved)!=snapshot[field] for saved,field in _BOUND.items()):return False
    try:
        stamp=mark['excluded_at']
        if not isinstance(stamp,str) or len(stamp)>64 or datetime.fromisoformat(stamp).tzinfo is None:
            return False
    except (ValueError,TypeError):
        return False
    return True


def exclude_snapshot(snapshot_id,reason,verification_dict):
    """Record an explicit verified administrative exclusion, preserving capture.

    The returned copy includes ``updated``. The state payload does not contain
    that transient flag. Identical repeated verification retains its timestamp
    and does not write another event.
    """
    if not isinstance(snapshot_id,str) or not snapshot_id.strip():
        raise ValueError('Eine vorhandene Snapshot-ID ist erforderlich.')
    if not _reason(reason):
        raise ValueError('Der bestätigte Aufnahmefehler benötigt eine Begründung mit 20 bis 2000 Zeichen.')
    verification=_verification(verification_dict)
    if verification is None:
        raise ValueError('Eine nichtleere, begrenzte JSON-Verifikation ist erforderlich.')
    reason=reason.strip()
    with db() as connection:
        connection.execute('BEGIN IMMEDIATE')
        found=connection.execute('SELECT * FROM snapshots WHERE id=?',(snapshot_id,)).fetchone()
        snapshot=dict(found) if found else None
        if not _snapshot(snapshot):
            raise ValueError('Nur ein vollständig vorhandener Seiten-Snapshot darf ausgeschlossen werden.')
        # An actual owning run is required for the audit event. Do not bind a
        # stale or cross-project archive reference to another run's history.
        run=connection.execute('SELECT project_id FROM runs WHERE id=?',(snapshot['run_id'],)).fetchone()
        if not run or run['project_id']!=snapshot['project_id']:
            raise ValueError('Der zugehörige Projektlauf ist nicht vollständig vorhanden.')
        key=PREFIX+snapshot_id
        found=connection.execute('SELECT value FROM state WHERE key=?',(key,)).fetchone()
        try:previous=json.loads(found['value']) if found else None
        except (ValueError,TypeError):previous=None
        if (_valid_mark(previous,snapshot) and previous['reason']==reason
                and previous['verification']==verification):
            return {**previous,'updated':False}
        mark={'policy':POLICY,'excluded_at':now(),'reason':reason,'verification':verification,
              **{saved:snapshot[field] for saved,field in _BOUND.items()}}
        connection.execute('INSERT OR REPLACE INTO state(key,value) VALUES(?,?)',
                           (key,json.dumps(mark,ensure_ascii=False,allow_nan=False,sort_keys=True)))
        connection.execute('INSERT INTO events(run_id,time,message) VALUES(?,?,?)',
                           (snapshot['run_id'],mark['excluded_at'],
                            ('Administrativ bestätigter Aufnahmefehler: Vergleichsstand '+snapshot_id+
                             ' ausgeschlossen ('+POLICY+'). '+reason)[:2000]))
        return {**mark,'updated':True}


def usable_snapshot(snapshot):
    """Default to usable; malformed, stale or mismatched marks exclude nothing."""
    if not _snapshot(snapshot):return True
    found=one('SELECT value FROM state WHERE key=?',(PREFIX+snapshot['id'],))
    if not found:return True
    try:mark=json.loads(found['value'])
    except (ValueError,TypeError):return True
    return not _valid_mark(mark,snapshot)
