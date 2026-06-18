"""Tests for delivery health (#54): the backlog flag + store roundtrip."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub import bridge
from agenthub.store import HubStore


# -- backlog flag ----------------------------------------------------------

def test_no_queue_is_healthy():
    assert bridge.delivery_backlog(0, 0, now=1000) is False
    assert bridge.delivery_backlog(0, 999, now=1000) is False


def test_queued_but_recently_delivered_is_healthy():
    # 3 queued, delivered 5s ago, stale_after 30 -> still draining
    assert bridge.delivery_backlog(3, 995, now=1000, stale_after=30) is False


def test_queued_and_stale_is_backlog():
    # 3 queued, last delivery 60s ago -> backed up
    assert bridge.delivery_backlog(3, 940, now=1000, stale_after=30) is True


def test_queued_and_never_delivered_is_backlog():
    assert bridge.delivery_backlog(2, 0, now=1000, stale_after=30) is True


# -- store roundtrip -------------------------------------------------------

def _store():
    s = HubStore(tempfile.mkdtemp(prefix="hub-deliv-"))
    s.init(token="t")
    return s


def test_delivery_defaults_to_empty():
    s = _store()
    s.register_agent("w", "w")
    rec = [a for a in s.list_agents() if a["id"] == "w"][0]
    assert rec["delivery"] == {}


def test_heartbeat_persists_delivery():
    s = _store()
    s.register_agent("w", "w")
    s.heartbeat("w", status="working",
                delivery={"queued": 4, "last_delivered_ts": 123.0,
                          "last_receipt_ts": 124.0})
    rec = [a for a in s.list_agents() if a["id"] == "w"][0]
    assert rec["delivery"]["queued"] == 4
    assert rec["delivery"]["last_delivered_ts"] == 123.0
    assert rec["delivery"]["last_receipt_ts"] == 124.0


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
