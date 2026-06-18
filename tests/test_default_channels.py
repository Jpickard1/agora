"""Tests for the all-channels-by-default bridge behaviour (#75)."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub import bridge
from agenthub.store import HubStore


# -- the default decision (pure) -------------------------------------------

def test_default_is_all_channels():
    # nothing specified -> follow all
    assert bridge.wants_all_channels(False, None, None, False) is True


def test_single_opts_out():
    assert bridge.wants_all_channels(False, None, None, True) is False


def test_channel_subset_opts_out():
    assert bridge.wants_all_channels(False, "data", None, False) is False
    assert bridge.wants_all_channels(False, None, "general,data", False) is False


def test_explicit_all_channels_always_wins():
    # even with a subset/single, an explicit --all-channels forces all
    assert bridge.wants_all_channels(True, "data", None, True) is True


# -- resolve_channels follows the decision ---------------------------------

def _store_with_channels(*names):
    s = HubStore(tempfile.mkdtemp(prefix="hub-defchan-"))
    s.init(token="t")
    for n in names:
        s.ensure_channel(n)
    return s


def test_resolve_all_channels_returns_every_channel():
    s = _store_with_channels("data", "memes")
    follow_all = bridge.wants_all_channels(False, None, None, False)
    got = set(bridge.resolve_channels(s, follow_all, None, "general"))
    assert {"general", "data", "memes"} <= got


def test_resolve_single_returns_just_general():
    s = _store_with_channels("data", "memes")
    follow_all = bridge.wants_all_channels(False, None, None, True)   # --single
    assert bridge.resolve_channels(s, follow_all, None, "general") == ["general"]


def test_resolve_subset():
    s = _store_with_channels("data", "memes")
    follow_all = bridge.wants_all_channels(False, None, "general,data", False)
    assert bridge.resolve_channels(s, follow_all, "general,data", "general") == ["general", "data"]


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
