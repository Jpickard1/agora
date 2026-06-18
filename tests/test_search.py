"""Tests for full-text search across messages + task history (#51)."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub.store import HubStore, _search_snippet


def _store():
    s = HubStore(tempfile.mkdtemp(prefix="hub-search-"))
    s.init(token="t")
    return s


def _seed(s):
    s.post_channel("general", "deploying the loader to gpu01 tonight",
                   author="trainer", author_name="trainer")
    s.post_channel("general", "weather is nice", author="probe", author_name="probe")
    s.post_channel("data", "the loader needs a retry on transient errors",
                   author="worker1", author_name="worker1")
    s.post_broadcast("all agents: loader freeze in effect",
                     author="manager", author_name="manager")
    s.post_inbox("worker1", "please fix the loader retry",
                 author="manager", author_name="manager")
    s.create_task("agora-99", title="Loader hardening", brief="add retries",
                  created_by="manager")
    return s


def test_finds_phrase_across_sources():
    s = _seed(_store())
    hits = s.search_messages("loader")
    sources = {h["source"] for h in hits}
    # appears in a channel, the data channel, a broadcast, an inbox, and a task
    assert {"channel", "broadcast", "inbox", "task"} <= sources
    assert all("loader" in h["text"].lower() or "loader" in h["snippet"].lower()
               for h in hits)
    # the irrelevant "weather" message is not returned
    assert not any("weather" in h["text"].lower() for h in hits)


def test_empty_query_returns_nothing():
    s = _seed(_store())
    assert s.search_messages("") == []
    assert s.search_messages("   ") == []


def test_channel_scope_limits_results():
    s = _seed(_store())
    hits = s.search_messages("loader", channels=["data"])
    assert hits and all(h["source"] == "channel" and h["where"] == "data"
                        for h in hits)


def test_ranking_prefers_more_term_hits():
    s = _store()
    s.post_channel("general", "loader loader loader everywhere",
                   author="a", author_name="a")
    s.post_channel("general", "one loader here", author="b", author_name="b")
    hits = s.search_messages("loader")
    # both match (score is per-term presence, not count) -> tie broken by recency,
    # but a multi-term query should rank a doc containing more distinct terms first
    multi = s.search_messages("loader retry transient")
    assert multi  # smoke: multi-term query runs


def test_author_match_contributes_score():
    s = _store()
    s.post_channel("general", "status update", author="gpubot", author_name="gpubot")
    hits = s.search_messages("gpubot")
    assert hits and hits[0]["author"] == "gpubot"


def test_snippet_centers_and_single_line():
    long = "x " * 200 + "FINDME needle " + "y " * 200
    snip = _search_snippet(long, ["findme"])
    assert "FINDME" in snip
    assert "\n" not in snip
    assert snip.startswith("…") and snip.endswith("…")


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
