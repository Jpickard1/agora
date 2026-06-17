"""Importable client for agents to talk to the hub.

Agents do not need the web server running -- they talk to the shared
filesystem directly through this client. Typical use inside an agent::

    from agenthub.client import HubClient

    hub = HubClient(name="trainer")          # auto-resolves hub root
    hub.register(capabilities=["gpu", "train"])
    hub.post("general", "training started")

    for msg in hub.poll_inbox():             # instructions sent to me
        handle(msg["text"])

    hub.heartbeat()                          # call periodically to stay "online"
"""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from .config import resolve_root
from .store import HubStore


def default_agent_id(name: str) -> str:
    host = socket.gethostname().split(".")[0]
    return f"{name}-{host}-{os.getpid()}"


class HubClient:
    def __init__(self, name: str | None = None, agent_id: str | None = None,
                 root: str | None = None, kind: str = "agent"):
        self.root = resolve_root(root)
        self.store = HubStore(self.root)
        self.name = name or "agent"
        self.id = agent_id or default_agent_id(self.name)
        self.host = socket.gethostname().split(".")[0]
        self.kind = kind
        # only see instructions/broadcasts sent after we start
        self._inbox_cursor = time.time()
        self._broadcast_cursor = time.time()

    # -- identity ----------------------------------------------------------

    def register(self, capabilities: list[str] | None = None,
                 extra: dict | None = None) -> dict[str, Any]:
        return self.store.register_agent(
            self.id, self.name, host=self.host, pid=os.getpid(),
            kind=self.kind, capabilities=capabilities, extra=extra)

    def heartbeat(self, status: str = "online",
                  activity: str | None = None) -> dict[str, Any] | None:
        return self.store.heartbeat(self.id, status=status, activity=activity)

    def set_activity(self, activity: str) -> dict[str, Any] | None:
        """Report what this agent is currently doing (shown in the UI)."""
        return self.store.heartbeat(self.id, activity=activity)

    def goodbye(self) -> None:
        self.store.set_agent_status(self.id, "offline")

    # -- sending -----------------------------------------------------------

    def post(self, channel: str, text: str, meta: dict | None = None):
        return self.store.post_channel(
            channel, text, author=self.id, author_name=self.name,
            author_kind=self.kind, host=self.host, meta=meta)

    def send_to(self, agent_id: str, text: str, meta: dict | None = None):
        """Send a directed message/instruction to another agent's inbox."""
        return self.store.post_inbox(
            agent_id, text, author=self.id, author_name=self.name,
            author_kind=self.kind, host=self.host, meta=meta)

    def broadcast(self, text: str, meta: dict | None = None):
        """Send an instruction to every agent (now and future)."""
        return self.store.post_broadcast(
            text, author=self.id, author_name=self.name,
            author_kind=self.kind, host=self.host, meta=meta)

    # -- reading -----------------------------------------------------------

    def read(self, channel: str, since_ts: float = 0.0, limit: int | None = None):
        return self.store.read_channel(channel, since_ts=since_ts, limit=limit)

    def inbox(self, since_ts: float = 0.0, limit: int | None = None):
        return self.store.read_inbox(self.id, since_ts=since_ts, limit=limit)

    def agents(self, online_window: float = 30.0):
        return self.store.list_agents(online_window=online_window)

    def channels(self):
        return self.store.list_channels()

    # -- convenience polling ----------------------------------------------

    def poll_inbox(self, include_broadcast: bool = True) -> list[dict[str, Any]]:
        """Return new instructions since the last poll: directed messages to
        this agent, plus broadcasts to all agents (chronologically merged)."""
        msgs = self.store.read_inbox(self.id, since_ts=self._inbox_cursor)
        if msgs:
            self._inbox_cursor = max(m["ts"] for m in msgs)
        if include_broadcast:
            bc = self.store.read_broadcast(since_ts=self._broadcast_cursor)
            if bc:
                self._broadcast_cursor = max(m["ts"] for m in bc)
            msgs = sorted(msgs + bc, key=lambda m: m["ts"])
        return msgs

    def watch_inbox(self, on_message: Callable[[dict[str, Any]], Any],
                    interval: float = 2.0, heartbeat: bool = True) -> None:
        """Block forever, invoking on_message for each new instruction.

        Also sends a heartbeat every poll so the agent shows as online while
        it is listening. Ctrl-C to stop.
        """
        self.register()
        try:
            while True:
                if heartbeat:
                    self.heartbeat()
                for msg in self.poll_inbox():
                    on_message(msg)
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
        finally:
            self.goodbye()

    def stream_channel(self, channel: str, interval: float = 1.5
                       ) -> Iterator[dict[str, Any]]:
        """Yield new messages on a channel as they arrive (generator)."""
        cursor = time.time()
        while True:
            for msg in self.store.read_channel(channel, since_ts=cursor):
                cursor = max(cursor, msg["ts"])
                yield msg
            time.sleep(interval)
