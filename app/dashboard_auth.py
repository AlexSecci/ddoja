"""One dashboard owner, Argon2id passwords and revocable persistent sessions."""
from contextlib import contextmanager
import hashlib
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError

COOKIE = '__Host-ddoja-session'
SESSION_SECONDS = 90 * 24 * 3600
RENEW_SECONDS = 24 * 3600
TOKEN = re.compile(r'[A-Za-z0-9_-]{43}\Z')
USERNAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._@+-]{1,63}\Z')


class AuthFailure(Exception):
    def __init__(self, detail='Invalid username or password.', status=401, retry_after=None):
        self.detail, self.status, self.retry_after = detail, status, retry_after


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class DashboardAuth:
    def __init__(self, root, *, clock=time.time, hasher=None):
        self.root = Path(root)
        self.path = self.root / 'data/secrets/dashboard-auth.sqlite'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.hasher = hasher or PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
        self.hash_slots = threading.BoundedSemaphore(2)
        self.dummy_hash = self.hasher.hash(secrets.token_urlsafe(32))
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS owner (
                    id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL,
                    password_hash TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL,
                    expires_at REAL NOT NULL, renew_after REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS bootstrap (
                    id INTEGER PRIMARY KEY CHECK(id=1), token_hash TEXT NOT NULL, expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (
                    bucket TEXT PRIMARY KEY, window_start REAL NOT NULL, count INTEGER NOT NULL);
            ''')

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def configured(self):
        with self.connect() as db:
            return db.execute('SELECT 1 FROM owner WHERE id=1').fetchone() is not None

    def prepare_setup(self):
        token = secrets.token_urlsafe(32)
        with self.connect(write=True) as db:
            if db.execute('SELECT 1 FROM owner').fetchone():
                raise AuthFailure('The owner account is already configured.', 409)
            db.execute('INSERT OR REPLACE INTO bootstrap VALUES(1,?,?)',
                       (digest(token), self.clock() + 24 * 3600))
        return token

    def check_setup(self, token, db):
        row = db.execute('SELECT * FROM bootstrap WHERE id=1').fetchone()
        if (not isinstance(token, str) or not TOKEN.fullmatch(token) or not row
                or row['expires_at'] <= self.clock()
                or not secrets.compare_digest(row['token_hash'], digest(token))):
            raise AuthFailure('The setup link is invalid or expired.', 403)
        if db.execute('SELECT 1 FROM owner').fetchone():
            raise AuthFailure('The owner account is already configured.', 409)

    def reserve_attempt(self, client_key):
        now = self.clock()
        keys = [('global', 40), ('client:' + digest(client_key), 8)]
        # Commit the reservation before checking the password. Concurrent
        # attempts cannot evade the limit by arriving before a failed verify.
        with self.connect(write=True) as db:
            for key, limit in keys:
                row = db.execute('SELECT * FROM attempts WHERE bucket=?', (key,)).fetchone()
                if row and now - row['window_start'] < 60 and row['count'] >= limit:
                    raise AuthFailure('Too many attempts. Please wait a minute.', 429,
                                      max(1, int(60 - (now - row['window_start']))))
            db.execute('DELETE FROM attempts WHERE window_start<?', (now - 3600,))
            for key, _ in keys:
                row = db.execute('SELECT * FROM attempts WHERE bucket=?', (key,)).fetchone()
                if not row or now - row['window_start'] >= 60:
                    db.execute('INSERT OR REPLACE INTO attempts VALUES(?,?,1)', (key, now))
                else:
                    db.execute('UPDATE attempts SET count=count+1 WHERE bucket=?', (key,))

    @staticmethod
    def validate_credentials(username, password):
        if not isinstance(username, str) or not USERNAME.fullmatch(username):
            raise AuthFailure('Use a username of 2–64 letters, digits, or . _ @ + -.', 422)
        if not isinstance(password, str) or not 15 <= len(password) <= 1024:
            raise AuthFailure('Use a password or passphrase of 15–1024 characters.', 422)

    def _hash(self, password):
        if not self.hash_slots.acquire(blocking=False):
            raise AuthFailure('Please try again in a moment.', 429, 2)
        try:
            return self.hasher.hash(password)
        finally:
            self.hash_slots.release()

    def _new_session(self, db):
        now = self.clock()
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        db.execute('DELETE FROM sessions WHERE expires_at<=?', (now,))
        db.execute('INSERT INTO sessions VALUES(?,?,?,?)',
                   (digest(token), csrf, now + SESSION_SECONDS, now + RENEW_SECONDS))
        return {'token': token, 'csrf': csrf, 'renew': True, 'expires_at': now + SESSION_SECONDS}

    def setup(self, token, username, password, client_key):
        self.reserve_attempt(client_key)
        self.validate_credentials(username, password)
        # Check before hashing, then revalidate inside the final write transaction.
        with self.connect() as db:
            self.check_setup(token, db)
        password_hash = self._hash(password)
        with self.connect(write=True) as db:
            self.check_setup(token, db)
            db.execute('INSERT INTO owner VALUES(1,?,?,1)', (username.casefold(), password_hash))
            db.execute('DELETE FROM bootstrap')
            session = self._new_session(db)
        try:
            (self.root / 'data/secrets/dashboard-setup.dpapi').unlink(missing_ok=True)
        except OSError:
            pass  # The consumed database token already prevents reuse.
        return session

    def configure(self, username, password):
        """Local terminal-only credential replacement revokes every saved session."""
        self.validate_credentials(username, password)
        password_hash = self._hash(password)
        with self.connect(write=True) as db:
            db.execute('INSERT INTO owner VALUES(1,?,?,1) ON CONFLICT(id) DO UPDATE SET '
                       'username=excluded.username,password_hash=excluded.password_hash,revision=revision+1',
                       (username.casefold(), password_hash))
            db.execute('DELETE FROM sessions')
            db.execute('DELETE FROM bootstrap')
        try:
            (self.root / 'data/secrets/dashboard-setup.dpapi').unlink(missing_ok=True)
        except OSError:
            pass

    def login(self, username, password, client_key):
        self.reserve_attempt(client_key)
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 64 or len(password) > 1024:
            raise AuthFailure()
        with self.connect() as db:
            row = db.execute('SELECT * FROM owner WHERE id=1').fetchone()
        encoded = row['password_hash'] if row else self.dummy_hash
        if not self.hash_slots.acquire(blocking=False):
            raise AuthFailure('Please try again in a moment.', 429, 2)
        try:
            try:
                valid = self.hasher.verify(encoded, password)
            except (VerificationError, InvalidHashError):
                valid = False
        finally:
            self.hash_slots.release()
        if not valid or not row or not secrets.compare_digest(row['username'].encode(), username.casefold().encode()):
            raise AuthFailure()
        new_hash = self._hash(password) if self.hasher.check_needs_rehash(encoded) else encoded
        with self.connect(write=True) as db:
            current = db.execute('SELECT * FROM owner WHERE id=1').fetchone()
            if not current or current['revision'] != row['revision'] or current['password_hash'] != encoded:
                raise AuthFailure()
            if new_hash != encoded:
                db.execute('UPDATE owner SET password_hash=? WHERE id=1', (new_hash,))
            return self._new_session(db)

    def authenticate(self, token):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            return None
        now = self.clock()
        with self.connect() as db:
            row = db.execute('SELECT s.*,o.username FROM sessions s CROSS JOIN owner o '
                             'WHERE s.token_hash=? AND o.id=1', (digest(token),)).fetchone()
        if not row or row['expires_at'] <= now:
            return None
        renewed = row['renew_after'] <= now
        expires = row['expires_at']
        if renewed:
            expires = now + SESSION_SECONDS
            with self.connect(write=True) as db:
                updated = db.execute('UPDATE sessions SET expires_at=?,renew_after=? WHERE token_hash=? '
                                     'AND expires_at>?', (expires, now + RENEW_SECONDS, digest(token), now))
                if updated.rowcount != 1:
                    return None
        return {'token': token, 'csrf': row['csrf'], 'username': row['username'],
                'renew': renewed, 'expires_at': expires}

    def logout(self, token):
        with self.connect(write=True) as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (digest(token),))
