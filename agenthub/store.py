"""Shared-filesystem message store for Agent Hub.

The store is the single source of truth and lives entirely on a shared
filesystem (e.g. NFS) so that agents on any server can read/write it without
talking to a central network service.

Design goals:
  * No file locking. NFS lock support is unreliable, so we avoid it entirely.
  * Concurrent multi-host writers. Each message is written as its own
    uniquely-named file using a write-temp-then-atomic-rename pattern
    (maildir style). Files are immutable once written.
  * Cheap chronological reads. Filenames are prefixed with a zero-padded
    microsecond timestamp so a lexical directory sort is a time sort.

On-disk layout (under HUB_ROOT)::

    config.json                      shared token + metadata
    channels/<channel>/meta.json     channel metadata
    channels/<channel>/messages/     one *.json file per message
    inbox/<agent_id>/                directed messages ("DMs"/instructions)
    agents/<agent_id>.json           agent registration + presence
"""

from __future__ import annotations

import json
import os
import socket
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> float:
    return time.time()


def _safe_name(name: str) -> str:
    """Sanitise a channel/agent identifier for use as a directory name."""
    keep = "-_.@"
    cleaned = "".join(c if (c.isalnum() or c in keep) else "-" for c in name.strip())
    cleaned = cleaned.strip("-.") or "unnamed"
    return cleaned[:128]


