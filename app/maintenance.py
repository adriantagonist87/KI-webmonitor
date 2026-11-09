"""Plan and apply maintenance to the app volume and its SQLite references."""
from collections import defaultdict
from hashlib import sha256
import json
import shutil

import comparison_validity
import history_cleanup
import retention
import store
from project_urls import detail_path


PROJECT_AREAS = ('documents', 'history', 'events')
GLOBAL_AREAS = ('reports', 'logs', 'cache')
FILE_AREAS = ('artifacts', 'reports', 'logs', 'ocr')
REPORT_SUFFIXES = ('.html', '.txt', '.images.json')
PENDING = 'maintenance:pending_files'
PURGED = 'purged'


def overview():
    root = retention._root()
    files = retention._files(root, 'artifacts')
    with store.db() as c:
        c.execute('BEGIN')
        projects = {r['id']: dict(r, url=detail_path(r['id']), documents=0, history=0, runs=0, events=0, event_bytes=0)
                    for r in c.execute('SELECT id,name,enabled FROM projects ORDER BY name COLLATE NOCASE,id')}
        runs = {r['id']: r['project_id'] for r in c.execute('SELECT id,project_id FROM runs')}
        docs = defaultdict(set)
        details = defaultdict(set)
        for row in c.execute('''SELECT project_id,kind,file,screenshot,html,
                length(CAST(text AS BLOB))+length(CAST(links AS BLOB)) AS payload FROM snapshots'''):
            pid = row['project_id']
            projects[pid]['history'] += row['payload']
            if row['file']:
                (docs if row['kind'] == 'document' else details)[pid].add(row['file'])
            details[pid].update(path for path in (row['screenshot'], row['html']) if path)
        for path in files:
            rid = path.split('/', 2)[1]
            if rid in runs:
                details[runs[rid]].add(path)
        for row in c.execute('''SELECT project_id,count(*) AS n,
                sum(coalesce(length(CAST(summary AS BLOB)),0)+coalesce(length(CAST(agent_result AS BLOB)),0)
                +coalesce(length(CAST(error AS BLOB)),0)) AS payload FROM runs GROUP BY project_id'''):
            project = projects[row['project_id']]
            project['runs'] = row['n']
            project['history'] += row['payload']
        for row in c.execute('SELECT project_id,sum(length(CAST(evidence AS BLOB))) AS payload FROM changes GROUP BY project_id'):
            if row['project_id'] in projects:
                projects[row['project_id']]['history'] += row['payload']
        for row in c.execute('''SELECT project_id,count(*) AS n,sum(length(CAST(message AS BLOB))) AS payload FROM (
                SELECT r.project_id,e.message FROM events e JOIN runs r ON r.id=e.run_id
                UNION ALL SELECT project_id,message FROM notifications) GROUP BY project_id'''):
            projects[row['project_id']].update(events=row['n'], event_bytes=row['payload'] or 0)
        reusable = c.execute('PRAGMA freelist_count').fetchone()[0] * c.execute('PRAGMA page_size').fetchone()[0]
        active = c.execute("SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1").fetchone() is not None
        pending = len(set(_pending(c)) | history_cleanup._pending(c))
    for pid, project in projects.items():
        project['documents'] = sum(files[path]['size'] for path in docs[pid] & files.keys())
        project['history'] += sum(files[path]['size'] for path in (details[pid] - docs[pid]) & files.keys())
    usage = retention._usage(root, {**files, **retention._files(root, 'ocr')})
    # Include original documents stored in old run directories, not only objects.
    original_paths = set().union(*docs.values()) | {path for path in files if path.startswith('artifacts/objects/')}
    usage['documents_bytes'] = sum(files[path]['size'] for path in original_paths & files.keys())
    usage['history_files_bytes'] = usage['artifacts_bytes'] - usage['documents_bytes']
    usage['backup_bytes'] = retention._bytes(root, 'import-backups')
    usage['other_bytes'] = max(0, retention._bytes(root, '.') - usage['total_bytes'] - usage['backup_bytes'])
    usage['total_bytes'] += usage['backup_bytes'] + usage['other_bytes']
    disk = shutil.disk_usage(root)
    return {'projects': list(projects.values()), 'usage': usage, 'disk': disk,
            'reusable': reusable, 'active': active, 'pending': pending}


