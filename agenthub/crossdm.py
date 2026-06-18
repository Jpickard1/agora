"""Cross-user direct messages (issue #88) — via a group-writable shared area.

Private roots are 0700, so user A can't write user B's inbox. Cross-user DMs are
dropped in `<shared_root>/dm/<recipient_user>/` (group-writable); each user's own
server drains its own subdir into its private inbox (it runs as that user, so it
can read it). Lock-free maildir model; dedup by message id.

Parameterized by `shared_root` (stub now, store.shared_root after #14). Recipient
addressing resolves via the participants registry (#86).
"""
from __future__ import annotations

import uuid
from pathlib import Path

from .store import _atomic_write_json, _read_json, _safe_name, _now, _msg_filename
from . import participants


def _dm_dir(shared_root, recipient_user) -> Path:
    return Path(shared_root) / "dm" / _safe_name(recipient_user)


def resolve_recipient(shared_root, target):
    """`target` is 'user:agent' (explicit) or 'agent' (user resolved via the
    participants registry). Returns (user_or_None, agent)."""
    if ":" in target:
        user, agent = target.split(":", 1)
        return user.strip() or None, agent.strip()
    return participants.lookup_user(shared_root, target), target


def post_cross_user_dm(shared_root, target, from_user, from_agent, text, now=None):
    """Drop a DM for `target` into the recipient-user's shared dm subdir.
    Returns {ok, msg|reason}. Fails cleanly if the recipient user is unknown."""
    now = _now() if now is None else now
    to_user, to_agent = resolve_recipient(shared_root, target)
    if not to_user:
        return {"ok": False, "reason": f"unknown recipient user for '{target}' "
                f"(not in participants registry)"}
    msg = {"id": uuid.uuid4().hex, "ts": now,
           "to_user": to_user, "to": to_agent,
           "from_user": from_user, "author": from_agent, "author_name": from_agent,
           "text": text, "kind": "cross_user_dm"}
    _atomic_write_json(_dm_dir(shared_root, to_user) / _msg_filename(now), msg)
    return {"ok": True, "msg": msg}


def drain_shared_dms(shared_root, my_user, since_ts=0.0, seen=None):
    """Recipient side: new DMs for `my_user` (ts >= since_ts, id not in seen),
    chronological. Returns (messages, new_since_ts, seen). The caller writes each
    into its private inbox; dedup by id makes repeated drains idempotent."""
    seen = set(seen or ())
    d = _dm_dir(shared_root, my_user)
    if not d.exists():
        return [], since_ts, seen
    out, newest = [], since_ts
    for f in sorted(d.glob("*.json")):
        m = _read_json(f)
        if not m or m.get("ts", 0) < since_ts or m.get("id") in seen:
            continue
        out.append(m)
        seen.add(m["id"])
        newest = max(newest, m.get("ts", 0))
    return out, newest, seen
