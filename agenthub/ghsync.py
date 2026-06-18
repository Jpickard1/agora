"""GitHub <-> task status sync (issue #9).

When a task's status changes, mirror it to the linked GitHub issue: comment the
new status (claimed / running / done / failed / cancelled) and close the issue
when the task is done. Driven by the task's ``ref`` ("owner/repo#number"); a
task without a ref is a silent no-op.

The driver (`GitHubSyncer`) runs inside the supervisor loop and remembers which
(task, status) pairs it already pushed — persisted to disk — so a restart never
re-comments. All decision logic is pure and unit-testable; the only
side-effecting step (`run_actions`) goes through a `gh` runner that tests swap
out, so nothing here touches the network under test.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

# Status transitions we mirror to GitHub. "open" is the default starting state
# and isn't worth a comment.
SYNC_STATUSES = ("claimed", "running", "done", "failed", "cancelled")
# Statuses that should also CLOSE the linked issue.
CLOSE_ON = ("done",)

_REF_RE = re.compile(r"^\s*([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)#(\d+)\s*$")

_STATUS_EMOJI = {
    "claimed": "🙋", "running": "⏳", "done": "✅",
    "failed": "❌", "cancelled": "⊘",
}


def parse_ref(ref: str | None):
    """Parse 'owner/repo#42' -> ('owner', 'repo', 42), or None if not a valid
    issue ref (so callers can treat 'no ref' and 'bad ref' the same — no-op)."""
    if not ref:
        return None
    m = _REF_RE.match(str(ref))
    if not m:
        return None
    return m.group(1), m.group(2), int(m.group(3))


def status_comment(task_id: str, status: str, by: str = "", note: str = "") -> str:
    """The issue-comment body for a task entering `status`."""
    emoji = _STATUS_EMOJI.get(status, "•")
    who = f" by @{by}" if by else ""
    extra = f"\n\n> {note}" if note else ""
    return (f"{emoji} agora task **{task_id}** is now **{status}**{who}.{extra}"
            f"\n\n_(automated by the agora GitHub sync)_")


def plan_sync(ref: str | None, status: str, task_id: str,
              by: str = "", note: str = "") -> list[dict]:
    """Pure planner: the gh actions for a task entering `status`. Empty when
    there's no valid ref or the status isn't one we mirror."""
    parsed = parse_ref(ref)
    if parsed is None or status not in SYNC_STATUSES:
        return []
    owner, repo, number = parsed
    slug = f"{owner}/{repo}"
    actions = [{
        "action": "comment", "repo": slug, "number": number,
        "body": status_comment(task_id, status, by, note),
    }]
    if status in CLOSE_ON:
        actions.append({"action": "close", "repo": slug, "number": number})
    return actions


def gh_args(action: dict) -> list[str]:
    """Translate a planned action into a `gh` argv (no execution)."""
    if action["action"] == "comment":
        return ["gh", "issue", "comment", str(action["number"]),
                "--repo", action["repo"], "--body", action["body"]]
    if action["action"] == "close":
        return ["gh", "issue", "close", str(action["number"]),
                "--repo", action["repo"]]
    raise ValueError(f"unknown action {action['action']!r}")


def _default_runner(argv: list[str]):
    return subprocess.run(argv, check=False,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_actions(actions, runner=None, dry_run: bool = False) -> list[list[str]]:
    """Execute planned actions via `gh`. `runner(argv)` defaults to subprocess.
    Returns the list of argv lists that were (or, when dry_run, would be) run."""
    if runner is None:
        runner = _default_runner
    ran = []
    for a in actions:
        argv = gh_args(a)
        ran.append(argv)
        if not dry_run:
            runner(argv)
    return ran


class GitHubSyncer:
    """Drives task->issue sync from inside the supervisor loop. Idempotent: each
    (task, status) pair is pushed at most once, tracked in a persisted state
    file so restarts don't duplicate comments."""

    def __init__(self, store, state_path: str, enabled: bool = True,
                 runner=None, dry_run: bool = False):
        self.store = store
        self.state_path = state_path
        self.enabled = enabled
        self.runner = runner
        self.dry_run = dry_run
        self._seen = self._load()

    def _load(self) -> dict:
        try:
            with open(self.state_path) as f:
                return {k: set(v) for k, v in json.load(f).items()}
        except Exception:
            return {}

    def _save(self) -> None:
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({k: sorted(v) for k, v in self._seen.items()}, f)
            os.replace(tmp, self.state_path)
        except Exception:
            pass

    def tick(self) -> list[str]:
        """Scan all tasks once; push any not-yet-synced status transitions to
        their linked issue. Returns the task ids synced this tick."""
        if not self.enabled:
            return []
        synced = []
        for t in self.store.list_tasks():
            ref = t.get("ref")
            status = t.get("status")
            if not parse_ref(ref) or status not in SYNC_STATUSES:
                continue
            seen = self._seen.setdefault(t["id"], set())
            if status in seen:
                continue
            evs = t.get("events") or []
            note = evs[-1].get("note", "") if evs else ""
            actions = plan_sync(ref, status, t["id"],
                                by=t.get("claimed_by") or "", note=note)
            if actions:
                run_actions(actions, runner=self.runner, dry_run=self.dry_run)
                seen.add(status)
                synced.append(t["id"])
        if synced:
            self._save()
        return synced
