"""Tests for export & reporting (issue #21): --since parsing, the gathered
snapshot, the per-agent standup, and the JSON/Markdown/HTML renderers.
Run: python tests/test_export.py"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub import export as exp  # noqa: E402
from agenthub.store import HubStore  # noqa: E402

NOW = 2_000_000_000.0  # large epoch so duration math never clamps at 0


def fresh():
    s = HubStore(tempfile.mkdtemp(prefix="export-"))
    s.init(token="t")
    return s


def populated():
    s = fresh()
    s.register_agent("alice", "alice")
    s.register_agent("bob", "bob")
    s.post_channel("general", "hello team", "alice", "alice")
    s.post_channel("general", "ship it 🚨", "bob", "bob", meta={"alert": True})
    s.ensure_channel("dev", description="engineering")
    s.post_channel("dev", "pushed a fix", "alice", "alice")
    s.create_task("t1", title="do thing", ref="o/r#1")
    s.claim_task("t1", "alice")
    s.update_task("t1", "running", by="alice")
    s.update_task("t1", "done", by="alice")
    s.create_task("t2", title="other", ref="o/r#2")
    s.claim_task("t2", "bob")
    return s


# -- parse_since -----------------------------------------------------------

def test_parse_since_all_and_empty():
    assert exp.parse_since(None) == 0.0
    assert exp.parse_since("") == 0.0
    assert exp.parse_since("all") == 0.0
    assert exp.parse_since("0") == 0.0


def test_parse_since_durations():
    assert exp.parse_since("24h", now=NOW) == NOW - 86400
    assert exp.parse_since("7d", now=NOW) == NOW - 7 * 86400
    assert exp.parse_since("30m", now=NOW) == NOW - 1800
    assert exp.parse_since("2w", now=NOW) == NOW - 2 * 604800


def test_parse_since_bare_epoch():
    assert exp.parse_since("12345", now=NOW) == 12345.0


# -- gather ----------------------------------------------------------------

def test_gather_snapshot_shape():
    s = populated()
    snap = exp.gather(s, now=NOW)
    m = snap["meta"]
    assert m["messages"] == 3
    assert m["tasks"] == 2
    assert m["channels"] >= 2
    assert {c["name"] for c in snap["channels"]} >= {"general", "dev"}
    assert len(snap["decisions"]) == 1            # the alert
    assert snap["decisions"][0]["channel"] == "general"


def test_gather_channel_has_description():
    s = populated()
    snap = exp.gather(s, now=NOW)
    dev = next(c for c in snap["channels"] if c["name"] == "dev")
    assert dev["description"] == "engineering"


# -- standup ---------------------------------------------------------------

def test_standup_counts_messages_and_tasks():
    s = populated()
    snap = exp.gather(s, now=NOW)
    rows = {r["agent"]: r for r in snap["standup"]}
    assert rows["alice"]["messages"] == 2
    assert "t1" in rows["alice"]["tasks_done"]
    # a done task is not also listed as in-progress
    assert "t1" not in rows["alice"]["tasks_claimed"]
    assert "t2" in rows["bob"]["tasks_claimed"]


def test_standup_text_is_readable():
    s = populated()
    snap = exp.gather(s, now=NOW)
    txt = exp.standup_text(snap)
    assert "Standup" in txt
    assert "alice" in txt and "bob" in txt


# -- renderers -------------------------------------------------------------

def test_json_roundtrips():
    s = populated()
    snap = exp.gather(s, now=NOW)
    data = json.loads(exp.to_json(snap))
    assert data["meta"]["messages"] == 3
    assert "standup" in data and "channels" in data


def test_markdown_has_all_sections():
    s = populated()
    md = exp.to_markdown(exp.gather(s, now=NOW))
    for header in ("# Agora activity report", "## Standup", "## Tasks",
                   "## Decisions & alerts", "## Conversations"):
        assert header in md, header
    assert "#general" in md and "#dev" in md


def test_html_is_self_contained():
    s = populated()
    h = exp.to_html(exp.gather(s, now=NOW))
    assert h.startswith("<!doctype html>")
    assert h.rstrip().endswith("</html>")
    assert "<style>" in h                  # embedded CSS, no external deps
    assert "Standup" in h and "Conversations" in h


def test_html_escapes_message_text():
    s = fresh()
    s.post_channel("general", "<script>alert(1)</script>", "alice", "alice")
    h = exp.to_html(exp.gather(s, now=NOW))
    assert "<script>alert(1)</script>" not in h
    assert "&lt;script&gt;" in h


def test_empty_hub_renders_without_error():
    s = fresh()
    snap = exp.gather(s, now=NOW)
    assert exp.to_json(snap)
    assert "No activity" in exp.to_markdown(snap) or "## Standup" in exp.to_markdown(snap)
    assert exp.to_html(snap).startswith("<!doctype html>")


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
