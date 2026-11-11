"""Optional real-SDK integration proof. Requires a Hermes source checkout.

Runs three isolated real PluginManagers, commands, tool registrations and hooks.
No mocked context and no model calls. Not a full gateway/LLM integration test.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def child(upstream):
    sys.path.insert(0, str(upstream))
    from hermes_cli.plugins import get_plugin_manager, get_plugin_command_handler
    from tools.registry import registry
    manager = get_plugin_manager()
    manager.discover_and_load()
    command = get_plugin_command_handler('crown')
    assert command is not None

    def output(data):
        print('CROWN_PROBE:' + json.dumps(data), flush=True)
    output({'ready': True})
    try:
        for line in sys.stdin:
            request = json.loads(line)
            op = request['op']
            if op == 'command':
                output({'result': command(request['text'], platform='local', session_id='host-conversation')})
            elif op == 'tool':
                entry = registry.get_entry(request['name'], scope=manager.scope_key)
                output({'result': json.loads(entry.handler(request.get('args', {}), conversation_id='host-conversation'))})
            elif op == 'recall':
                output({'result': manager.invoke_hook('pre_llm_call', user_message='Continue', session_id='host-conversation')})
            elif op == 'unload':
                manager.unload()
                output({'removed': registry.get_entry('crown_report', scope=manager.scope_key) is None})
                return
    finally:
        manager.unload()


def receive(process, timeout=12):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if select.select([process.stdout], [], [], max(0, end - time.monotonic()))[0]:
            line = process.stdout.readline()
            if not line:
                raise AssertionError('Hermes probe exited: ' + process.stderr.read()[-3000:])
            if line.startswith('CROWN_PROBE:'):
                return json.loads(line.removeprefix('CROWN_PROBE:'))
    raise AssertionError('Hermes probe response timed out')


def rpc(process, **request):
    process.stdin.write(json.dumps(request) + '\n')
    process.stdin.flush()
    return receive(process)


def parent(upstream, evidence):
    sys.path.insert(0, str(ROOT))
    from crown.store import Store
    from crown.demo import wait_for
    children = {}
    paused = set()
    with tempfile.TemporaryDirectory(prefix='crown-hermes-hosts-') as folder:
        base = Path(folder)
        db = base / 'shared.db'
        store = Store(db)
        try:
            for name in ('A', 'B', 'C'):
                home = base / name
                (home / 'empty-bundled').mkdir(parents=True)
                shutil.copytree(ROOT, home / 'plugins' / 'crown',
                                ignore=shutil.ignore_patterns('__pycache__', 'proof', '.git'))
                (home / 'config.yaml').write_text('plugins:\n  enabled: [crown]\n  entries:\n    crown:\n      settings:\n        database: '
                                                 + str(db) + '\n        instance_name: ' + name + '\n')
                env = dict(os.environ, HERMES_HOME=str(home), HERMES_BUNDLED_PLUGINS=str(home / 'empty-bundled'))
                process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--child', '--upstream', str(upstream)],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           text=True, env=env, cwd=upstream)
                children[name] = process
                assert receive(process)['ready']
                assert 'Joined Crown' in rpc(process, op='command', text='here')['result']
                if name == 'A':
                    wait_for(lambda: store.snapshot()['lease'])

            def report(process, event_id, text):
                reply = rpc(process, op='tool', name='crown_report', args={
                    'event_id': event_id, 'task_id': 'real-sdk-task', 'kind': 'question', 'text': text})
                assert reply['result']['ok'], reply

            report(children['B'], 'sdk-q1', 'PDF or Markdown?')
            wait_for(lambda: store.snapshot()['publication_count'] == 1)
            rpc(children['A'], op='command', text='answer sdk-q1 Markdown')
            children['A'].kill()
            children['A'].wait(timeout=4)
            started = time.monotonic()
            report(children['C'], 'sdk-q2', 'Who is the report for?')
            state = wait_for(lambda: (s if (s := store.snapshot())['publication_count'] == 2 else None))
            elapsed = time.monotonic() - started
            successor = state['lease']['label']
            result = rpc(children[successor], op='tool', name='crown_context', args={'task_id': 'real-sdk-task'})['result']
            assert result['answers'][0]['text'] == 'Markdown'
            assert result['answers'][0]['source'] == 'B'
            assert result['answers'][0]['session_id'] == 'host-conversation'
            assert [x['event_id'] for x in result['pending']] == ['sdk-q2']
            recall = rpc(children[successor], op='recall')['result']
            assert any('Markdown' in x.get('context', '') for x in recall)

            previous_epoch = state['lease']['epoch']
            old = children[successor]
            os.kill(old.pid, signal.SIGSTOP)
            paused.add(old.pid)
            replacement = wait_for(lambda: (s['lease'] if (s := store.snapshot())['lease']['epoch'] > previous_epoch else None))
            os.kill(old.pid, signal.SIGCONT)
            paused.remove(old.pid)
            wait_for(lambda: json.loads(rpc(old, op='command', text='status')['result'])['stale_attempts_blocked'] > 0)
            for name, process in children.items():
                if process.poll() is None:
                    assert rpc(process, op='unload')['removed']
                    process.wait(timeout=5)
                    assert process.returncode == 0
            final = store.snapshot()
            assert final['publication_count'] == 2
            assert not final['lease']['active']
            result = {'passed': True, 'scope': 'Three real Hermes PluginManager processes; native registration, tools, hooks, commands and unload; no LLM/gateway sessions',
                      'upstream_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=upstream, text=True).strip(),
                      'python': sys.version.split()[0], 'lease_seconds': 3, 'takeover_seconds': elapsed,
                      'first_successor': successor, 'next_successor': replacement['label'],
                      'stale_owner_blocked': True, 'unload_removed_tools': True, 'final_state': final}
            Path(evidence).write_text(json.dumps(result, indent=2))
            print(json.dumps({k: v for k, v in result.items() if k != 'final_state'}, indent=2))
        finally:
            for process in children.values():
                if process.pid in paused and process.poll() is None:
                    os.kill(process.pid, signal.SIGCONT)
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for handle in (process.stdin, process.stdout, process.stderr):
                    handle.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--upstream', type=Path, required=True)
    parser.add_argument('--evidence', default=str(ROOT / 'proof' / 'hermes-hosts.json'))
    parser.add_argument('--child', action='store_true')
    args = parser.parse_args()
    if args.child:
        child(args.upstream.resolve())
    else:
        parent(args.upstream.resolve(), args.evidence)
