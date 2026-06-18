"""Tests for task release / reassign / stale-claim recovery (issue #8).
Repo style: plain asserts + a __main__ runner. Run: python tests/test_task_release.py"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub.store import HubStore  # noqa: E402


def fresh():
    s = HubStore(tempfile.mkdtemp(prefix="taskrel-"))
    s.init(token="t")
    return s


def _task(s, tid="t1"):
    s.create_task(tid, title="demo", capability="general")
    return tid


def test_release_by_owner_reopens_and_is_reclaimable():
    s = fresh(); t = _task(s)
    assert s.claim_task(t, "alice") is True
    assert s.get_task(t)["claimed_by"] == "alice"
    assert s.release_task(t, by="alice") is True
    rec = s.get_task(t)
    assert rec["status"] == "open" and rec["claimed_by"] is None
    # the whole point: someone else can now take it
    assert s.claim_task(t, "bob") is True
    assert s.get_task(t)["claimed_by"] == "bob"


def test_release_by_non_owner_denied():
    s = fresh(); t = _task(s)
    s.claim_task(t, "alice")
    assert s.release_task(t, by="bob") is False        # not the owner
    assert s.get_task(t)["claimed_by"] == "alice"      # claim left intact


def test_release_force_overrides_owner():
    s = fresh(); t = _task(s)
    s.claim_task(t, "alice")
    assert s.release_task(t, by="manager", force=True) is True
    assert s.get_task(t)["claimed_by"] is None


def test_release_unclaimed_noop_and_unknown_fails():
    s = fresh(); t = _task(s)
    assert s.release_task(t, by="anyone") is True       # already open
    assert s.release_task("nope", by="x") is False      # unknown task


def test_reassign_moves_claim_to_new_agent():
    s = fresh(); t = _task(s)
    s.claim_task(t, "alice")
    rec = s.reassign_task(t, "bob", by="manager")
    assert rec is not None
    assert rec["claimed_by"] == "bob" and rec["status"] == "claimed"
    assert s.reassign_task("nope", "bob", by="manager") is None


def test_stale_flags_offline_owner_only():
    s = fresh(); t = _task(s)
    s.register_agent("alice", "alice")
    s.claim_task(t, "alice")
    assert s.stale_tasks(offline_window=30) == []        # owner online → not stale
    s.set_agent_status("alice", "offline")
    stale = s.stale_tasks(offline_window=30)
    assert len(stale) == 1 and stale[0]["id"] == t       # owner offline → stale


def test_stale_ignores_unclaimed_and_terminal():
    s = fresh(); t = _task(s)
    assert s.stale_tasks() == []                         # unclaimed → not stale
    s.register_agent("alice", "alice")
    s.claim_task(t, "alice")
    s.update_task(t, "done", by="alice")                 # terminal
    s.set_agent_status("alice", "offline")
    assert s.stale_tasks(offline_window=30) == []        # terminal → never stale


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1; print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
