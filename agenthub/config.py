"""Configuration resolution for Agent Hub.

Everything is discoverable from two pieces of information:

  * HUB_ROOT  -- the shared-filesystem directory that holds the hub.
  * token     -- shared secret stored inside HUB_ROOT/config.json.

Resolution order for the hub root:
  1. explicit argument
  2. $AGENT_HUB_ROOT
  3. ~/.agent-hub-path  (a tiny file containing the path, written by `hubcli init`)
  4. ~/.agent-hub       (default local fallback)

The token is read from HUB_ROOT/config.json, but $AGENT_HUB_TOKEN overrides it
(useful for read-only clients that only know the secret, not the path).
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ROOT = "~/.agent-hub"
POINTER_FILE = Path("~/.agent-hub-path").expanduser()


def resolve_root(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("AGENT_HUB_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    if POINTER_FILE.exists():
        try:
            p = POINTER_FILE.read_text(encoding="utf-8").strip()
            if p:
                return Path(p).expanduser().resolve()
        except OSError:
            pass
    return Path(DEFAULT_ROOT).expanduser().resolve()


def read_pointer() -> str | None:
    """The hub root currently recorded in ~/.agent-hub-path, or None."""
    if POINTER_FILE.exists():
        try:
            p = POINTER_FILE.read_text(encoding="utf-8").strip()
            return p or None
        except OSError:
            return None
    return None


def write_pointer(root: Path) -> None:
    """Low-level, UNCONDITIONAL write of ~/.agent-hub-path. Prefer set_pointer()
    in user-facing flows so an existing pointer to a different hub isn't silently
    clobbered (issue #39)."""
    try:
        POINTER_FILE.write_text(str(root), encoding="utf-8")
    except OSError:
        pass


def set_pointer(root: Path, force: bool = False) -> tuple[str, str | None]:
    """Safely record the hub root in ~/.agent-hub-path (issue #39).

    Returns (action, previous) where action is one of:
      "written"     — no pointer existed; we created it.
      "unchanged"   — a pointer already pointed here; left as-is.
      "overwritten" — a different pointer existed and force=True; replaced it.
      "refused"     — a DIFFERENT pointer existed and force=False; left untouched
                      so a throwaway/test root can't hijack the shared pointer.
    """
    target = str(Path(root).expanduser().resolve())
    existing = read_pointer()
    if existing is None:
        write_pointer(target)
        return ("written", None)
    existing_resolved = str(Path(existing).expanduser().resolve())
    if existing_resolved == target:
        return ("unchanged", existing)
    if force:
        write_pointer(target)
        return ("overwritten", existing)
    return ("refused", existing)


def resolve_token(store_token: str | None) -> str | None:
    return os.environ.get("AGENT_HUB_TOKEN") or store_token
