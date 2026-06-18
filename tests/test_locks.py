"""Tests for advisory locks (issue #10): acquire/refresh/contention, owner-only
release (+ force), and auto-expiry when the owner goes offline.
Run: python tests/test_locks.py"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub.store import HubStore  # noqa: E402


def fresh():
    s = HubStore(tempfile.mkdtemp(prefix="locks-"))
    s.init(token="t")
    return s


def _online(s, aid):
    s.register_agent(aid, aid)
    s.heartbeat(aid)


def _make_offline(s, aid):
    p = os.path.join(str(s.agents_dir), f"{aid}.json")
    rec = json.load(open(p))
    rec["last_seen"] = time.time() - 9999
    json.dump(rec, open(p, "w"))


def test_acquire_then_held_against_others():
    s = fresh()
    _online(s, "alice"); _online(s, "bob")
    assert s.acquire_lock("f.py", "alice")["ok"] is True
    r = s.acquire_lock("f.py", "bob")
    assert r["ok"] is False and r["reason"] == "held"
    assert r["lock"]["owner"] == "alice"


def test_reacquire_own_lock_refreshes():
    s = fresh()
    _online(s, "alice")
    s.acquire_lock("f.py", "alice", note="v1")
    r = s.acquire_lock("f.py", "alice", note="v2")
    assert r["ok"] is True and r["reason"] == "refreshed"
    assert s.get_lock("f.py")["note"] == "v2"


def test_distinct_resources_independent():
    s = fresh()
    _online(s, "alice"); _online(s, "bob")
    assert s.acquire_lock("a.py", "alice")["ok"]
    assert s.acquire_lock("b.py", "bob")["ok"]
    assert len(s.list_locks()) == 2


def test_release_owner_only():
    s = fresh()
    _online(s, "alice"); _online(s, "bob")
    s.acquire_lock("f.py", "alice")
    assert s.release_lock("f.py", "bob") is False        # not the owner
    assert s.release_lock("f.py", "alice") is True
    assert s.list_locks() == []


def test_release_force_overrides_owner():
    s = fresh()
    _online(s, "alice")
    s.acquire_lock("f.py", "alice")
    assert s.release_lock("f.py", "manager", force=True) is True


def test_release_missing_is_false():
    s = fresh()
    assert s.release_lock("nope.py", "alice") is False


def test_expires_when_owner_offline():
    s = fresh()
    _online(s, "alice")
    s.acquire_lock("f.py", "alice")
    assert s.get_lock("f.py")["expired"] is False
    _make_offline(s, "alice")
    assert s.get_lock("f.py")["expired"] is True


def test_offline_owner_lock_is_taken_over():
    s = fresh()
    _online(s, "alice"); _online(s, "bob")
    s.acquire_lock("f.py", "alice")
    _make_offline(s, "alice")
    r = s.acquire_lock("f.py", "bob")
    assert r["ok"] is True and r["reason"] == "expired-takeover"
    assert r["lock"]["stole_from"] == "alice"
    assert s.get_lock("f.py")["owner"] == "bob"


def test_unknown_owner_never_expires():
    s = fresh()
    # 'jpic' has no agent record (a human) -> not auto-expired
    s.acquire_lock("notes.md", "jpic")
    assert s.get_lock("notes.md")["expired"] is False
    # and another agent can't steal it
    _online(s, "bob")
    assert s.acquire_lock("notes.md", "bob")["ok"] is False


def test_list_locks_annotates_age_and_expired():
    s = fresh()
    _online(s, "alice")
    s.acquire_lock("f.py", "alice", note="hi")
    lk = s.list_locks()[0]
    assert set(["resource", "owner", "expired", "age", "note"]) <= set(lk)
    assert lk["expired"] is False


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
