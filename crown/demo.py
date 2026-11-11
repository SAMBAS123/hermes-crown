"""Kill and resume, then pause and revive the OLD speaker. No mocked processes."""
from __future__ import annotations

import json
import multiprocessing as mp
import os
from pathlib import Path
import signal
import tempfile
import time

from .store import Store
from .worker import process_node


def wait_for(fn, *, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = fn()
        if result:
            return result
        time.sleep(0.015)
    raise AssertionError('timed out waiting for expected state')


def run_round(number=1, *, slow=False, emit=print):
    ttl = 0.45
    ctx = mp.get_context('spawn')
    processes = []
    pipes = []
    owners = {}
    paused = set()
    steps = []

    def step(message):
        steps.append(message)
        emit(message)
        if slow:
            time.sleep(0.8)

    with tempfile.TemporaryDirectory(prefix='crown-proof-') as folder:
        store = Store(Path(folder) / 'record.db')
        try:
            for label in ('A', 'B', 'C'):
                parent, child = ctx.Pipe()
                proc = ctx.Process(target=process_node, args=(str(store.path), label, ttl, child))
                proc.start()
                child.close()
                assert parent.poll(8), 'worker did not start'
                owner = parent.recv()['owner']
                processes.append(proc)
                pipes.append(parent)
                owners[owner] = (proc, parent)
                if label == 'A':
                    wait_for(lambda: store.snapshot()['lease'])
            state = store.snapshot()
            assert state['lease']['label'] == 'A'
            step('Three real processes joined. A holds the speaking lease.')

            store.report('q-format', 'research', 'report-42', 'question', 'PDF or Markdown?', 'session-research')
            wait_for(lambda: store.snapshot()['publication_count'] == 1)
            store.answer('q-format', 'answer-format', 'Markdown, please.')
            step('User answered: Markdown, please. The answer is committed to disk.')

            first = store.snapshot()['lease']
            killed, _ = owners[first['owner']]
            killed.kill()
            killed.join(3)
            started = time.monotonic()
            # Queue a question after the process died but before lease expiry.
            store.report('q-audience', 'writer', 'report-42', 'question', 'Who is the report for?', 'session-writer')
            next_state = wait_for(lambda: (s if (s := store.snapshot())['publication_count'] == 2 else None))
            takeover_seconds = time.monotonic() - started
            successor = next_state['lease']
            message = next_state['outlet'][-1]
            assert successor['owner'] != first['owner']
            assert successor['epoch'] > first['epoch']
            assert message['event_id'] == 'q-audience'
            assert message['known_answers'][0]['text'] == 'Markdown, please.'
            assert [x['event_id'] for x in next_state['pending']] == ['q-audience']
            step(f"Killed A. {successor['label']} continued in {takeover_seconds:.3f}s: knows Markdown; asks only the pending question.")

            # Harder than a restart: the old process retains its exact token
            # in RAM while a different process wins a later epoch.
            old_proc, old_pipe = owners[successor['owner']]
            os.kill(old_proc.pid, signal.SIGSTOP)
            paused.add(old_proc.pid)
            old_pipe.send('stale-publish')
            newer = wait_for(lambda: (s['lease'] if (s := store.snapshot())['lease']['epoch'] > successor['epoch'] else None))
            # A new event is present, so fencing is not an empty-queue illusion.
            store.report('done', 'builder', 'report-42', 'update', 'Draft is ready.')
            os.kill(old_proc.pid, signal.SIGCONT)
            paused.remove(old_proc.pid)
            assert old_pipe.poll(5), 'resumed worker did not respond'
            assert old_pipe.recv()['fenced'], 'old speaker was allowed to publish'
            wait_for(lambda: store.snapshot()['publication_count'] == 3)
            step(f"Paused {successor['label']}; {newer['label']} took over. Revived {successor['label']}: its old token was REJECTED.")

            final = store.snapshot()
            ids = [x['event_id'] for x in final['outlet']]
            assert ids == ['q-format', 'q-audience', 'done']
            assert len(ids) == len(set(ids))
            assert final['answers'][0]['text'] == 'Markdown, please.'
            assert [x['event_id'] for x in final['pending']] == ['q-audience']
            # Check the actual publication order against the authoritative
            # acquire/release log, not only against process-side beliefs.
            active = None
            for event in final['audit']:
                if event['action'] == 'acquire':
                    active = (event['owner'], event['epoch'])
                elif event['action'] == 'release':
                    active = None
                else:
                    assert (event['owner'], event['epoch']) == active
            step('PASS: three unique publications; saved answer intact; pending question intact; stale speaker fenced.')
            return {'round': number, 'passed': True, 'takeover_seconds': takeover_seconds,
                    'process_ids': [p.pid for p in processes], 'steps': steps, 'state': final,
                    'scope': 'Three real Crown processes; not three live Hermes model sessions.'}
        finally:
            for proc in processes:
                if proc.pid in paused and proc.is_alive():
                    os.kill(proc.pid, signal.SIGCONT)
            for pipe, proc in zip(pipes, processes):
                if proc.is_alive():
                    try:
                        pipe.send('stop')
                    except (BrokenPipeError, EOFError):
                        pass
            for proc in processes:
                proc.join(2)
                if proc.is_alive():
                    proc.kill()
                    proc.join(2)
            for pipe in pipes:
                pipe.close()


def demo(rounds=1, evidence=None, slow=False):
    if not 1 <= rounds <= 100:
        raise ValueError('rounds must be between 1 and 100')
    results = []
    for number in range(1, rounds + 1):
        print(f'\nCROWN FAILURE PROOF — ROUND {number}/{rounds}', flush=True)
        results.append(run_round(number, slow=slow, emit=lambda text: print(text, flush=True)))
    if evidence:
        target = Path(evidence)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({'scope': 'Coordination core, synthetic task content, real OS process failures',
            'rounds': results}, indent=2), encoding='utf-8')
    print(f'\n{rounds}/{rounds} rounds passed.', flush=True)
    return 0
