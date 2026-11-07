"""Single-admin ENV credentials and revocable, opaque server-side sessions."""
from collections import OrderedDict, deque
from contextlib import contextmanager
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import unquote, urlsplit

COOKIE = 'kiwm_session'


def safe_next(value):
    value = str(value or '/')
    decoded = value
    for _ in range(3):
        decoded = unquote(decoded)
    if (not value.startswith('/') or decoded.startswith('//') or '\\' in decoded
            or any(ord(c) < 32 or ord(c) == 127 for c in decoded)
            or len(value) > 2048 or urlsplit(decoded).scheme or urlsplit(decoded).netloc
            or urlsplit(decoded).path in ('/login', '/logout', '/auth/check')):
        return '/'
    return value


class AdminAuth:
    def __init__(self, root, environ=None, clock=time.time):
        values = os.environ if environ is None else environ
        self.username = values.get('KIWM_ADMIN_USERNAME', '').strip()
        password = values.get('KIWM_ADMIN_PASSWORD', '')
        self.enabled = bool(self.username or password)
        self.error = ''
        self.clock = clock
        self.root = Path(root) / 'auth'
        self.lock = threading.Lock()
        self.failures = OrderedDict()
        self.global_failures = deque()
        self.hours = 168
        self._ready = False
        if not self.enabled:
            return
        try:
            self.hours = int(values.get('KIWM_SESSION_HOURS') or '168')
            if (not 1 <= len(self.username) <= 80 or any(ord(c) < 32 for c in self.username)
                    or not 12 <= len(password) <= 512 or not 1 <= self.hours <= 720):
                raise ValueError()
        except (ValueError, TypeError):
            self.error = 'Administratorzugang ist noch nicht vollständig eingerichtet.'
            return
        self._password = password  # Removed immediately after deriving the credential verifier.

    def ready(self):
        if not self.enabled or self.error:
            return False
        with self.lock:
            if self._ready:
                return True
            if self.root.is_symlink() or any(p.is_symlink() for p in self.root.parents):
                raise ValueError('Authentication storage must not use symlinks.')
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.root, 0o700)
            key_path = self.root / 'session.key'
            try:
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(secrets.token_bytes(32))
            fd = os.open(key_path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as stream:
                key = stream.read(33)
            if len(key) != 32 or (self.root / 'sessions.db').is_symlink():
                raise ValueError('Authentication storage is invalid.')
            self.key = key
            self.verifier = self.derive(self._password)
            self.principal = hmac.digest(key, self.username.encode() + b'\0' + self.verifier, 'sha256').hex()
            with self.database() as db:
                db.execute('CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, principal TEXT NOT NULL, created INTEGER NOT NULL, expires INTEGER NOT NULL)')
                db.execute('DELETE FROM sessions WHERE expires<=? OR principal!=?', (int(self.clock()), self.principal))
            os.chmod(self.root / 'sessions.db', 0o600)
            del self._password
            self._ready = True
        return True

    def derive(self, password):
        return hashlib.scrypt(password.encode(), salt=self.key[:16], n=32768, r=8, p=1,
                              maxmem=128 * 1024 * 1024, dklen=32)

    @contextmanager
    def database(self):
        db = sqlite3.connect(self.root / 'sessions.db', timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def blocked(self, client):
        now = self.clock()
        with self.lock:
            while self.global_failures and self.global_failures[0] <= now - 60:
                self.global_failures.popleft()
            attempts = self.failures.get(client, deque())
            while attempts and attempts[0] <= now - 60:
                attempts.popleft()
            return len(attempts) >= 5 or len(self.global_failures) >= 50

    def login(self, username, password, client, previous=None):
        if not self.ready():
            return None, 'configuration'
        if self.blocked(client):
            return None, 'rate'
        if len(password) > 512 or len(username) > 80:
            valid = False
        else:
            candidate = self.derive(password)
            valid = hmac.compare_digest(candidate, self.verifier) & hmac.compare_digest(username.strip().encode(), self.username.encode())
        if not valid:
            with self.lock:
                self.failures.setdefault(client, deque()).append(self.clock())
                self.failures.move_to_end(client)
                self.global_failures.append(self.clock())
                while len(self.failures) > 4096:
                    self.failures.popitem(last=False)
            return None, 'credentials'
        with self.lock:
            self.failures.pop(client, None)
        token = secrets.token_urlsafe(32)
        now = int(self.clock())
        with self.database() as db:
            if previous:
                db.execute('DELETE FROM sessions WHERE token_hash=?', (self.token_hash(previous),))
            db.execute('DELETE FROM sessions WHERE expires<=? OR principal!=?', (now, self.principal))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)',
                       (self.token_hash(token), self.principal, now, now + self.hours * 3600))
            db.execute('DELETE FROM sessions WHERE token_hash NOT IN (SELECT token_hash FROM sessions ORDER BY created DESC, rowid DESC LIMIT 32)')
        return token, None

    @staticmethod
    def token_hash(token):
        return hashlib.sha256(str(token or '').encode()).hexdigest()

    def authorized(self, token):
        if not token or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token) or not self.ready():
            return False
        with self.database() as db:
            row = db.execute('SELECT principal,expires FROM sessions WHERE token_hash=?', (self.token_hash(token),)).fetchone()
        return bool(row and row[1] > int(self.clock()) and hmac.compare_digest(row[0], self.principal))

    def logout(self, token):
        if self.ready():
            with self.database() as db:
                db.execute('DELETE FROM sessions WHERE token_hash=?', (self.token_hash(token),))


_manager = None


def current():
    global _manager
    if _manager is None:
        import store
        _manager = AdminAuth(store.DATA)
    return _manager
