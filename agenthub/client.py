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
        self._inbox_cursor = time.time()  # only see instructions sent after we start

    # -- identity ----------------------------------------------------------

    def register(self, capabilities: list[str] | None = None,
                 extra: dict | None = None) -> dict[str, Any]:
        return self.store.register_agent(
            self.id, self.name, host=self.host, pid=os.getpid(),
            kind=self.kind, capabilities=capabilities, extra=extra)

    def heartbeat(self, status: str = "online") -> dict[str, Any] | None:
        return self.store.heartbeat(self.id, status=status)

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

    def poll_inbox(self) -> list[dict[str, Any]]:
        """Return inbox messages received since the last poll (or since start)."""
        msgs = self.store.read_inbox(self.id, since_ts=self._inbox_cursor)
        if msgs:
            self._inbox_cursor = max(m["ts"] for m in msgs)
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
