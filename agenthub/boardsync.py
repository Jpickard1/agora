"""Multi-repo task-board sync (issue #123).

Pull issues/PRs from **any number of GitHub repos** and present them as one
board, grouped/filterable by source repo. This generalizes the single
hard-wired ``AGORA_GH_REPO`` to a *list* of repos (``task_board.repos`` in
config.json), keeping single-repo back-compat.

Design mirrors ``ghsync.py``: all decision logic is **pure and unit-testable**;
the only side-effecting step (fetching via ``gh``) goes through a swappable
``runner`` so tests never touch the network. State (per-repo incremental cursor
+ the card snapshot) is persisted to disk so a restart resumes incrementally
instead of re-paging every repo from scratch.

Vocabulary:
  * **repo slug** — "owner/name".
  * **card** — a normalized board item: one issue/PR from one repo.

Out of scope for v1 (per #123): cross-repo dependency links; webhooks (we poll).
"""

from __future__ import annotations

import json
import re
import subprocess
import time

# Fields we ask `gh issue list` for (one paged call per repo, one auth).
_GH_JSON_FIELDS = "number,title,state,labels,url,updatedAt,author,assignees"
_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

# A small, stable palette so the UI can color-code by repo deterministically
# without storing a color per repo (index into this by sorted-repo position).
REPO_PALETTE = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd",
                "#ff7f0e", "#17becf", "#8c564b", "#e377c2"]


# ---------------------------------------------------------------------------
# repo-list resolution (scalar -> list, with single-repo back-compat)
# ---------------------------------------------------------------------------
def normalize_repos(cfg_repos=None, env_repo: str | None = None) -> list[str]:
    """Resolve the board's repo list from config + env, preserving order and
    dropping dups/invalid slugs.

    Precedence: an explicit ``task_board.repos`` list (config) wins; otherwise
    fall back to the single ``AGORA_GH_REPO`` env (back-compat). Accepts a
    scalar string for ``cfg_repos`` too (treated as one repo).
    """
    raw: list[str] = []
    if cfg_repos:
        raw = [cfg_repos] if isinstance(cfg_repos, str) else list(cfg_repos)
    elif env_repo:
        raw = [env_repo]
    out: list[str] = []
    for r in raw:
        slug = (r or "").strip()
        if _SLUG_RE.match(slug) and slug not in out:
            out.append(slug)
    return out


def valid_slug(slug: str) -> bool:
    return bool(_SLUG_RE.match((slug or "").strip()))


def repo_color(repo: str, repos: list[str]) -> str:
    """Deterministic color for a repo given the (sorted) board repo set."""
    order = sorted(set(repos))
    try:
        return REPO_PALETTE[order.index(repo) % len(REPO_PALETTE)]
    except ValueError:
        return REPO_PALETTE[abs(hash(repo)) % len(REPO_PALETTE)]


# ---------------------------------------------------------------------------
# card normalization
# ---------------------------------------------------------------------------
def card_from_issue(issue: dict, repo: str) -> dict:
    """Normalize one `gh issue list --json` record into a board card. Stable
    schema the UI/API consume; carries its source `repo` for grouping/filter."""
    labels = [l.get("name") for l in (issue.get("labels") or []) if l.get("name")]
    author = (issue.get("author") or {}).get("login", "")
    assignees = [a.get("login") for a in (issue.get("assignees") or []) if a.get("login")]
    num = issue.get("number")
    return {
        "repo": repo,
        "number": num,
        "id": f"{repo}#{num}",                 # globally unique across repos
        "title": issue.get("title", ""),
        "state": (issue.get("state") or "").lower(),
        "labels": labels,
        "assignees": assignees,
        "author": author,
        "url": issue.get("url", f"https://github.com/{repo}/issues/{num}"),
        "updated_at": issue.get("updatedAt", ""),
    }


# ---------------------------------------------------------------------------
# fetch planning (pure) — one paged `gh` call per repo, rate-limit aware
# ---------------------------------------------------------------------------
def fetch_args(repo: str, *, state: str = "all", limit: int = 200,
               since: str | None = None) -> list[str]:
    """`gh` argv to list a repo's issues as JSON. Incremental when `since`
    (ISO-8601) is given — only issues updated at/after the cursor — which keeps
    each poll small and the total API cost bounded across N repos."""
    argv = ["gh", "issue", "list", "--repo", repo,
            "--state", state, "--limit", str(limit),
            "--json", _GH_JSON_FIELDS]
    if since:
        # gh search syntax; `updated:>=DATE` narrows to the changed slice.
        argv += ["--search", f"updated:>={since}"]
    return argv