def selection(form):
    kind = form.get('kind')
    if kind in ('reset', 'retry', 'policy'):
        return {'kind': kind, 'items': [], 'global': [], 'mode': 'all', 'amount': None}
    if kind != 'cleanup':
        raise ValueError('Ungültige Wartungsaktion.')
    items = set()
    for value in form.getlist('item'):
        area, _, pid = str(value).partition(':')
        if area not in PROJECT_AREAS or (pid != '*' and not retention._ID.fullmatch(pid)):
            raise ValueError('Ungültige Projektauswahl.')
        items.add((area, pid))
    global_areas = set(form.getlist('global'))
    if global_areas - set(GLOBAL_AREAS):
        raise ValueError('Ungültiger Ablagebereich.')
    if form.get('all_areas') == 'on':
        items = {(area, '*') for area in PROJECT_AREAS}
        global_areas = set(GLOBAL_AREAS)
    items = {(area, pid) for area, pid in items if pid == '*' or (area, '*') not in items}
    if not items and not global_areas:
        raise ValueError('Wähle mindestens einen Bereich aus.')
    mode, amount = history_cleanup.rule(form.get('mode', 'all'), form.get('amount')) if any(
        area == 'history' for area, _ in items) else ('all', None)
    return {'kind': kind, 'items': sorted(items), 'global': sorted(global_areas),
            'mode': mode, 'amount': amount}


def _pending(c):
    row = c.execute('SELECT value FROM state WHERE key=?', (PENDING,)).fetchone()
    return json.loads(row['value']) if row else {}


def _protected_runs(c, root, projects):
    """Keep the visible latest run and each usable source/scope baseline."""
    newest = set()
    successes = set()
    protected = set()
    for row in c.execute('SELECT id,project_id,status FROM runs ORDER BY julianday(started) DESC,rowid DESC'):
        pid = row['project_id']
        if pid not in projects:
            continue
        if pid not in newest:
            protected.add(row['id'])
            newest.add(pid)
        if row['status'] == 'success' and pid not in successes:
            protected.add(row['id'])
            successes.add(pid)
    seen = set()
    for row in c.execute('''SELECT s.id,s.run_id,s.project_id,s.kind,s.url,s.html,s.sha256,s.text_sha256,
             CASE WHEN m.key IS NOT NULL THEN s.text END AS text,m.value AS exclusion
             FROM snapshots s JOIN runs r ON r.id=s.run_id
             LEFT JOIN state m ON m.key=? || s.id
             WHERE r.status IN ('success','partial') ORDER BY s.captured DESC,s.rowid DESC''', (comparison_validity.PREFIX,)):
        if row['project_id'] not in projects:
            continue
        if row['exclusion']:
            try:
                if comparison_validity._valid_mark(json.loads(row['exclusion']), dict(row)):
                    continue
            except ValueError:
                pass
        key = (row['project_id'], row['kind'], row['url'], retention._scope(root, dict(row)))
        if key not in seen:
            protected.add(row['run_id'])
            seen.add(key)
    protected.update(r['run_id'] for r in c.execute("SELECT run_id FROM notifications WHERE status IN ('queued','sending')"))
    return protected


def _report_selection(c):
    """Archive whole report families together, preserving deliverable mail."""
    families = defaultdict(list)
    blocked = set()
    for row in c.execute('SELECT id,period_key,parent_id,mail_status,attempts FROM reports WHERE mail_status<>?', (PURGED,)):
        family = row['parent_id'] or row['id']
        families[family].append(row)
        if row['mail_status'] in ('pending', 'sending') or (row['mail_status'] == 'failed' and row['attempts'] < 3):
            blocked.add(family)
    return [r for key, group in families.items() if key not in blocked for r in group], sum(len(families[k]) for k in blocked)


