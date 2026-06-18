"""Tests for threaded replies (#64): reply_to on messages + thread grouping."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub.store import HubStore


def _store():
    s = HubStore(tempfile.mkdtemp(prefix="hub-thread-"))
    s.init(token="t")
    return s


def test_reply_to_is_persisted():
    s = _store()
    parent = s.post_channel("general", "deploy plan?", author="a", author_name="a")
    reply = s.post_channel("general", "ship at 5pm", author="b", author_name="b",
                           reply_to=parent.id)
    msgs = s.read_channel("general")
    by_id = {m["id"]: m for m in msgs}
    assert by_id[reply.id]["reply_to"] == parent.id


def test_non_reply_is_backcompat_none():
    s = _store()
    m = s.post_channel("general", "hello", author="a", author_name="a")
    assert s.read_channel("general")[-1]["reply_to"] is None
    # and an old-style record without the key still reads fine
    assert m.to_dict()["reply_to"] is None


def test_read_thread_groups_parent_and_replies():
    s = _store()
    p = s.post_channel("general", "topic", author="a", author_name="a")
    r1 = s.post_channel("general", "r1", author="b", author_name="b", reply_to=p.id)
    s.post_channel("general", "unrelated", author="c", author_name="c")
    r2 = s.post_channel("general", "r2", author="d", author_name="d", reply_to=p.id)
    th = s.read_thread("general", p.id)
    assert th["parent"]["id"] == p.id
    assert [m["id"] for m in th["replies"]] == [r1.id, r2.id]   # chronological


def test_read_thread_unknown_parent():
    s = _store()
    s.post_channel("general", "x", author="a", author_name="a")
    th = s.read_thread("general", "nope")
    assert th["parent"] is None and th["replies"] == []


def test_inbox_reply_to():
    s = _store()
    p = s.post_inbox("worker1", "task?", author="mgr", author_name="mgr")
    r = s.post_inbox("worker1", "on it", author="worker1", author_name="worker1",
                     reply_to=p.id)
    got = {m["id"]: m for m in s.read_inbox("worker1")}
    assert got[r.id]["reply_to"] == p.id


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
