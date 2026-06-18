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


def _atomic_create_exclusive(path: Path, data: dict[str, Any]) -> bool:
    """Create `path` containing JSON `data`, FAILING if it already exists.

    Uses O_CREAT|O_EXCL, an atomic, lock-free mutual-exclusion primitive that is
    reliable on NFS (unlike file locking). This is how a task is *claimed*: the
    first writer to create the claim file wins; everyone else gets False. No
    read-modify-write race, so two agents can never both win a claim.
    Returns True if THIS caller created the file, False if it already existed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    return True


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        # A partially written or vanished file: skip it. Writers use atomic
        # rename so a fully-named *.json file should always be complete, but we
        # stay defensive against NFS caching quirks.
        return None


# Task lifecycle. A task moves open -> claimed -> running -> done|failed (or
# cancelled). The current status is the latest status event; terminal statuses
# end the task.
TASK_STATUSES = ("open", "claimed", "running", "done", "failed", "cancelled")
TASK_TERMINAL = ("done", "failed", "cancelled")


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
        self.uploads_dir = self.root / "uploads"
        self.tasks_dir = self.root / "tasks"
        self.config_path = self.root / "config.json"

    # -- lifecycle ---------------------------------------------------------

    def init(self, token: str | None = None) -> dict[str, Any]:
        """Create the hub directory tree and config if missing. Idempotent."""
        for d in (self.channels_dir, self.inbox_dir, self.agents_dir,
                  self.broadcast_dir, self.uploads_dir, self.tasks_dir):
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

    # -- uploads (image / file attachments) -------------------------------

    def save_upload(self, data: bytes, ext: str) -> str:
        """Save an uploaded file under HUB_ROOT/uploads and return its URL path
        (/uploads/<name>). Attachments live with the rest of the hub data —
        outside the git repo — so chat content is never committed."""
        ext = "".join(c for c in (ext or "") if c.isalnum()).lower()[:8] or "bin"
        name = f"{uuid.uuid4().hex}.{ext}"
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        path = self.uploads_dir / name
        tmp = self.uploads_dir / f".{name}.{os.getpid()}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return f"/uploads/{name}"

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

    # -- diagnostics -------------------------------------------------------

    def stats(self, online_window: float = 30.0) -> dict[str, Any]:
        """A health snapshot of the hub (used by `hubcli doctor`)."""
        def _count(d: Path) -> int:
            if not d.exists():
                return 0
            return sum(1 for n in os.listdir(d)
                       if n.endswith(".json") and not n.startswith("."))

        channels = self.list_channels()
        ch_counts = {c["name"]: _count(self.channels_dir / c["name"] / "messages")
                     for c in channels}
        agents = self.list_agents(online_window=online_window)
        inbox_total = 0
        if self.inbox_dir.exists():
            for idir in self.inbox_dir.iterdir():
                if idir.is_dir():
                    inbox_total += _count(idir)
        return {
            "root": str(self.root),
            "config_ok": self.get_config() is not None,
            "auth_enabled": bool(self.token),
            "channels": len(channels),
            "channel_message_counts": ch_counts,
            "channel_messages_total": sum(ch_counts.values()),
            "broadcast_messages": _count(self.broadcast_dir),
            "inbox_messages_total": inbox_total,
            "agents_total": len(agents),
            "agents_online": sum(1 for a in agents if a.get("online")),
        }

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

    def forget_agent(self, agent_id: str) -> bool:
        """Remove an agent's record entirely (drops it from the roster). Returns
        True if a record existed and was removed, False if there was none."""
        path = self.agents_dir / f"{_safe_name(agent_id)}.json"
        if path.exists():
            path.unlink()
            return True
        return False

    # -- tasks (durable work dispatch) ------------------------------------
    #
    # The manager turns each dispatchable unit of work (e.g. a GitHub issue)
    # into a task here, so dispatch state is DURABLE — it survives restarts and
    # is not re-derived from chat each tick. A task is a small directory:
    #
    #     tasks/<task_id>/task.json     immutable definition (title, ref, ...)
    #     tasks/<task_id>/claim.json    the atomic claim (first-writer-wins)
    #     tasks/<task_id>/events/       append-only status events (immutable)
    #
    # Claiming is lock-free and race-proof via O_EXCL create (see
    # _atomic_create_exclusive): two workers can never both claim one task, so a
    # task is never double-assigned even if the manager dispatches it twice.

    def create_task(self, task_id: str, title: str = "", ref: str = "",
                    brief: str = "", capability: str = "",
                    created_by: str = "", labels: list[str] | None = None,
                    meta: dict | None = None) -> dict[str, Any]:
        """Create a task (idempotent: if it already exists, return it as-is so a
        re-dispatch never clobbers an in-progress task)."""
        tid = _safe_name(task_id)
        tdir = self.tasks_dir / tid
        (tdir / "events").mkdir(parents=True, exist_ok=True)
        task_path = tdir / "task.json"
        if not task_path.exists():
            _atomic_write_json(task_path, {
                "id": tid,
                "title": title,
                "ref": ref,                 # e.g. "Jpickard1/MGB-main#42"
                "brief": brief,
                "capability": capability,   # skill needed (matches agent caps)
                "labels": labels or [],
                "created_by": created_by,
                "created_ts": _now(),
                "meta": meta or {},
            })
        return self.get_task(tid)

    def _append_task_event(self, task_id: str, status: str, by: str = "",
                           note: str = "") -> dict[str, Any]:
        tid = _safe_name(task_id)
        ts = _now()
        ev = {"task_id": tid, "status": status, "by": by, "ts": ts, "note": note}
        _atomic_write_json(self.tasks_dir / tid / "events" / _msg_filename(ts), ev)
        return ev

    def claim_task(self, task_id: str, agent_id: str, note: str = "") -> bool:
        """Atomically claim a task for `agent_id`. Returns True if THIS agent won
        the claim, False if it was already claimed (or the task is unknown).
        Lock-free and race-proof — exactly one caller can ever win."""
        tid = _safe_name(task_id)
        tdir = self.tasks_dir / tid
        if not (tdir / "task.json").exists():
            return False
        won = _atomic_create_exclusive(tdir / "claim.json", {
            "task_id": tid,
            "agent": _safe_name(agent_id),
            "claimed_ts": _now(),
            "note": note,
        })
        if won:
            self._append_task_event(tid, "claimed", by=agent_id, note=note)
        return won

    def update_task(self, task_id: str, status: str, by: str = "",
                    note: str = "") -> dict[str, Any] | None:
        """Append a status event (running/done/failed/cancelled/…). Returns the
        updated task, or None if the task is unknown."""
        tid = _safe_name(task_id)
        if not (self.tasks_dir / tid / "task.json").exists():
            return None
        self._append_task_event(tid, status, by=by, note=note)
        return self.get_task(tid)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """A task with its derived current status, claimer, and event history."""
        tid = _safe_name(task_id)
        tdir = self.tasks_dir / tid
        rec = _read_json(tdir / "task.json")
        if rec is None:
            return None
        claim = _read_json(tdir / "claim.json")
        events = self._read_dir_messages(tdir / "events")  # chronological
        status = "open"
        if claim:
            status = "claimed"
        if events:
            status = events[-1].get("status", status)
        rec = dict(rec)
        rec["status"] = status
        rec["claimed_by"] = (claim or {}).get("agent")
        rec["claimed_ts"] = (claim or {}).get("claimed_ts")
        rec["events"] = events
        rec["updated_ts"] = events[-1]["ts"] if events else rec.get("created_ts")
        return rec

    def list_tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        """All tasks (optionally filtered by current status), newest update first.
        This is the manager's durable 'what's assigned / done' view."""
        if not self.tasks_dir.exists():
            return []
        out = []
        for tdir in sorted(self.tasks_dir.iterdir()):
            if not tdir.is_dir():
                continue
            t = self.get_task(tdir.name)
            if t is None:
                continue
            if status and t["status"] != status:
                continue
            out.append(t)
        out.sort(key=lambda t: t.get("updated_ts") or 0, reverse=True)
        return out

    def release_task(self, task_id: str, by: str, force: bool = False) -> bool:
        """Drop a task's claim so it returns to 'open' and can be reclaimed.
        Only the current owner may release it, unless `force` (manager override).
        Returns True if released (or already unclaimed), False if `by` isn't the
        owner and not forced, or the task is unknown."""
        tid = _safe_name(task_id)
        tdir = self.tasks_dir / tid
        if not (tdir / "task.json").exists():
            return False
        claim = _read_json(tdir / "claim.json")
        if claim is None:
            return True  # already open — nothing to release
        if not force and claim.get("agent") != _safe_name(by):
            return False  # can't release someone else's claim
        try:
            (tdir / "claim.json").unlink()
        except OSError:
            pass
        self._append_task_event(tid, "open", by=by, note="released")
        return True

    def reassign_task(self, task_id: str, new_agent: str, by: str = "",
                      note: str = "") -> dict[str, Any] | None:
        """Manager override: move a task's claim to `new_agent` regardless of the
        current owner (e.g. to recover a stale/abandoned task). Returns the task,
        or None if unknown."""
        tid = _safe_name(task_id)
        tdir = self.tasks_dir / tid
        if not (tdir / "task.json").exists():
            return None
        _atomic_write_json(tdir / "claim.json", {
            "task_id": tid,
            "agent": _safe_name(new_agent),
            "claimed_ts": _now(),
            "note": note or f"reassigned by {by}",
        })
        self._append_task_event(tid, "claimed", by=new_agent,
                                note=note or f"reassigned by {by}")
        return self.get_task(tid)

    def stale_tasks(self, offline_window: float = 300.0) -> list[dict[str, Any]]:
        """Tasks that are claimed/running but whose owner has not heart-beat
        within `offline_window` seconds — i.e. likely abandoned by a dead agent
        and eligible for reassignment. Terminal tasks are never stale."""
        online = {a["id"] for a in self.list_agents(online_window=offline_window)
                  if a.get("online")}
        stale = []
        for t in self.list_tasks():
            owner = t.get("claimed_by")
            if owner and t["status"] not in TASK_TERMINAL and owner not in online:
                stale.append(t)
        return stale


    # -- usage / utilization (issue #6) -----------------------------------

    def _host_metrics(self):
        """Best-effort host CPU/mem (psutil if present, else stdlib fallback)."""
        host = socket.gethostname().split(".")[0]
        out = {"host": host}
        try:
            import psutil
            vm = psutil.virtual_memory()
            out.update(cpu_percent=psutil.cpu_percent(interval=0.1),
                       mem_percent=vm.percent,
                       mem_used_gb=round(vm.used / 1e9, 1),
                       mem_total_gb=round(vm.total / 1e9, 1))
        except Exception:
            pass
        try:
            out["load1"] = round(os.getloadavg()[0], 2)
        except Exception:
            pass
        return out

    def usage_stats(self, online_window: float = 30.0):
        """Utilization snapshot for the efficiency panel (issue #6): per-agent
        message + task counts, totals, and host metrics. Token usage needs an
        agent-side reporting hook -- tracked as a follow-up."""
        # messages per author (across channels)
        msgs_by = {}
        total_msgs = 0
        for ch in self.list_channels():
            for m in self.read_channel(ch["name"]):
                who = m.get("author") or m.get("author_name") or "?"
                msgs_by[who] = msgs_by.get(who, 0) + 1
                total_msgs += 1
        # tasks per owner
        tasks = self.list_tasks()
        tasks_by = {}
        for t in tasks:
            owner = t.get("claimed_by")
            if not owner:
                continue
            d = tasks_by.setdefault(owner, {"total": 0, "done": 0, "running": 0})
            d["total"] += 1
            if t["status"] == "done":
                d["done"] += 1
            elif t["status"] == "running":
                d["running"] += 1
        agents = self.list_agents(online_window=online_window)
        per_agent = []
        for a in agents:
            tc = tasks_by.get(a["id"], {})
            per_agent.append({
                "id": a["id"], "name": a["name"], "host": a.get("host"),
                "online": a.get("online"), "status": a.get("status"),
                "activity": a.get("activity"),
                "messages": msgs_by.get(a["id"], 0),
                "tasks_total": tc.get("total", 0),
                "tasks_done": tc.get("done", 0),
                "tasks_running": tc.get("running", 0),
            })
        done = sum(1 for t in tasks if t["status"] == "done")
        return {
            "totals": {
                "agents": len(agents),
                "online": sum(1 for a in agents if a.get("online")),
                "messages": total_msgs,
                "tasks": len(tasks),
                "tasks_done": done,
                "tasks_per_agent": round(len(tasks) / len(agents), 2) if agents else 0,
            },
            "agents": per_agent,
            "host": self._host_metrics(),
            "token_tracking": "token usage: follow-up — agents report per-turn tokens via a bridge hook",
        }
