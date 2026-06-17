#!/usr/bin/env python3
"""A tiny example agent that connects to the hub.

It registers, announces itself on #general, then listens for directed
instructions and echoes how it would act on them. Run several copies (on any
server sharing the hub filesystem) to see them appear in the UI.

    python scripts/demo_agent.py --name trainer --caps gpu,train
"""

import argparse
import time

from agenthub.client import HubClient


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
    print(f"[{args.name}] registered as {hub.id}. Listening for instructions… Ctrl-C to quit.")

    def on_instruction(msg):
        print(f"[{args.name}] 📨 instruction from {msg['author_name']}: {msg['text']}")
        # Acknowledge back on the channel so it shows in the UI.
        hub.post("general", f"{args.name} ack: '{msg['text']}'")

    try:
        hub.watch_inbox(on_instruction, interval=2.0)
    finally:
        hub.post("general", f"{args.name} going offline")


if __name__ == "__main__":
    main()
