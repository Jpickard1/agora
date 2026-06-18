"""Tests for the cross-channel activity digest (#79)."""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub.store import HubStore
from agenthub.cli import _parse_duration


def _store():
    s = HubStore(tempfile.mkdtemp(prefix="hub-digest-"))
    s.init(token="t")
    return s


def test_per_channel_counts_and_mentions():
    s = _store()
    # mention counts are per-message (a message is deduped), so worker1 in TWO
    # messages -> 2, probe in one -> 1
    s.post_channel("general", "hi @worker1 @worker1", author="a", author_name="a")
    s.post_channel("general", "@worker1 and @probe look", author="b", author_name="b")
    s.ensure_channel("data")
    s.post_channel("data", "no mentions here", author="c", author_name="c")
    d = s.activity_digest(since_ts=0.0)
    by = {c["channel"]: c for c in d["channels"]}
    assert by["general"]["messages"] == 2
    assert by["data"]["messages"] == 1
    top = dict(by["general"]["top_mentions"])
    assert top["worker1"] == 2 and top["probe"] == 1
    assert d["totals"]["messages"] == 3


def test_since_window_excludes_old():
    s = _store()
    s.post_channel("general", "old", author="a", author_name="a")
    future = time.time() + 1000
    d = s.activity_digest(since_ts=future)   # window starts in the future
    assert d["totals"]["messages"] == 0
    assert all(c["messages"] == 0 for c in d["channels"])


def test_task_changes_included():
    s = _store()
    s.create_task("agora-79", title="digest")
    s.claim_task("agora-79", "worker1")
    s.update_task("agora-79", "running", by="worker1")
    d = s.activity_digest(since_ts=0.0)
    statuses = [(c["task"], c["status"]) for c in d["task_changes"]]
    assert ("agora-79", "claimed") in statuses
    assert ("agora-79", "running") in statuses
    assert d["totals"]["task_changes"] >= 2


def test_broadcasts_counted():
    s = _store()
    s.post_broadcast("all hands", author="mgr", author_name="mgr")
    d = s.activity_digest(since_ts=0.0)
    assert d["broadcasts"] == 1


def test_parse_duration():
    assert _parse_duration("24h") == 86400
    assert _parse_duration("30m") == 1800
    assert _parse_duration("7d") == 604800
    assert _parse_duration("2w") == 1209600
    assert _parse_duration("3600") == 3600     # bare = seconds
    assert _parse_duration("all") == 0.0       # non-numeric -> 0 (all time)
    assert _parse_duration("") == 0.0


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
