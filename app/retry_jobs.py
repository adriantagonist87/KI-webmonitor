"""One durable, thirty-minute retry per newly failed monitoring run.

Only the controller writes these records. Historical failures are never scanned
or backfilled. A retry remains consumed even if its child is later removed.
"""
import contextlib
from datetime import datetime, timedelta, timezone

import store
import site_safety

DELAY = timedelta(minutes=30)
FAILURES = ('failed', 'partial')
CANCEL_REASON = 'Automatische Wiederholung verworfen: Projekt pausiert oder durch eine neuere Prüfung ersetzt.'


def initialize(c):
    c.execute('''CREATE TABLE IF NOT EXISTS run_retries(
        source_run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        due_at TEXT NOT NULL,
        retry_run_id TEXT UNIQUE,
        state TEXT NOT NULL CHECK(state IN ('pending','queued','running','finished','cancelled'))
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS retry_due ON run_retries(state,due_at)')


def _time(value=None):
    value = store.now() if value is None else value
    date = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(date, datetime) or date.tzinfo is None or date.utcoffset() is None:
        raise ValueError('Wiederholungszeit benötigt eine Zeitzone.')
    return date.astimezone(timezone.utc)


@contextlib.contextmanager
def _transaction(connection=None):
    if connection is not None:
        yield connection
    else:
        with store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            yield c


def _latest(c, project_id):
    return c.execute("SELECT id FROM runs WHERE project_id=? AND status<>'cancelled' ORDER BY started DESC,rowid DESC LIMIT 1", (project_id,)).fetchone()


def _cancel(c, plan, stamp):
    if plan['state'] not in ('pending', 'queued'):
        return
    if plan['retry_run_id']:
        c.execute("UPDATE runs SET status='cancelled',finished=?,error=? WHERE id=? AND origin='retry' AND status='queued'",
                  (stamp, CANCEL_REASON, plan['retry_run_id']))
    c.execute("UPDATE run_retries SET state='cancelled' WHERE source_run_id=? AND state IN ('pending','queued')", (plan['source_run_id'],))


def cancel_project(project_id, *, connection=None, stamp=None):
    """Cancel pending/never-started retries; never abort an active browser run."""
    stamp = _time(stamp).isoformat()
    with _transaction(connection) as c:
        for plan in c.execute("SELECT * FROM run_retries WHERE project_id=? AND state IN ('pending','queued')", (project_id,)).fetchall():
            _cancel(c, plan, stamp)


def _register_result(c, run_id):
    run = c.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
    if not run or run['status'] not in ('success', 'failed', 'partial', 'cancelled'):
        return
    if run['origin'] == 'retry':
        c.execute("UPDATE run_retries SET state=? WHERE retry_run_id=? AND state IN ('queued','running')",
                  ('cancelled' if run['status'] == 'cancelled' else 'finished', run_id))
        return
    if run['status'] not in FAILURES or not run['finished']:
        return
    if site_safety.protected_run(run):
        return
    project = c.execute('SELECT enabled FROM projects WHERE id=?', (run['project_id'],)).fetchone()
    latest = _latest(c, run['project_id'])
    active = c.execute("SELECT 1 FROM runs WHERE project_id=? AND status IN ('queued','running')", (run['project_id'],)).fetchone()
    if not project or not project['enabled'] or not latest or latest['id'] != run_id or active:
        return
    due = (_time(run['finished']) + DELAY).isoformat()
    inserted = c.execute("INSERT OR IGNORE INTO run_retries(source_run_id,project_id,due_at,state) VALUES(?,?,?,'pending')",
                         (run_id, run['project_id'], due)).rowcount
    if inserted:
        c.execute('INSERT INTO events(run_id,time,message) VALUES(?,?,?)',
                  (run_id, run['finished'], 'Einmalige automatische Wiederholung in 30 Minuten vorgemerkt.'))


def register_result(run_id, *, connection=None):
    """Idempotent hook for a newly completed/interrupted run, not a history scan."""
    with _transaction(connection) as c:
        _register_result(c, run_id)


def finish_run(run_id, status, *, error=None, summary=None, agent_result=None, finished=None, connection=None):
    """Publish the terminal outcome and its retry plan in one write transaction."""
    if status not in ('success', 'failed', 'partial'):
        raise ValueError('Ungültiger Abschlussstatus.')
    stamp = _time(finished).isoformat()
    with _transaction(connection) as c:
        run = c.execute('SELECT project_id,status FROM runs WHERE id=?', (run_id,)).fetchone()
        if not run or run['status'] not in ('queued', 'running'):
            return False
        c.execute('UPDATE runs SET status=?,finished=?,error=?,summary=?,agent_result=? WHERE id=?',
                  (status, stamp, error, summary, agent_result, run_id))
        if status == 'success':
            c.execute('UPDATE projects SET last_success=? WHERE id=?', (stamp, run['project_id']))
        _register_result(c, run_id)
    return True


def recover_interrupted(c, stamp=None):
    """Recover only jobs that were running at this startup; preserve existing due dates."""
    stamp = _time(stamp).isoformat()
    interrupted = [r['id'] for r in c.execute("SELECT id FROM runs WHERE status='running'").fetchall()]
    c.execute("UPDATE runs SET status='failed',finished=?,error='Prozess wurde während der Prüfung beendet; erneut starten.' WHERE status='running'", (stamp,))
    for rid in interrupted:
        _register_result(c, rid)


def _valid(c, plan):
    project = c.execute('SELECT enabled FROM projects WHERE id=?', (plan['project_id'],)).fetchone()
    source = c.execute('SELECT * FROM runs WHERE id=?', (plan['source_run_id'],)).fetchone()
    latest = _latest(c, plan['project_id'])
    wanted = plan['source_run_id'] if plan['state'] == 'pending' else plan['retry_run_id']
    return bool(project and project['enabled'] and source and source['project_id'] == plan['project_id']
                and not site_safety.protected_run(source)
                and source['status'] in FAILURES and source['origin'] != 'retry'
                and latest and latest['id'] == wanted)


def sweep_due(stamp=None, *, can_queue=True):
    """Called by the scheduler. Waiting for the browser never changes the deadline."""
    stamp = _time(stamp).isoformat()
    queued = []
    with _transaction() as c:
        for plan in c.execute("SELECT * FROM run_retries WHERE state IN ('pending','queued')").fetchall():
            if not _valid(c, plan):
                _cancel(c, plan, stamp)
        manual = c.execute("SELECT value FROM state WHERE key='manual'").fetchone()
        if not can_queue or (manual and manual['value'] == '1'):
            return []
        for plan in c.execute("SELECT * FROM run_retries WHERE state='pending' AND due_at<=? ORDER BY due_at,source_run_id", (stamp,)).fetchall():
            if not _valid(c, plan):
                _cancel(c, plan, stamp)
                continue
            if c.execute("SELECT 1 FROM runs WHERE project_id=? AND status IN ('queued','running')", (plan['project_id'],)).fetchone():
                continue
            rid = store.uid()
            c.execute("INSERT INTO runs(id,project_id,started,status,origin) VALUES(?,?,?,'queued','retry')", (rid, plan['project_id'], stamp))
            c.execute("UPDATE run_retries SET state='queued',retry_run_id=? WHERE source_run_id=? AND state='pending'", (rid, plan['source_run_id']))
            c.execute('INSERT INTO events(run_id,time,message) VALUES(?,?,?)',
                      (rid, stamp, 'Einmalige automatische Wiederholung nach fehlgeschlagener Prüfung.'))
            queued.append(rid)
    return queued


def begin_attempt(run_id, *, stamp=None):
    """Atomically start a queued job after BUSY is acquired, rejecting stale claims."""
    stamp = _time(stamp).isoformat()
    with _transaction() as c:
        run = c.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if not run or run['status'] != 'queued':
            return False
        if run['origin']=='scheduled':
            project=c.execute('SELECT enabled FROM projects WHERE id=?',(run['project_id'],)).fetchone()
            if not project or not project['enabled']:
                c.execute("UPDATE runs SET status='cancelled',finished=?,error=? WHERE id=?",
                          (stamp,'Automatische Prüfung verworfen: Projekt pausiert.',run_id))
                return False
        if run['origin'] == 'retry':
            plan = c.execute('SELECT * FROM run_retries WHERE retry_run_id=?', (run_id,)).fetchone()
            if not plan or plan['state'] != 'queued' or not _valid(c, plan):
                if plan:
                    _cancel(c, plan, stamp)
                else:
                    c.execute("UPDATE runs SET status='cancelled',finished=?,error=? WHERE id=? AND status='queued'", (stamp, CANCEL_REASON, run_id))
                return False
            manual = c.execute("SELECT value FROM state WHERE key='manual'").fetchone()
            if manual and manual['value'] == '1':
                return False
            c.execute("UPDATE run_retries SET state='running' WHERE source_run_id=? AND state='queued'", (plan['source_run_id'],))
        c.execute("UPDATE runs SET status='running' WHERE id=? AND status='queued'", (run_id,))
    return True


def project_retry(project_id, latest_run_id=None):
    """Small presentation contract; elapsed deadlines stay pending while BUSY/manual."""
    with store.db() as c:
        c.execute('BEGIN')
        plans = c.execute("SELECT * FROM run_retries WHERE project_id=? AND state IN ('pending','queued','running') ORDER BY due_at DESC", (project_id,)).fetchall()
        for plan in plans:
            if not _valid(c, plan):
                continue
            wanted = plan['source_run_id'] if plan['state'] == 'pending' else plan['retry_run_id']
            if latest_run_id is not None and latest_run_id != wanted:
                continue
            if plan['state'] != 'pending':
                child = c.execute('SELECT status FROM runs WHERE id=?', (plan['retry_run_id'],)).fetchone()
                if not child or child['status'] != plan['state']:
                    continue
            return {key: plan[key] for key in ('state', 'due_at', 'source_run_id', 'retry_run_id')}
    return None
