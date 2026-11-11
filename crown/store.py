"""The database is both the lease authority and the sole publication boundary.

All mutations serialize with BEGIN IMMEDIATE. No process may send externally
after a successful permission check: publishing means inserting in this outlet.
This first version is deliberately restricted to one Linux host and local disk.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import sqlite3
import time
from typing import Iterator


class CrownError(Exception):
    pass


class Fenced(CrownError):
    """This process no longer owns the speaking role."""


class Conflict(CrownError):
    """An idempotency key was reused with different content."""


class SystemClock:
    def __init__(self):
        boot = Path('/proc/sys/kernel/random/boot_id')
        if not boot.is_file() or not hasattr(time, 'CLOCK_BOOTTIME'):
            raise CrownError('v0.1 requires Linux (WSL2 is suitable).')
        self.boot_id = boot.read_text().strip()
        self.host_id = os.uname().nodename

    def now_ns(self) -> int:
        # Unlike wall time this cannot jump with NTP; unlike MONOTONIC it
        # includes machine suspend. A reboot changes boot_id and expires leases.
        return time.clock_gettime_ns(time.CLOCK_BOOTTIME)


@dataclass(frozen=True)
class Token:
    owner: str
    epoch: int


SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lease (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    owner TEXT NOT NULL, label TEXT NOT NULL, epoch INTEGER NOT NULL,
    boot_id TEXT NOT NULL, expires_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
    source TEXT NOT NULL, task_id TEXT NOT NULL, session_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('update','question')),
    text TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS answers (
    question_id TEXT PRIMARY KEY REFERENCES events(event_id),
    answer_id TEXT UNIQUE NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS outlet (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL
        REFERENCES events(event_id), speaker TEXT NOT NULL, owner TEXT NOT NULL,
    epoch INTEGER NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
    owner TEXT NOT NULL, epoch INTEGER NOT NULL, at_ns INTEGER NOT NULL,
    boot_id TEXT NOT NULL
);
'''


def _text(value: str, name: str, limit: int = 8000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise CrownError(f'{name} must be nonempty text, at most {limit} characters')
    return value


def _ttl(seconds: float) -> int:
    if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0.05 <= seconds <= 60:
        raise CrownError('lease duration must be between 0.05 and 60 seconds')
    return int(seconds * 1_000_000_000)


class Store:
    def __init__(self, path: str | Path, *, clock=None, busy_ms: int = 5000):
        raw_path = Path(path).expanduser()
        if raw_path.is_symlink():
            raise CrownError('database must be a regular local file, not a symlink')
        if not isinstance(busy_ms, int) or not 1 <= busy_ms <= 60_000:
            raise CrownError('busy_ms must be an integer between 1 and 60000')
        self.path = raw_path.resolve()
        self.clock = clock or SystemClock()
        self.busy_ms = busy_ms
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Only the owning OS user should be able to read conversations.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self._connection() as con:
            version = con.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise CrownError(f'unsupported database schema {version}; refusing to modify it')
            # Existing databases must not take a write lock on every Store()
            # construction. Capture-token and standby snapshots used to run
            # CREATE IF NOT EXISTS + INSERT OR IGNORE here, which blocked
            # acquire while a paused speaker held the same file.
            if version == 0:
                con.execute('PRAGMA journal_mode=WAL')
                con.executescript(SCHEMA)
                con.execute('PRAGMA user_version=1')
        with self._read() as con:
            row = con.execute("SELECT value FROM meta WHERE key='host'").fetchone()
        if row:
            if row['value'] != self.clock.host_id:
                raise CrownError('Crown v0.1 is single-host; this database belongs to another host')
        else:
            with self._transaction(synchronous='NORMAL') as con:
                con.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('host', self.clock.host_id))
                host = con.execute("SELECT value FROM meta WHERE key='host'").fetchone()[0]
                if host != self.clock.host_id:
                    raise CrownError('Crown v0.1 is single-host; this database belongs to another host')

    @contextmanager
    def _connection(self, *, synchronous: str = 'FULL') -> Iterator[sqlite3.Connection]:
        if synchronous not in ('FULL', 'NORMAL'):
            raise CrownError('unsupported synchronous mode')
        timeout = max(self.busy_ms / 1000, 0.05)
        con = sqlite3.connect(self.path, timeout=timeout, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys=ON')
        con.execute(f'PRAGMA synchronous={synchronous}')
        con.execute(f'PRAGMA busy_timeout={int(self.busy_ms)}')
        try:
            yield con
        finally:
            con.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as con:
            con.execute('BEGIN')
            try:
                yield con
            finally:
                try:
                    con.execute('ROLLBACK')
                except sqlite3.Error:
                    pass

    @contextmanager
    def _transaction(self, *, synchronous: str = 'FULL'):
        with self._connection(synchronous=synchronous) as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                yield con
                con.execute('COMMIT')
            except BaseException:
                con.execute('ROLLBACK')
                raise

    def _live(self, row, now: int) -> bool:
        return bool(row and row['boot_id'] == self.clock.boot_id and row['expires_ns'] > now)

    def _check(self, con, token: Token):
        row = con.execute('SELECT * FROM lease WHERE singleton=1').fetchone()
        if not self._live(row, self.clock.now_ns()) or (row['owner'], row['epoch']) != (token.owner, token.epoch):
            raise Fenced('speaking lease is expired or belongs to another process')
        return row

    def _audit(self, con, action: str, token: Token):
        con.execute('INSERT INTO audit(action,owner,epoch,at_ns,boot_id) VALUES (?,?,?,?,?)',
                    (action, token.owner, token.epoch, self.clock.now_ns(), self.clock.boot_id))

    def acquire(self, owner: str, label: str, seconds: float = 3) -> Token | None:
        _text(owner, 'owner', 200)
        _text(label, 'label', 100)
        ttl = _ttl(seconds)
        # Live foreign leases are a read. A paused speaker may still hold a
        # write lock from an in-flight renew; standbys must not take one just
        # to observe that they are not the owner.
        with self._read() as con:
            now = self.clock.now_ns()
            row = con.execute('SELECT * FROM lease WHERE singleton=1').fetchone()
            if self._live(row, now):
                return Token(owner, row['epoch']) if row['owner'] == owner else None
        with self._transaction(synchronous='NORMAL') as con:
            now = self.clock.now_ns()
            row = con.execute('SELECT * FROM lease WHERE singleton=1').fetchone()
            if self._live(row, now):
                return Token(owner, row['epoch']) if row['owner'] == owner else None
            epoch = row['epoch'] + 1 if row else 1
            token = Token(owner, epoch)
            con.execute('INSERT OR REPLACE INTO lease VALUES (1,?,?,?,?,?)',
                        (owner, label, epoch, self.clock.boot_id, now + ttl))
            self._audit(con, 'acquire', token)
            return token

    def renew(self, token: Token, seconds: float = 3):
        ttl = _ttl(seconds)
        with self._transaction(synchronous='NORMAL') as con:
            self._check(con, token)
            con.execute('UPDATE lease SET expires_ns=? WHERE singleton=1',
                        (self.clock.now_ns() + ttl,))

    def release(self, token: Token):
        with self._transaction(synchronous='NORMAL') as con:
            self._check(con, token)
            con.execute('UPDATE lease SET expires_ns=0 WHERE singleton=1')
            self._audit(con, 'release', token)

    def lease_remaining_seconds(self, token: Token | None = None) -> float:
        with self._read() as con:
            row = con.execute('SELECT * FROM lease WHERE singleton=1').fetchone()
            now = self.clock.now_ns()
            if not self._live(row, now):
                return 0.0
            if token is not None and (row['owner'], row['epoch']) != (token.owner, token.epoch):
                return 0.0
            return (row['expires_ns'] - now) / 1e9

    def has_unpublished(self) -> bool:
        with self._read() as con:
            row = con.execute('''SELECT e.event_id FROM events e
                LEFT JOIN outlet o ON o.event_id=e.event_id
                LEFT JOIN answers a ON a.question_id=e.event_id
                WHERE o.seq IS NULL AND a.question_id IS NULL LIMIT 1''').fetchone()
            return row is not None

    def report(self, event_id: str, source: str, task_id: str, kind: str,
               text: str, session_id: str = '') -> dict:
        for value, name in [(event_id, 'event_id'), (source, 'source'), (task_id, 'task_id')]:
            _text(value, name, 200)
        _text(text, 'text')
        if kind not in ('update', 'question'):
            raise CrownError('kind must be update or question')
        if not isinstance(session_id, str) or len(session_id) > 200:
            raise CrownError('invalid session_id')
        values = (event_id, source, task_id, session_id, kind, text)
        with self._transaction() as con:
            old = con.execute('SELECT * FROM events WHERE event_id=?', (event_id,)).fetchone()
            if old:
                if tuple(old[k] for k in ('event_id', 'source', 'task_id', 'session_id', 'kind', 'text')) != values:
                    raise Conflict('event_id already belongs to different content')
                return {'event_id': event_id, 'duplicate': True}
            con.execute('INSERT INTO events(event_id,source,task_id,session_id,kind,text,created_at) VALUES (?,?,?,?,?,?,?)',
                        (*values, time.time()))
            return {'event_id': event_id, 'duplicate': False}

    def answer(self, question_id: str, answer_id: str, text: str) -> dict:
        """Operator-only entry point. Never exposed as an LLM tool."""
        _text(question_id, 'question_id', 200)
        _text(answer_id, 'answer_id', 200)
        _text(text, 'answer')
        with self._transaction() as con:
            question = con.execute('SELECT kind FROM events WHERE event_id=?', (question_id,)).fetchone()
            if not question or question['kind'] != 'question':
                raise CrownError('answer must reference an existing question ID')
            old = con.execute('SELECT * FROM answers WHERE question_id=? OR answer_id=?',
                              (question_id, answer_id)).fetchone()
            if old:
                if (old['question_id'], old['answer_id'], old['text']) != (question_id, answer_id, text):
                    raise Conflict('answer already recorded; answers cannot be silently replaced')
                return {'question_id': question_id, 'duplicate': True}
            con.execute('INSERT INTO answers VALUES (?,?,?,?)', (question_id, answer_id, text, time.time()))
            return {'question_id': question_id, 'duplicate': False}

    def _context(self, con) -> dict:
        # Pending questions are not truncated. Answer history is bounded in
        # notifications but every row remains available in context / export.
        pending = [dict(x) for x in con.execute('''SELECT e.* FROM events e
                   LEFT JOIN answers a ON a.question_id=e.event_id
                   WHERE e.kind='question' AND a.question_id IS NULL ORDER BY e.seq''')]
        answers = [dict(x) for x in con.execute('''SELECT a.*,e.text AS question,
                   e.source,e.task_id,e.session_id FROM answers a
                   JOIN events e ON e.event_id=a.question_id ORDER BY e.seq''')]
        return {'pending': pending, 'answers': answers}

    def publish_next(self, token: Token) -> dict | None:
        """Fence, read answers, choose event, and publish in ONE transaction."""
        with self._transaction() as con:
            lease = self._check(con, token)
            event = con.execute('''SELECT e.* FROM events e
                LEFT JOIN outlet o ON o.event_id=e.event_id
                LEFT JOIN answers a ON a.question_id=e.event_id
                WHERE o.seq IS NULL AND a.question_id IS NULL ORDER BY e.seq LIMIT 1''').fetchone()
            if not event:
                return None
            context = self._context(con)
            payload = {
                'event_id': event['event_id'], 'source': event['source'],
                'task_id': event['task_id'], 'session_id': event['session_id'],
                'kind': event['kind'], 'text': event['text'],
                'known_answers': context['answers'][-20:],
                'answer_count': len(context['answers']),
            }
            # A fresh check after assembling the payload also handles a slow
            # operation that used up its lease while holding the transaction.
            self._check(con, token)
            cur = con.execute('''INSERT INTO outlet(event_id,speaker,owner,epoch,payload,created_at)
                VALUES (?,?,?,?,?,?)''', (event['event_id'], lease['label'], token.owner,
                token.epoch, json.dumps(payload, ensure_ascii=False), time.time()))
            self._audit(con, 'publish', token)
            return {'seq': cur.lastrowid, 'speaker': lease['label'], 'epoch': token.epoch, **payload}

    def snapshot(self, after: int = 0) -> dict:
        with self._connection() as con:
            con.execute('BEGIN')  # consistent read across lease, record and outlet
            lease = con.execute('SELECT * FROM lease WHERE singleton=1').fetchone()
            state = self._context(con)
            state['lease'] = dict(lease) if lease else None
            if state['lease']:
                state['lease']['active'] = self._live(lease, self.clock.now_ns())
            state['outlet'] = []
            for row in con.execute('SELECT * FROM outlet WHERE seq>? ORDER BY seq', (after,)):
                state['outlet'].append({'seq': row['seq'], 'speaker': row['speaker'],
                    'epoch': row['epoch'], **json.loads(row['payload'])})
            state['event_count'] = con.execute('SELECT COUNT(*) FROM events').fetchone()[0]
            state['publication_count'] = con.execute('SELECT COUNT(*) FROM outlet').fetchone()[0]
            state['audit'] = [dict(x) for x in con.execute('SELECT * FROM audit ORDER BY seq')]
            return state
