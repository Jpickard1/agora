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


def _search_terms(query: str) -> list[str]:
    return [w for w in (query or "").strip().lower().split() if w]


def _search_snippet(text: str, terms: list[str], width: int = 140) -> str:
    """A one-line snippet of `text` centred on the first matched term, with
    ellipses. Whitespace is collapsed so it renders on one line in the UI/CLI."""
    flat = " ".join((text or "").split())
    low = flat.lower()
    pos = -1
    for w in terms:
        i = low.find(w)
        if i != -1 and (pos == -1 or i < pos):
            pos = i
    if pos == -1:
        return flat[:width] + ("…" if len(flat) > width else "")
    start = max(0, pos - width // 3)
    end = min(len(flat), start + width)
    snippet = flat[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(flat):
        snippet = snippet + "…"
    return snippet


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
    reply_to: str | None = None  # parent message id, for threaded replies (#64)
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
        self.kb_dir = self.root / "kb"
        self.projects_dir = self.root / "projects"
        self.locks_dir = self.root / "locks"
        self.reactions_dir = self.root / "reactions"             # issue #61
        self.reaction_events_dir = self.root / "reaction_events"  # for SSE tailing
        self.config_path = self.root / "config.json"

    # -- lifecycle ---------------------------------------------------------

    def init(self, token: str | None = None,
             shared_root: str | None = None) -> dict[str, Any]:
        """Create the hub directory tree and config if missing. Idempotent.
        `shared_root` (issue #14) is the group-accessible SHARED store for public
        channels + the participants roster; persisted in config when given."""
        for d in (self.channels_dir, self.inbox_dir, self.agents_dir,
                  self.broadcast_dir, self.uploads_dir, self.tasks_dir,
                  self.kb_dir, self.projects_dir, self.locks_dir,
                  self.reactions_dir, self.reaction_events_dir):
            d.mkdir(parents=True, exist_ok=True)
        cfg = self.get_config()
        if cfg is None:
            cfg = {
                "token": token or uuid.uuid4().hex,
                "created": _now(),
                "version": 1,
                "shared_root": str(Path(shared_root).expanduser().resolve())
                               if shared_root else None,
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
        elif shared_root and not cfg.get("shared_root"):
            cfg["shared_root"] = str(Path(shared_root).expanduser().resolve())
            _atomic_write_json(self.config_path, cfg)
        # If a shared store is configured, ensure its channels dir exists +
        # group-accessible (public channels live here).
        sh = self.shared_channels_dir()
        if sh is not None:
            sh.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(sh, 0o2770)
            except OSError:
                pass
        # Always make sure the default channel exists — it's PUBLIC (#14).
        self.ensure_channel("general", description="Default channel for all agents",
                            visibility="public")
        return cfg

    def get_config(self) -> dict[str, Any] | None:
        return _read_json(self.config_path)

    @property
    def token(self) -> str | None:
        cfg = self.get_config()
        return cfg.get("token") if cfg else None

    # -- channels (two-store: private + shared, issue #14) -----------------
    # PUBLIC channels live in the SHARED store (a group-accessible root the whole
    # ewsc_users group can reach); PRIVATE channels stay in this per-user private
    # root (owner-only). Reads merge both roots; writes go to the channel's root.
    # If no shared_root is configured, everything is private_root (back-compat).

    def shared_root(self) -> Path | None:
        """The SHARED store root, or None (single-root/back-compat). Source: env
        AGORA_SHARED_ROOT, else config.json 'shared_root'. The shared store holds
        ONLY public channels (+ the participants roster / cross-user DM relay that
        other features layer on, e.g. #86/#88) — never tasks/agents/locks/KB."""
        p = os.environ.get("AGORA_SHARED_ROOT")
        if not p:
            cfg = self.get_config() or {}
            p = cfg.get("shared_root")
        return Path(p).expanduser().resolve() if p else None

    def shared_channels_dir(self) -> Path | None:
        """The SHARED store's channels dir, or None (single-root/back-compat)."""
        sr = self.shared_root()
        return (sr / "channels") if sr is not None else None

    def _apply_channel_perms(self, cdir: Path, visibility: str) -> None:
        """OS-enforce visibility via Unix dir perms — the dir is the access gate
        (others can't traverse a 0700 dir): private -> 0o700 (owner-only),
        public -> 0o2770 (setgid + group rwx; ewsc_users can read+post)."""
        mode = 0o700 if visibility == "private" else 0o2770
        for p in (cdir, cdir / "messages"):
            try:
                os.chmod(p, mode)
            except OSError:
                pass

    def _channel_base(self, name: str) -> Path | None:
        """An existing channel's base dir across both roots (private first)."""
        name = _safe_name(name)
        priv = self.channels_dir / name
        if (priv / "meta.json").exists():
            return priv
        sh = self.shared_channels_dir()
        if sh is not None and (sh / name / "meta.json").exists():
            return sh / name
        return None

    def _root_for_visibility(self, visibility: str) -> Path:
        """Where a NEW channel of this visibility is created."""
        sh = self.shared_channels_dir()
        if visibility == "public" and sh is not None:
            return sh
        return self.channels_dir

    def ensure_channel(self, channel: str, description: str = "",
                       visibility: str | None = None) -> str:
        name = _safe_name(channel)
        base = self._channel_base(name)
        if base is None:
            vis = visibility or "private"   # #14: new channels are private by default
            base = self._root_for_visibility(vis) / name
            (base / "messages").mkdir(parents=True, exist_ok=True)
            _atomic_write_json(base / "meta.json", {
                "name": name, "description": description,
                "created": _now(), "visibility": vis,
            })
            self._apply_channel_perms(base, vis)
            return name
        if visibility is not None:
            meta = _read_json(base / "meta.json") or {"name": name}
            if meta.get("visibility") != visibility:
                base = self._set_visibility(name, base, meta, visibility)
            self._apply_channel_perms(base, visibility)
        return name

    def _set_visibility(self, name: str, base: Path, meta: dict,
                        visibility: str) -> Path:
        """Set visibility, MOVING the channel dir to the matching root if needed
        (private<->shared). The move is the migration step (gated to live go)."""
        import shutil as _shutil
        meta["visibility"] = visibility
        target_root = self._root_for_visibility(visibility)
        if base.parent != target_root:
            dest = target_root / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            _shutil.move(str(base), str(dest))
            base = dest
        _atomic_write_json(base / "meta.json", meta)
        return base

    def set_channel_visibility(self, channel: str, visibility: str) -> str:
        """Make a channel public/private (chmod + meta, moving roots if needed)."""
        if visibility not in ("public", "private"):
            raise ValueError("visibility must be 'public' or 'private'")
        return self.ensure_channel(channel, visibility=visibility)

    # -- migration to the dual-root layout (issue #92) ---------------------
    # The 3 default-public channels; everything else becomes private. Override
    # with an explicit list when calling.
    DEFAULT_PUBLIC_CHANNELS = ("general", "compute-resources", "memes")

    def _expected_mode(self, visibility: str) -> int:
        return 0o700 if visibility == "private" else 0o2770

    def relocate_channel(self, channel: str, visibility: str) -> bool:
        """Ensure a channel sits in the correct ROOT for `visibility` with the
        correct perms — moving + chmod'ing as needed, regardless of what its meta
        currently says (legacy channels default meta 'public' but may physically
        live in the private root). Idempotent. Returns True if anything changed."""
        import shutil as _shutil
        name = _safe_name(channel)
        base = self._channel_base(name)
        if base is None:
            return False
        target_root = self._root_for_visibility(visibility)
        changed = False
        if base.parent != target_root:
            dest = target_root / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            _shutil.move(str(base), str(dest))
            base = dest
            changed = True
        meta = _read_json(base / "meta.json") or {"name": name}
        if meta.get("visibility") != visibility:
            meta["visibility"] = visibility
            changed = True
        _atomic_write_json(base / "meta.json", meta)
        try:
            import stat as _stat
            if _stat.S_IMODE(os.stat(base).st_mode) != self._expected_mode(visibility):
                changed = True
        except OSError:
            pass
        self._apply_channel_perms(base, visibility)
        return changed

    def migration_plan(self, public: list[str] | None = None) -> list[dict[str, Any]]:
        """What `migrate_channels` WOULD do (no changes): one entry per channel
        that isn't already correctly placed/permed for its target visibility."""
        import stat as _stat
        public_set = set(public or self.DEFAULT_PUBLIC_CHANNELS)
        sh = self.shared_channels_dir()
        plan = []
        for c in self.list_channels():
            name = c["name"]
            base = self._channel_base(name)
            if base is None:
                continue
            target = "public" if name in public_set else "private"
            target_root = self._root_for_visibility(target)
            cur_root = base.parent
            will_move = cur_root != target_root
            try:
                will_chmod = _stat.S_IMODE(os.stat(base).st_mode) != self._expected_mode(target)
            except OSError:
                will_chmod = True
            if will_move or will_chmod or c.get("visibility") != target:
                plan.append({
                    "channel": name, "to": target,
                    "from_store": "shared" if (sh is not None and cur_root == sh) else "private",
                    "to_store": "shared" if target_root == sh else "private",
                    "move": will_move, "chmod": will_chmod,
                })
        return plan

    def migrate_channels(self, public: list[str] | None = None,
                         dry_run: bool = False) -> list[dict[str, Any]]:
        """Enforce the #14 split: the `public` channels go to the shared store
        (2770); all others become private (0700). Idempotent (a second run is a
        no-op). With dry_run, NOTHING changes — returns the same plan either way.
        Requires a configured shared_root."""
        if self.shared_root() is None:
            raise ValueError("no shared_root configured — run "
                             "'hubcli init --shared-root <path>' first")
        plan = self.migration_plan(public)
        if not dry_run:
            for a in plan:
                self.relocate_channel(a["channel"], a["to"])
        return plan

    def _channel_messages_dir(self, name: str) -> Path:
        """Messages dir of a channel (resolved across roots; falls back to the
        private root for a just-created channel)."""
        base = self._channel_base(name) or (self.channels_dir / _safe_name(name))
        return base / "messages"

    def list_channels(self) -> list[dict[str, Any]]:
        roots = [self.channels_dir]
        sh = self.shared_channels_dir()
        if sh is not None:
            roots.append(sh)
        seen: dict[str, dict[str, Any]] = {}
        for root in roots:
            if not root or not root.exists():
                continue
            for cdir in sorted(root.iterdir()):
                if not cdir.is_dir():
                    continue
                meta = _read_json(cdir / "meta.json") or {"name": cdir.name}
                meta.setdefault("visibility", "public")
                seen[meta.get("name", cdir.name)] = meta
        return sorted(seen.values(), key=lambda m: m.get("name", ""))

    # -- posting -----------------------------------------------------------

    def post_channel(self, channel: str, text: str, author: str,
                     author_name: str, author_kind: str = "agent",
                     host: str = "", meta: dict | None = None,
                     reply_to: str | None = None) -> Message:
        name = self.ensure_channel(channel)
        ts = _now()
        msg = Message(
            id=uuid.uuid4().hex, ts=ts, text=text, author=author,
            author_name=author_name, author_kind=author_kind,
            channel=name, host=host, meta=meta or {},
            reply_to=reply_to or None,
        )
        path = self._channel_messages_dir(name) / _msg_filename(ts)
        _atomic_write_json(path, msg.to_dict())
        return msg

    def post_inbox(self, to_agent: str, text: str, author: str,
                   author_name: str, author_kind: str = "human",
                   host: str = "", meta: dict | None = None,
                   reply_to: str | None = None) -> Message:
        """Send a directed message (instruction/DM) to a specific agent."""
        to_id = _safe_name(to_agent)
        ts = _now()
        msg = Message(
            id=uuid.uuid4().hex, ts=ts, text=text, author=author,
            author_name=author_name, author_kind=author_kind,
            to=to_id, host=host, meta=meta or {},
            reply_to=reply_to or None,
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
        return self._read_dir_messages(
            self._channel_messages_dir(channel), since_ts, limit)

    def read_thread(self, channel: str, parent_id: str) -> dict[str, Any]:
        """A thread (issue #64): the parent message plus its direct replies
        (messages whose reply_to == parent_id), chronological. parent is None if
        the id isn't in this channel."""
        msgs = self.read_channel(channel)
        parent = next((m for m in msgs if m.get("id") == parent_id), None)
        replies = sorted((m for m in msgs if m.get("reply_to") == parent_id),
                         key=lambda m: m.get("ts", 0))
        return {"parent": parent, "replies": replies}

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

    def search_messages(self, query: str, channels: list[str] | None = None,
                        since_ts: float = 0.0, limit: int | None = 50,
                        include_tasks: bool = True,
                        author: str | None = None,
                        include_archive: bool = False) -> list[dict[str, Any]]:
        """Full-text search across channel messages, inboxes, broadcasts, and
        (optionally) task history. Ranks by term hits — message text weighs most,
        author less — newest-first on ties, and attaches a one-line snippet
        centred on the first match. Each hit is:
            {source, where, id, ts, author, text, snippet, score}
        where source is channel|inbox|broadcast|task and `where` is the channel
        name / agent id / "*" / task id. Empty query -> []. If `channels` is
        given, only those channels are searched (scopes inboxes/broadcast/tasks
        out). `author` (case-insensitive substring) restricts to a sender.

        Visibility is implicit + safe (issue #115): it searches only
        self.list_channels(), which merges THIS hub's own private root + the shared
        public store — another user's private channels live in *their* root and are
        never loaded here, so they can't appear in results."""
        terms = _search_terms(query)
        if not terms:
            return []
        amatch = (author or "").strip().lower()
        hits: list[dict[str, Any]] = []

        def consider(m: dict, source: str, where: str) -> None:
            text = m.get("text") or ""
            low = text.lower()
            author = (m.get("author_name") or m.get("author") or "")
            alow = author.lower()
            if amatch and amatch not in alow and amatch not in (m.get("author") or "").lower():
                return                       # author filter (#115)
            score = 0
            for w in terms:
                if w in low:
                    score += 3
                if w in alow:
                    score += 1
            if score:
                hits.append({
                    "source": source, "where": where, "id": m.get("id"),
                    "ts": m.get("ts", 0), "author": author, "text": text,
                    "snippet": _search_snippet(text, terms), "score": score,
                })

        scoped = channels is not None
        names = ([_safe_name(c) for c in channels] if scoped
                 else [c["name"] for c in self.list_channels()])
        for name in names:
            for m in self.read_channel(name, since_ts=since_ts):
                consider(m, "channel", name)

        if not scoped:
            for m in self.read_broadcast(since_ts=since_ts):
                consider(m, "broadcast", "*")
            if self.inbox_dir.exists():
                for idir in sorted(self.inbox_dir.iterdir()):
                    if idir.is_dir():
                        for m in self._read_dir_messages(idir, since_ts):
                            consider(m, "inbox", idir.name)
            if include_tasks:
                for t in self.list_tasks():
                    blob = " ".join(filter(None, [
                        t.get("title"), t.get("brief"), t.get("ref"),
                        " ".join(e.get("note", "") for e in t.get("events") or []),
                    ]))
                    consider({
                        "text": blob, "id": t.get("id"),
                        "author_name": t.get("claimed_by") or t.get("created_by"),
                        "ts": t.get("updated_ts", 0),
                    }, "task", t.get("id"))

        # Optionally also search pruned/archived history (#115). read_archive uses
        # _channel_base, so this stays visibility-safe (own root + shared only).
        if include_archive:
            for name in names:
                for m in self.read_archive(channel=name):
                    if (m.get("ts", 0) or 0) >= since_ts:
                        consider(m, "archive", name)
            if not scoped:
                for m in self.read_archive(broadcast=True):
                    if (m.get("ts", 0) or 0) >= since_ts:
                        consider(m, "archive", "*")

        hits.sort(key=lambda h: (h["score"], h["ts"]), reverse=True)
        return hits[:limit] if limit else hits

    def activity_digest(self, since_ts: float = 0.0,
                        top_mentions: int = 5) -> dict[str, Any]:
        """Cross-channel activity summary since `since_ts` (issue #79): per-channel
        message count + top @mentions, broadcast count, and task status changes in
        the window. Reads the store only.

        Returns:
          {since, channels: [{channel, messages, top_mentions: [[name,n],...]}],
           broadcasts, task_changes: [{task, status, by, ts}], totals: {...}}
        """
        channels = []
        total_msgs = 0
        for ch in self.list_channels():
            name = ch["name"]
            msgs = self.read_channel(name, since_ts=since_ts)
            counts: dict[str, int] = {}
            for m in msgs:
                for who in extract_mentions(m.get("text", "")):
                    counts[who] = counts.get(who, 0) + 1
            top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top_mentions]
            channels.append({"channel": name, "messages": len(msgs),
                             "top_mentions": top})
            total_msgs += len(msgs)
        channels.sort(key=lambda c: c["messages"], reverse=True)

        broadcasts = len(self.read_broadcast(since_ts=since_ts))

        task_changes = []
        for t in self.list_tasks():
            for e in (t.get("events") or []):
                if e.get("ts", 0) >= since_ts:
                    task_changes.append({"task": t["id"], "status": e.get("status"),
                                         "by": e.get("by"), "ts": e.get("ts", 0)})
        task_changes.sort(key=lambda x: x["ts"])

        return {
            "since": since_ts,
            "channels": channels,
            "broadcasts": broadcasts,
            "task_changes": task_changes,
            "totals": {"messages": total_msgs, "broadcasts": broadcasts,
                       "task_changes": len(task_changes)},
        }

    def last_active_ts(self, viewer: str) -> float:
        """Latest ts of a message authored by `viewer` (channels + broadcasts) —
        i.e. when they were last active on the hub. 0.0 if never seen."""
        v = (viewer or "").lower()
        latest = 0.0
        for ch in self.list_channels():
            for m in self.read_channel(ch["name"]):
                if v in ((m.get("author") or "").lower(), (m.get("author_name") or "").lower()):
                    latest = max(latest, m.get("ts", 0) or 0)
        for m in self.read_broadcast():
            if v in ((m.get("author") or "").lower(), (m.get("author_name") or "").lower()):
                latest = max(latest, m.get("ts", 0) or 0)
        return latest

    def catchup(self, viewer: str, since_ts: float | None = None,
                default_window: float = 86400.0) -> dict[str, Any]:
        """'What happened while you were away' for `viewer` (catch-up summary).
        The window defaults to the viewer's last activity (or `default_window`
        seconds ago if they've never posted). Personalized: new activity per
        channel (from #79's digest) + the viewer's unanswered @mentions, unread
        DMs, open tasks, and recent alerts. Reads the store only."""
        now = _now()
        if since_ts is None:
            la = self.last_active_ts(viewer)
            since_ts = la if la > 0 else now - default_window
        v = (viewer or "").lower()
        digest = self.activity_digest(since_ts)

        mentions, alerts = [], []
        for ch in self.list_channels():
            for m in self.read_channel(ch["name"], since_ts=since_ts):
                author = m.get("author_name") or m.get("author") or ""
                if author.lower() == v:
                    continue                       # skip the viewer's own posts
                row = {"channel": ch["name"], "author": author,
                       "text": m.get("text", ""), "ts": m.get("ts", 0) or 0}
                if message_mentions(m.get("text", ""), viewer):
                    mentions.append(row)
                if (m.get("meta") or {}).get("alert"):
                    alerts.append(row)
        mentions.sort(key=lambda x: x["ts"])
        alerts.sort(key=lambda x: x["ts"])

        unread_dms = [m for m in self.read_inbox(viewer, since_ts=since_ts)
                      if (m.get("author_name") or m.get("author") or "").lower() != v]
        open_tasks = [t for t in self.list_tasks()
                      if (t.get("claimed_by") or "").lower() == v
                      and t.get("status") in ("claimed", "running")]
        return {
            "viewer": viewer, "since": since_ts, "now": now,
            "channels": digest["channels"], "broadcasts": digest["broadcasts"],
            "mentions": mentions, "alerts": alerts,
            "unread_dms": unread_dms, "open_tasks": open_tasks,
            "task_changes": digest["task_changes"],   # claimed/done/stalled deltas (#113)
            "totals": {"new_messages": digest["totals"]["messages"],
                       "mentions": len(mentions), "unread_dms": len(unread_dms),
                       "open_tasks": len(open_tasks), "alerts": len(alerts),
                       "task_changes": len(digest["task_changes"])},
        }

    def comm_graph(self, since_ts: float = 0.0) -> dict[str, Any]:
        """Directed communication graph derived from directed messages: for each
        inbox, edges are author -> recipient with a message count. Shows which
        agents are actually talking to one another (self-messages excluded)."""
        edges: dict[tuple[str, str], int] = {}
        if self.inbox_dir.exists():
            for idir in self.inbox_dir.iterdir():
                if not idir.is_dir():
                    continue
                dst = idir.name
                for m in self._read_dir_messages(idir, since_ts, None):
                    src = m.get("author") or m.get("author_name") or "?"
                    if src == dst:
                        continue
                    edges[(src, dst)] = edges.get((src, dst), 0) + 1
        nodes = sorted({n for pair in edges for n in pair})
        return {
            "nodes": nodes,
            "edges": [{"source": s, "target": d, "count": c}
                      for (s, d), c in sorted(edges.items())],
        }

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
        base = self._channel_base(channel) or (self.channels_dir / _safe_name(channel))
        return self._prune_dir(base / "messages", base / "archive.jsonl",
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
            base = self._channel_base(channel or "general") or \
                (self.channels_dir / _safe_name(channel or "general"))
            path = base / "archive.jsonl"
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
        ch_counts = {c["name"]: _count(self._channel_messages_dir(c["name"]))
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
            # Liveness sub-status (issue #53): responsive | busy | wedged | idle.
            # online says "heartbeating"; liveness says "actually keeping up".
            "liveness": existing.get("liveness", "responsive"),
            # Delivery health (issue #54): queued count + last-delivered/last-receipt ts.
            "delivery": existing.get("delivery", {}),
            "activity": existing.get("activity", ""),
            "registered": existing.get("registered", now),
            "last_seen": now,
            "extra": extra or existing.get("extra", {}),
        }
        _atomic_write_json(self.agents_dir / f"{aid}.json", record)
        return record

    def heartbeat(self, agent_id: str, status: str = "online",
                  activity: str | None = None,
                  liveness: str | None = None,
                  delivery: dict | None = None) -> dict[str, Any] | None:
        aid = _safe_name(agent_id)
        path = self.agents_dir / f"{aid}.json"
        record = _read_json(path)
        if record is None:
            return None
        record["last_seen"] = _now()
        record["status"] = status
        if activity is not None:
            record["activity"] = activity
        if liveness is not None:
            record["liveness"] = liveness
        if delivery is not None:
            record["delivery"] = delivery
        _atomic_write_json(path, record)
        return record

    def set_agent_status(self, agent_id: str, status: str) -> dict[str, Any] | None:
        return self.heartbeat(agent_id, status=status)

    # Default: collapse agents offline longer than this into a "retired" group.
    RETIRE_AFTER = 24 * 3600.0

    def list_agents(self, online_window: float = 30.0,
                    retire_after: float | None = None) -> list[dict[str, Any]]:
        if not self.agents_dir.exists():
            return []
        if retire_after is None:
            retire_after = self.RETIRE_AFTER
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
            rec.setdefault("delivery", {})   # issue #54: delivery-health fields
            # An agent that isn't online can't be "responsive/busy/…"; report
            # offline so the roster/API never show a stale liveness (issue #53).
            rec.setdefault("liveness", "responsive")
            if not rec["online"]:
                rec["liveness"] = "offline"
            # Auto-retire (issue #11): an agent offline longer than retire_after
            # is flagged so the roster can collapse it into a "retired" group.
            # retire_after <= 0 disables retirement (nothing is ever retired).
            rec["retired"] = bool(retire_after) and not rec["online"] and age > retire_after
            out.append(rec)
        return out

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        return _read_json(self.agents_dir / f"{_safe_name(agent_id)}.json")

    # -- reactions (issue #61) --------------------------------------------
    # Emoji reactions on a message. Each (message, emoji, author) is ONE file —
    # reactions/<msg_id>/<author>__<emoji-codepoints>.json — so add is an atomic,
    # idempotent create and remove is an unlink: lock-free + concurrent-safe (the
    # maildir pattern), and one author can't double-count an emoji. Messages stay
    # immutable. Every change also appends a tiny event for SSE tailing.

    @staticmethod
    def _emoji_key(emoji: str) -> str:
        # Deterministic, filesystem-safe encoding of an emoji (which may be
        # multi-codepoint, e.g. 👍🏽) — used in the reaction filename.
        return "-".join(f"{ord(c):x}" for c in (emoji or "")) or "none"

    def _reaction_file(self, msg_id: str, emoji: str, author: str) -> Path:
        return (self.reactions_dir / _safe_name(msg_id)
                / f"{_safe_name(author)}__{self._emoji_key(emoji)}.json")

    def _emit_reaction_event(self, msg_id: str, emoji: str, author: str, op: str):
        ts = _now()
        _atomic_write_json(self.reaction_events_dir / _msg_filename(ts), {
            "msg_id": _safe_name(msg_id), "emoji": emoji, "author": author,
            "op": op, "ts": ts,
        })

    def add_reaction(self, msg_id: str, emoji: str, author: str,
                     author_name: str = "") -> dict[str, Any]:
        """Add (or re-affirm) `author`'s `emoji` reaction to a message.
        Idempotent. Returns the message's aggregated reactions."""
        emoji = (emoji or "").strip()
        if not emoji or len(emoji) > 16:
            raise ValueError("a non-empty emoji (<=16 chars) is required")
        path = self._reaction_file(msg_id, emoji, author)
        if not path.exists():
            _atomic_write_json(path, {
                "msg_id": _safe_name(msg_id), "emoji": emoji,
                "author": _safe_name(author),
                "author_name": author_name or author, "ts": _now(),
            })
            self._emit_reaction_event(msg_id, emoji, author, "add")
        return self.get_reactions(msg_id)

    def remove_reaction(self, msg_id: str, emoji: str, author: str
                        ) -> dict[str, Any]:
        """Remove `author`'s `emoji` reaction (no-op if absent). Returns the
        message's aggregated reactions."""
        path = self._reaction_file(msg_id, emoji, author)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
            self._emit_reaction_event(msg_id, emoji, author, "remove")
        return self.get_reactions(msg_id)

    def toggle_reaction(self, msg_id: str, emoji: str, author: str,
                        author_name: str = "") -> dict[str, Any]:
        """Toggle `author`'s `emoji` reaction: remove if present, else add."""
        if self._reaction_file(msg_id, emoji, author).exists():
            return self.remove_reaction(msg_id, emoji, author)
        return self.add_reaction(msg_id, emoji, author, author_name=author_name)

    def get_reactions(self, msg_id: str) -> dict[str, Any]:
        """Aggregated reactions for a message:
        {"<emoji>": {"count": n, "authors": [name, …]}, …} (emoji insertion order
        by first reactor)."""
        mdir = self.reactions_dir / _safe_name(msg_id)
        agg: dict[str, dict[str, Any]] = {}
        if mdir.exists():
            files = sorted(mdir.glob("*.json"), key=lambda p: _read_json(p).get("ts", 0)
                           if _read_json(p) else 0)
            for f in files:
                rec = _read_json(f)
                if not rec:
                    continue
                e = rec.get("emoji")
                if not e:
                    continue
                bucket = agg.setdefault(e, {"count": 0, "authors": []})
                bucket["count"] += 1
                bucket["authors"].append(rec.get("author_name") or rec.get("author"))
        return agg

    def reactions_for(self, msg_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Aggregated reactions for several messages at once (skips messages with
        none), keyed by message id — for decorating a list of messages."""
        out: dict[str, dict[str, Any]] = {}
        for mid in msg_ids:
            r = self.get_reactions(mid)
            if r:
                out[_safe_name(mid)] = r
        return out

    def read_reaction_events(self, since_ts: float = 0.0,
                             limit: int | None = None) -> list[dict[str, Any]]:
        """Reaction add/remove events since `since_ts` (for the SSE stream)."""
        return self._read_dir_messages(self.reaction_events_dir, since_ts, limit)

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

    # -- knowledge base (issue #25) ---------------------------------------
    # A shared, searchable store of markdown notes / links / artifacts so
    # agents can consult prior work + record decisions instead of duplicating.
    # One JSON file per entry under kb/, mirroring the channel/task layout.

    def kb_add(self, title, body="", tags=None, kind="note", url="",
               author="", author_name="", entry_id=None):
        """Create or update a KB entry. Passing an existing entry_id updates it
        in place (preserving created_ts); otherwise a slug id is derived from
        the title (with a short suffix to avoid collisions)."""
        self.kb_dir.mkdir(parents=True, exist_ok=True)
        tags = [t.strip().lower() for t in (tags or []) if str(t).strip()]
        if entry_id:
            eid = _safe_name(entry_id)
        else:
            base = _safe_name(title)[:48] or "entry"
            eid = base
            if (self.kb_dir / f"{eid}.json").exists():
                eid = f"{base}-{uuid.uuid4().hex[:6]}"
        path = self.kb_dir / f"{eid}.json"
        existing = _read_json(path)
        now = _now()
        rec = {
            "id": eid,
            "title": title,
            "body": body,
            "tags": tags,
            "kind": kind,                 # note | link | artifact
            "url": url,
            "author": author,
            "author_name": author_name or author,
            "created_ts": existing.get("created_ts", now) if existing else now,
            "updated_ts": now,
        }
        _atomic_write_json(path, rec)
        return rec

    def kb_get(self, entry_id):
        return _read_json(self.kb_dir / f"{_safe_name(entry_id)}.json")

    def kb_delete(self, entry_id):
        path = self.kb_dir / f"{_safe_name(entry_id)}.json"
        if path.exists():
            path.unlink()
            return True
        return False

    def _kb_all(self):
        if not self.kb_dir.exists():
            return []
        out = []
        for f in self.kb_dir.iterdir():
            if f.suffix == ".json" and f.is_file():
                rec = _read_json(f)
                if rec:
                    out.append(rec)
        return out

    def kb_list(self, tag=None, limit=None):
        """All entries, newest-updated first; optionally filtered by tag."""
        entries = self._kb_all()
        if tag:
            t = tag.strip().lower()
            entries = [e for e in entries if t in (e.get("tags") or [])]
        entries.sort(key=lambda e: e.get("updated_ts", 0), reverse=True)
        return entries[:limit] if limit else entries

    def kb_tags(self):
        """All distinct tags with their entry counts (for the UI tag cloud)."""
        counts = {}
        for e in self._kb_all():
            for t in e.get("tags") or []:
                counts[t] = counts.get(t, 0) + 1
        return dict(sorted(counts.items()))

    def kb_search(self, query, tag=None, limit=None):
        """Case-insensitive full-text search over title/body/tags. Results are
        ranked: title hits weigh most, then tags, then body; ties break by
        most-recently updated. An empty query degenerates to kb_list."""
        entries = self.kb_list(tag=tag)
        q = (query or "").strip().lower()
        if not q:
            return entries[:limit] if limit else entries
        terms = [w for w in q.split() if w]
        scored = []
        for e in entries:
            title = (e.get("title") or "").lower()
            body = (e.get("body") or "").lower()
            tags = " ".join(e.get("tags") or []).lower()
            score = 0
            for w in terms:
                if w in title:
                    score += 5
                if w in tags:
                    score += 3
                if w in body:
                    score += 1
            if score:
                scored.append((score, e.get("updated_ts", 0), e))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        ranked = [e for _, _, e in scored]
        return ranked[:limit] if limit else ranked

    # -- projects (issue #22) ---------------------------------------------
    # A Project groups tasks + channels under a named goal with milestones,
    # and rolls up progress from the durable task store. One JSON per project.

    def project_new(self, project_id, name="", goal="", owner="",
                    created_by=""):
        """Create a project (idempotent — returns the existing one untouched)."""
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        pid = _safe_name(project_id)
        path = self.projects_dir / f"{pid}.json"
        if not path.exists():
            _atomic_write_json(path, {
                "id": pid,
                "name": name or project_id,
                "goal": goal,
                "owner": owner,
                "milestones": [],          # [{name, done}]
                "task_ids": [],
                "channels": [],
                "created_by": created_by,
                "created_ts": _now(),
                "updated_ts": _now(),
            })
        return self.project_get(pid, rollup=False)

    def project_get(self, project_id, rollup=True):
        rec = _read_json(self.projects_dir / f"{_safe_name(project_id)}.json")
        if rec is None:
            return None
        if rollup:
            rec = dict(rec)
            rec["progress"] = self.project_progress(project_id)
        return rec

    def _project_save(self, rec):
        rec["updated_ts"] = _now()
        _atomic_write_json(self.projects_dir / f"{rec['id']}.json", rec)
        return rec

    def project_update(self, project_id, name=None, goal=None, owner=None):
        rec = self.project_get(project_id, rollup=False)
        if rec is None:
            return None
        if name is not None:
            rec["name"] = name
        if goal is not None:
            rec["goal"] = goal
        if owner is not None:
            rec["owner"] = owner
        return self._project_save(rec)

    def project_add_task(self, project_id, task_id):
        rec = self.project_get(project_id, rollup=False)
        if rec is None:
            return None
        if task_id not in rec["task_ids"]:
            rec["task_ids"].append(task_id)
            self._project_save(rec)
        return self.project_get(project_id)

    def project_add_channel(self, project_id, channel):
        rec = self.project_get(project_id, rollup=False)
        if rec is None:
            return None
        ch = _safe_name(channel)
        if ch not in rec["channels"]:
            rec["channels"].append(ch)
            self._project_save(rec)
        return self.project_get(project_id)

    def project_add_milestone(self, project_id, name, done=False):
        rec = self.project_get(project_id, rollup=False)
        if rec is None:
            return None
        if not any(m["name"] == name for m in rec["milestones"]):
            rec["milestones"].append({"name": name, "done": bool(done)})
            self._project_save(rec)
        return self.project_get(project_id)

    def project_set_milestone(self, project_id, name, done):
        rec = self.project_get(project_id, rollup=False)
        if rec is None:
            return None
        changed = False
        for m in rec["milestones"]:
            if m["name"] == name:
                m["done"] = bool(done)
                changed = True
        if changed:
            self._project_save(rec)
        return self.project_get(project_id)

    def project_delete(self, project_id):
        path = self.projects_dir / f"{_safe_name(project_id)}.json"
        if path.exists():
            path.unlink()
            return True
        return False

    def project_progress(self, project_id):
        """Roll up the project's task statuses into a progress summary."""
        rec = _read_json(self.projects_dir / f"{_safe_name(project_id)}.json")
        if rec is None:
            return None
        by_status = {}
        for tid in rec.get("task_ids", []):
            t = self.get_task(tid)
            status = t["status"] if t else "unknown"
            by_status[status] = by_status.get(status, 0) + 1
        total = sum(by_status.values())
        done = by_status.get("done", 0)
        ms = rec.get("milestones", [])
        ms_done = sum(1 for m in ms if m.get("done"))
        return {
            "total_tasks": total,
            "by_status": by_status,
            "done": done,
            "percent": round(100 * done / total) if total else 0,
            "milestones_total": len(ms),
            "milestones_done": ms_done,
        }

    def project_list(self):
        """All projects (newest-updated first), each with its progress rollup."""
        if not self.projects_dir.exists():
            return []
        out = []
        for f in self.projects_dir.iterdir():
            if f.suffix == ".json" and f.is_file():
                rec = _read_json(f)
                if rec:
                    rec["progress"] = self.project_progress(rec["id"])
                    out.append(rec)
        out.sort(key=lambda r: r.get("updated_ts", 0), reverse=True)
        return out

    # -- advisory locks (issue #10) ---------------------------------------
    # Cooperative (advisory) locks so agents avoid editing the same resource at
    # once. Lock-free + race-proof via O_EXCL create; a lock whose owner has
    # gone offline auto-expires so work is never stranded. Honoring them is by
    # convention — nothing forces an agent to check.

    def _lock_path(self, resource):
        import hashlib
        h = hashlib.sha1(resource.strip().encode("utf-8")).hexdigest()[:16]
        prefix = _safe_name(resource)[:60]
        return self.locks_dir / f"{prefix}-{h}.json"

    def _owner_offline(self, owner, online_window):
        """True if `owner` is a known agent that hasn't heart-beat within the
        window. Unknown owners (e.g. a human) never auto-expire."""
        rec = self.get_agent(owner)
        if not rec:
            return False
        if rec.get("status") == "offline":
            return True
        return (_now() - rec.get("last_seen", 0)) > online_window

    def acquire_lock(self, resource, owner, owner_name="", note="",
                     online_window=30.0):
        """Acquire an advisory lock on `resource` for `owner`.
        Returns {ok, lock, reason}. Re-acquiring your own lock refreshes it; a
        lock held by an OFFLINE owner is auto-expired and taken over."""
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        path = self._lock_path(resource)
        rec = {
            "resource": resource,
            "owner": owner,
            "owner_name": owner_name or owner,
            "note": note,
            "acquired_ts": _now(),
        }
        if _atomic_create_exclusive(path, rec):
            return {"ok": True, "lock": rec, "reason": "acquired"}
        existing = _read_json(path)
        if existing is None:              # raced with a release; try once more
            if _atomic_create_exclusive(path, rec):
                return {"ok": True, "lock": rec, "reason": "acquired"}
            existing = _read_json(path) or {}
        if existing.get("owner") == owner:
            _atomic_write_json(path, rec)         # refresh held lock
            return {"ok": True, "lock": rec, "reason": "refreshed"}
        if self._owner_offline(existing.get("owner", ""), online_window):
            rec["stole_from"] = existing.get("owner")
            _atomic_write_json(path, rec)         # owner offline → take over
            return {"ok": True, "lock": rec, "reason": "expired-takeover"}
        return {"ok": False, "lock": existing, "reason": "held"}

    def release_lock(self, resource, owner, force=False):
        """Release a lock. Only the owner may release it unless force=True.
        Returns True if a lock was removed."""
        path = self._lock_path(resource)
        existing = _read_json(path)
        if existing is None:
            return False
        if not force and existing.get("owner") != owner:
            return False
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    def get_lock(self, resource, online_window=30.0):
        rec = _read_json(self._lock_path(resource))
        if rec is None:
            return None
        rec["expired"] = self._owner_offline(rec.get("owner", ""), online_window)
        rec["age"] = _now() - rec.get("acquired_ts", _now())
        return rec

    def list_locks(self, online_window=30.0, include_expired=True):
        """All advisory locks, each annotated with expired/age. Expired = the
        owner is an offline agent (the lock is reclaimable)."""
        if not self.locks_dir.exists():
            return []
        out = []
        for f in sorted(self.locks_dir.glob("*.json")):
            rec = _read_json(f)
            if not rec:
                continue
            rec["expired"] = self._owner_offline(rec.get("owner", ""), online_window)
            rec["age"] = _now() - rec.get("acquired_ts", _now())
            if include_expired or not rec["expired"]:
                out.append(rec)
        out.sort(key=lambda r: r.get("acquired_ts", 0), reverse=True)
        return out


# -- @mentions (issue #52) -------------------------------------------------

import re as _re_mentions
_MENTION_RE = _re_mentions.compile(r"@([A-Za-z0-9][A-Za-z0-9_.\-]*)")
_MENTION_ALL = {"all", "everyone", "channel", "here"}


def extract_mentions(text):
    """Lower-cased @mentions in a message ('@Worker1 hi' -> {'worker1'})."""
    return {m.lower() for m in _MENTION_RE.findall(text or "")}


def message_mentions(text, viewer):
    """True if `text` @mentions `viewer` (by name) or @all/@everyone/@here."""
    ms = extract_mentions(text)
    if not ms:
        return False
    return viewer.lower() in ms or bool(ms & _MENTION_ALL)


def collect_mentions(store, viewer, since_ts=0.0, limit=None):
    """All channel + broadcast messages that @mention `viewer` (newest first).
    Self-authored messages are skipped (you don't get notified by your own posts).
    Each result carries its 'channel' (or '*' for broadcast) for context."""
    hits = []
    for ch in store.list_channels():
        for m in store.read_channel(ch["name"], since_ts=since_ts):
            if (m.get("author") != viewer and m.get("author_name") != viewer
                    and message_mentions(m.get("text", ""), viewer)):
                hits.append({**m, "channel": m.get("channel", ch["name"])})
    for m in store.read_broadcast(since_ts=since_ts):
        if (m.get("author") != viewer and m.get("author_name") != viewer
                and message_mentions(m.get("text", ""), viewer)):
            hits.append({**m, "channel": "*"})
    hits.sort(key=lambda m: m.get("ts", 0), reverse=True)
    return hits[:limit] if limit else hits
