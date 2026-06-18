"""Tests for `hubcli catchup` — personalized, read-only catch-up summary (#113).
Hermetic: temp roots only, no live hub/pointer, no writes by catchup itself."""

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub import cli
from agenthub.store import HubStore


def _hub():
    s = HubStore(tempfile.mkdtemp(prefix="catchup-"))
    s.init(token="t")
    return s


def _seed(s):
    """ana is the viewer. She posts once (sets 'last active'); then, after that,
    others post a mention, an alert, a normal msg, a DM, and a task changes."""
    ana = s.post_channel("general", "morning all", author="ana", author_name="ana")
    since = ana.ts
    s.post_channel("general", "@ana can you review the PR?", author="bob", author_name="bob")
    s.post_channel("general", "deploy finished", author="cy", author_name="cy")
    s.post_channel("general", "🚨 prod alert", author="cy", author_name="cy",
                   meta={"alert": True})
    s.post_inbox("ana", "ping — need your input", author="bob", author_name="bob")
    s.create_task("T-1", title="ship widget", created_by="mgr")
    s.claim_task("T-1", "ana")
    return since


def test_catchup_is_personalized():
    s = _hub()
    since = _seed(s)
    c = s.catchup("ana", since_ts=since)
    assert any("review the PR" in m["text"] for m in c["mentions"])     # @ana mention
    assert all(m["author"] != "ana" for m in c["mentions"])             # not her own posts
    assert any("prod alert" in a["text"] for a in c["alerts"])          # alert surfaced
    assert any("need your input" in d["text"] for d in c["unread_dms"]) # DM to her
    assert any(t["id"] == "T-1" for t in c["open_tasks"])               # her open task
    assert any(ch["channel"] == "general" and ch["messages"] >= 3       # new channel activity
               for ch in c["channels"])
    assert any(e["task"] == "T-1" for e in c["task_changes"])           # task-board delta


def test_catchup_defaults_to_last_activity():
    s = _hub()
    _seed(s)
    c = s.catchup("ana")                       # no since -> since ana's last post
    assert c["since"] > 0
    assert any("review the PR" in m["text"] for m in c["mentions"])


def test_catchup_empty_when_nothing_new():
    s = _hub()
    s.post_channel("general", "all quiet", author="ana", author_name="ana")
    c = s.catchup("ana")                        # nothing happened after ana's post
    assert c["totals"]["mentions"] == 0 and c["totals"]["unread_dms"] == 0
    assert c["totals"]["new_messages"] == 0


def test_catchup_is_read_only():
    s = _hub()
    since = _seed(s)
    before = {ch["name"]: len(s.read_channel(ch["name"])) for ch in s.list_channels()}
    s.catchup("ana", since_ts=since)
    after = {ch["name"]: len(s.read_channel(ch["name"])) for ch in s.list_channels()}
    assert before == after                      # catchup never writes


def test_catchup_cli_json_hermetic():
    s = _hub()
    _seed(s)
    pointer = os.path.expanduser("~/.agent-hub-path")
    snap = open(pointer).read() if os.path.exists(pointer) else None
    args = cli.build_parser().parse_args(
        ["--root", s.root if isinstance(s.root, str) else str(s.root),
         "catchup", "--name", "ana", "--json"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        args.func(args)
    data = json.loads(buf.getvalue())
    assert data["viewer"] == "ana"
    assert "mentions" in data and "open_tasks" in data
    after = open(pointer).read() if os.path.exists(pointer) else None
    assert after == snap                        # live pointer untouched


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
