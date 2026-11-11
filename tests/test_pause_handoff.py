"""Idle-speaker pause must let a live standby acquire. No model calls."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
CHILD = r'''
import json, os, sys
sys.path.insert(0, os.environ["CROWN_ROOT"])
from crown.store import Store, Token, Fenced
from crown.worker import Participant
part = Participant(Store(os.environ["CROWN_DB"], busy_ms=250), os.environ["CROWN_LABEL"],
                   float(os.environ["CROWN_TTL"]))
part.start()
print("READY", flush=True)
saved = None
try:
    for line in sys.stdin:
        req = json.loads(line)
        op = req["op"]
        if op == "status":
            print("REPLY " + json.dumps({
                "token": vars(part.token) if part.token else None,
                "error": part.last_error,
                "thread": bool(part.thread and part.thread.is_alive()),
                "fenced": part.fenced_count,
            }), flush=True)
        elif op == "capture":
            part.hold_ticks()
            lease = part.store.snapshot()["lease"]
            if not (lease and lease["active"] and lease["label"] == part.label):
                raise RuntimeError("not the speaker")
            saved = Token(lease["owner"], lease["epoch"])
            print("REPLY " + json.dumps({"epoch": saved.epoch}), flush=True)
        elif op == "stale-publish":
            try:
                part.store.publish_next(saved)
                print("REPLY " + json.dumps({"fenced": False}), flush=True)
            except Fenced:
                print("REPLY " + json.dumps({"fenced": True, "epoch": saved.epoch}), flush=True)
        elif op == "stop":
            break
finally:
    part.stop()
'''


def rpc(proc, **req):
    proc.stdin.write(json.dumps(req) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line.startswith("REPLY "):
        raise RuntimeError(f"bad reply {line!r}")
    return json.loads(line[len("REPLY "):])


class PauseHandoffTest(unittest.TestCase):
    def wait_lease(self, store, pred, timeout, label):
        end = time.monotonic() + timeout
        last = None
        while time.monotonic() < end:
            last = store.snapshot()["lease"]
            if pred(last):
                return last
            time.sleep(0.03)
        raise TimeoutError(f"{label}: {last}")

    def test_paused_idle_speaker_yields_to_standby_and_old_token_is_fenced(self):
        sys.path.insert(0, str(ROOT))
        from crown.store import Store

        ttl = 0.6
        budget = ttl * 3
        folder = tempfile.TemporaryDirectory(prefix="crown-pause-")
        self.addCleanup(folder.cleanup)
        db = str(Path(folder.name) / "shared.db")
        store = Store(db)
        procs = {}
        paused = []

        def cleanup():
            for proc in paused:
                try:
                    os.kill(proc.pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
            for proc in procs.values():
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=3)
                for handle in (proc.stdin, proc.stdout, proc.stderr):
                    handle.close()
        self.addCleanup(cleanup)

        for label in ("desk", "field", "shop"):
            env = dict(os.environ, CROWN_ROOT=str(ROOT), CROWN_DB=db, CROWN_LABEL=label,
                       CROWN_TTL=str(ttl))
            proc = subprocess.Popen([sys.executable, "-u", "-c", CHILD],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, env=env,
                                    start_new_session=True)
            procs[label] = proc
            self.assertEqual(proc.stdout.readline().strip(), "READY")
            if label == "desk":
                self.wait_lease(store, lambda l: l and l["label"] == "desk", budget, "desk")

        store.report("live-q1", "field", "live-job", "question", "Ship the photos as JPEG or PNG?",
                     "field-session")
        self.wait_lease(store, lambda l: store.snapshot()["publication_count"] >= 1, budget, "pub1")
        store.answer("live-q1", "ans-jpeg", "JPEG")
        first = store.snapshot()["lease"]
        os.killpg(procs["desk"].pid, signal.SIGKILL)
        procs["desk"].wait(timeout=3)
        store.report("live-q2", "shop", "live-job", "question", "Who receives the package?",
                     "shop-session")
        successor = self.wait_lease(
            store, lambda l: l and l["active"] and l["epoch"] > first["epoch"], budget, "kill")
        old = procs[successor["label"]]
        captured = rpc(old, op="capture")
        os.kill(old.pid, signal.SIGSTOP)
        paused.append(old)
        started = time.monotonic()
        replacement = self.wait_lease(
            store,
            lambda l: l and l["active"] and l["epoch"] > captured["epoch"],
            budget,
            "pause takeover",
        )
        takeover = time.monotonic() - started
        self.assertLess(takeover, budget)
        self.assertGreater(replacement["epoch"], captured["epoch"])
        self.assertNotEqual(replacement["label"], successor["label"])
        self.assertEqual(store.snapshot()["lease"]["owner"], replacement["owner"])

        store.report("live-standby", replacement["label"], "live-job", "update",
                     "Standby holds the lease.", f"{replacement['label']}-session")
        os.kill(old.pid, signal.SIGCONT)
        paused.remove(old)
        refused = rpc(old, op="stale-publish")
        self.assertTrue(refused["fenced"])
        self.assertEqual(refused["epoch"], captured["epoch"])
        final = self.wait_lease(
            store, lambda l: store.snapshot()["publication_count"] == 3, budget, "standby publish")
        snap = store.snapshot()
        self.assertEqual(snap["publication_count"], 3)
        self.assertEqual([row["event_id"] for row in snap["outlet"]],
                         ["live-q1", "live-q2", "live-standby"])
        self.assertEqual(snap["outlet"][-1]["speaker"], replacement["label"])
        self.assertEqual(snap["answers"][0]["text"], "JPEG")
        self.assertEqual([row["event_id"] for row in snap["pending"]], ["live-q2"])
        self.assertEqual(len({row["event_id"] for row in snap["outlet"]}), 3)


if __name__ == "__main__":
    unittest.main()
