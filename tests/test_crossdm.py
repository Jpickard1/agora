"""Tests for cross-user DMs (issue #88): addressing via participants + the
group-writable shared DM area + recipient drain. Stub shared_root, mocked fs."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agenthub import crossdm as C, participants as P

def sr(): return tempfile.mkdtemp(prefix="cdm-")

def test_explicit_user_addressing():
    s = sr()
    r = C.post_cross_user_dm(s, "bob:bob-agent", "alice", "alice-agent", "hi", now=1.0)
    assert r["ok"] and r["msg"]["to_user"] == "bob"
    msgs, _, _ = C.drain_shared_dms(s, "bob")
    assert [m["text"] for m in msgs] == ["hi"]

def test_addressing_via_participants():
    s = sr()
    P.register_participant(s, "bob", "bob-agent", now=1.0)
    r = C.post_cross_user_dm(s, "bob-agent", "alice", "a", "yo", now=2.0)  # bare agent
    assert r["ok"] and r["msg"]["to_user"] == "bob"

def test_unknown_recipient_fails_cleanly():
    s = sr()
    r = C.post_cross_user_dm(s, "ghost-agent", "alice", "a", "?", now=1.0)
    assert r["ok"] is False and "unknown recipient" in r["reason"]

def test_drain_only_my_user_and_dedup():
    s = sr()
    C.post_cross_user_dm(s, "bob:b", "alice", "a", "m1", now=1.0)
    C.post_cross_user_dm(s, "carol:c", "alice", "a", "for carol", now=2.0)
    msgs, cur, seen = C.drain_shared_dms(s, "bob")
    assert [m["text"] for m in msgs] == ["m1"]
    again, _, _ = C.drain_shared_dms(s, "bob", since_ts=cur, seen=seen)
    assert again == []

def test_drain_chronological_and_identity():
    s = sr()
    C.post_cross_user_dm(s, "bob:b", "alice", "alice-agent", "first", now=1.0)
    C.post_cross_user_dm(s, "bob:b", "dave", "dave-agent", "second", now=2.0)
    msgs, _, _ = C.drain_shared_dms(s, "bob")
    assert [m["text"] for m in msgs] == ["first", "second"]
    assert msgs[0]["from_user"] == "alice"

def test_private_roots_untouched():
    # cross-user DM only writes under <shared_root>/dm/ — nothing else
    s = sr()
    C.post_cross_user_dm(s, "bob:b", "alice", "a", "x", now=1.0)
    entries = set(os.listdir(s))
    assert entries == {"dm"}

def run():
    t=[v for k,v in sorted(globals().items()) if k.startswith("test_")]; p=0
    for f in t:
        f(); print("PASS", f.__name__); p+=1
    print(f"\n{p}/{len(t)} passed"); return p==len(t)
if __name__ == "__main__": sys.exit(0 if run() else 1)
