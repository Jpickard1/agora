"""Bridge delivery transports (issue #15 — cross-platform support).

The bridge needs to hand an incoming hub line to the local agent. On Unix that's
done by injecting into the agent's tmux pane (`tmux send-keys`). Windows has no
tmux, so this module abstracts delivery behind a tiny transport interface and
adds a **file** transport that simply appends each line to a local inbox file the
agent (or the user) can tail/poll — no tmux required, works anywhere.

Transports are intentionally minimal:
  deliver(line) -> None     # hand one '[HUB ...]: ...' line to the agent
  busy() -> bool            # is the agent mid-turn? (file/stdout: never)

Selection is platform-aware: tmux when it's available and we have a pane, else
the file transport (the sensible default on Windows or any tmux-less host).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_windows() -> bool:
    return os.name == "nt" or sys.platform.startswith("win")


def hostname() -> str:
    """Portable short hostname (os.uname() is Unix-only)."""
    import socket
    return socket.gethostname().split(".")[0]


def choose_transport(explicit: str | None, has_tmux: bool, has_pane: bool) -> str:
    """Pick a transport kind: 'tmux' | 'file' | 'stdout'.

    - explicit ('tmux'/'file'/'stdout') always wins (validated by the caller).
    - else tmux when it's usable (installed AND we have a pane to inject into);
    - else 'file' (the cross-platform default — Windows included)."""
    if explicit and explicit != "auto":
        return explicit
    if has_tmux and has_pane:
        return "tmux"
    return "file"


class StdoutTransport:
    """Print lines to stdout (the old no-pane fallback)."""
    kind = "stdout"

    def deliver(self, line: str) -> None:
        print(line, flush=True)

    def busy(self) -> bool:
        return False


class FileTransport:
    """Append each delivered line to an inbox file the agent can tail/poll.

    This is the Windows / no-tmux delivery path: instead of injecting into a
    terminal, the bridge writes lines the agent reads from a file. Each line is
    newline-terminated; writes are append-only so nothing is lost."""
    kind = "file"

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def deliver(self, line: str) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")

    def busy(self) -> bool:
        # A polled file inbox is never "mid-turn"; deliver immediately.
        return False


class TmuxTransport:
    """Inject lines into the agent's tmux pane (the Unix default).

    The actual tmux calls live in bridge.py (capture/inject); this wraps them so
    the bridge loop can treat all transports uniformly."""
    kind = "tmux"

    def __init__(self, pane, inject_fn, busy_fn):
        self.pane = pane
        self._inject = inject_fn
        self._busy = busy_fn

    def deliver(self, line: str) -> None:
        self._inject(self.pane, line)

    def busy(self) -> bool:
        return bool(self._busy())


def default_inbox_path(root, name) -> Path:
    """Where a file transport writes by default: <hub-root>/inbox-<name>.txt."""
    return Path(root) / f"inbox-{name}.txt"
