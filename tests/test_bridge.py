"""Tests for the tmux bridge's reliable-delivery logic (A1/A3).

These cover the pure decision helpers — busy-detection, self/loop filtering,
and the flush gate — without needing a real tmux pane.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub import bridge


def test_pane_busy_detects_claude_working():
    assert bridge.pane_busy("doing things… (esc to interrupt)") is True
    assert bridge.pane_busy("Press ctrl+b to run in background") is True
    assert bridge.pane_busy("spinner ⠹ still going") is True


def test_pane_busy_idle_is_false():
    assert bridge.pane_busy("") is False
    assert bridge.pane_busy("│ > \n  ready for input") is False
    # ASCII slashes/pipes are NOT treated as spinners (too common in output).
    assert bridge.pane_busy("path/to/file | grep x") is False


def test_is_self_message_by_id_and_name():
    assert bridge.is_self_message({"author": "worker1"}, "worker1", "worker1") is True
    assert bridge.is_self_message({"author_name": "worker1"}, "worker1", "worker1") is True
    # A message from someone else must pass through.
    assert bridge.is_self_message({"author": "jpic", "author_name": "jpic"},
                                  "worker1", "worker1") is False


def test_is_self_message_skips_own_receipts():
    receipt = {"author": "x", "author_name": "x", "meta": {"msg_kind": "delivery_receipt"}}
    assert bridge.is_self_message(receipt, "worker1", "worker1") is True


def test_ready_to_flush_waits_for_settle():
    # Not settled yet, message is fresh -> hold.
    assert bridge.ready_to_flush(idle_streak=0, settle_checks=2,
                                 head_age=1.0, max_wait=120.0) is False
    assert bridge.ready_to_flush(idle_streak=1, settle_checks=2,
                                 head_age=1.0, max_wait=120.0) is False


def test_ready_to_flush_when_idle_enough():
    assert bridge.ready_to_flush(idle_streak=2, settle_checks=2,
                                 head_age=1.0, max_wait=120.0) is True


def test_ready_to_flush_max_wait_safety_valve():
    # Pane still looks busy (idle_streak 0) but the message has waited too long.
    assert bridge.ready_to_flush(idle_streak=0, settle_checks=2,
                                 head_age=200.0, max_wait=120.0) is True


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
