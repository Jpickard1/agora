"""Tests for agent liveness sub-status (#53): the classifier + store roundtrip."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub import bridge
from agenthub.store import HubStore


# -- classifier (pure, mocked pane states) ---------------------------------

def test_no_pane_is_responsive():
    assert bridge.classify_liveness(False, False, 999) == "responsive"


def test_busy_and_producing_is_busy():
    # busy, output changed recently (small unchanged_for) -> busy
    assert bridge.classify_liveness(True, True, 2, wedged_after=20) == "busy"


def test_busy_and_frozen_is_wedged():
    # busy but output frozen past the threshold -> wedged (the key signal)
    assert bridge.classify_liveness(True, True, 25, wedged_after=20) == "wedged"
    # exactly at threshold counts as wedged
    assert bridge.classify_liveness(True, True, 20, wedged_after=20) == "wedged"


def test_idle_recent_is_responsive():
    assert bridge.classify_liveness(True, False, 5, idle_after=45) == "responsive"


def test_idle_quiet_is_idle():
    assert bridge.classify_liveness(True, False, 60, idle_after=45) == "idle"


# -- store roundtrip --------------------------------------------------------

def _store():
    s = HubStore(tempfile.mkdtemp(prefix="hub-live-"))
    s.init(token="t")
    return s


def test_heartbeat_persists_liveness_and_shows_online():
    s = _store()
    s.register_agent("w", "w")
    s.heartbeat("w", status="working", liveness="wedged")
    rec = [a for a in s.list_agents() if a["id"] == "w"][0]
    assert rec["online"] is True
    assert rec["liveness"] == "wedged"   # visible while still heartbeating


def test_default_liveness_is_responsive():
    s = _store()
    s.register_agent("w", "w")
    rec = [a for a in s.list_agents() if a["id"] == "w"][0]
    assert rec["liveness"] == "responsive"


def test_offline_agent_reports_offline_liveness():
    s = _store()
    s.register_agent("w", "w")
    s.heartbeat("w", status="working", liveness="busy")
    s.set_agent_status("w", "offline")
    rec = [a for a in s.list_agents() if a["id"] == "w"][0]
    assert rec["online"] is False
    assert rec["liveness"] == "offline"   # never show a stale busy/wedged


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
