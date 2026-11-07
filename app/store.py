import contextlib
import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from croniter import croniter
import configuration
import communication_config

DATA = Path(os.environ.get('DATA_DIR', '/data'))
CONFIG = Path('/config/settings.json')
SECRETS = Path('/secrets/credentials.json')
TZ = ZoneInfo('Europe/Berlin')

def now():
    return datetime.now(timezone.utc).isoformat()

def uid():
    return uuid.uuid4().hex

def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()

@contextlib.contextmanager
def db():
    c = sqlite3.connect(DATA / 'monitor.db', timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    try:
        yield c
        c.commit()
    finally:
        c.close()

def rows(sql, args=()):
    with db() as c:
        return [dict(r) for r in c.execute(sql, args)]

def one(sql, args=()):
    r = rows(sql, args)
    return r[0] if r else None

def execute(sql, args=()):
    with db() as c:
        c.execute(sql, args)

def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default or {}

def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as f:
        os.chmod(tmp, 0o600)
        json.dump(value, f, ensure_ascii=False, indent=2)
    tmp.replace(path)

def persisted_settings():
    """Saved UI values only. Use this for edits/exports, never effective ENV values."""
    return configuration.public_settings(read_json(CONFIG))

def persisted_credentials():
    """Saved credentials only; deployment tokens must never be written back."""
    return configuration.private_credentials(read_json(SECRETS))

def settings():
    return communication_config.resolve(configuration.resolve_settings(persisted_settings()), communication_config.FIELDS, strict=False)

def credentials():
    return communication_config.resolve(configuration.resolve_credentials(persisted_credentials()), communication_config.SECRETS, strict=False)

def configuration_sources():
    return {**configuration.sources(), **communication_config.sources()}

def next_due(cron, after=None):
    dt = after or datetime.now(TZ)
    return croniter(cron, dt.astimezone(TZ)).get_next(datetime).astimezone(timezone.utc).isoformat()

def init():
    DATA.mkdir(parents=True, exist_ok=True)
    for name in ('artifacts', 'reports', 'logs'):
        (DATA / name).mkdir(exist_ok=True)
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS projects(
          id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL, prompt TEXT NOT NULL,
          domains TEXT NOT NULL, cron TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
          next_due TEXT NOT NULL, last_success TEXT, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs(
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
          started TEXT NOT NULL, finished TEXT, status TEXT NOT NULL, error TEXT,
          summary TEXT, agent_result TEXT, origin TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS snapshots(
          id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
          project_id TEXT NOT NULL REFERENCES projects(id), url TEXT NOT NULL,
          kind TEXT NOT NULL, captured TEXT NOT NULL, sha256 TEXT NOT NULL,
          text_sha256 TEXT NOT NULL, text TEXT NOT NULL, links TEXT NOT NULL DEFAULT '[]',
          file TEXT, screenshot TEXT, html TEXT);
        CREATE INDEX IF NOT EXISTS snapshots_source ON snapshots(project_id,url,captured);
        CREATE TABLE IF NOT EXISTS changes(
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, run_id TEXT NOT NULL,
          url TEXT NOT NULL, kind TEXT NOT NULL, detected TEXT NOT NULL,
          previous_id TEXT, current_id TEXT NOT NULL, evidence TEXT NOT NULL,
          fingerprint TEXT NOT NULL, UNIQUE(project_id,fingerprint));
        CREATE TABLE IF NOT EXISTS events(
          id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
          time TEXT NOT NULL, message TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reports(
          id TEXT PRIMARY KEY, period_key TEXT NOT NULL UNIQUE, created TEXT NOT NULL,
          start TEXT NOT NULL, end TEXT NOT NULL, content TEXT NOT NULL,
          mail_status TEXT NOT NULL DEFAULT 'pending', error TEXT);
        CREATE TABLE IF NOT EXISTS report_changes(
          change_id TEXT PRIMARY KEY, report_id TEXT NOT NULL REFERENCES reports(id));
        CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS change_reviews(
          change_id TEXT PRIMARY KEY REFERENCES changes(id) ON DELETE CASCADE,
          reviewed_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS changes_project_run ON changes(project_id,run_id);
        ''')
        columns={row[1] for row in c.execute('PRAGMA table_info(projects)')}
        if 'color' not in columns:c.execute("ALTER TABLE projects ADD COLUMN color TEXT NOT NULL DEFAULT '#1967d2'")
        if 'tags' not in columns:c.execute("ALTER TABLE projects ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
        if 'use_global_schedule' not in columns:c.execute('ALTER TABLE projects ADD COLUMN use_global_schedule INTEGER NOT NULL DEFAULT 0')
        if 'schedule_config' not in columns:c.execute("ALTER TABLE projects ADD COLUMN schedule_config TEXT NOT NULL DEFAULT '{}'")
        if 'telegram_enabled' not in columns:c.execute('ALTER TABLE projects ADD COLUMN telegram_enabled INTEGER NOT NULL DEFAULT 0')
        import notifications
        notifications.initialize(c)
        import distributions
        distributions.initialize(c)
        import tag_registry
        tag_registry.initialize(c)
        import retry_jobs
        retry_jobs.initialize(c)
        retry_jobs.recover_interrupted(c, now())
    if not one("SELECT key FROM state WHERE key='manual'"):
        execute("INSERT INTO state VALUES('manual','0')")
    if not one("SELECT key FROM state WHERE key='weekly_due'"):
        execute("INSERT INTO state VALUES('weekly_due',?)", (next_due('0 8 * * 1'),))

def event(run_id, message):
    execute('INSERT INTO events(run_id,time,message) VALUES(?,?,?)', (run_id, now(), message[:2000]))

def manual():
    r = one("SELECT value FROM state WHERE key='manual'")
    return bool(r and r['value'] == '1')

def enqueue(project_id, origin='manual'):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if origin == 'scheduled':
            project=c.execute('SELECT enabled FROM projects WHERE id=?',(project_id,)).fetchone()
            if not project or not project['enabled']:return None
        if origin == 'retry':
            return None  # Automatic retries can only be claimed from their durable plan.
        import retry_jobs
        retry_jobs.cancel_project(project_id, connection=c)
        if c.execute("SELECT 1 FROM runs WHERE project_id=? AND status IN ('queued','running')", (project_id,)).fetchone():
            return None
        rid = uid()
        c.execute('INSERT INTO runs(id,project_id,started,status,origin) VALUES(?,?,?,?,?)', (rid, project_id, now(), 'queued', origin))
        return rid
