"""Tests for message reactions (#61): add/remove/toggle, aggregation, events."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub.store import HubStore


def _store():
    s = HubStore(tempfile.mkdtemp(prefix="hub-react-"))
    s.init(token="t")
    return s


def test_add_reaction_aggregates():
    s = _store()
    r = s.add_reaction("m1", "👍", "worker1", author_name="worker1")
    assert r["👍"]["count"] == 1
    assert r["👍"]["authors"] == ["worker1"]


def test_add_is_idempotent_per_author():
    s = _store()
    s.add_reaction("m1", "👍", "worker1")
    r = s.add_reaction("m1", "👍", "worker1")   # same author+emoji again
    assert r["👍"]["count"] == 1                 # not double-counted


def test_two_authors_same_emoji():
    s = _store()
    s.add_reaction("m1", "🎉", "worker1", author_name="worker1")
    r = s.add_reaction("m1", "🎉", "probe", author_name="probe")
    assert r["🎉"]["count"] == 2
    assert set(r["🎉"]["authors"]) == {"worker1", "probe"}


def test_distinct_emojis_coexist():
    s = _store()
    s.add_reaction("m1", "👍", "a")
    s.add_reaction("m1", "✅", "a")
    r = s.get_reactions("m1")
    assert set(r.keys()) == {"👍", "✅"}


def test_remove_reaction():
    s = _store()
    s.add_reaction("m1", "👍", "a")
    r = s.remove_reaction("m1", "👍", "a")
    assert "👍" not in r              # bucket gone once empty
    assert s.get_reactions("m1") == {}


def test_toggle_adds_then_removes():
    s = _store()
    r1 = s.toggle_reaction("m1", "👍", "a")   # absent -> add
    assert r1["👍"]["count"] == 1
    r2 = s.toggle_reaction("m1", "👍", "a")   # present -> remove
    assert "👍" not in r2


def test_empty_or_huge_emoji_rejected():
    s = _store()
    for bad in ("", "   ", "x" * 17):
        try:
            s.add_reaction("m1", bad, "a")
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass


def test_reactions_for_many_and_events():
    s = _store()
    s.add_reaction("m1", "👍", "a")
    s.add_reaction("m2", "🎉", "b")
    many = s.reactions_for(["m1", "m2", "m3"])
    assert set(many.keys()) == {"m1", "m2"}    # m3 has none -> skipped
    evs = s.read_reaction_events()
    assert len(evs) == 2 and {e["op"] for e in evs} == {"add"}


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
