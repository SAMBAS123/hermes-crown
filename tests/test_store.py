import multiprocessing as mp
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from crown.store import Conflict, CrownError, Fenced, Store


class Clock:
    boot_id = 'boot-one'
    host_id = 'test-host'

    def __init__(self):
        self.n = 1_000_000_000

    def now_ns(self):
        return self.n

    def advance(self, seconds):
        self.n += int(seconds * 1_000_000_000)


def race(db, barrier, queue, number):
    store = Store(db)
    barrier.wait(8)
    token = store.acquire(f'owner-{number}', f'node-{number}', 30)
    queue.put(vars(token) if token else None)


def die_in_transaction(db):
    con = sqlite3.connect(db, isolation_level=None)
    con.execute('BEGIN IMMEDIATE')
    con.execute("UPDATE lease SET owner='uncommitted-ghost',epoch=999 WHERE singleton=1")
    con.execute("INSERT INTO answers VALUES ('q1','ghost-answer','WRONG',0)")
    os._exit(37)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.path = Path(self.temp.name) / 'shared.db'
        self.store = Store(self.path, clock=self.clock)

    def tearDown(self):
        self.temp.cleanup()

    def question(self, event='q1'):
        self.store.report(event, 'worker', 'task', 'question', 'Which format?', 'source-session')

    def test_one_speaker_and_no_preemption(self):
        a = self.store.acquire('a', 'A', 2)
        self.assertIsNotNone(a)
        self.assertIsNone(self.store.acquire('b', 'B', 2))
        self.clock.advance(1)
        self.store.renew(a, 2)
        self.clock.advance(1.5)
        self.assertIsNone(self.store.acquire('b', 'B', 2))

    def test_expired_owner_cannot_renew_publish_or_release(self):
        self.question()
        a = self.store.acquire('a', 'A', 1)
        self.clock.advance(1)
        for operation in (self.store.renew, self.store.publish_next, self.store.release):
            with self.assertRaises(Fenced):
                operation(a)
        b = self.store.acquire('b', 'B', 1)
        self.assertGreater(b.epoch, a.epoch)
        with self.assertRaises(Fenced):
            self.store.publish_next(a)
        self.assertEqual(self.store.publish_next(b)['speaker'], 'B')

    def test_same_process_reacquisition_has_new_generation(self):
        old = self.store.acquire('a', 'A', 1)
        self.clock.advance(2)
        new = self.store.acquire('a', 'A', 1)
        self.assertGreater(new.epoch, old.epoch)
        with self.assertRaises(Fenced):
            self.store.release(old)
        self.assertTrue(self.store.snapshot()['lease']['active'])

    def test_reboot_expires_lease_but_keeps_memory(self):
        old = self.store.acquire('a', 'A', 10)
        self.question()
        self.store.answer('q1', 'a1', 'Markdown')
        self.clock.boot_id = 'boot-two'
        self.clock.n = 0
        reopened = Store(self.path, clock=self.clock)
        new = reopened.acquire('b', 'B', 10)
        self.assertGreater(new.epoch, old.epoch)
        with self.assertRaises(Fenced):
            reopened.renew(old)
        self.assertEqual(reopened.snapshot()['answers'][0]['text'], 'Markdown')

    def test_retries_and_key_conflicts(self):
        self.question()
        self.assertTrue(self.store.report('q1', 'worker', 'task', 'question', 'Which format?', 'source-session')['duplicate'])
        with self.assertRaises(Conflict):
            self.store.report('q1', 'worker', 'task', 'question', 'Different question')
        self.store.answer('q1', 'a1', 'Markdown')
        self.assertTrue(self.store.answer('q1', 'a1', 'Markdown')['duplicate'])
        with self.assertRaises(Conflict):
            self.store.answer('q1', 'a2', 'PDF')
        self.assertEqual(self.store.snapshot()['event_count'], 1)

    def test_answer_bound_to_question_and_task(self):
        self.question()
        self.store.report('u1', 'worker', 'task', 'update', 'Finished')
        for invalid in ('unknown', 'u1'):
            with self.assertRaises(CrownError):
                self.store.answer(invalid, 'a1', 'Yes')
        self.store.answer('q1', 'a1', 'Markdown')
        answer = self.store.snapshot()['answers'][0]
        self.assertEqual((answer['source'], answer['task_id'], answer['session_id']), ('worker', 'task', 'source-session'))

    def test_answered_question_is_not_reasked(self):
        self.question()
        self.store.answer('q1', 'a1', 'Markdown')
        token = self.store.acquire('a', 'A', 3)
        self.assertIsNone(self.store.publish_next(token))
        self.assertEqual(self.store.snapshot()['pending'], [])

    def test_publication_carries_committed_answer_and_is_unique(self):
        self.question()
        token = self.store.acquire('a', 'A', 3)
        self.store.publish_next(token)
        self.store.answer('q1', 'a1', 'Markdown')
        self.question('q2')
        message = self.store.publish_next(token)
        self.assertEqual(message['known_answers'][0]['text'], 'Markdown')
        self.assertIsNone(self.store.publish_next(token))
        self.assertEqual(self.store.snapshot()['publication_count'], 2)

    def test_expiry_during_payload_build_cannot_publish(self):
        self.question()
        token = self.store.acquire('a', 'A', 1)
        original = self.store._context
        def slow_context(con):
            self.clock.advance(2)
            return original(con)
        self.store._context = slow_context
        with self.assertRaises(Fenced):
            self.store.publish_next(token)
        self.assertEqual(self.store.snapshot()['publication_count'], 0)

    def test_transaction_crash_does_not_corrupt_lease_or_answers(self):
        token = self.store.acquire('a', 'A', 3)
        self.question()
        ctx = mp.get_context('spawn')
        process = ctx.Process(target=die_in_transaction, args=(str(self.path),))
        process.start()
        process.join(8)
        self.assertEqual(process.exitcode, 37)
        reopened = Store(self.path, clock=self.clock)
        self.assertEqual(reopened.snapshot()['answers'], [])
        self.assertEqual(reopened.publish_next(token)['speaker'], 'A')

    def test_different_host_is_rejected(self):
        other = Clock()
        other.host_id = 'other-host'
        with self.assertRaises(CrownError):
            Store(self.path, clock=other)

    def test_invalid_durations_are_rejected(self):
        for value in (0, -1, float('nan'), float('inf'), 61):
            with self.assertRaises(CrownError):
                self.store.acquire('a', 'A', value)

    def test_lease_remaining_seconds_tracks_expiry(self):
        token = self.store.acquire('a', 'A', 2)
        self.assertAlmostEqual(self.store.lease_remaining_seconds(token), 2)
        self.clock.advance(1)
        self.assertAlmostEqual(self.store.lease_remaining_seconds(token), 1)
        self.clock.advance(1)
        self.assertEqual(self.store.lease_remaining_seconds(token), 0.0)

    def test_has_unpublished_is_a_read(self):
        self.assertFalse(self.store.has_unpublished())
        self.question()
        self.assertTrue(self.store.has_unpublished())
        token = self.store.acquire('a', 'A', 3)
        self.store.publish_next(token)
        self.assertFalse(self.store.has_unpublished())

    def test_reopen_and_foreign_acquire_do_not_need_the_write_lock(self):
        self.store.acquire('a', 'A', 30)
        holder = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute('BEGIN IMMEDIATE')
        other = Store(self.path, clock=self.clock, busy_ms=200)
        self.assertIsNone(other.acquire('b', 'B', 30))
        self.assertTrue(other.snapshot()['lease']['active'])
        self.assertFalse(other.has_unpublished())
        holder.execute('ROLLBACK')


class MultiprocessTest(unittest.TestCase):
    def test_twelve_simultaneous_candidates_have_one_winner(self):
        with tempfile.TemporaryDirectory() as folder:
            db = str(Path(folder) / 'race.db')
            store = Store(db)
            ctx = mp.get_context('spawn')
            barrier = ctx.Barrier(12)
            queue = ctx.Queue()
            processes = [ctx.Process(target=race, args=(db, barrier, queue, n)) for n in range(12)]
            try:
                for process in processes:
                    process.start()
                winners = [queue.get(timeout=12) for _ in processes]
                self.assertEqual(sum(x is not None for x in winners), 1)
                self.assertEqual(store.snapshot()['lease']['epoch'], 1)
                for process in processes:
                    process.join(5)
                    self.assertEqual(process.exitcode, 0)
            finally:
                for process in processes:
                    if process.is_alive():
                        process.kill()
                        process.join(3)
                queue.close()
                queue.join_thread()


if __name__ == '__main__':
    unittest.main()
