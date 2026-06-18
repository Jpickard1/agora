"""Tests for agent auto-retire (issue #11): list_agents flags agents offline
longer than a configurable window as retired (and keeps active ones not).
Run: python tests/test_retire.py"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub.store import HubStore  # noqa: E402


def fresh():
    s = HubStore(tempfile.mkdtemp(prefix="retire-"))
    s.init(token="t")
    return s


def _backdate(store, agent_id, seconds_ago):
    p = os.path.join(str(store.agents_dir), f"{agent_id}.json")
    rec = json.load(open(p))
    rec["last_seen"] = time.time() - seconds_ago
    json.dump(rec, open(p, "w"))


def test_online_agent_is_not_retired():
    s = fresh()
    s.register_agent("a", "a")
    s.heartbeat("a")
    a = {x["id"]: x for x in s.list_agents()}["a"]
    assert a["online"] is True
    assert a["retired"] is False


def test_long_offline_agent_is_retired():
    s = fresh()
    s.register_agent("old", "old")
    _backdate(s, "old", 30 * 3600)          # 30h ago, default window 24h
    a = {x["id"]: x for x in s.list_agents()}["old"]
    assert a["online"] is False
    assert a["retired"] is True


def test_recently_offline_agent_not_retired():
    s = fresh()
    s.register_agent("napping", "napping")
    _backdate(s, "napping", 300)            # 5 min ago: offline but not retired
    a = {x["id"]: x for x in s.list_agents()}["napping"]
    assert a["online"] is False
    assert a["retired"] is False


def test_window_is_configurable():
    s = fresh()
    s.register_agent("old", "old")
    _backdate(s, "old", 30 * 3600)
    # widen the window past the agent's age -> no longer retired
    a = {x["id"]: x for x in s.list_agents(retire_after=48 * 3600)}["old"]
    assert a["retired"] is False


def test_retire_can_be_disabled():
    s = fresh()
    s.register_agent("old", "old")
    _backdate(s, "old", 1000 * 3600)
    a = {x["id"]: x for x in s.list_agents(retire_after=0)}["old"]
    assert a["retired"] is False


def test_mixed_roster_split():
    s = fresh()
    s.register_agent("live", "live"); s.heartbeat("live")
    s.register_agent("gone", "gone"); _backdate(s, "gone", 50 * 3600)
    ags = s.list_agents()
    retired = [a for a in ags if a["retired"]]
    active = [a for a in ags if not a["retired"]]
    assert [a["id"] for a in retired] == ["gone"]
    assert "live" in [a["id"] for a in active]


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
