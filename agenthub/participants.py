"""Participants registry (issue #86) — who has agents on the SHARED hub.

Lives in the shared root only (`<shared_root>/participants/<user>/<agent>.json`),
one file per (user, agent) so concurrent agents never read-modify-write the same
file (lock-free, like the message store). Surfaces "the list of all people who
have agents working on it" (jpic).

Parameterized by `shared_root` so it's built/tested now against a stub dir and
wired to the store's real shared_root once #14 (dual-root core) lands.
"""
from __future__ import annotations

from pathlib import Path

from .store import _atomic_write_json, _read_json, _safe_name, _now


def _agent_path(shared_root, user, agent) -> Path:
    return Path(shared_root) / "participants" / _safe_name(user) / f"{_safe_name(agent)}.json"


def register_participant(shared_root, user, agent, host="", caps=None, now=None):
    """Upsert one agent's presence under its user. Called on connect + heartbeat.
    Race-free: each (user,agent) owns its own file."""
    now = _now() if now is None else now
    rec = {"user": user, "agent": agent, "host": host,
           "capabilities": list(caps or []), "last_seen": now}
    _atomic_write_json(_agent_path(shared_root, user, agent), rec)
    return rec


def list_participants(shared_root, online_window=30.0, now=None):
    """All participants grouped by user, each agent annotated online/age.
    Returns [{user, agents:[{name,host,last_seen,age,online}], online_agents,
    online}], sorted by user."""
    now = _now() if now is None else now
    base = Path(shared_root) / "participants"
    if not base.exists():
        return []
    by_user: dict[str, list] = {}
    for f in base.glob("*/*.json"):
        rec = _read_json(f)
        if not rec or not rec.get("user"):
            continue
        by_user.setdefault(rec["user"], []).append(rec)
    out = []
    for user, recs in by_user.items():
        agents = []
        for r in recs:
            ls = r.get("last_seen", 0)
            agents.append({"name": r.get("agent"), "host": r.get("host", ""),
                           "capabilities": r.get("capabilities", []),
                           "last_seen": ls, "age": now - ls,
                           "online": (now - ls) <= online_window})
        agents.sort(key=lambda a: a["name"] or "")
        out.append({"user": user, "agents": agents,
                    "online_agents": sum(1 for a in agents if a["online"]),
                    "online": any(a["online"] for a in agents)})
    out.sort(key=lambda p: p["user"] or "")
    return out


def lookup_user(shared_root, agent_name):
    """Which user owns `agent_name` (for cross-user DM addressing). None if
    unknown. Ignores online-ness (window=inf)."""
    for p in list_participants(shared_root, online_window=float("inf")):
        for a in p["agents"]:
            if a["name"] == agent_name:
                return p["user"]
    return None
