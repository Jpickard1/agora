"""Self-update (issue #69): pull the latest Agora into this install and apply it.

`hubcli update` runs do_update(): git pull (--ff-only) the checkout where Agora
is installed, refresh the editable install (`pip install -e .`), and restart the
server so the new code is live. The restart is done IN PLACE (kill + immediately
relaunch from the live tree) and then POLLS /api/health — success is only reported
once the server is serving again; otherwise it fails loudly with recovery steps.
Because it brings the server back itself, the supervisor's health-restart never
has to fire, so the two don't race. It prints the old→new commit, is a safe no-op
if already current, and gives a clear message if this isn't a git checkout.

The supervisor can optionally run this periodically (see supervisor.py + the
`selfupdate` config block) so pushed changes propagate to every install.

Design: each external step is its own module-level function so tests can mock
git/pip/tmux without touching the real repo or a live server.
"""

from __future__ import annotations

import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SERVER_SESSION = "agora-server"
DEFAULT_PORT = 8910


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


def resolve_hub_root() -> str:
    """The server's data root (AGENT_HUB_ROOT / pointer), not the code checkout.
    Late-imported to avoid a circular import with cli at module load."""
    from .cli import resolve_root
    return str(resolve_root(None))


def serve_command(hub_root: str, port: int = DEFAULT_PORT,
                  python: str | None = None) -> str:
    """The exact command `hubcli up` uses to launch the server — same env, port,
    and log file — so a self-update restart is identical to a normal start."""
    py = python or sys.executable
    return (f"AGENT_HUB_ROOT={hub_root} {py} -m agenthub.cli serve "
            f"--host 127.0.0.1 --port {port} > {hub_root}/server.log 2>&1")


def _tmux_restart(session: str, command: str, cwd: str | None = None) -> None:
    """Kill + start the session in one shot (mirrors cli._tmux_start) so there's no
    'killed, waiting for the supervisor to respawn' gap. `cwd` pins the start dir to
    the live code tree."""
    subprocess.run(["tmux", "kill-session", "-t", session],
                   stderr=subprocess.DEVNULL, check=False)
    args = ["tmux", "new-session", "-d", "-s", session]
    if cwd:
        args += ["-c", str(cwd)]
    args.append(command)
    subprocess.run(args, check=False)


def restart_server(session: str = SERVER_SESSION, *, hub_root: str | None = None,
                   port: int = DEFAULT_PORT, cwd: str | None = None,
                   python: str | None = None) -> bool:
    """Restart the server IN PLACE — kill its tmux session and immediately start a
    fresh one on the new code, from the live-tree `cwd`. This replaces the old
    'kill and hope the supervisor respawns it' behaviour, which left :PORT down for
    several seconds. Returns True if the (re)start command was issued.

    Because we bring the server back ourselves (and the caller then waits for
    health), the supervisor's own health-restart never has to fire — so the two
    don't race for the agora-server session."""
    if hub_root is None:
        try:
            hub_root = resolve_hub_root()
        except Exception:
            return False
    try:
        _tmux_restart(session, serve_command(hub_root, port, python), cwd=cwd)
        return True
    except Exception:
        return False


def server_health_ok(port: int = DEFAULT_PORT, timeout: float = 3.0) -> bool:
    """One health probe: True iff GET /api/health returns HTTP 200."""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def wait_for_health(port: int = DEFAULT_PORT, *, timeout: float = 20.0,
                    interval: float = 0.5) -> bool:
    """Poll /api/health until it returns 200 or `timeout` seconds elapse. This is
    what lets a restart REPORT SUCCESS ONLY once the server is actually serving
    again, instead of optimistically assuming it came back."""
    deadline = time.monotonic() + timeout
    while True:
        if server_health_ok(port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def do_update(root: Path | str | None = None, *, restart: bool = True,
              check_only: bool = False,
              server_session: str = SERVER_SESSION,
              hub_root: str | None = None, port: int = DEFAULT_PORT,
              health_timeout: float = 20.0) -> dict:
    """Pull + apply the latest Agora. Returns a result dict with a human `message`
    and structured fields (ok/git/changed/old/new/restarted/healthy).

    After a restart it POLLS /api/health and only reports success once the server
    is serving again (`healthy`); if it doesn't come back, `ok` is False and the
    message gives recovery steps."""
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
    msg = f"updated {(old or '?')[:8]} → {(new or '?')[:8]}"
    if pip_rc != 0:
        msg += " (pip refresh reported a warning)"

    if not restart:
        return {"ok": True, "git": True, "changed": True, "old": old, "new": new,
                "pip_ok": pip_rc == 0, "restarted": False, "healthy": None,
                "message": msg + " — restart the server to apply (--no-restart)"}

    if hub_root is None:
        try:
            hub_root = resolve_hub_root()
        except Exception:
            hub_root = None
    restarted = restart_server(server_session, hub_root=hub_root, port=port,
                               cwd=str(root))
    if not restarted:
        return {"ok": True, "git": True, "changed": True, "old": old, "new": new,
                "pip_ok": pip_rc == 0, "restarted": False, "healthy": None,
                "message": msg + f" — could not restart the server (tmux start failed); "
                                 f"start it with: hubcli up"}

    # Only report success once the server is actually serving again.
    healthy = wait_for_health(port, timeout=health_timeout)
    if healthy:
        return {"ok": True, "git": True, "changed": True, "old": old, "new": new,
                "pip_ok": pip_rc == 0, "restarted": True, "healthy": True,
                "message": msg + f" — server back up on :{port} (health OK)"}
    # Restarted but never came back: fail loudly with recovery steps.
    return {"ok": False, "git": True, "changed": True, "old": old, "new": new,
            "pip_ok": pip_rc == 0, "restarted": True, "healthy": False,
            "message": msg + f" — ⚠ server did NOT return on :{port} within "
                             f"{int(health_timeout)}s. Recover: run 'hubcli up', or "
                             f"AGENT_HUB_ROOT={hub_root} hubcli serve --port {port}; "
                             f"check {hub_root}/server.log for the error."}
