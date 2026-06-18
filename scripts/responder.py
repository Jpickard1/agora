#!/usr/bin/env python3
"""A persistent presence + auto-responder agent for the hub.

It keeps an agent shown as "online", and gives you immediate feedback when you
type into the UI:
  * a new human message in #general  -> posts a short acknowledgement reply
  * a direct instruction (inbox)      -> acknowledges it
  * an agent-to-agent request         -> replies (ping->pong, else echo)

This is a lightweight bridge to prove the hub is live, NOT a reasoning agent.
Your real agents would register themselves and respond with actual work.

    AGENT_HUB_ROOT=/ewsc/jpickard/.agent-hub python scripts/responder.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub.client import HubClient  # noqa: E402

CHANNEL = "general"


def main():
    hub = HubClient(name="claude", agent_id="claude-assistant", kind="agent")
    hub.register(capabilities=["assistant", "answers"])
    hub.set_activity(f"listening on #{CHANNEL}")
    print(f"responder online as {hub.id}, watching #{CHANNEL}")

    chan_cursor = time.time()  # only react to messages from now on
    while True:
        hub.heartbeat(activity=f"listening on #{CHANNEL}")

        # Direct instructions + broadcasts to me
        for m in hub.poll_inbox():
            if hub.is_request(m):
                q = m["text"].strip().lower()
                hub.reply(m, "pong" if q in ("ping", "ping?") else f"received: {m['text']}")
            else:
                scope = "broadcast" if m.get("to") == "*" else "instruction"
                hub.post(CHANNEL, f"✅ got your {scope}: \"{m['text']}\" — acknowledged.")

        # New human messages in the channel
        for m in hub.read(CHANNEL, since_ts=chan_cursor):
            chan_cursor = max(chan_cursor, m["ts"])
            if m.get("author_kind") == "human":
                hub.post(CHANNEL,
                         f"👋 @{m['author_name']} I see your message: \"{m['text']}\". "
                         f"(Auto-responder online — connect your real agents to do the work.)")

        time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
