#!/usr/bin/env python3
"""A REAL Claude-powered agent for the hub.

Unlike scripts/responder.py (a dumb echo bot), this agent answers messages
intelligently by shelling out to the local `claude` CLI in headless mode
(`claude -p`). It uses your existing Claude Code auth -- no API key needed.

For each new human message in #general (or a direct instruction sent to it), it:
  1. shows "thinking…" as its activity,
  2. asks the claude CLI for a reply (with recent chat as context),
  3. posts the reply back.

    AGENT_HUB_ROOT=/ewsc/jpickard/.agent-hub \
        CLAUDE_BIN=/home/unix/jpickard/.local/bin/claude \
        python scripts/claude_agent.py
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def daemonize(logfile: str):
    """Classic double-fork so we fully detach from the launching shell and
    survive its death (needed because this environment reaps a command's whole
    process tree when it exits)."""
    if os.fork() > 0:
        os._exit(0)          # parent exits immediately -> launch cmd returns
    os.setsid()              # new session, no controlling terminal
    if os.fork() > 0:
        os._exit(0)          # second fork: can never reacquire a terminal
    sys.stdout.flush(); sys.stderr.flush()
    fd = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)

from agenthub.client import HubClient  # noqa: E402

CHANNEL = "general"
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/home/unix/jpickard/.local/bin/claude")
NEUTRAL_CWD = os.environ.get("AGENT_HUB_ROOT", os.path.expanduser("~"))
TIMEOUT = float(os.environ.get("CLAUDE_TIMEOUT", "150"))

SYSTEM = (
    "You are 'claude', an AI agent connected to a team chat hub (Discord-style) "
    "for the user jpickard's agents running across several servers. Reply to the "
    "latest message helpfully and concisely in plain text (a few sentences, no "
    "markdown headers). You are chatting, not performing file operations."
)


def ask_claude(transcript: str, author: str, text: str) -> str:
    prompt = (
        f"{SYSTEM}\n\n"
        f"Recent #{CHANNEL} messages:\n{transcript}\n\n"
        f"The latest message is from {author}: {text!r}\n"
        f"Write your reply now."
    )
    cmd = [CLAUDE_BIN, "-p"]
    if os.environ.get("FULL_TOOLS") == "1":
        # User-authorized full-agent mode: tools work without a human approver.
        cmd += ["--permission-mode", "bypassPermissions"]
    else:
        # Text-only: deny every action tool so it can only chat.
        cmd += ["--disallowedTools", "Bash Edit Write NotebookEdit WebFetch Task"]
    cmd.append(prompt)
    try:
        out = subprocess.run(
            cmd, cwd=NEUTRAL_CWD, capture_output=True, text=True, timeout=TIMEOUT,
        )
        reply = (out.stdout or "").strip()
        if not reply:
            reply = f"(claude returned no text; stderr: {(out.stderr or '').strip()[:200]})"
        return reply
    except subprocess.TimeoutExpired:
        return "(I took too long to answer that — try again or simplify the question.)"
    except Exception as e:
        return f"(error calling claude: {e})"


def transcript_of(hub: HubClient, limit: int = 8) -> str:
    msgs = hub.read(CHANNEL, limit=limit)
    return "\n".join(f"{m['author_name']}: {m['text']}" for m in msgs) or "(empty)"


def main():
    hub = HubClient(name="claude", agent_id="claude-assistant", kind="agent")
    hub.register(capabilities=["assistant", "reasoning", "claude-cli"])
    hub.set_activity(f"listening on #{CHANNEL}")
    print(f"Claude agent online as {hub.id}, powered by {CLAUDE_BIN}")
    hub.post(CHANNEL, "🧠 Real Claude agent connected — ask me anything in this channel.")

    chan_cursor = time.time()
    while True:
        hub.heartbeat(activity=f"listening on #{CHANNEL}")

        # Collect new human messages in the channel + any direct instructions.
        jobs = []
        for m in hub.read(CHANNEL, since_ts=chan_cursor):
            chan_cursor = max(chan_cursor, m["ts"])
            if m.get("author_kind") == "human":
                jobs.append(("channel", m))
        for m in hub.poll_inbox():
            if hub.is_request(m):
                hub.reply(m, ask_claude(transcript_of(hub), m["author_name"], m["text"]))
            else:
                jobs.append(("inbox", m))

        for source, m in jobs:
            hub.set_activity(f"thinking about: {m['text'][:50]}")
            reply = ask_claude(transcript_of(hub), m["author_name"], m["text"])
            hub.post(CHANNEL, reply)
            hub.set_activity(f"listening on #{CHANNEL}")

        time.sleep(2)


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        daemonize(os.path.join(NEUTRAL_CWD, "claude_agent.log"))
    try:
        main()
    except KeyboardInterrupt:
        pass
