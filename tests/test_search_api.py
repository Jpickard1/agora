"""Tests for the search layers added on top of worker1's store.search_messages
(issue #51): channel scoping, task inclusion toggle, ranking, and the
hit-shape the CLI/REST/UI depend on (snippet + source/where/id).
(worker1's tests/test_search.py covers the base matcher; this covers the
contract the new hubcli search / GET /api/search / UI jump-to rely on.)
Run: python tests/test_search_api.py"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub.store import HubStore  # noqa: E402


def fresh():
    s = HubStore(tempfile.mkdtemp(prefix="searchapi-"))
    s.init(token="t")
    return s


def test_empty_query_returns_nothing():
    s = fresh()
    s.post_channel("general", "hello world", "a", "a")
    assert s.search_messages("") == []
    assert s.search_messages("   ") == []


def test_hit_shape_has_contract_fields():
    s = fresh()
    s.post_channel("general", "kafka pipeline tuning", "alice", "alice")
    h = s.search_messages("kafka")[0]
    for k in ("source", "where", "id", "ts", "author", "text", "snippet", "score"):
        assert k in h, k
    assert h["source"] == "channel" and h["where"] == "general"
    assert "kafka" in h["snippet"].lower()
    assert h["id"]                       # needed for UI jump-to (data-msg-id)


def test_channel_scope_restricts_sources():
    s = fresh()
    s.post_channel("general", "kafka here", "a", "a")
    s.post_channel("dev", "kafka there", "b", "b")
    s.post_broadcast("kafka broadcast", author="m", author_name="m")
    only_dev = s.search_messages("kafka", channels=["dev"])
    assert {h["where"] for h in only_dev} == {"dev"}
    # scoping to channels excludes broadcasts/inboxes/tasks
    assert all(h["source"] == "channel" for h in only_dev)


def test_searches_broadcast_and_inbox_unscoped():
    s = fresh()
    s.post_broadcast("kafka to all", author="m", author_name="m")
    s.post_inbox("bob", "kafka dm", author="alice", author_name="alice")
    sources = {h["source"] for h in s.search_messages("kafka")}
    assert "broadcast" in sources
    assert "inbox" in sources


def test_include_tasks_toggle():
    s = fresh()
    s.create_task("t1", title="kafka migration", brief="move to kafka")
    with_tasks = s.search_messages("kafka")
    assert any(h["source"] == "task" for h in with_tasks)
    without = s.search_messages("kafka", include_tasks=False)
    assert all(h["source"] != "task" for h in without)


def test_text_outranks_author_match():
    s = fresh()
    s.post_channel("general", "talking about widgets", "kafka", "kafka")  # author hit
    s.post_channel("general", "kafka kafka kafka everywhere", "bob", "bob")  # text hits
    hits = s.search_messages("kafka")
    assert hits[0]["text"] == "kafka kafka kafka everywhere"


def test_limit_applies():
    s = fresh()
    for i in range(5):
        s.post_channel("general", f"kafka note {i}", "a", "a")
    assert len(s.search_messages("kafka", limit=3)) == 3


def test_no_match_returns_empty():
    s = fresh()
    s.post_channel("general", "nothing relevant", "a", "a")
    assert s.search_messages("zzqq-nomatch") == []


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
