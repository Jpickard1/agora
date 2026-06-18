"""Tests for the GitHub <-> task syncer (issue #9): ref parsing, action
planning (comment + close-on-done), the gh argv translation, and the
idempotent supervisor-loop driver — all without touching the network (a fake
`gh` runner records calls instead).
Run: python tests/test_ghsync.py"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub import ghsync  # noqa: E402
from agenthub.store import HubStore  # noqa: E402


def fresh():
    s = HubStore(tempfile.mkdtemp(prefix="ghsync-"))
    s.init(token="t")
    return s


class Recorder:
    """A fake `gh` runner that just records the argv lists handed to it."""
    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)


# -- parse_ref -------------------------------------------------------------

def test_parse_ref_valid():
    assert ghsync.parse_ref("Jpickard1/agora#42") == ("Jpickard1", "agora", 42)


def test_parse_ref_none_and_bad():
    assert ghsync.parse_ref(None) is None
    assert ghsync.parse_ref("") is None
    assert ghsync.parse_ref("not-a-ref") is None
    assert ghsync.parse_ref("owner/repo") is None     # no #number
    assert ghsync.parse_ref("owner#5") is None        # no repo


# -- plan_sync -------------------------------------------------------------

def test_plan_comment_only_for_running():
    p = ghsync.plan_sync("o/r#5", "running", "agora-9")
    assert len(p) == 1 and p[0]["action"] == "comment"
    assert p[0]["repo"] == "o/r" and p[0]["number"] == 5


def test_plan_done_comments_and_closes():
    p = ghsync.plan_sync("o/r#5", "done", "agora-9")
    kinds = [a["action"] for a in p]
    assert kinds == ["comment", "close"]


def test_plan_empty_without_ref_or_unsynced_status():
    assert ghsync.plan_sync(None, "done", "x") == []
    assert ghsync.plan_sync("o/r#5", "open", "x") == []


def test_status_comment_mentions_task_and_status():
    body = ghsync.status_comment("agora-9", "done", by="probe", note="shipped")
    assert "agora-9" in body and "done" in body and "probe" in body
    assert "shipped" in body


# -- gh_args ---------------------------------------------------------------

def test_gh_args_comment_and_close():
    c = ghsync.gh_args({"action": "comment", "repo": "o/r", "number": 5, "body": "hi"})
    assert c[:5] == ["gh", "issue", "comment", "5", "--repo"]
    z = ghsync.gh_args({"action": "close", "repo": "o/r", "number": 5})
    assert z == ["gh", "issue", "close", "5", "--repo", "o/r"]


def test_run_actions_dry_run_runs_nothing():
    rec = Recorder()
    actions = ghsync.plan_sync("o/r#5", "done", "x")
    ran = ghsync.run_actions(actions, runner=rec, dry_run=True)
    assert len(ran) == 2          # planned/returned
    assert rec.calls == []        # but nothing executed


# -- GitHubSyncer driver ---------------------------------------------------

def state_path():
    return os.path.join(tempfile.mkdtemp(prefix="ghstate-"), "ghsync_state.json")


def test_syncer_pushes_status_for_referenced_task():
    s = fresh()
    s.create_task("agora-9", title="sync", ref="o/r#5")
    s.claim_task("agora-9", "probe")
    rec = Recorder()
    syncer = ghsync.GitHubSyncer(s, state_path(), runner=rec)
    synced = syncer.tick()
    assert synced == ["agora-9"]
    assert rec.calls and rec.calls[0][:3] == ["gh", "issue", "comment"]


def test_syncer_noop_for_task_without_ref():
    s = fresh()
    s.create_task("agora-x", title="no ref")     # no ref
    s.claim_task("agora-x", "probe")
    rec = Recorder()
    syncer = ghsync.GitHubSyncer(s, state_path(), runner=rec)
    assert syncer.tick() == []
    assert rec.calls == []


def test_syncer_is_idempotent_per_status():
    s = fresh()
    s.create_task("agora-9", title="sync", ref="o/r#5")
    s.claim_task("agora-9", "probe")
    rec = Recorder()
    sp = state_path()
    syncer = ghsync.GitHubSyncer(s, sp, runner=rec)
    syncer.tick()
    n_after_first = len(rec.calls)
    # second tick with no status change → no new calls
    assert syncer.tick() == []
    assert len(rec.calls) == n_after_first


def test_syncer_pushes_each_new_status_and_closes_on_done():
    s = fresh()
    s.create_task("agora-9", title="sync", ref="o/r#5")
    s.claim_task("agora-9", "probe")
    rec = Recorder()
    sp = state_path()
    syncer = ghsync.GitHubSyncer(s, sp, runner=rec)
    syncer.tick()                                  # claimed
    s.update_task("agora-9", "running", by="probe")
    assert syncer.tick() == ["agora-9"]            # running
    s.update_task("agora-9", "done", by="probe")
    assert syncer.tick() == ["agora-9"]            # done
    actions = [c[1:3] for c in rec.calls]
    assert ["issue", "close"] in actions           # closed on done


def test_syncer_state_persists_across_restart():
    s = fresh()
    s.create_task("agora-9", title="sync", ref="o/r#5")
    s.claim_task("agora-9", "probe")
    sp = state_path()
    ghsync.GitHubSyncer(s, sp, runner=Recorder()).tick()
    # a brand-new syncer (simulating a restart) loads the state and re-syncs nothing
    rec2 = Recorder()
    assert ghsync.GitHubSyncer(s, sp, runner=rec2).tick() == []
    assert rec2.calls == []


def test_syncer_disabled_does_nothing():
    s = fresh()
    s.create_task("agora-9", title="sync", ref="o/r#5")
    s.claim_task("agora-9", "probe")
    rec = Recorder()
    syncer = ghsync.GitHubSyncer(s, state_path(), enabled=False, runner=rec)
    assert syncer.tick() == []
    assert rec.calls == []


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
