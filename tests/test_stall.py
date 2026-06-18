"""Tests for the task stall detector (issue #80). All mocked — no real git,
no sleeps, injected time. Covers issue-ref parsing, the commit-time probe, the
owner hub-activity probe, and the pure detect_stalled decision (stall / recent /
unowned / de-dup / re-arm).
Run: python tests/test_stall.py"""

import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub import supervisor as S  # noqa: E402
from agenthub.store import HubStore  # noqa: E402

NOW = 1_000_000.0
THRESH = 300.0


# -- _issue_number ---------------------------------------------------------

def test_issue_number_from_ref():
    assert S._issue_number("Jpickard1/agora#80") == "80"
    assert S._issue_number("o/r#7") == "7"


def test_issue_number_none():
    assert S._issue_number("agora-80") is None     # task id, no #n
    assert S._issue_number(None) is None
    assert S._issue_number("") is None


# -- last_commit_ts (mocked git) -------------------------------------------

def _run_returning(stdout):
    return lambda argv, **kw: types.SimpleNamespace(stdout=stdout)


def test_last_commit_ts_parses_git_output():
    assert S.last_commit_ts("/repo", "o/r#80", run=_run_returning("1700000123\n")) == 1700000123


def test_last_commit_ts_zero_without_repo_or_issue():
    assert S.last_commit_ts(None, "o/r#80", run=_run_returning("123")) == 0
    assert S.last_commit_ts("/repo", "no-issue", run=_run_returning("123")) == 0


def test_last_commit_ts_handles_empty_and_errors():
    assert S.last_commit_ts("/repo", "o/r#80", run=_run_returning("")) == 0   # no matching commit

    def boom(argv, **kw):
        raise OSError("git missing")
    assert S.last_commit_ts("/repo", "o/r#80", run=boom) == 0


def test_last_commit_ts_greps_the_issue_number():
    seen = {}

    def capture(argv, **kw):
        seen["argv"] = argv
        return types.SimpleNamespace(stdout="1")
    S.last_commit_ts("/repo", "o/r#42", run=capture)
    assert "--grep" in seen["argv"]
    assert any("#42" in a for a in seen["argv"])


# -- owner_hub_ts ----------------------------------------------------------

def test_owner_hub_ts_finds_owner_messages():
    s = HubStore(tempfile.mkdtemp(prefix="stall-")); s.init(token="t")
    s.post_channel("general", "on it", "alice", "alice")
    assert S.owner_hub_ts(s, "alice") > 0
    assert S.owner_hub_ts(s, "bob") == 0
    assert S.owner_hub_ts(s, None) == 0


# -- detect_stalled (pure) -------------------------------------------------

def _running():
    return [
        {"id": "t1", "claimed_by": "alice"},   # recent activity
        {"id": "t2", "claimed_by": "bob"},     # stalled
        {"id": "t3", "claimed_by": None},      # unowned -> ignored
    ]


def _acts(map_):
    return lambda t: map_[t["id"]]


def test_detects_only_stalled_owned_tasks():
    acts = {"t1": NOW - 10, "t2": NOW - 9999, "t3": NOW}
    to_alert, active = S.detect_stalled(_running(), NOW, THRESH, set(), _acts(acts))
    assert [t["id"] for t in to_alert] == ["t2"]
    assert active == {"t1"}                     # t3 unowned, never considered


def test_dedup_does_not_realert():
    acts = {"t1": NOW - 10, "t2": NOW - 9999, "t3": NOW}
    to_alert, _ = S.detect_stalled(_running(), NOW, THRESH, {"t2"}, _acts(acts))
    assert to_alert == []                        # t2 already alerted


def test_rearm_when_activity_resumes():
    # t2 now active again -> reported in `active` so caller clears it from alerted
    acts = {"t1": NOW, "t2": NOW - 5, "t3": NOW}
    _, active = S.detect_stalled(_running(), NOW, THRESH, {"t2"}, _acts(acts))
    assert "t2" in active


def test_threshold_boundary_not_stalled():
    acts = {"t1": NOW, "t2": NOW - THRESH, "t3": NOW}   # exactly at threshold = active
    to_alert, active = S.detect_stalled(_running(), NOW, THRESH, set(), _acts(acts))
    assert to_alert == [] and "t2" in active


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