def _msg_filename(ts: float) -> str:
    # 20-digit microsecond timestamp keeps lexical == chronological until year 5138.
    return f"{int(ts * 1_000_000):020d}-{uuid.uuid4().hex[:12]}.json"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically: temp file in the same dir, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    text = json.dumps(data, ensure_ascii=False, indent=2)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)  # atomic on POSIX within the same directory


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        # A partially written or vanished file: skip it. Writers use atomic
        # rename so a fully-named *.json file should always be complete, but we
        # stay defensive against NFS caching quirks.
        return None


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Message:
    id: str
    ts: float
    text: str
    author: str                 # stable id, e.g. "trainer-gpu01-12345" or "human:jpic"
    author_name: str            # display name
    author_kind: str = "agent"  # agent | human | system
    channel: str | None = None  # set for channel messages
    to: str | None = None       # set for directed messages (inbox)
    host: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class HubStore:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root).expanduser().resolve()
        self.channels_dir = self.root / "channels"
        self.inbox_dir = self.root / "inbox"
        self.agents_dir = self.root / "agents"
        self.broadcast_dir = self.root / "broadcast"
        self.config_path = self.root / "config.json"

    # -- lifecycle ---------------------------------------------------------

    def init(self, token: str | None = None) -> dict[str, Any]:
        """Create the hub directory tree and config if missing. Idempotent."""
        for d in (self.channels_dir, self.inbox_dir, self.agents_dir,
                  self.broadcast_dir):
            d.mkdir(parents=True, exist_ok=True)
        cfg = self.get_config()
        if cfg is None:
            cfg = {
                "token": token or uuid.uuid4().hex,
                "created": _now(),
                "version": 1,
                # Retention is OFF by default (no surprise data loss). Set
                # keep_last and/or max_age_days to enable the server's auto-pruner.
                "retention": {
                    "keep_last": None,
                    "max_age_days": None,
                    "interval_sec": 3600,
                    "archive": True,
                },
            }
            _atomic_write_json(self.config_path, cfg)
        # Always make sure a default channel exists.
        self.ensure_channel("general", description="Default channel for all agents")
        return cfg

    def get_config(self) -> dict[str, Any] | None:
        return _read_json(self.config_path)

    @property
    def token(self) -> str | None:
        cfg = self.get_config()
        return cfg.get("token") if cfg else None

    # -- channels ----------------------------------------------------------

    def ensure_channel(self, channel: str, description: str = "") -> str:
        name = _safe_name(channel)
        cdir = self.channels_dir / name
        (cdir / "messages").mkdir(parents=True, exist_ok=True)
        meta_path = cdir / "meta.json"
        if not meta_path.exists():
            _atomic_write_json(meta_path, {
                "name": name,
                "description": description,
                "created": _now(),
            })
        return name

    def list_channels(self) -> list[dict[str, Any]]:
        if not self.channels_dir.exists():
            return []
        out = []
        for cdir in sorted(self.channels_dir.iterdir()):
            if not cdir.is_dir():
                continue
            meta = _read_json(cdir / "meta.json") or {"name": cdir.name}
            out.append(meta)
        return out

    # -- posting -----------------------------------------------------------

    def post_channel(self, channel: str, text: str, author: str,
                     author_name: str, author_kind: str = "agent",
                     host: str = "", meta: dict | None = None) -> Message:
        name = self.ensure_channel(channel)
        ts = _now()
        msg = Message(
            id=uuid.uuid4().hex, ts=ts, text=text, author=author,
            author_name=author_name, author_kind=author_kind,
            channel=name, host=host, meta=meta or {},
        )
        path = self.channels_dir / name / "messages" / _msg_filename(ts)
        _atomic_write_json(path, msg.to_dict())
        return msg

    def post_inbox(self, to_agent: str, text: str, author: str,
                   author_name: str, author_kind: str = "human",
                   host: str = "", meta: dict | None = None) -> Message:
        """Send a directed message (instruction/DM) to a specific agent."""
        to_id = _safe_name(to_agent)
        ts = _now()
        msg = Message(
            id=uuid.uuid4().hex, ts=ts, text=text, author=author,
            author_name=author_name, author_kind=author_kind,
            to=to_id, host=host, meta=meta or {},
        )
        path = self.inbox_dir / to_id / _msg_filename(ts)
        _atomic_write_json(path, msg.to_dict())
        return msg

    def post_broadcast(self, text: str, author: str, author_name: str,
                       author_kind: str = "human", host: str = "",
                       meta: dict | None = None) -> Message:
        """Send one instruction to *every* agent. Agents poll the broadcast
        stream in addition to their own inbox, so this reaches agents that
        register later too."""
        ts = _now()
        msg = Message(
            id=uuid.uuid4().hex, ts=ts, text=text, author=author,
            author_name=author_name, author_kind=author_kind,
            to="*", host=host, meta=meta or {},
        )
        path = self.broadcast_dir / _msg_filename(ts)
        _atomic_write_json(path, msg.to_dict())
        return msg

    def broadcast_to_capability(self, capability: str, text: str, author: str,
                                author_name: str, author_kind: str = "human",
                                host: str = "", online_only: bool = False
                                ) -> list[Message]:
        """Send a directed instruction to every agent advertising a capability.
        Writes to each matching agent's inbox so it is individually addressed."""
        sent = []
        for a in self.list_agents(online_window=30.0):
            if online_only and not a.get("online"):
                continue
            if capability in (a.get("capabilities") or []):
                sent.append(self.post_inbox(
                    a["id"], text, author=author, author_name=author_name,
                    author_kind=author_kind, host=host,
                    meta={"capability": capability}))
        return sent

    # -- reading -----------------------------------------------------------

    def _read_dir_messages(self, msg_dir: Path, since_ts: float = 0.0,
                           limit: int | None = None) -> list[dict[str, Any]]:
        if not msg_dir.exists():
            return []
        names = sorted(
            n for n in os.listdir(msg_dir)
            if n.endswith(".json") and not n.startswith(".")
        )
        # Filename prefix is microsecond ts; prune cheaply before parsing.
        if since_ts > 0:
            cutoff = f"{int(since_ts * 1_000_000):020d}"
            names = [n for n in names if n[:20] > cutoff]
        if limit is not None and len(names) > limit:
            names = names[-limit:]
        out = []
        for n in names:
            data = _read_json(msg_dir / n)
            if data is not None:
                out.append(data)
        return out

    def read_channel(self, channel: str, since_ts: float = 0.0,
                     limit: int | None = None) -> list[dict[str, Any]]:
        name = _safe_name(channel)
        return self._read_dir_messages(
            self.channels_dir / name / "messages", since_ts, limit)

    def read_inbox(self, agent_id: str, since_ts: float = 0.0,
                   limit: int | None = None) -> list[dict[str, Any]]:
        to_id = _safe_name(agent_id)
        return self._read_dir_messages(self.inbox_dir / to_id, since_ts, limit)

    def read_broadcast(self, since_ts: float = 0.0,
                       limit: int | None = None) -> list[dict[str, Any]]:
        return self._read_dir_messages(self.broadcast_dir, since_ts, limit)

    def firehose(self, since_ts: float = 0.0, limit: int = 200
                 ) -> list[dict[str, Any]]:
        """All channel + broadcast activity merged chronologically. The
        management 'see everything' view."""
        merged: list[dict[str, Any]] = []
        for ch in self.list_channels():
            merged.extend(self.read_channel(ch["name"], since_ts=since_ts))
        merged.extend(self.read_broadcast(since_ts=since_ts))
        merged.sort(key=lambda m: m["ts"])
        return merged[-limit:] if limit else merged

    # -- retention / rotation ---------------------------------------------

    def _prune_dir(self, msg_dir: Path, archive_path: Path,
                   keep_last: int | None, max_age: float | None,
                   archive: bool = True) -> int:
        """Prune a message directory. A message is removed if it falls outside
        the last `keep_last` OR is older than `max_age` seconds. Removed
        messages are appended (oldest-first) to `archive_path` as JSONL unless
        archive=False. Returns the number pruned."""
        if not msg_dir.exists():
            return 0
        names = sorted(
            n for n in os.listdir(msg_dir)
            if n.endswith(".json") and not n.startswith(".")
        )
        to_remove: set[str] = set()
        if keep_last is not None and len(names) > keep_last:
            to_remove.update(names[:-keep_last] if keep_last > 0 else names)
        if max_age is not None:
            cutoff = f"{int((_now() - max_age) * 1_000_000):020d}"
            to_remove.update(n for n in names if n[:20] < cutoff)
        if not to_remove:
            return 0
        ordered = [n for n in names if n in to_remove]  # oldest-first
        if archive:
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            with open(archive_path, "a", encoding="utf-8") as f:
                for n in ordered:
                    data = _read_json(msg_dir / n)
                    if data is not None:
                        f.write(json.dumps(data, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        for n in ordered:
            try:
                (msg_dir / n).unlink()
            except OSError:
                pass
        return len(ordered)

    def prune_channel(self, channel: str, keep_last: int | None = None,
                      max_age: float | None = None, archive: bool = True) -> int:
        name = _safe_name(channel)
        cdir = self.channels_dir / name
        return self._prune_dir(cdir / "messages", cdir / "archive.jsonl",
                               keep_last, max_age, archive)

    def prune_broadcast(self, keep_last: int | None = None,
                        max_age: float | None = None, archive: bool = True) -> int:
        return self._prune_dir(self.broadcast_dir,
                               self.broadcast_dir / "archive.jsonl",
                               keep_last, max_age, archive)

    def prune_inbox(self, agent_id: str, keep_last: int | None = None,
                    max_age: float | None = None, archive: bool = True) -> int:
        to_id = _safe_name(agent_id)
        idir = self.inbox_dir / to_id
        return self._prune_dir(idir, idir / "archive.jsonl",
                               keep_last, max_age, archive)

    def prune_all(self, keep_last: int | None = None, max_age: float | None = None,
                  archive: bool = True) -> dict[str, int]:
        """Prune every channel, broadcast, and inbox. Returns counts per target."""
        result: dict[str, int] = {}
        for ch in self.list_channels():
            n = self.prune_channel(ch["name"], keep_last, max_age, archive)
            if n:
                result[f"#{ch['name']}"] = n
        n = self.prune_broadcast(keep_last, max_age, archive)
        if n:
            result["broadcast"] = n
        if self.inbox_dir.exists():
            for idir in self.inbox_dir.iterdir():
                if idir.is_dir():
                    n = self.prune_inbox(idir.name, keep_last, max_age, archive)
                    if n:
                        result[f"inbox:{idir.name}"] = n
        return result

    def read_archive(self, channel: str | None = None, broadcast: bool = False,
                     agent_id: str | None = None, limit: int | None = None
                     ) -> list[dict[str, Any]]:
        """Read archived (pruned) messages back from JSONL, oldest-first."""
        if broadcast:
            path = self.broadcast_dir / "archive.jsonl"
        elif agent_id:
            path = self.inbox_dir / _safe_name(agent_id) / "archive.jsonl"
        else:
            path = self.channels_dir / _safe_name(channel or "general") / "archive.jsonl"
        if not path.exists():
            return []
        out: list[dict[str, Any]] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out[-limit:] if limit else out

    # -- agents / presence -------------------------------------------------

    def register_agent(self, agent_id: str, name: str, host: str = "",
                       pid: int | None = None, kind: str = "agent",
                       capabilities: list[str] | None = None,
                       extra: dict | None = None) -> dict[str, Any]:
        aid = _safe_name(agent_id)
        now = _now()
        existing = _read_json(self.agents_dir / f"{aid}.json") or {}
        record = {
            "id": aid,
            "name": name,
            "host": host,
            "pid": pid,
            "kind": kind,
            # Preserve previously-declared capabilities on a bare re-register.
            "capabilities": capabilities if capabilities is not None
                            else existing.get("capabilities", []),
            "status": "online",
            "activity": existing.get("activity", ""),
            "registered": existing.get("registered", now),
            "last_seen": now,
            "extra": extra or existing.get("extra", {}),
        }
        _atomic_write_json(self.agents_dir / f"{aid}.json", record)
        return record

    def heartbeat(self, agent_id: str, status: str = "online",
                  activity: str | None = None) -> dict[str, Any] | None:
        aid = _safe_name(agent_id)
        path = self.agents_dir / f"{aid}.json"
        record = _read_json(path)
        if record is None:
            return None
        record["last_seen"] = _now()
        record["status"] = status
        if activity is not None:
            record["activity"] = activity
        _atomic_write_json(path, record)
        return record

    def set_agent_status(self, agent_id: str, status: str) -> dict[str, Any] | None:
        return self.heartbeat(agent_id, status=status)

    def list_agents(self, online_window: float = 30.0) -> list[dict[str, Any]]:
        if not self.agents_dir.exists():
            return []
        now = _now()
        out = []
        for path in sorted(self.agents_dir.glob("*.json")):
            rec = _read_json(path)
            if rec is None:
                continue
            last_seen = rec.get("last_seen", 0)
            age = now - last_seen
            # Derive an effective presence: an agent that stops heart-beating
            # is shown as "offline" regardless of its last self-reported status.
            if rec.get("status") == "offline":
                rec["online"] = False
            else:
                rec["online"] = age <= online_window
            rec["age"] = age
            out.append(rec)
        return out

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        return _read_json(self.agents_dir / f"{_safe_name(agent_id)}.json")
