"""Tests for @mentions (issue #52): mention detection and the collect_mentions
query (channels + broadcasts, @all groups, self-authored skipped).
Run: python tests/test_mentions.py"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub.store import (HubStore, extract_mentions, message_mentions,  # noqa: E402
                            collect_mentions)


def fresh():
    s = HubStore(tempfile.mkdtemp(prefix="mentions-"))
    s.init(token="t")
    return s


# -- extract_mentions ------------------------------------------------------

def test_extract_basic_and_lowercase():
    assert extract_mentions("hi @Alice and @BOB-2!") == {"alice", "bob-2"}


def test_extract_none():
    assert extract_mentions("no mentions here") == set()
    assert extract_mentions("") == set()


# -- message_mentions ------------------------------------------------------

def test_mentions_by_name():
    assert message_mentions("yo @probe look", "probe") is True
    assert message_mentions("yo @probe look", "alice") is False


def test_mentions_name_case_insensitive():
    assert message_mentions("hey @Probe", "probe") is True


def test_mentions_all_groups_reach_everyone():
    for kw in ("all", "everyone", "channel", "here"):
        assert message_mentions(f"@{kw} standup", "anyone") is True


def test_no_mention_is_false():
    assert message_mentions("just chatting", "probe") is False


# -- collect_mentions ------------------------------------------------------

def test_collect_from_channels_and_broadcast():
    s = fresh()
    s.post_channel("general", "hey @probe pls review", "alice", "alice")
    s.post_channel("dev", "@all deploy now", "bob", "bob")
    s.post_channel("general", "unrelated chatter", "carol", "carol")
    s.post_broadcast("@probe urgent", author="mgr", author_name="manager")
    hits = collect_mentions(s, "probe")
    texts = [m["text"] for m in hits]
    assert "hey @probe pls review" in texts
    assert "@all deploy now" in texts
    assert "@probe urgent" in texts
    assert "unrelated chatter" not in texts


def test_collect_skips_self_authored():
    s = fresh()
    s.post_channel("general", "@probe note to self", "probe", "probe")
    assert collect_mentions(s, "probe") == []


def test_collect_carries_channel_context():
    s = fresh()
    s.post_channel("dev", "@probe in dev", "alice", "alice")
    s.post_broadcast("@probe everywhere", author="mgr", author_name="manager")
    by_text = {m["text"]: m["channel"] for m in collect_mentions(s, "probe")}
    assert by_text["@probe in dev"] == "dev"
    assert by_text["@probe everywhere"] == "*"


def test_collect_newest_first_and_limit():
    s = fresh()
    s.post_channel("general", "@probe one", "a", "a")
    s.post_channel("general", "@probe two", "a", "a")
    s.post_channel("general", "@probe three", "a", "a")
    hits = collect_mentions(s, "probe", limit=2)
    assert len(hits) == 2
    assert hits[0]["text"] == "@probe three"     # most recent first


def test_collect_none_for_unmentioned_agent():
    s = fresh()
    s.post_channel("general", "@alice hi", "bob", "bob")
    assert collect_mentions(s, "probe") == []


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
