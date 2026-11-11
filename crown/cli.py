from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sqlite3
import sys
import time
import uuid

from .store import CrownError, Store
from .worker import Participant


def configure(parser):
    parser.add_argument('--db', default=os.environ.get('CROWN_DB'),
                        help='Explicit shared local SQLite path; never a network filesystem')
    sub = parser.add_subparsers(dest='crown_command', required=True)
    run = sub.add_parser('run', help='Enroll a process as speaker/standby until Ctrl-C')
    run.add_argument('--name', required=True)
    run.add_argument('--ttl', type=float, default=3)
    report = sub.add_parser('report', help='Queue an attributed update or question')
    report.add_argument('--id', required=True)
    report.add_argument('--source', required=True)
    report.add_argument('--task', required=True)
    report.add_argument('--session', default='')
    report.add_argument('--kind', choices=['question', 'update'], default='update')
    report.add_argument('text')
    answer = sub.add_parser('answer', help='Record a human answer against a question ID')
    answer.add_argument('question_id')
    answer.add_argument('text')
    answer.add_argument('--id', default=None, help='Stable answer ID for retry-safe recording')
    sub.add_parser('status', help='Show the current speaker and pending questions')
    sub.add_parser('context', help='Export complete shared answers, questions, outlet and audit')
    watch = sub.add_parser('watch', help='Read the single durable outlet')
    watch.add_argument('--after', type=int, default=0)
    watch.add_argument('--once', action='store_true')
    demo = sub.add_parser('demo', help='Real multiprocess failure test; no model calls')
    demo.add_argument('--rounds', type=int, default=1)
    demo.add_argument('--evidence', default=None)
    demo.add_argument('--slow', action='store_true', help='Pause briefly between demo steps')
    return parser


def handle(args):
    if args.crown_command == 'demo':
        from .demo import demo
        return demo(rounds=args.rounds, evidence=args.evidence, slow=args.slow)
    if not args.db or not Path(args.db).expanduser().is_absolute():
        raise CrownError('Set --db or CROWN_DB to one shared absolute local path')
    store = Store(args.db)
    if args.crown_command == 'run':
        participant = Participant(store, args.name, args.ttl)
        print(f'{args.name} enrolled. Read publications with crown watch; Ctrl-C leaves.', file=sys.stderr)
        try:
            participant.run()
        except KeyboardInterrupt:
            pass
        finally:
            participant.stop()
        return 0
    if args.crown_command == 'report':
        result = store.report(args.id, args.source, args.task, args.kind, args.text, args.session)
    elif args.crown_command == 'answer':
        result = store.answer(args.question_id, args.id or uuid.uuid4().hex, args.text)
    elif args.crown_command == 'context':
        result = store.snapshot()
    elif args.crown_command == 'status':
        state = store.snapshot()
        result = {key: state[key] for key in ('lease', 'pending', 'answers', 'publication_count')}
    elif args.crown_command == 'watch':
        cursor = args.after
        try:
            while True:
                for event in store.snapshot(after=cursor)['outlet']:
                    print(json.dumps(event, ensure_ascii=False), flush=True)
                    cursor = event['seq']
                if args.once:
                    break
                time.sleep(0.15)
        except KeyboardInterrupt:
            pass
        return 0
    else:
        raise CrownError('unknown command')
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def main(argv=None):
    parser = configure(argparse.ArgumentParser(prog='crown'))
    try:
        return handle(parser.parse_args(argv))
    except (CrownError, OSError, sqlite3.Error) as exc:
        print(f'Crown: {exc}', file=sys.stderr)
        return 1

