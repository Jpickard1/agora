#!/usr/bin/env python3
"""A tiny example agent that connects to the hub.

It registers, announces itself on #general, then listens for directed
instructions and echoes how it would act on them. Run several copies (on any
server sharing the hub filesystem) to see them appear in the UI.

    python scripts/demo_agent.py --name trainer --caps gpu,train
"""

import argparse
import os
import sys
import time

# Make `agenthub` importable when run directly (without `pip install -e .`).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub.client import HubClient  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="demo")
    ap.add_argument("--caps", default="")
    ap.add_argument("--root", default=None)
    args = ap.parse_args()

    caps = [c.strip() for c in args.caps.split(",") if c.strip()]
    hub = HubClient(name=args.name, root=args.root)
    hub.register(capabilities=caps)
    hub.post("general", f"{args.name} online on {hub.host} (id={hub.id})")
    hub.set_activity("idle, waiting for work")
    print(f"[{args.name}] registered as {hub.id}. Listening for instructions… Ctrl-C to quit.")

    def on_instruction(msg):
        # Agent-to-agent request? Answer it directly (RPC-style).
        if hub.is_request(msg):
            q = msg["text"].strip().lower()
            answer = "pong" if q in ("ping", "ping?") else f"{args.name} received: {msg['text']}"
            print(f"[{args.name}] ❓ request from {msg['author_name']}: {msg['text']} → replying '{answer}'")
            hub.reply(msg, answer)
            return
        scope = "📢 broadcast" if msg.get("to") == "*" else "📨 direct"
        print(f"[{args.name}] {scope} from {msg['author_name']}: {msg['text']}")
        # Report that we're working on it (shows in the UI agent panel).
        hub.set_activity(f"working on: {msg['text'][:40]}")
        # Acknowledge back on the channel so it shows in the UI.
        hub.post("general", f"{args.name} ack: '{msg['text']}'")

    try:
        hub.watch_inbox(on_instruction, interval=2.0)
    finally:
        hub.post("general", f"{args.name} going offline")


if __name__ == "__main__":
    main()