def _plan(c, root, options):
    if (c.execute("SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1").fetchone()
            or c.execute("SELECT 1 FROM state WHERE key='manual' AND value='1'").fetchone()):
        raise ValueError('Laufende und wartende Prüfungen sowie den manuellen Browsermodus zuerst beenden.')
    projects = {r['id']: r['name'] for r in c.execute('SELECT id,name FROM projects ORDER BY name COLLATE NOCASE,id')}
    scoped = defaultdict(set)
    for area, pid in options['items']:
        if pid == '*':
            scoped[area].update(projects)
        elif pid in projects:
            scoped[area].add(pid)
        else:
            raise ValueError('Die Projektauswahl hat sich geändert. Bitte neu auswählen.')
    reset = options['kind'] == 'reset'
    if reset:
        scoped['history'].update(projects)
    history = scoped['history']
    runs = history_cleanup._selection(c, options['mode'], options['amount'], history) if history else {}
    protected_count = 0
    if runs and not reset:
        protected = runs.keys() & _protected_runs(c, root, history)
        protected_count = len(protected)
        runs = {rid: pid for rid, pid in runs.items() if rid not in protected}

    requested = options['global']
    reports, deferred = _report_selection(c) if 'reports' in requested else ([], 0)
    report_ids = {row['id'] for row in reports}
    pending = _pending(c) if options['kind'] == 'retry' else {}
    old_pending = history_cleanup._pending(c) if options['kind'] == 'retry' else set()
    areas = {path.partition('/')[0] for path in pending}
    if reset or runs or scoped['documents'] or old_pending:
        areas.add('artifacts')
    areas.update(name for choice, name in (('reports', 'reports'), ('logs', 'logs'), ('cache', 'ocr')) if choice in requested)
    if areas - set(FILE_AREAS):
        raise ValueError('Ungültiger Wartungsverweis.')
    files = {path: info for area in areas for path, info in retention._files(root, area).items()}
    drop, keep, documents = set(), set(), {}
    for snap in c.execute('SELECT id,run_id,project_id,kind,file,screenshot,html FROM snapshots'):
        if snap['run_id'] in runs:
            drop.update(path for path in (snap['file'], snap['screenshot'], snap['html']) if path)
        else:
            if snap['file'] and snap['kind'] == 'document' and snap['project_id'] in scoped['documents']:
                documents[snap['id']] = snap['file']
                drop.add(snap['file'])
            elif snap['file']:
                keep.add(snap['file'])
            keep.update(path for path in (snap['screenshot'], snap['html']) if path)
    for path in drop:
        retention._safe(root, path, ('artifacts',))
    drop.update(path for path in files if path.startswith('artifacts/') and path.split('/', 2)[1] in runs)
    if reset or ('documents', '*') in options['items']:
        drop.update(path for path in files if path.startswith('artifacts/objects/')
                    and retention._OBJECT.fullmatch(path.removeprefix('artifacts/objects/')))
    for rid in report_ids:
        if not retention._ID.fullmatch(rid):
            raise ValueError('Ungültige Berichts-ID.')
        drop.update('reports/' + rid + suffix for suffix in REPORT_SUFFIXES)
    drop.update(path for path in files if (path.startswith('logs/') and 'logs' in requested)
                or (path.startswith('ocr/') and 'cache' in requested))

    if pending:
        active_report_files = {'reports/' + row['id'] + suffix for row in c.execute(
            'SELECT id FROM reports WHERE mail_status<>?', (PURGED,)) for suffix in REPORT_SUFFIXES}
        drop.update(path for path, info in pending.items() if files.get(path) == info and path not in active_report_files)
    for path in old_pending:
        retention._safe(root, path, ('artifacts',))
    drop.update(old_pending)
    drop = (drop - keep) & files.keys()

    events = [r['id'] for r in c.execute('SELECT e.id,r.project_id FROM events e JOIN runs r ON r.id=e.run_id')
              if r['project_id'] in scoped['events'] or 'logs' in requested]
    delivery_events = [r['id'] for r in c.execute('SELECT id,report_id FROM report_mail_events')
                       if 'logs' in requested or r['report_id'] in report_ids]
    messages = [r['id'] for r in c.execute("SELECT id,project_id FROM notifications WHERE status NOT IN ('queued','sending')")
                if r['project_id'] in scoped['events'] or 'logs' in requested]
    selected_ids = set().union(*scoped.values())
    reset_ids = sorted(history) if reset else []
    planned_files = {path: files[path] for path in sorted(drop)}
    signature = [options, sorted(selected_ids), sorted(runs), reset_ids, sorted(documents.items()), sorted(report_ids),
                 sorted(events), sorted(delivery_events), sorted(messages), planned_files, pending, sorted(old_pending), deferred]
    fingerprint = sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    return {'runs': runs, 'reset_ids': reset_ids, 'documents': documents, 'reports': reports, 'events': events,
            'delivery_events': delivery_events, 'messages': messages, 'files': planned_files, 'pending': pending,
            'old_pending': old_pending,
            'preview': {'fingerprint': fingerprint, 'projects': [name for pid, name in projects.items() if pid in selected_ids],
                        'runs': len(runs), 'reset': reset, 'protected': protected_count, 'deferred': deferred,
                        'documents': len(documents), 'reports': len(reports), 'logs': len(events)+len(delivery_events)+len(messages),
                        'files': len(drop), 'bytes': sum(info['size'] for info in planned_files.values()),
                        'actionable': bool(runs or reset_ids or documents or reports or events or delivery_events or messages or drop or pending or old_pending)}}


