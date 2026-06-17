"""Tests for the shared-filesystem store. Run with: python -m pytest tests/ -q
(falls back to plain asserts if pytest is unavailable -- see __main__)."""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub.store import HubStore  # noqa: E402


def fresh():
    d = tempfile.mkdtemp(prefix="hubtest-")
    s = HubStore(d)
    s.init(token="testtoken")
    return s


def test_init_and_config():
    s = fresh()
    cfg = s.get_config()
    assert cfg["token"] == "testtoken"
    assert any(c["name"] == "general" for c in s.list_channels())


def test_channel_post_and_read_order():
    s = fresh()
    s.post_channel("general", "first", "a", "Alice")
    time.sleep(0.002)
    s.post_channel("general", "second", "b", "Bob")
    msgs = s.read_channel("general")
    assert [m["text"] for m in msgs] == ["first", "second"]
    assert msgs[0]["author_name"] == "Alice"


def test_since_filter():
    s = fresh()
    s.post_channel("general", "old", "a", "A")
    time.sleep(0.01)
    cut = time.time()
    time.sleep(0.01)
    s.post_channel("general", "new", "a", "A")
    recent = s.read_channel("general", since_ts=cut)
    assert [m["text"] for m in recent] == ["new"]


def test_inbox_directed():
    s = fresh()
    s.post_inbox("trainer-1", "please checkpoint", "human:jpic", "jpic", author_kind="human")
    inbox = s.read_inbox("trainer-1")
    assert len(inbox) == 1
    assert inbox[0]["to"] == "trainer-1"
    assert inbox[0]["author_kind"] == "human"
    # other agents do not see it
    assert s.read_inbox("other") == []


def test_presence():
    s = fresh()
    s.register_agent("t1", "trainer", host="gpu01", capabilities=["gpu"])
    agents = s.list_agents(online_window=30)
    assert len(agents) == 1
    assert agents[0]["online"] is True
    assert agents[0]["capabilities"] == ["gpu"]
    # mark offline
    s.set_agent_status("t1", "offline")
    assert s.list_agents()[0]["online"] is False


def test_broadcast_reaches_all():
    s = fresh()
    s.post_broadcast("all hands: pause", "human:jpic", "jpic")
    bc = s.read_broadcast()
    assert len(bc) == 1
    assert bc[0]["to"] == "*"
    assert bc[0]["text"] == "all hands: pause"


def test_broadcast_to_capability():
    s = fresh()
    s.register_agent("g1", "gpu-a", capabilities=["gpu"])
    s.register_agent("g2", "gpu-b", capabilities=["gpu", "train"])
    s.register_agent("c1", "cpu-a", capabilities=["cpu"])
    sent = s.broadcast_to_capability("gpu", "checkpoint now", "human:jpic", "jpic")
    assert {m.to for m in sent} == {"g1", "g2"}
    assert s.read_inbox("g1")[0]["text"] == "checkpoint now"
    assert s.read_inbox("c1") == []  # cpu agent not targeted


def test_reregister_preserves_capabilities():
    s = fresh()
    s.register_agent("t1", "trainer", capabilities=["gpu", "train"])
    # a bare re-register (e.g. from watch_inbox) must not wipe capabilities
    s.register_agent("t1", "trainer")
    assert s.get_agent("t1")["capabilities"] == ["gpu", "train"]
    # but an explicit new list replaces them
    s.register_agent("t1", "trainer", capabilities=["cpu"])
    assert s.get_agent("t1")["capabilities"] == ["cpu"]


def test_activity_reporting():
    s = fresh()
    s.register_agent("t1", "trainer")
    s.heartbeat("t1", activity="training epoch 3")
    rec = s.get_agent("t1")
    assert rec["activity"] == "training epoch 3"
    # heartbeat without activity preserves prior activity
    s.heartbeat("t1")
    assert s.get_agent("t1")["activity"] == "training epoch 3"


def test_firehose_merges_chronologically():
    s = fresh()
    s.post_channel("general", "g1", "a", "A")
    time.sleep(0.002)
    s.post_channel("ops", "o1", "a", "A")
    time.sleep(0.002)
    s.post_broadcast("b1", "human:jpic", "jpic")
    fh = s.firehose()
    assert [m["text"] for m in fh] == ["g1", "o1", "b1"]


def test_limit():
    s = fresh()
    for i in range(10):
        s.post_channel("general", f"m{i}", "a", "A")
    last3 = s.read_channel("general", limit=3)
    assert [m["text"] for m in last3] == ["m7", "m8", "m9"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
