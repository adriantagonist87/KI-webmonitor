"""Explicit, global pruning of completed monitoring runs."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json

import comparison_validity
import retention
import store


PENDING_KEY = 'history_cleanup:pending_files'
MODES = ('last', 'days', 'all')
MAX_AMOUNT = 36500


def rule(mode, amount):
    if mode not in MODES:
        raise ValueError('Ungültige Auswahl für den Prüfverlauf.')
    if mode == 'all':
        return mode, None
    if (not isinstance(amount, str) or not 1 <= len(amount) <= 5
            or not amount.isascii() or not amount.isdecimal() or not 1 <= int(amount) <= MAX_AMOUNT):
        raise ValueError('Eine ganze Zahl zwischen 1 und 36.500 eingeben.')
    return mode, int(amount)


def _selection(connection, mode, amount, project_ids=None):
    if connection.execute("SELECT 1 FROM runs WHERE status IN ('running','queued') LIMIT 1").fetchone():
        raise ValueError('Laufende und wartende Prüfungen zuerst abschließen lassen.')
    cutoff = datetime.now(timezone.utc) - timedelta(days=amount) if mode == 'days' else None
    runs = {}
    rank = Counter()
    for row in connection.execute('''SELECT id,project_id,started,finished FROM runs
                                     ORDER BY julianday(started) DESC,rowid DESC'''):
        pid = row['project_id']
        if project_ids is not None and pid not in project_ids:
            continue
        rank[pid] += 1
        finished = retention._date(row['finished'] or row['started']) if cutoff else None
        if mode == 'all' or (mode == 'last' and rank[pid] > amount) or (finished and finished < cutoff):
            runs[row['id']] = pid
    return runs


def _pending(connection):
    row = connection.execute('SELECT value FROM state WHERE key=?', (PENDING_KEY,)).fetchone()
    return set(json.loads(row['value'])) if row else set()


def _preview(mode, amount, runs, pending):
    selection = json.dumps([mode, amount, sorted(runs), sorted(pending)])
    return {'mode': mode, 'amount': amount, 'runs': len(runs), 'projects': len(set(runs.values())),
            'pending': len(pending), 'fingerprint': hashlib.sha256(selection.encode()).hexdigest()}


def preview(mode, amount):
    with store.db() as connection:
        connection.execute('BEGIN')
        return _preview(mode, amount, _selection(connection, mode, amount), _pending(connection))


def _stage_deletion(connection, root, runs, pending, *, artifacts=None, reset_projects=()):
    if any(not retention._ID.fullmatch(rid) or rid == 'objects' for rid in runs):
        raise ValueError('Unsichere Prüfungs-ID; der Verlauf wurde nicht verändert.')
    connection.execute('CREATE TEMP TABLE history_runs(id TEXT PRIMARY KEY)')
    connection.executemany('INSERT INTO history_runs VALUES(?)', ((rid,) for rid in runs))
    connection.execute('CREATE TEMP TABLE history_snapshots(id TEXT PRIMARY KEY)')
    connection.execute('INSERT INTO history_snapshots SELECT id FROM snapshots WHERE run_id IN history_runs')
    candidates = pending | {path for row in connection.execute(
        'SELECT file,screenshot,html FROM snapshots WHERE id IN history_snapshots') for path in row if path}
    if runs:
        paths = retention._files(root, 'artifacts') if artifacts is None else artifacts
        candidates.update(path for path in paths if path.split('/', 2)[1] in runs)
    # Check all deletion paths before changing the database.
    for path in candidates:
        retention._safe(root, path)
        if not path.startswith('artifacts/'):
            raise ValueError('Archivverweis außerhalb der Prüfungsablage.')

    connection.execute('CREATE TEMP TABLE history_changes(id TEXT PRIMARY KEY)')
    connection.execute('''INSERT INTO history_changes SELECT id FROM changes
                          WHERE run_id IN history_runs OR current_id IN history_snapshots''')
    connection.execute('DELETE FROM report_changes WHERE change_id IN history_changes')
    connection.execute('DELETE FROM changes WHERE id IN history_changes')
    connection.execute('UPDATE changes SET previous_id=NULL WHERE previous_id IN history_snapshots')
    connection.execute('DELETE FROM events WHERE run_id IN history_runs')
    connection.execute("UPDATE run_retries SET state='cancelled',retry_run_id=NULL WHERE retry_run_id IN history_runs")
    connection.execute('DELETE FROM state WHERE key IN (SELECT ? || id FROM history_snapshots)',
                       (comparison_validity.PREFIX,))
    connection.execute('DELETE FROM snapshots WHERE id IN history_snapshots')
    connection.execute('DELETE FROM runs WHERE id IN history_runs')

    for pid in set(runs.values()) | set(reset_projects):
        connection.execute('''UPDATE projects SET last_success=(
            SELECT finished FROM runs WHERE project_id=? AND status='success'
            ORDER BY julianday(finished) DESC,rowid DESC LIMIT 1) WHERE id=?''', (pid, pid))
        if pid in reset_projects or not connection.execute('''SELECT 1 FROM snapshots s JOIN runs r ON r.id=s.run_id
                                     WHERE s.project_id=? AND r.status IN ('success','partial') LIMIT 1''', (pid,)).fetchone():
            prefix = 'collection_index:' + pid + ':'
            connection.execute('DELETE FROM state WHERE key=? OR substr(key,1,?)=?',
                               ('content_scopes:' + pid, len(prefix), prefix))

    referenced = {path for row in connection.execute('SELECT file,screenshot,html FROM snapshots') for path in row if path}
    return candidates - referenced


def _save_pending(connection, files):
    if files:
        connection.execute('INSERT OR REPLACE INTO state VALUES(?,?)', (PENDING_KEY, json.dumps(sorted(files))))
    else:
        connection.execute('DELETE FROM state WHERE key=?', (PENDING_KEY,))


def clean(mode, amount, fingerprint):
    """Commit references first. Interrupted unlinks can be retried safely."""
    with retention.maintenance_lock():
        root = retention._root()
        with store.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            runs = _selection(connection, mode, amount)
            pending = _pending(connection)
            result = _preview(mode, amount, runs, pending)
            if fingerprint != result['fingerprint']:
                raise ValueError('Der Verlauf hat sich seit der Vorschau geändert. Bitte erneut prüfen.')
            files = _stage_deletion(connection, root, runs, pending)
            _save_pending(connection, files)
        failed = set()
        for relative in sorted(files):
            try:
                path = retention._safe(root, relative)
                path.unlink(missing_ok=True)
            except (OSError, ValueError):
                failed.add(relative)
                continue
            for parent in path.parents:
                if parent == root / 'artifacts':
                    break
                try:
                    parent.rmdir()
                except OSError:
                    break
        with store.db() as connection:
            _save_pending(connection, failed)
        return {**result, 'pending': len(failed)}