def preview(options):
    if options['kind'] == 'policy':
        if store.manual():
            raise ValueError('Zuerst den manuellen Browsermodus beenden.')
        result = retention.inspect()
        if result['skipped']:
            raise ValueError(result['skipped'][0])
        candidates = result['candidates']
        return {'policy': True, 'files': candidates['files'], 'bytes': candidates['bytes'],
                'snapshots': candidates['snapshot_rows'], 'fingerprint': result['fingerprint'],
                'actionable': bool(candidates['files'] or candidates['snapshot_rows'])}
    with store.db() as c:
        c.execute('BEGIN')
        return _plan(c, retention._root(), options)['preview']


def _write_pending(c, files):
    if files:
        c.execute('INSERT OR REPLACE INTO state VALUES(?,?)', (PENDING, json.dumps(files, sort_keys=True)))
    else:
        c.execute('DELETE FROM state WHERE key=?', (PENDING,))


def clean(options, fingerprint):
    if not isinstance(fingerprint, str) or not retention._HASH.fullmatch(fingerprint):
        raise ValueError('Bitte zuerst eine aktuelle Vorschau erstellen.')
    if options['kind'] == 'policy':
        if store.manual():
            raise ValueError('Zuerst den manuellen Browsermodus beenden.')
        result = retention.cleanup(expected=fingerprint)
        if result['status'] != 'cleaned':
            raise ValueError(result['skipped'][0])
        return {'removed_bytes': result['removed']['bytes'], 'removed_files': result['removed']['files'],
                'pending': 0, 'warning': bool(result['skipped'])}
    with retention.maintenance_lock():
        root = retention._root()
        with store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            plan = _plan(c, root, options)
            if plan['preview']['fingerprint'] != fingerprint:
                raise ValueError('Die Auswahl hat sich seit der Vorschau geändert. Bitte erneut prüfen.')
            c.executemany('UPDATE snapshots SET file=NULL WHERE id=?', ((sid,) for sid in plan['documents']))
            if plan['runs'] or plan['reset_ids']:
                history_cleanup._stage_deletion(c, root, plan['runs'], set(), artifacts=plan['files'], reset_projects=plan['reset_ids'])
            for table, ids in (('events', plan['events']), ('report_mail_events', plan['delivery_events']), ('notifications', plan['messages'])):
                c.executemany('DELETE FROM ' + table + ' WHERE id=?', ((ident,) for ident in ids))
            for report in plan['reports']:
                rid = report['id']
                if report['parent_id'] or (report['period_key'].startswith('test-') and not c.execute(
                        'SELECT 1 FROM report_changes WHERE report_id=? LIMIT 1', (rid,)).fetchone()):
                    c.execute('DELETE FROM reports WHERE id=?', (rid,))
                else:
                    # Keep the weekly period and change markers, so a cleanup
                    # cannot trigger a duplicate issue or resend old findings.
                    c.execute("""UPDATE reports SET mail_status=?,content='',error=NULL,sent_at=NULL,
                                 next_attempt=NULL,attempts=0,selection_snapshot='{}' WHERE id=?""", (PURGED, rid))
            previous = _pending(c)
            for path in plan['pending']:
                previous.pop(path, None)
            previous.update(plan['files'])
            _write_pending(c, previous)
            if plan['old_pending']:
                history_cleanup._save_pending(c, set())

        removed_bytes = removed_files = 0
        for path, info in plan['files'].items():
            try:
                file = retention._safe(root, path, FILE_AREAS)
                file.unlink()
                removed_bytes += info['size']
                removed_files += 1
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                continue
            previous.pop(path, None)
            for directory in file.parents:
                if directory.parent == root:
                    break
                try:
                    directory.rmdir()
                except OSError:
                    break
        with store.db() as c:
            _write_pending(c, previous)
    return {**plan['preview'], 'removed_bytes': removed_bytes, 'removed_files': removed_files, 'pending': len(previous)}
