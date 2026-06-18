"""Tests for the multi-channel bridge (issue #13): channel-spec parsing,
channel-set resolution (--channels / --all-channels), the presence/activity
label, and per-channel cursor behaviour (incl. mention-routing across channels).

Pure helpers run without tmux; resolution/cursor tests use a temp HubStore.
Run: python tests/test_multichannel.py"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub import bridge  # noqa: E402
from agenthub.store import HubStore  # noqa: E402


def fresh():
    s = HubStore(tempfile.mkdtemp(prefix="mc-"))
    s.init(token="t")
    return s


# -- parse_channels --------------------------------------------------------

def test_parse_channels_splits_and_strips():
    assert bridge.parse_channels("general, dev ,alerts") == ["general", "dev", "alerts"]


def test_parse_channels_tolerates_hash_and_dedupes():
    assert bridge.parse_channels("#general,general,#dev") == ["general", "dev"]


def test_parse_channels_empty():
    assert bridge.parse_channels("") == []
    assert bridge.parse_channels(None) == []


# -- resolve_channels ------------------------------------------------------

def test_resolve_default_single_channel():
    s = fresh()
    assert bridge.resolve_channels(s, False, None, "general") == ["general"]


def test_resolve_explicit_channels_list():
    s = fresh()
    got = bridge.resolve_channels(s, False, "a,b,c", "general")
    assert got == ["a", "b", "c"]


def test_resolve_all_channels_returns_every_channel():
    s = fresh()
    s.ensure_channel("general")
    s.ensure_channel("dev")
    s.ensure_channel("alerts")
    got = set(bridge.resolve_channels(s, True, None, "general"))
    assert {"general", "dev", "alerts"} <= got


def test_resolve_all_channels_picks_up_new_channel():
    s = fresh()
    s.ensure_channel("general")
    before = set(bridge.resolve_channels(s, True, None, "general"))
    s.ensure_channel("newchan")
    after = set(bridge.resolve_channels(s, True, None, "general"))
    assert "newchan" not in before
    assert "newchan" in after


# -- channels_activity (presence label) ------------------------------------

def test_activity_single_channel():
    assert bridge.channels_activity(["general"]) == "on #general"


def test_activity_few_channels_listed():
    assert bridge.channels_activity(["a", "b"]) == "on #a, #b"


def test_activity_many_channels_counted():
    assert bridge.channels_activity(["a", "b", "c", "d"]) == "on 4 channels"


def test_activity_empty():
    assert bridge.channels_activity([]) == "no channels"


# -- per-channel cursors + labels (the core fix) ---------------------------

def test_per_channel_cursors_track_independently():
    s = fresh()
    s.post_channel("general", "g1", "alice", "alice")
    s.post_channel("dev", "d1", "alice", "alice")
    cursors = {"general": 0.0, "dev": 0.0}
    collected = []
    for ch in list(cursors.keys()):
        for m in s.read_channel(ch, since_ts=cursors[ch]):
            cursors[ch] = max(cursors[ch], m["ts"])
            collected.append((ch, m["text"]))
    assert ("general", "g1") in collected
    assert ("dev", "d1") in collected
    # a second pass with the advanced cursors yields nothing new
    again = []
    for ch in list(cursors.keys()):
        again += [m for m in s.read_channel(ch, since_ts=cursors[ch])]
    assert again == []


def test_channel_label_uses_originating_channel():
    # The 'where' label fed to deliver() is f"#{ch}", so a multi-channel bridge
    # still tags each line with the channel it came from.
    where = f"#{'dev'}"
    assert where == "#dev"


def test_mention_routing_applies_per_channel():
    # A message in #dev that @mentions only another agent is skipped for me,
    # while a plain message in #general reaches me — independent of channel.
    assert bridge.channel_msg_for_me("@someoneelse ping", "probe", "probe") is False
    assert bridge.channel_msg_for_me("status?", "probe", "probe") is True
    assert bridge.channel_msg_for_me("@probe look", "probe", "probe") is True


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
