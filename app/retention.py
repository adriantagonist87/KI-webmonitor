"""Bounded original-file storage, without deleting reports or change evidence.

Snapshots and their text hashes remain historical records; an expired original
has ``file=None``. Old, redundant snapshots may be removed only when no change
references them. The current comparison baseline per URL/scope is always kept.
All writers/readers of archive files use maintenance_lock. A dry run never
creates that lock file or changes application data.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import threading

from bs4 import BeautifulSoup
import store
from document_inventory import document_identity
from document_types import EXTENSIONS


DEFAULT_POLICY = {'document_versions': 2, 'detail_days': 30, 'cache_days': 30}
# Existing OCR caches remain removable historical artifacts; no renderer or
# transcription module is required to recognize their on-disk policy directory.
LEGACY_OCR_CACHE_POLICY = 'pdf-vision-literal-hybrid80-rgb2400-white24-v4'
LIMITS = {'document_versions': (1, 3), 'detail_days': (7, 365), 'cache_days': (1, 90)}
LAST_KEY = 'storage_retention:last_cleanup'
_LOCAL = threading.local()
_ID = re.compile(r'[A-Za-z0-9_-]{1,80}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_OBJECT = re.compile(r'[0-9a-f]{64}(?:'+'|'.join(re.escape(extension) for extension in sorted(EXTENSIONS))+r')\Z')


def validate_policy(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_POLICY):
        raise ValueError('Ungültige Speicherregeln.')
    result = dict(DEFAULT_POLICY, **value)
    for key, val in result.items():
        low, high = LIMITS[key]
        if type(val) is not int or not low <= val <= high:
            raise ValueError('Ungültiger Wert für die Aufbewahrung.')
    return result


def policy():
    return validate_policy(store.settings().get('retention', {}))


def _root():
    root = Path(store.DATA)
    if not root.is_absolute() or root.resolve() != root or root.is_symlink():
        raise ValueError('Unsicheres Datenverzeichnis; keine Bereinigung.')
    if any(p.is_symlink() for p in root.parents):
        raise ValueError('Unsicherer Datenpfad; keine Bereinigung.')
    return root


@contextmanager
def maintenance_lock(*, exclusive=True):
    """Private flock, reentrant within a thread; never upgrade a held read lock."""
    import fcntl
    root = _root()
    held = getattr(_LOCAL, 'held', None)
    if held:
        if held['root'] != root or (exclusive and not held['exclusive']):
            raise RuntimeError('Archivzugriff kann nicht auf eine Schreibsperre erweitert werden.')
        held['depth'] += 1
        try:
            yield
        finally:
            held['depth'] -= 1
        return
    path = root / '.storage.lock'
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or path.is_symlink():
            raise ValueError('Unsichere Archivsperre.')
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        _LOCAL.held = {'root': root, 'exclusive': exclusive, 'depth': 1}
        try:
            yield
        finally:
            _LOCAL.held = None
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _date(value):
    try:
        result = datetime.fromisoformat(value)
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _instant(at):
    at = at or datetime.now(timezone.utc)
    if isinstance(at, str):
        at = _date(at)
    if not isinstance(at, datetime):
        raise ValueError('Ungültiger Bereinigungszeitpunkt.')
    return at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at.astimezone(timezone.utc)


def _safe(root, relative, areas=('artifacts', 'ocr')):
    if not isinstance(relative, str) or '\\' in relative or ':' in relative or '\x00' in relative:
        raise ValueError('Unsicherer Archivverweis; keine Bereinigung.')
    rel = PurePosixPath(relative)
    if (rel.is_absolute() or len(rel.parts) < 2 or relative != rel.as_posix()
            or any(p in ('.', '..') for p in rel.parts) or rel.parts[0] not in areas):
        raise ValueError('Archivverweis außerhalb des verwalteten Bereichs.')
    path = root.joinpath(*rel.parts)
    if path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Symbolische Links werden nicht bereinigt.')
    if path.exists() and not path.is_file():
        raise ValueError('Ungültiger Archivdateiverweis.')
    return path


def _files(root, directory):
    """Never follow a directory link, including links to another DATA subtree."""
    base = root / directory
    if base.is_symlink() or (base.exists() and not base.is_dir()):
        raise ValueError('Unsicherer Ablagebereich; keine Bereinigung.')
    if not base.exists():
        return {}
    found = {}
    for current, dirs, files in os.walk(base, followlinks=False):
        parent = Path(current)
        if any((parent / name).is_symlink() for name in (*dirs, *files)):
            raise ValueError('Symbolischer Link in der Ablage; keine Bereinigung.')
        for name in files:
            path = parent / name
            relative = path.relative_to(root).as_posix()
            _safe(root, relative, (directory,))
            try:
                info = path.stat()
            except FileNotFoundError:
                # Read-only statistics may race normal cache eviction or an
                # already-running cleanup. Do not turn that into a UI error.
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('Unbekannter Dateityp in der Ablage.')
            found[relative] = {'size': info.st_size, 'mtime': info.st_mtime}
    return found


def _bytes(root, name):
    base = root / name
    total = 0
    if base.is_symlink() or not base.is_dir():
        return 0
    for current, dirs, files in os.walk(base, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(current) / name).is_symlink()]
        for name in files:
            path = Path(current) / name
            if not path.is_symlink() and path.is_file():
                try:
                    total += path.stat().st_size
                except FileNotFoundError:
                    continue
    return total


def _usage(root, files):
    artifacts = sum(v['size'] for k, v in files.items() if k.startswith('artifacts/'))
    ocr = sum(v['size'] for k, v in files.items() if k.startswith('ocr/'))
    documents = sum(v['size'] for k, v in files.items() if k.startswith('artifacts/objects/'))
    db = 0
    for path in root.glob('monitor.db*'):
        if path.is_file() and not path.is_symlink():
            try:
                db += path.stat().st_size
            except FileNotFoundError:
                continue
    reports = _bytes(root, 'reports')
    browser, logs = _bytes(root, 'browser'), _bytes(root, 'logs')
    return {'total_bytes': artifacts + ocr + db + reports + browser + logs, 'documents_bytes': documents,
            'ocr_bytes': ocr, 'artifacts_bytes': artifacts, 'database_bytes': db, 'reports_bytes': reports,
            'browser_bytes': browser, 'logs_bytes': logs}


def _scope(root, snapshot):
    if snapshot['kind'] == 'document':
        return 'document'
    relative = snapshot.get('html')
    if relative:
        path = _safe(root, relative)
        if path.is_file() and path.stat().st_size <= 20 * 1024**2:
            try:
                element = BeautifulSoup(path.read_text(encoding='utf-8'), 'html.parser').find(attrs={'data-kiwm-scope': True})
                if element:
                    return element['data-kiwm-scope']
            except (UnicodeError, OSError, ValueError):
                pass
    return 'default'


def _read(path, limit=32 * 1024**2):
    try:
        if path.is_file() and path.stat().st_size <= limit:
            return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError, OSError):
        pass
    return None


def _authoritative(root, runs):
    """Only the latest successful run may certify that a source disappeared."""
    latest = {}
    for run in runs:
        if run['status'] == 'success' and run['project_id'] not in latest:
            latest[run['project_id']] = run
    result, keep = {}, set()
    for pid, run in latest.items():
        if not _ID.fullmatch(run['id']):
            continue
        rel = 'artifacts/' + run['id'] + '/evidence.json'
        evidence = _read(_safe(root, rel))
        if not isinstance(evidence, dict):
            continue
        inventory, agent = evidence.get('document_check'), evidence.get('agent')
        if not isinstance(inventory, dict) or not isinstance(agent, dict) or agent.get('complete') is not True:
            continue
        documents = inventory.get('documents')
        found, checked = inventory.get('found'), inventory.get('checked')
        if (type(found) is not int or type(checked) is not int or found < 0 or found != checked
                or not isinstance(documents, list) or len(documents) != found
                or inventory.get('failed') or inventory.get('unverified')):
            continue
        urls = set()
        valid = True
        for entry in documents:
            if (not isinstance(entry, dict) or entry.get('status') != 'checked'
                    or not isinstance(entry.get('url'), str)):
                valid = False
                break
            urls.add(document_identity(entry['url']))
        if valid and len(urls) == found and _date(run['finished']):
            result[pid] = {'urls': urls, 'finished': _date(run['finished'])}
            keep.add(rel)
    return result, keep


def _ocr_candidates(root, files, retained_hashes, cutoff):
    """Raw manifests and shared visual interpretations form a per-policy graph."""
    candidates, visual_needed = set(), set()
    for relative, info in files.items():
        parts = PurePosixPath(relative).parts
        if parts[0] != 'ocr' or len(parts) != 4 or parts[2] != 'raw' or not parts[3].endswith('.json'):
            continue
        manifest = _read(_safe(root, relative), 8 * 1024**2)
        age_expired = info['mtime'] < cutoff.timestamp()
        if not isinstance(manifest, dict):
            # An old malformed optional cache may safely be recreated. It must
            # never make a referenced valid raw manifest lose its visual entry.
            if age_expired:
                candidates.add(relative)
            continue
        raw_hash, visual_hash = manifest.get('raw_sha256'), manifest.get('visual_sha256')
        if (not isinstance(raw_hash, str) or not _HASH.fullmatch(raw_hash)
                or not isinstance(visual_hash, str) or not _HASH.fullmatch(visual_hash)):
            if age_expired:
                candidates.add(relative)
            continue
        if age_expired and (raw_hash not in retained_hashes or parts[1] != LEGACY_OCR_CACHE_POLICY):
            candidates.add(relative)
        else:
            visual_needed.add('/'.join(parts[:2]) + '/visual/' + visual_hash + '.json')
    for relative, info in files.items():
        if not relative.startswith('ocr/') or relative in candidates:
            continue
        parts = PurePosixPath(relative).parts
        if len(parts) == 4 and parts[2] == 'raw':
            continue
        if relative not in visual_needed and info['mtime'] < cutoff.timestamp():
            candidates.add(relative)
    return candidates


def _plan(root, connection, at, selected):
    all_files = {**_files(root, 'artifacts'), **_files(root, 'ocr')}
    usage = _usage(root, all_files)
    active = connection.execute("SELECT 1 FROM runs WHERE status IN ('running','queued') LIMIT 1").fetchone()
    if active:
        return {'files': set(), 'prune': set(), 'clear': {}, 'usage': usage,
                'all_files': all_files, 'skipped': ['Eine Prüfung läuft oder wartet.']}
    runs = [dict(row) for row in connection.execute('SELECT id,project_id,status,started,finished FROM runs ORDER BY started DESC,id DESC')]
    run_map = {run['id']: run for run in runs}
    snapshots = [dict(row) for row in connection.execute(
        'SELECT id,run_id,project_id,url,kind,captured,sha256,text_sha256,file,screenshot,html FROM snapshots ORDER BY captured DESC,id DESC')]
    # Every reference is checked before anything is changed. Missing managed
    # files are tolerated as historical input; unsafe references abort cleanup.
    for snap in snapshots:
        for key in ('file', 'screenshot', 'html'):
            if snap.get(key):
                _safe(root, snap[key])
    cutoff = at - timedelta(days=selected['detail_days'])
    authoritative, evidence_keep = _authoritative(root, runs)
    stable = {'success', 'partial'}
    baselines, latest_by_group, doc_groups = set(), {}, {}
    for snap in snapshots:
        run = run_map.get(snap['run_id'])
        if run is None:
            raise ValueError('Archivquelle ohne Prüfung; keine Bereinigung.')
        if snap['kind'] == 'document':
            key = (snap['project_id'], document_identity(snap['url']))
            doc_groups.setdefault(key, []).append(snap)
        scope = _scope(root, snap)
        identity = document_identity(snap['url']) if snap['kind'] == 'document' else snap['url']
        key = (snap['project_id'], identity, snap['kind'], scope)
        if run['status'] in stable and key not in latest_by_group:
            latest_by_group[key] = snap['id']
            baselines.add(snap['id'])
    linked = {value for row in connection.execute('SELECT previous_id,current_id FROM changes') for value in row if value}
    document_keep, representatives = set(), set()
    for (pid, url), records in doc_groups.items():
        last_seen = max((_date(s['captured']) or at for s in records))
        current = authoritative.get(pid)
        retired = (current is not None and url not in current['urls']
                   and current['finished'] >= last_seen and last_seen < cutoff)
        if retired:
            continue
        distinct = set()
        pool = [snap for snap in records if run_map[snap['run_id']]['status'] in stable and snap.get('file')]
        if not pool:
            # With no usable successful baseline, retain only a bounded recent
            # failed fallback; repeated failures must not grow originals forever.
            pool = [snap for snap in records if snap.get('file') and (_date(snap['captured']) or at) >= cutoff]
        for snap in pool:
            path = snap['file']
            if snap['sha256'] not in distinct and len(distinct) < selected['document_versions']:
                distinct.add(snap['sha256'])
                document_keep.add(path)
                representatives.add(snap['id'])
    # Keep any non-document use of a path. This is uncommon but protects shared
    # source objects imported from older portable archives.
    document_keep.update(s[k] for s in snapshots if s['kind'] != 'document'
                         for k in ('file', 'screenshot', 'html') if s.get(k))
    doc_paths = {s['file'] for s in snapshots if s['kind'] == 'document' and s.get('file')}
    remove = doc_paths - document_keep
    protected_rows = baselines | linked | representatives
    prune = {s['id'] for s in snapshots if s['id'] not in protected_rows
             and _date(s['captured']) is not None and _date(s['captured']) < cutoff}
    keep_detail = set(evidence_keep)
    clear = {'file': set(), 'screenshot': set(), 'html': set()}
    for snap in snapshots:
        old = _date(snap['captured']) is not None and _date(snap['captured']) < cutoff
        for field in ('screenshot', 'html'):
            relative = snap.get(field)
            if not relative:
                continue
            if not old or snap['id'] in baselines:
                keep_detail.add(relative)
            else:
                clear[field].add(relative)
        if snap['id'] not in prune and snap['kind'] == 'document' and _ID.fullmatch(snap['run_id']) and _ID.fullmatch(snap['id']):
            keep_detail.add('artifacts/' + snap['run_id'] + '/' + snap['id'] + '.extraction.json')
    # A current baseline or other shared reference wins over expiration.
    for field in ('screenshot', 'html'):
        clear[field] -= keep_detail
        remove.update(clear[field])
    clear['file'] = set(remove & doc_paths)
    kept_refs = {s[field] for s in snapshots if s['id'] not in prune for field in ('file', 'screenshot', 'html')
                 if s.get(field) and s[field] not in remove}
    remove -= kept_refs
    for relative, info in all_files.items():
        parts = PurePosixPath(relative).parts
        if parts[0] != 'artifacts':
            continue
        if len(parts) >= 3 and parts[1] == 'objects':
            if (len(parts) == 3 and _OBJECT.fullmatch(parts[2])
                    and relative not in kept_refs and relative not in document_keep
                    and info['mtime'] < (at - timedelta(days=1)).timestamp()):
                remove.add(relative)
            continue
        if len(parts) >= 3 and parts[1] in run_map:
            run = run_map[parts[1]]
            finished = _date(run['finished'])
            if finished is not None and finished < cutoff and relative not in kept_refs and relative not in keep_detail:
                remove.add(relative)
    retained_hashes = {s['sha256'] for s in snapshots if s.get('file') in document_keep}
    remove.update(_ocr_candidates(root, all_files, retained_hashes, at - timedelta(days=selected['cache_days'])))
    remove &= set(all_files)
    # Unknown orphan directories are deliberately left to the operator. Only
    # content-addressed objects and known completed run files are managed.
    return {'files': remove, 'prune': prune, 'clear': clear, 'usage': usage,
            'all_files': all_files, 'skipped': []}


def _result(plan, selected, at, last):
    files = plan['files']
    signature = [selected, {p: plan['all_files'][p] for p in sorted(files)}, sorted(plan['prune']),
                 {field: sorted(paths) for field, paths in plan['clear'].items()}]
    return {'status': 'skipped' if plan['skipped'] else 'preview', 'checked_at': at.isoformat(),
            'fingerprint': store.digest(json.dumps(signature, sort_keys=True)),
            'policy': selected, 'usage': plan['usage'], 'last_cleanup': last,
            'candidates': {'files': len(files), 'bytes': sum(plan['all_files'][p]['size'] for p in files),
                           'document_files': sum(p.startswith('artifacts/objects/') for p in files),
                           'snapshot_rows': len(plan['prune'])}, 'skipped': plan['skipped']}


def _last(connection):
    row = connection.execute('SELECT value FROM state WHERE key=?', (LAST_KEY,)).fetchone()
    try:
        return json.loads(row[0]) if row else None
    except (ValueError, TypeError):
        return None


def inspect(at=None):
    """Read-only usage and concrete cleanup preview; no lock file is created."""
    root, at, selected = _root(), _instant(at), policy()
    with store.db() as connection:
        connection.execute('BEGIN')
        last = _last(connection)
        try:
            return _result(_plan(root, connection, at, selected), selected, at, last)
        except ValueError as error:
            return {'status': 'skipped', 'checked_at': at.isoformat(), 'policy': selected,
                    'usage': {'total_bytes': 0, 'documents_bytes': 0, 'ocr_bytes': 0,
                              'artifacts_bytes': 0, 'database_bytes': 0, 'reports_bytes': 0,
                              'browser_bytes': 0, 'logs_bytes': 0},
                    'candidates': {'files': 0, 'bytes': 0, 'document_files': 0, 'snapshot_rows': 0},
                    'last_cleanup': last, 'skipped': [str(error)]}


def cleanup(dry_run=False, at=None, *, expected=None):
    """Expire unneeded originals/details. Caller must also hold engine.BUSY.

    Clear references first, commit, then unlink while holding the archive lock.
    An interrupted unlink leaves an orphan, never a dangling snapshot pointer.
    Report and change tables, browser profiles, configuration and secrets are
    outside the managed deletion areas.
    """
    if dry_run:
        return inspect(at)
    at, selected = _instant(at), policy()
    with maintenance_lock(exclusive=True):
        root = _root()
        with store.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            last = _last(connection)
            try:
                plan = _plan(root, connection, at, selected)
            except ValueError as error:
                return dict(inspect(at), status='skipped', skipped=[str(error)])
            result = _result(plan, selected, at, last)
            if plan['skipped']:
                return result
            if expected is not None and result['fingerprint'] != expected:
                raise ValueError('Die Auswahl hat sich seit der Vorschau geändert. Bitte erneut prüfen.')
            for field, paths in plan['clear'].items():
                for relative in paths & plan['files']:
                    connection.execute('UPDATE snapshots SET ' + field + '=NULL WHERE ' + field + '=?', (relative,))
            # Paths selected through shared/detail analysis can be represented
            # in another pointer field. Clear all pointers before removing them.
            for relative in plan['files']:
                for field in ('file', 'screenshot', 'html'):
                    connection.execute('UPDATE snapshots SET ' + field + '=NULL WHERE ' + field + '=?', (relative,))
            for sid in plan['prune']:
                connection.execute('DELETE FROM snapshots WHERE id=? AND id NOT IN (SELECT current_id FROM changes) '
                                   'AND id NOT IN (SELECT previous_id FROM changes WHERE previous_id IS NOT NULL)', (sid,))
        removed_files = removed_bytes = removed_docs = 0
        for relative in sorted(plan['files']):
            path = _safe(root, relative)
            if not path.exists():
                continue
            size = path.stat().st_size
            try:
                path.unlink()
            except OSError:
                result['skipped'].append('Eine freigegebene Archivdatei konnte nicht entfernt werden.')
                continue
            removed_files += 1
            removed_bytes += size
            removed_docs += relative.startswith('artifacts/objects/')
        # Remove empty directories only under the managed artifact/cache roots.
        for base in (root / 'artifacts', root / 'ocr'):
            if base.is_dir() and not base.is_symlink():
                for parent, dirs, files in os.walk(base, topdown=False, followlinks=False):
                    for name in dirs:
                        path = Path(parent) / name
                        if not path.is_symlink():
                            try:
                                path.rmdir()
                            except OSError:
                                pass
        result['status'] = 'cleaned'
        result['removed'] = {'files': removed_files, 'bytes': removed_bytes,
                             'document_files': removed_docs, 'snapshot_rows': len(plan['prune'])}
        result['last_cleanup'] = None
        # No recursive historical result chain in state.
        saved = {key: value for key, value in result.items() if key != 'last_cleanup'}
        store.execute('INSERT OR REPLACE INTO state(key,value) VALUES(?,?)', (LAST_KEY, json.dumps(saved, ensure_ascii=False)))
        return result
