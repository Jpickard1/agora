"""Tests for the shared knowledge base (issue #25): add/get/update/delete,
tag filtering + tag counts, and ranked full-text search.
Run: python tests/test_kb.py"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub.store import HubStore  # noqa: E402


def fresh():
    s = HubStore(tempfile.mkdtemp(prefix="kb-"))
    s.init(token="t")
    return s


def test_add_and_get():
    s = fresh()
    e = s.kb_add("Deploy runbook", body="steps", tags=["ops", "deploy"],
                 author="probe")
    got = s.kb_get(e["id"])
    assert got["title"] == "Deploy runbook"
    assert got["body"] == "steps"
    assert got["tags"] == ["ops", "deploy"]
    assert got["kind"] == "note"


def test_ids_are_slugged_and_unique():
    s = fresh()
    a = s.kb_add("Same Title", author="x")
    b = s.kb_add("Same Title", author="x")
    assert a["id"] != b["id"]          # collision-suffixed
    assert a["id"].lower().startswith("same")


def test_tags_normalised_lowercase():
    s = fresh()
    e = s.kb_add("t", tags=["  Ops ", "DEPLOY", ""], author="x")
    assert e["tags"] == ["ops", "deploy"]


def test_update_in_place_preserves_created():
    s = fresh()
    a = s.kb_add("Note", body="v1", author="x")
    b = s.kb_add("Note", body="v2", entry_id=a["id"], author="x")
    assert b["id"] == a["id"]
    assert b["created_ts"] == a["created_ts"]
    assert b["body"] == "v2"
    assert len(s.kb_list()) == 1       # not a duplicate


def test_list_newest_first_and_tag_filter():
    s = fresh()
    s.kb_add("first", tags=["a"], author="x")
    second = s.kb_add("second", tags=["a", "b"], author="x")
    ids = [e["id"] for e in s.kb_list()]
    assert ids[0] == second["id"]      # most-recently-updated first
    assert len(s.kb_list(tag="b")) == 1
    assert len(s.kb_list(tag="a")) == 2
    assert s.kb_list(tag="nope") == []


def test_tag_counts():
    s = fresh()
    s.kb_add("x", tags=["ops"], author="x")
    s.kb_add("y", tags=["ops", "deploy"], author="x")
    assert s.kb_tags() == {"deploy": 1, "ops": 2}


def test_delete():
    s = fresh()
    e = s.kb_add("doomed", author="x")
    assert s.kb_delete(e["id"]) is True
    assert s.kb_get(e["id"]) is None
    assert s.kb_delete("missing") is False


def test_search_ranks_title_over_body():
    s = fresh()
    body_hit = s.kb_add("unrelated", body="mentions kafka here", author="x")
    title_hit = s.kb_add("kafka setup", body="nothing", author="x")
    results = s.kb_search("kafka")
    assert [e["id"] for e in results][0] == title_hit["id"]
    assert body_hit["id"] in [e["id"] for e in results]


def test_search_respects_tag_and_limit():
    s = fresh()
    s.kb_add("alpha note", body="redis", tags=["db"], author="x")
    s.kb_add("beta note", body="redis", tags=["web"], author="x")
    assert len(s.kb_search("redis")) == 2
    assert len(s.kb_search("redis", tag="db")) == 1
    assert len(s.kb_search("redis", limit=1)) == 1


def test_search_empty_query_returns_all():
    s = fresh()
    s.kb_add("a", author="x")
    s.kb_add("b", author="x")
    assert len(s.kb_search("")) == 2


def test_search_no_match_is_empty():
    s = fresh()
    s.kb_add("a", body="b", author="x")
    assert s.kb_search("zzz-nomatch") == []


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
