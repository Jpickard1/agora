"""Tests for system-utilization stats (issue #6) — store.usage_stats():
per-agent message + task counts, totals, and host metrics.
Run: python tests/test_usage.py"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub.store import HubStore  # noqa: E402


def fresh():
    s = HubStore(tempfile.mkdtemp(prefix="usage-"))
    s.init(token="t")
    return s


def test_empty_hub_has_zero_totals():
    s = fresh()
    u = s.usage_stats()
    assert u["totals"]["agents"] == 0
    assert u["totals"]["messages"] == 0
    assert u["totals"]["tasks"] == 0
    assert u["totals"]["tasks_per_agent"] == 0
    assert u["agents"] == []


def test_message_counts_per_author():
    s = fresh()
    s.register_agent("alice", "alice")
    s.register_agent("bob", "bob")
    s.post_channel("general", "hi", "alice", "alice")
    s.post_channel("general", "yo", "alice", "alice")
    s.post_channel("general", "hey", "bob", "bob")
    u = s.usage_stats()
    by = {a["id"]: a for a in u["agents"]}
    assert by["alice"]["messages"] == 2
    assert by["bob"]["messages"] == 1
    assert u["totals"]["messages"] == 3


def test_task_counts_per_owner():
    s = fresh()
    s.register_agent("alice", "alice")
    s.create_task("t1", title="one")
    s.create_task("t2", title="two")
    s.claim_task("t1", "alice")
    s.claim_task("t2", "alice")
    s.update_task("t1", "done", by="alice")
    u = s.usage_stats()
    by = {a["id"]: a for a in u["agents"]}
    assert by["alice"]["tasks_total"] == 2
    assert by["alice"]["tasks_done"] == 1
    assert u["totals"]["tasks"] == 2
    assert u["totals"]["tasks_done"] == 1


def test_running_tasks_counted():
    s = fresh()
    s.register_agent("alice", "alice")
    s.create_task("t1")
    s.claim_task("t1", "alice")
    s.update_task("t1", "running", by="alice")
    u = s.usage_stats()
    by = {a["id"]: a for a in u["agents"]}
    assert by["alice"]["tasks_running"] == 1


def test_totals_tasks_per_agent():
    s = fresh()
    s.register_agent("alice", "alice")
    s.register_agent("bob", "bob")
    s.create_task("t1"); s.claim_task("t1", "alice")
    s.create_task("t2"); s.claim_task("t2", "bob")
    u = s.usage_stats()
    assert u["totals"]["tasks_per_agent"] == 1.0


def test_host_metrics_present():
    s = fresh()
    h = s.usage_stats()["host"]
    assert "host" in h and h["host"]
    # at least one load/cpu metric should be reported on a real host
    assert any(k in h for k in ("cpu_percent", "load1"))


def test_token_tracking_note_present():
    s = fresh()
    u = s.usage_stats()
    assert "token" in u["token_tracking"].lower()


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
