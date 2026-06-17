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


def write_pointer(root: Path) -> None:
    """Remember the chosen hub root so future CLI calls find it automatically."""
    try:
        POINTER_FILE.write_text(str(root), encoding="utf-8")
    except OSError:
        pass


def resolve_token(store_token: str | None) -> str | None:
    return os.environ.get("AGENT_HUB_TOKEN") or store_token
