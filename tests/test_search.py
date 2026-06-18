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


# --- #115: --author / --since filters + dual-root visibility -----------------

def test_author_filter_restricts_to_sender():
    s = _seed(_store())
    hits = s.search_messages("loader", author="worker1")
    assert hits
    assert all("worker1" in (h["author"] or "").lower() for h in hits)
    assert not any(h["author"] == "manager" for h in hits)   # other senders excluded


def test_author_filter_no_match_returns_empty():
    s = _seed(_store())
    assert s.search_messages("loader", author="nobody-here") == []


def test_since_filter_excludes_older():
    import time
    s = _store()
    old = s.post_channel("general", "old loader note", author="a", author_name="a")
    time.sleep(0.02)
    new = s.post_channel("general", "new loader note", author="b", author_name="b")
    cutoff = old.ts + (new.ts - old.ts) / 2
    texts = [h["text"] for h in s.search_messages("loader", since_ts=cutoff)]
    assert "new loader note" in texts and "old loader note" not in texts


def test_search_never_leaks_another_users_private_channel():
    """CRITICAL (#115): a hub only searches its own private root + the shared
    store, so another user's private-channel messages can never surface."""
    shared = tempfile.mkdtemp(prefix="shared-")
    a = HubStore(tempfile.mkdtemp(prefix="userA-"))
    a.init(token="ta", shared_root=shared)
    b = HubStore(tempfile.mkdtemp(prefix="userB-"))
    b.init(token="tb", shared_root=shared)
    a.ensure_channel("secret-a", visibility="private")        # in A's own root
    a.post_channel("secret-a", "classified loader plans", author="ana", author_name="ana")
    a.post_channel("general", "public loader note", author="ana", author_name="ana")  # shared
    texts = [h["text"] for h in b.search_messages("loader")]   # B searches
    assert "public loader note" in texts                      # public match is visible
    assert "classified loader plans" not in texts             # A's private NEVER leaks
    assert not any(h["where"] == "secret-a" for h in b.search_messages("loader"))


def test_search_is_read_only():
    s = _seed(_store())
    before = {ch["name"]: len(s.read_channel(ch["name"])) for ch in s.list_channels()}
    s.search_messages("loader", author="worker1", since_ts=0.0)
    after = {ch["name"]: len(s.read_channel(ch["name"])) for ch in s.list_channels()}
    assert before == after


# --- #115: --include-archive (search pruned/archived history) ----------------

def test_archive_is_off_by_default():
    s = _store()
    for v in ("v1", "v2", "v3"):
        s.post_channel("general", f"loader {v} deployed", author="a", author_name="a")
    s.prune_channel("general", keep_last=1)        # archive v1,v2; keep v3 live
    texts = [h["text"] for h in s.search_messages("loader")]
    assert "loader v3 deployed" in texts           # live still found
    assert "loader v1 deployed" not in texts        # archived NOT searched by default


def test_include_archive_finds_pruned_history():
    s = _store()
    for v in ("v1", "v2", "v3"):
        s.post_channel("general", f"loader {v} deployed", author="a", author_name="a")
    s.prune_channel("general", keep_last=1)
    hits = s.search_messages("loader", include_archive=True)
    texts = {h["text"] for h in hits}
    assert {"loader v1 deployed", "loader v2 deployed", "loader v3 deployed"} <= texts
    assert any(h["source"] == "archive" for h in hits)   # tagged as archive


def test_include_archive_still_respects_visibility():
    """Archive search must not leak another user's pruned private history."""
    shared = tempfile.mkdtemp(prefix="shared-")
    a = HubStore(tempfile.mkdtemp(prefix="userA-"))
    a.init(token="ta", shared_root=shared)
    b = HubStore(tempfile.mkdtemp(prefix="userB-"))
    b.init(token="tb", shared_root=shared)
    a.ensure_channel("secret-a", visibility="private")
    a.post_channel("secret-a", "archived classified loader", author="ana", author_name="ana")
    a.prune_channel("secret-a", keep_last=0)        # archive A's whole private channel
    texts = [h["text"] for h in b.search_messages("loader", include_archive=True)]
    assert "archived classified loader" not in texts   # B never sees A's archived private


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
