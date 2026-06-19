"""Tests for the multi-repo task-board syncer (issue #123): repo-list
resolution + back-compat, card normalization, incremental fetch planning, the
incremental merge, grouping/filtering, and the persisted driver — all without
the network (a fake runner returns canned `gh --json` output).
Run: python tests/test_boardsync.py"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub import boardsync as bs  # noqa: E402


# -- normalize_repos (scalar -> list, back-compat) -------------------------

def test_normalize_list():
    assert bs.normalize_repos(["o/a", "o/b"]) == ["o/a", "o/b"]


def test_normalize_scalar_and_env_backcompat():
    assert bs.normalize_repos("o/a") == ["o/a"]            # scalar config
    assert bs.normalize_repos(None, env_repo="o/x") == ["o/x"]  # AGORA_GH_REPO
    # explicit list wins over env
    assert bs.normalize_repos(["o/a"], env_repo="o/x") == ["o/a"]


def test_normalize_dedup_and_drop_invalid():
    assert bs.normalize_repos(["o/a", "o/a", "bad", "x/y/z", "o/b"]) == ["o/a", "o/b"]
    assert bs.normalize_repos([]) == []
    assert bs.normalize_repos(None) == []


def test_valid_slug():
    assert bs.valid_slug("Jpickard1/agora")
    assert not bs.valid_slug("owner")
    assert not bs.valid_slug("a/b/c")
    assert not bs.valid_slug("")


def test_repo_color_deterministic():
    repos = ["o/b", "o/a"]
    # color keyed by SORTED position -> stable regardless of input order
    assert bs.repo_color("o/a", repos) == bs.REPO_PALETTE[0]
    assert bs.repo_color("o/b", repos) == bs.REPO_PALETTE[1]


# -- card_from_issue -------------------------------------------------------

def test_card_from_issue():
    issue = {
        "number": 42, "title": "Add retry", "state": "OPEN",
        "labels": [{"name": "ready"}, {"name": "agent-task"}],
        "author": {"login": "jpic"},
        "assignees": [{"login": "Yi2"}],
        "url": "https://github.com/o/r/issues/42", "updatedAt": "2026-06-19T00:00:00Z",
    }
    c = bs.card_from_issue(issue, "o/r")
    assert c["repo"] == "o/r" and c["number"] == 42 and c["id"] == "o/r#42"
    assert c["state"] == "open" and c["labels"] == ["ready", "agent-task"]
    assert c["author"] == "jpic" and c["assignees"] == ["Yi2"]
    assert c["url"].endswith("/42")


def test_card_url_default_when_missing():
    c = bs.card_from_issue({"number": 7}, "o/r")
    assert c["url"] == "https://github.com/o/r/issues/7"


# -- fetch_args / plan_fetch ----------------------------------------------

def test_fetch_args_incremental():
    base = bs.fetch_args("o/r")
    assert "--repo" in base and "o/r" in base and "--json" in base
    assert "--search" not in base                      # no cursor -> full
    inc = bs.fetch_args("o/r", since="2026-06-19T00:00:00Z")
    assert "--search" in inc and "updated:>=2026-06-19T00:00:00Z" in inc


def test_plan_fetch_per_repo_cursor():
    jobs = bs.plan_fetch(["o/a", "o/b"], {"o/a": "2026-06-01T00:00:00Z"})
    assert [j["repo"] for j in jobs] == ["o/a", "o/b"]
    assert jobs[0]["since"] == "2026-06-01T00:00:00Z"
    assert jobs[1]["since"] is None


# -- merge_cards (incremental) --------------------------------------------

def test_merge_updates_repo_keeps_others():
    existing = [
        {"id": "o/a#1", "repo": "o/a", "number": 1, "state": "open"},
        {"id": "o/b#9", "repo": "o/b", "number": 9, "state": "open"},
    ]
    fetched = [{"id": "o/a#1", "repo": "o/a", "number": 1, "state": "closed"},
               {"id": "o/a#2", "repo": "o/a", "number": 2, "state": "open"}]
    merged = bs.merge_cards(existing, fetched, "o/a")
    ids = {c["id"]: c for c in merged}
    assert ids["o/b#9"]["state"] == "open"          # other repo untouched
    assert ids["o/a#1"]["state"] == "closed"        # updated in place
    assert "o/a#2" in ids                            # new card added
    # this repo's cards sorted by number desc
    mine = [c for c in merged if c["repo"] == "o/a"]
    assert [c["number"] for c in mine] == [2, 1]


# -- filter / group --------------------------------------------------------

def test_filter_and_group():
    cards = [
        {"id": "o/a#1", "repo": "o/a", "state": "open"},
        {"id": "o/a#2", "repo": "o/a", "state": "closed"},
        {"id": "o/b#1", "repo": "o/b", "state": "open"},
    ]
    assert len(bs.filter_cards(cards, repo="o/a")) == 2
    assert len(bs.filter_cards(cards, state="open")) == 2
    assert len(bs.filter_cards(cards, repo="o/a", state="open")) == 1
    g = bs.group_by_repo(cards)
    assert set(g) == {"o/a", "o/b"} and len(g["o/a"]) == 2


# -- BoardSyncer (driver, fake runner) ------------------------------------

class FakeGH:
    """Maps a repo -> canned issue list; records calls. Returns JSON text."""
    def __init__(self, by_repo):
        self.by_repo = by_repo
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        repo = argv[argv.index("--repo") + 1]
        return json.dumps(self.by_repo.get(repo, []))


def _state():
    return os.path.join(tempfile.mkdtemp(prefix="board-"), "board_state.json")


def test_syncer_multi_repo_and_persist():
    fake = FakeGH({
        "o/a": [{"number": 1, "title": "A1", "state": "OPEN", "labels": []}],
        "o/b": [{"number": 5, "title": "B5", "state": "OPEN", "labels": []}],
    })
    path = _state()
    s = bs.BoardSyncer(path, repos=["o/a", "o/b"], runner=fake)
    cards = s.sync()
    assert {c["id"] for c in cards} == {"o/a#1", "o/b#5"}
    assert len(fake.calls) == 2                       # one gh call per repo
    # cursors advanced for both repos; snapshot persisted
    s2 = bs.BoardSyncer(path, repos=["o/a", "o/b"], runner=fake)
    assert {c["id"] for c in s2.cards()} == {"o/a#1", "o/b#5"}
    assert s2._cursors.get("o/a") and s2._cursors.get("o/b")


def test_syncer_incremental_keeps_unrefetched():
    # First sync sees a/1; second sync (incremental) returns only the changed
    # slice for a, but the old card must persist.
    fake = FakeGH({"o/a": [{"number": 1, "title": "A1", "state": "OPEN"}]})
    path = _state()
    s = bs.BoardSyncer(path, repos=["o/a"], runner=fake)
    s.sync()
    fake.by_repo["o/a"] = [{"number": 2, "title": "A2", "state": "OPEN"}]  # only new
    cards = s.sync()
    assert {c["id"] for c in cards} == {"o/a#1", "o/a#2"}   # #1 retained


def test_syncer_set_repos_drops_removed():
    fake = FakeGH({"o/a": [{"number": 1, "state": "OPEN"}],
                   "o/b": [{"number": 5, "state": "OPEN"}]})
    path = _state()
    s = bs.BoardSyncer(path, repos=["o/a", "o/b"], runner=fake)
    s.sync()
    s.set_repos(["o/a"])                               # remove o/b at runtime
    ids = {c["id"] for c in s.cards()}
    assert ids == {"o/a#1"} and "o/b" not in s._cursors


def test_syncer_bad_repo_not_fatal():
    fake = FakeGH({"o/a": [{"number": 1, "state": "OPEN"}]})   # o/bad -> []
    s = bs.BoardSyncer(_state(), repos=["o/a", "o/bad"], runner=fake)
    cards = s.sync()
    assert {c["id"] for c in cards} == {"o/a#1"}        # bad repo skipped, not fatal


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print(f"  ok  {fn.__name__}")
    print(f"\n{passed}/{len(fns)} boardsync tests passed")
