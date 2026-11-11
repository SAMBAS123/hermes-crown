"""Explicitly enrolled participant; no attention detection and no LLM polling."""
from __future__ import annotations

import os
from pathlib import Path
import threading
import time
import uuid

from .store import Fenced, Store, Token, _text, _ttl


def hold_path(db, pid=None) -> Path:
    path = Path(str(db)).expanduser()
    try:
        path = path.resolve()
    except OSError:
        pass
    return Path(str(path) + f'.hold.{pid or os.getpid()}')


class Participant:
    def __init__(self, store: Store, label: str, ttl: float = 3):
        _ttl(ttl)
        _text(label, 'instance name', 100)
        self.store = store
        self.label = label
        self.ttl = ttl
        self.owner = f'{label}:{uuid.uuid4().hex}'
        self.token: Token | None = None
        self.fenced_count = 0
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.last_error: str | None = None

    def _hold_file(self) -> Path:
        return hold_path(self.store.path)

    def ticks_held(self) -> bool:
        return self._hold_file().exists()

    def hold_ticks(self):
        """Stop further sqlite write ticks; drain any in-flight tick.

        Capture-then-SIGSTOP must not freeze the speaker inside BEGIN IMMEDIATE.
        """
        self._hold_file().write_text('1')
        with self.lock:
            pass

    def release_ticks(self):
        try:
            self._hold_file().unlink()
        except FileNotFoundError:
            pass

    def tick(self, *, publish=True):
        with self.lock:
            if self.ticks_held():
                return None
            try:
                if self.token is None:
                    self.token = self.store.acquire(self.owner, self.label, self.ttl)
                else:
                    remaining = self.store.lease_remaining_seconds(self.token)
                    if remaining <= 0:
                        raise Fenced('speaking lease is expired or belongs to another process')
                    if remaining < self.ttl * 0.5:
                        self.store.renew(self.token, self.ttl)
                if self.token is not None and publish:
                    # Empty publish_next used to take BEGIN IMMEDIATE on every
                    # heartbeat. SIGSTOP during that write lock blocked every
                    # standby acquire even after the committed lease expired.
                    if not self.store.has_unpublished():
                        return None
                    return self.store.publish_next(self.token)
            except Fenced:
                self.fenced_count += 1
                self.token = None
            return None

    def _wait_seconds(self) -> float:
        if self.token is None:
            return min(max(self.ttl / 8, 0.05), 0.25)
        return max(self.ttl / 3, 0.05)

    def run(self):
        while not self.stop_event.is_set():
            if self.ticks_held():
                self.stop_event.wait(0.05)
                continue
            try:
                self.tick()
                self.last_error = None
            except Exception as exc:
                # Database unavailable means no publication. Never send by
                # another route or claim success on a failed durable write.
                self.last_error = f'{type(exc).__name__}: {exc}'
            self.stop_event.wait(self._wait_seconds())

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.run, name='crown-speaker', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.release_ticks()
        if self.thread:
            self.thread.join(timeout=6)
        with self.lock:
            if self.token:
                try:
                    self.store.release(self.token)
                except Fenced:
                    pass
                self.token = None


def process_node(db: str, label: str, ttl: float, pipe):
    """Real child process used by the failure demo and integration tests."""
    participant = Participant(Store(db), label, ttl)
    pipe.send({'owner': participant.owner})
    try:
        while True:
            if pipe.poll(min(ttl / 4, 0.1)):
                command = pipe.recv()
                if command == 'stop':
                    break
                if command == 'status':
                    pipe.send({'owner': participant.owner,
                        'token': vars(participant.token) if participant.token else None,
                        'fenced_count': participant.fenced_count})
                elif command == 'stale-publish':
                    # The caller pauses this process, permits takeover, then
                    # resumes it. Try its OLD token before any renewal.
                    try:
                        participant.store.publish_next(participant.token)
                        pipe.send({'fenced': False})
                    except Fenced:
                        pipe.send({'fenced': True})
                else:
                    raise ValueError(command)
            participant.tick()
    finally:
        participant.stop()
        pipe.close()