def plan_fetch(repos: list[str], cursors: dict | None = None, *,
               limit: int = 200) -> list[dict]:
    """Pure planner: one fetch job per repo, each carrying its incremental
    cursor. Returns [{repo, argv, since}] — the driver runs them in order."""
    cursors = cursors or {}
    jobs = []
    for repo in repos:
        since = cursors.get(repo)
        jobs.append({"repo": repo, "since": since,
                     "argv": fetch_args(repo, since=since, limit=limit)})
    return jobs


def merge_cards(existing: list[dict], fetched: list[dict], repo: str) -> list[dict]:
    """Incremental merge: replace `repo`'s cards that were re-fetched (by id),
    keep other repos untouched, keep `repo`'s un-refetched cards (incremental
    syncs only return the changed slice). Closed issues that reappear are
    updated in place. Deterministic order: other repos as-was, then this repo's
    cards sorted by number desc (newest first)."""
    by_id = {c["id"]: c for c in existing}
    for c in fetched:
        by_id[c["id"]] = c
    others = [c for c in by_id.values() if c["repo"] != repo]
    mine = [c for c in by_id.values() if c["repo"] == repo]
    mine.sort(key=lambda c: (c.get("number") or 0), reverse=True)
    return others + mine


# ---------------------------------------------------------------------------
# grouping / filtering (pure) — for the API/UI
# ---------------------------------------------------------------------------
def filter_cards(cards: list[dict], *, repo: str | None = None,
                 state: str | None = None) -> list[dict]:
    out = cards
    if repo:
        out = [c for c in out if c["repo"] == repo]
    if state:
        out = [c for c in out if c.get("state") == state.lower()]
    return out


def group_by_repo(cards: list[dict]) -> dict[str, list[dict]]:
    """{repo: [cards]} preserving first-seen repo order."""
    groups: dict[str, list[dict]] = {}
    for c in cards:
        groups.setdefault(c["repo"], []).append(c)
    return groups


# ---------------------------------------------------------------------------
# the driver
# ---------------------------------------------------------------------------
def _default_runner(argv: list[str]) -> str:
    """Run `gh ... --json ...` and return stdout (JSON text). Empty on failure
    so one bad repo never breaks the whole board sync."""
    try:
        r = subprocess.run(argv, check=False, capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else ""
    except OSError:
        return ""


class BoardSyncer:
    """Drives multi-repo board sync. Persists per-repo cursors + the card
    snapshot so syncs are incremental across restarts. `runner(argv)->stdout`
    is swappable (tests pass a fake returning canned JSON); the default shells
    out to `gh`. Rate-limit aware: one `gh` (one auth) per repo, paged via
    --limit, optional inter-repo sleep to spread bursts across many repos."""

    def __init__(self, state_path: str, repos: list[str] | None = None,
                 runner=None, limit: int = 200, sleep_between: float = 0.0):
        self.state_path = state_path
        self.repos = list(repos or [])
        self.runner = runner or _default_runner
        self.limit = limit
        self.sleep_between = sleep_between
        st = self._load()
        self._cards: list[dict] = st.get("cards", [])
        self._cursors: dict = st.get("cursors", {})

    def _load(self) -> dict:
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"cards": self._cards, "cursors": self._cursors,
                           "updated": _now_iso()}, f)
            import os
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def set_repos(self, repos: list[str]) -> None:
        """Update the tracked repo set at runtime. Drops cards/cursors for any
        repo no longer tracked (so removing a repo clears it from the board)."""
        self.repos = list(repos)
        keep = set(self.repos)
        self._cards = [c for c in self._cards if c["repo"] in keep]
        self._cursors = {r: v for r, v in self._cursors.items() if r in keep}
        self._save()

    def sync(self) -> list[dict]:
        """Incrementally fetch every tracked repo and update the snapshot.
        Returns the full card list. One repo failing (empty stdout) is skipped,
        not fatal."""
        for job in plan_fetch(self.repos, self._cursors, limit=self.limit):
            repo = job["repo"]
            out = self.runner(job["argv"])
            if not out:
                continue
            try:
                issues = json.loads(out)
            except ValueError:
                continue
            fetched = [card_from_issue(i, repo) for i in issues]
            self._cards = merge_cards(self._cards, fetched, repo)
            # advance the cursor to "now" so the next poll only sees newer changes.
            self._cursors[repo] = _now_iso()
            if self.sleep_between:
                time.sleep(self.sleep_between)
        self._save()
        return self._cards

    def cards(self, *, repo: str | None = None, state: str | None = None) -> list[dict]:
        return filter_cards(self._cards, repo=repo, state=state)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
