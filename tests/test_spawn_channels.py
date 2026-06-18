"""Tests for channel-selection at spawn time (issue #81): the modal's selection
maps to the right bridge listen flag, end to end through build_spawn_plan, plus
the UI wiring is present. Runner-agnostic (no monkeypatch fixture).
Run: python tests/test_spawn_channels.py"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agenthub import spawn  # noqa: E402


def _bridge_cmd(channels):
    plan = spawn.build_spawn_plan("bot", "/tmp/work", "", "bot", "do things",
                                  hub_root="/hub", channels=channels)
    # immediate[1] is the bridge tmux new-session; its last arg is the shell cmd
    return plan["immediate"][1][-1]


# -- _channels_flag ---------------------------------------------------------

def test_flag_default_is_all_channels():
    assert spawn._channels_flag(None) == " --all-channels"
    assert spawn._channels_flag("all") == " --all-channels"
    assert spawn._channels_flag([]) == " --all-channels"


def test_flag_specific_list():
    assert spawn._channels_flag(["general", "dev"]) == " --channels general,dev"


def test_flag_comma_string():
    assert spawn._channels_flag("general,data") == " --channels general,data"


def test_flag_sanitises_and_drops_blanks():
    out = spawn._channels_flag(["general", "", "  ", "dev"])
    assert out == " --channels general,dev"


# -- end-to-end through build_spawn_plan -----------------------------------

def test_default_spawn_follows_all_channels():
    assert "--all-channels" in _bridge_cmd(None)


def test_specific_channels_in_bridge_cmd():
    cmd = _bridge_cmd(["general", "data"])
    assert "--channels general,data" in cmd
    assert "--all-channels" not in cmd


def test_single_channel_selection():
    assert "--channels general" in _bridge_cmd(["general"])


def test_plan_reports_channels():
    assert spawn.build_spawn_plan("b", "/t", "", "b", "t", hub_root="/h",
                                  channels=["x"])["channels"] == ["x"]
    assert spawn.build_spawn_plan("b", "/t", "", "b", "t",
                                  hub_root="/h")["channels"] == "all"


# -- UI wiring present ------------------------------------------------------

def test_modal_has_channel_select():
    html = open(os.path.join(ROOT, "agenthub", "web", "index.html"), encoding="utf-8").read()
    assert 'id="spawn-chan-mode"' in html
    assert 'id="spawn-chan-list"' in html


def test_spawn_post_includes_channels():
    js = open(os.path.join(ROOT, "agenthub", "web", "app.js"), encoding="utf-8").read()
    assert "session, channels })" in js          # channels sent in the spawn body
    assert 'spawn-chan-list input:checked' in js  # gathers ticked channels


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
