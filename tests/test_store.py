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


def test_forget_agent():
    s = fresh()
    s.register_agent("t1", "trainer", capabilities=["gpu"])
    s.register_agent("t2", "tester")
    assert len(s.list_agents()) == 2
    # forget an existing agent -> True, dropped from the roster
    assert s.forget_agent("t1") is True
    ids = [a["id"] for a in s.list_agents()]
    assert ids == ["t2"]
    # forgetting again / unknown agent -> False
    assert s.forget_agent("t1") is False
    assert s.forget_agent("nope") is False


def test_comm_graph():
    s = fresh()
    # manager DMs worker1 twice, worker1 replies once; self-message ignored
    s.post_inbox("worker1", "task A", author="manager", author_name="manager")
    s.post_inbox("worker1", "task B", author="manager", author_name="manager")
    s.post_inbox("manager", "done", author="worker1", author_name="worker1")
    s.post_inbox("worker1", "self note", author="worker1", author_name="worker1")
    g = s.comm_graph()
    assert set(g["nodes"]) == {"manager", "worker1"}
    edges = {(e["source"], e["target"]): e["count"] for e in g["edges"]}
    assert edges == {("manager", "worker1"): 2, ("worker1", "manager"): 1}


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


def test_prune_keep_last_archives():
    s = fresh()
    for i in range(10):
        s.post_channel("general", f"m{i}", "a", "A")
    pruned = s.prune_channel("general", keep_last=3)
    assert pruned == 7
    remaining = [m["text"] for m in s.read_channel("general")]
    assert remaining == ["m7", "m8", "m9"]
    archived = [m["text"] for m in s.read_archive("general")]
    assert archived == [f"m{i}" for i in range(7)]  # oldest-first, all 7


def test_prune_max_age_deletes_when_no_archive():
    s = fresh()
    for i in range(4):
        s.post_channel("general", f"m{i}", "a", "A")
    # everything older than "now" -> all pruned, archive disabled
    pruned = s.prune_channel("general", max_age=0.0, archive=False)
    assert pruned == 4
    assert s.read_channel("general") == []
    assert s.read_archive("general") == []  # nothing archived


def test_prune_all_covers_channels_and_broadcast():
    s = fresh()
    s.post_channel("general", "g", "a", "A")
    s.post_channel("ops", "o", "a", "A")
    s.post_broadcast("b", "human:jpic", "jpic")
    result = s.prune_all(keep_last=0)  # remove everything
    assert result.get("#general") == 1
    assert result.get("#ops") == 1
    assert result.get("broadcast") == 1
    assert s.read_channel("general") == [] and s.read_broadcast() == []


def test_stats_snapshot():
    s = fresh()
    s.ensure_channel("ops")
    s.post_channel("general", "g1", "a", "A")
    s.post_channel("general", "g2", "a", "A")
    s.post_channel("ops", "o1", "a", "A")
    s.post_broadcast("b1", "human:jpic", "jpic")
    s.register_agent("t1", "trainer")
    s.post_inbox("t1", "do x", "human:jpic", "jpic")
    st = s.stats()
    assert st["config_ok"] is True
    assert st["auth_enabled"] is True
    assert st["channels"] == 2
    assert st["channel_messages_total"] == 3
    assert st["channel_message_counts"]["general"] == 2
    assert st["broadcast_messages"] == 1
    assert st["inbox_messages_total"] == 1
    assert st["agents_total"] == 1 and st["agents_online"] == 1


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
