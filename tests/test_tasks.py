"""Tests for the durable task store (B1) and atomic claim (B2)."""

import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub.store import HubStore


def _store():
    d = tempfile.mkdtemp(prefix="hub-tasks-")
    s = HubStore(d)
    s.init()
    return s


def test_create_task_is_open_then_listed():
    s = _store()
    t = s.create_task("MGB-main#42", title="fix bug", ref="Jpickard1/MGB-main#42",
                      capability="data", created_by="manager")
    assert t["status"] == "open"
    assert t["capability"] == "data"
    assert t["claimed_by"] is None
    listed = s.list_tasks()
    assert [x["id"] for x in listed] == [t["id"]]
    assert s.list_tasks(status="open") and not s.list_tasks(status="done")


def test_create_task_is_idempotent():
    s = _store()
    s.create_task("t1", title="first")
    # Re-dispatch must not clobber the original definition.
    again = s.create_task("t1", title="SECOND")
    assert again["title"] == "first"


def test_claim_is_exclusive_sequential():
    s = _store()
    s.create_task("t1")
    assert s.claim_task("t1", "worker1") is True       # first wins
    assert s.claim_task("t1", "worker2") is False      # already claimed
    t = s.get_task("t1")
    assert t["status"] == "claimed"
    assert t["claimed_by"] == "worker1"


def test_claim_unknown_task_fails():
    s = _store()
    assert s.claim_task("does-not-exist", "worker1") is False


def test_claim_is_race_proof_under_threads():
    s = _store()
    s.create_task("hot")
    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def race(name):
        barrier.wait()  # maximise contention
        won = s.claim_task("hot", name)
        with lock:
            results.append(won)

    threads = [threading.Thread(target=race, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Exactly ONE thread may win the claim.
    assert results.count(True) == 1
    assert results.count(False) == 7


def test_lifecycle_events_and_status():
    s = _store()
    s.create_task("t1")
    s.claim_task("t1", "worker1")
    s.update_task("t1", "running", by="worker1", note="started")
    t = s.update_task("t1", "done", by="worker1", note="closed issue")
    assert t["status"] == "done"
    # full history preserved, in order
    statuses = [e["status"] for e in t["events"]]
    assert statuses == ["claimed", "running", "done"]
    assert s.list_tasks(status="done")[0]["id"] == "t1"


def test_update_unknown_task_returns_none():
    s = _store()
    assert s.update_task("nope", "done") is None


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
        passed += 1
    print(f"\n{passed}/{len(tests)} passed")
    return passed == len(tests)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
