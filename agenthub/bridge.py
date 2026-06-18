"""hub bridge -- connect a LIVE Claude Code agent (running in tmux) to the hub.

This is the piece that makes a normal, interactive `claude` agent "listen" to
the hub without any API or headless mode. You run it in the BACKGROUND from
inside your agent's tmux pane; it:

  1. registers your agent and heartbeats it (so it shows "online" in the web UI
     for as long as your tmux session lives),
  2. watches the hub for new messages (a channel + this agent's direct inbox +
     broadcasts), and
  3. types each incoming message into this tmux pane via `tmux send-keys`, so
     your interactive Claude Code agent sees it as if you had typed it.

Your agent then replies with ordinary commands, e.g.:
    hubcli post -c general "on it"            # to the channel
    hubcli send <agent-id> "done"            # direct to another agent

Usage (run it from inside the agent's tmux pane, in the background):
    nohup python -m agenthub.bridge --name trainer >/tmp/hub-trainer.log 2>&1 &
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time

from .config import resolve_root
from .store import HubStore


def detect_pane(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    pane = os.environ.get("TMUX_PANE")
    if pane:
        return pane
    try:
        return subprocess.check_output(
            ["tmux", "display-message", "-p", "#{pane_id}"], text=True
        ).strip() or None
    except Exception:
        return None


def inject(pane: str, text: str) -> None:
    """Type `text` into the agent's pane, then press Enter (submit the prompt).
    Newlines are flattened so the whole message is submitted as one prompt."""
    one_line = " ".join(text.splitlines())
    # `--` guards against text starting with a dash; `-l` sends it literally.
    subprocess.run(["tmux", "send-keys", "-t", pane, "-l", "--", one_line],
                   check=False)
    subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=False)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hub-bridge")
    ap.add_argument("--name", required=True, help="This agent's name (also its hub id)")
    ap.add_argument("--channel", default="general", help="Channel to listen to")
    ap.add_argument("--root", default=None, help="Hub root (else env/pointer)")
    ap.add_argument("--pane", default=None, help="tmux pane id (else auto-detect)")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--history", action="store_true",
                    help="Also deliver messages from before the bridge started")
    args = ap.parse_args(argv)

    pane = detect_pane(args.pane)
    if not shutil.which("tmux"):
        print("[bridge] WARNING: tmux not found; will print messages instead of injecting.")
        pane = None
    elif not pane:
        print("[bridge] WARNING: no tmux pane detected; will print messages instead. "
              "Run me from inside the agent's tmux pane, or pass --pane.")

    store = HubStore(resolve_root(args.root))
    store.init()
    aid = args.name  # keep it simple: the agent's name IS its hub id (use unique names)
    host = os.uname().nodename.split(".")[0]
    store.register_agent(aid, args.name, host=host, pid=os.getpid(),
                         capabilities=["claude-code"])
    store.heartbeat(aid, activity=f"listening on #{args.channel}")
    print(f"[bridge] '{args.name}' connected (id={aid}, host={host}, pane={pane}). "
          f"Listening on #{args.channel} + direct inbox + broadcasts.")

    start = 0.0 if args.history else time.time()
    chan_cursor = start
    inbox_cursor = start
    bcast_cursor = start

    def is_mine(m) -> bool:
        return m.get("author") == aid or m.get("author_name") == args.name

    try:
        while True:
            store.heartbeat(aid, activity=f"listening on #{args.channel}")
            incoming = []
            for m in store.read_channel(args.channel, since_ts=chan_cursor):
                chan_cursor = max(chan_cursor, m["ts"])
                if not is_mine(m):
                    incoming.append((f"#{args.channel}", m))
            for m in store.read_inbox(aid, since_ts=inbox_cursor):
                inbox_cursor = max(inbox_cursor, m["ts"])
                if not is_mine(m):
                    incoming.append(("direct-to-you", m))
            for m in store.read_broadcast(since_ts=bcast_cursor):
                bcast_cursor = max(bcast_cursor, m["ts"])
                if not is_mine(m):
                    incoming.append(("broadcast-to-all", m))

            incoming.sort(key=lambda x: x[1]["ts"])
            for where, m in incoming:
                line = f"[HUB {where} from {m['author_name']}]: {m['text']}"
                if pane:
                    inject(pane, line)
                else:
                    print(line)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        store.set_agent_status(aid, "offline")


if __name__ == "__main__":
    main()
