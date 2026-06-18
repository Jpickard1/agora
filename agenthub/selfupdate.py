"""Self-update (issue #69): pull the latest Agora into this install and apply it.

`hubcli update` runs do_update(): git pull (--ff-only) the checkout where Agora
is installed, refresh the editable install (`pip install -e .`), and restart the
server so the new code is live. It prints the old→new commit, is a safe no-op if
already current, and gives a clear message if this isn't a git checkout.

The supervisor can optionally run this periodically (see supervisor.py + the
`selfupdate` config block) so pushed changes propagate to every install.

Design: each external step is its own module-level function so tests can mock
git/pip/tmux without touching the real repo or a live server.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def repo_root() -> Path:
    """The Agora checkout root = the parent of the agenthub package directory."""
    return Path(__file__).resolve().parent.parent


def is_git_checkout(root: Path) -> bool:
    return (Path(root) / ".git").exists()


def _git(root: Path, *args: str) -> tuple[int, str]:
    p = subprocess.run(["git", "-C", str(root), *args],
                       capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def current_commit(root: Path) -> str | None:
    rc, out = _git(root, "rev-parse", "HEAD")
    return out if rc == 0 else None


def commits_behind(root: Path) -> int:
    """How many commits the upstream is ahead (after a fetch). 0 if unknown."""
    _git(root, "fetch", "--quiet")
    rc, out = _git(root, "rev-list", "--count", "HEAD..@{u}")
    return int(out) if rc == 0 and out.isdigit() else 0


def git_pull(root: Path) -> tuple[int, str]:
    return _git(root, "pull", "--ff-only")


def pip_install_editable(root: Path) -> tuple[int, str]:
    p = subprocess.run([sys.executable, "-m", "pip", "install", "-e", str(root), "-q"],
                       capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def restart_server(session: str = "agora-server") -> bool:
    """Kill the server's tmux session so the supervisor (or `hubcli up`) respawns
    it on the freshly-pulled code. Returns True if a session was killed."""
    try:
        has = subprocess.run(["tmux", "has-session", "-t", session],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL).returncode == 0
        if has:
            subprocess.run(["tmux", "kill-session", "-t", session],
                           stderr=subprocess.DEVNULL, check=False)
            return True
    except Exception:
        pass
    return False


def do_update(root: Path | str | None = None, *, restart: bool = True,
              check_only: bool = False,
              server_session: str = "agora-server") -> dict:
    """Pull + apply the latest Agora. Returns a result dict with a human `message`
    and structured fields (ok/git/changed/old/new/restarted)."""
    root = Path(root) if root else repo_root()
    if not is_git_checkout(root):
        return {"ok": False, "git": False, "changed": False,
                "message": f"{root} is not a git checkout — can't self-update "
                           "(installed from a non-git copy?). Re-install from a clone."}

    old = current_commit(root)

    if check_only:
        behind = commits_behind(root)
        return {"ok": True, "git": True, "changed": False, "check_only": True,
                "commit": old, "behind": behind,
                "message": (f"{behind} new commit(s) upstream — run 'hubcli update'"
                            if behind else f"already up to date at {(old or '?')[:8]}")}

    rc, out = git_pull(root)
    if rc != 0:
        return {"ok": False, "git": True, "changed": False,
                "message": f"git pull failed: {out}"}

    new = current_commit(root)
    if old == new:
        return {"ok": True, "git": True, "changed": False, "commit": old,
                "message": f"already up to date at {(old or '?')[:8]}"}

    pip_rc, _ = pip_install_editable(root)
    restarted = restart_server(server_session) if restart else False
    msg = f"updated {(old or '?')[:8]} → {(new or '?')[:8]}"
    if pip_rc != 0:
        msg += " (pip refresh reported a warning)"
    if restarted:
        msg += " — server restarting on new code"
    elif restart:
        msg += " — restart the server to apply (no agora-server tmux found)"
    else:
        msg += " — restart the server to apply (--no-restart)"
    return {"ok": True, "git": True, "changed": True, "old": old, "new": new,
            "pip_ok": pip_rc == 0, "restarted": restarted, "message": msg}
