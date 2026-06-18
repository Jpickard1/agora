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

Reliable delivery (A1): a turn-based agent only reads stdin between turns, so
injecting WHILE it is mid-turn corrupts the prompt or loses the message. The
bridge therefore *queues* incoming messages and only injects when the pane is
idle (no "esc to interrupt"/spinner and the screen has settled). A per-message
``--max-wait`` is a safety valve so a wrongly-detected-busy pane never starves
delivery forever. Pass ``--no-idle-wait`` to restore the old blind behaviour.

Mention routing (A4): a #channel message that @mentions only *other* agents is
not injected into this agent's pane, so agents aren't interrupted by chatter
that isn't for them. A message with no @mention (general) or one that mentions
this agent / @all reaches it. The message still posts to the channel unchanged
(the web UI shows the full stream); only terminal injection is filtered. Pass
``--firehose`` to opt into the full stream.

Your agent then replies with ordinary commands, e.g.:
    hubcli post -c general "on it"            # to the channel
    hubcli send <agent-id> "done"            # direct to another agent

Usage (run it from inside the agent's tmux pane, in the background):
    nohup python -m agenthub.bridge --name trainer >/tmp/hub-trainer.log 2>&1 &
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time

from .config import resolve_root
from .store import HubStore


# A mention is @ followed by an agent name/id (letters, digits, _ . -).
_MENTION_RE = re.compile(r"@([A-Za-z0-9][A-Za-z0-9_.\-]*)")
# Mentions that mean "everyone on the channel".
_MENTION_ALL = {"all", "everyone", "channel", "here"}


# Markers that mean the agent (Claude Code or similar) is actively working and
# is NOT ready to receive a new prompt. "esc to interrupt" is the dependable
# Claude Code signal; the braille glyphs catch generic spinners.
BUSY_TEXT = (
    "esc to interrupt",
    "interrupt to stop",
    "ctrl+b to run in background",
)
BRAILLE = set(
    "⠁⠂⠃⠄⠅⠆⠇⠈⠉⠊⠋⠌⠍⠎⠏⠐⠑⠒⠓⠔⠕⠖⠗⠘⠙⠚⠛⠜⠝⠞⠟"
    "⠠⠡⠢⠣⠤⠥⠦⠧⠨⠩⠪⠫⠬⠭⠮⠯⠰⠱⠲⠳⠴⠵⠶⠷⠸⠹⠺⠻⠼⠽⠾⠿"
    "⣾⣽⣻⢿⡿⣟⣯⣷"
)


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


def capture_pane(pane: str) -> str:
    """Return the current visible contents of the pane (best-effort)."""
    try:
        return subprocess.check_output(
            ["tmux", "capture-pane", "-p", "-t", pane],
            text=True, stderr=subprocess.DEVNULL,
        )
    except Exception:
        return ""


def pane_busy(text: str) -> bool:
    """True if the pane shows the agent is mid-turn (not ready for input)."""
    low = text.lower()
    if any(m in low for m in BUSY_TEXT):
        return True
    # A live spinner glyph on screen means it's still rendering/working.
    return any(ch in BRAILLE for ch in text)


def is_self_message(m: dict, aid: str, name: str) -> bool:
    """A3 loop-prevention: don't re-inject our own posts or delivery receipts.
    Keyed on the stable agent id first, display name second."""
    if m.get("author") == aid or m.get("author_name") == name:
        return True
    return (m.get("meta") or {}).get("msg_kind") in ("delivery_receipt",)


def ready_to_flush(idle_streak: int, settle_checks: int,
                   head_age: float, max_wait: float) -> bool:
    """Decide whether a queued message may be injected now: either the pane has
    been idle for `settle_checks` consecutive observations, or the message has
    waited longer than `max_wait` (safety valve against mis-detected busy)."""
    return idle_streak >= settle_checks or head_age >= max_wait


def build_receipt(m: dict, aid: str, where: str) -> tuple[str, str, dict]:
    """A2: build a delivery receipt for a message we just injected. Returns
    (recipient_inbox, text, meta). The receipt is addressed back to the original
    sender so a manager/supervisor can tell 'delivered' from 'agent stuck'."""
    to = m.get("author") or "unknown"
    meta = {
        "msg_kind": "delivery_receipt",
        "in_reply_to": m.get("id"),
        "delivered_to": aid,
        "where": where,
        "delivered_ts": time.time(),
    }
    return to, f"✓ delivered to {aid} ({where})", meta


def extract_mentions(text: str) -> set[str]:
    """Lower-cased @mentions in a message ('@Worker1, hi' -> {'worker1'})."""
    return {m.lower() for m in _MENTION_RE.findall(text or "")}


def channel_msg_for_me(text: str, aid: str, name: str,
                       firehose: bool = False) -> bool:
    """A4 mention routing: should a #channel message be injected into THIS
    agent's pane? Yes if the agent opted into the full stream (firehose), or the
    message has no @mention (a general message for everyone), or it mentions this
    agent (by id or name) or @all/@everyone/@channel/@here. A message that
    mentions ONLY other agents is skipped so this agent isn't interrupted."""
    if firehose:
        return True
    mentions = extract_mentions(text)
    if not mentions:
        return True
    me = {aid.lower(), name.lower()}
    return bool(mentions & me) or bool(mentions & _MENTION_ALL)


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
    ap.add_argument("--no-idle-wait", action="store_true",
                    help="Inject immediately instead of waiting for an idle pane (old behaviour)")
    ap.add_argument("--settle-checks", type=int, default=2,
                    help="Consecutive idle observations required before injecting")
    ap.add_argument("--max-wait", type=float, default=120.0,
                    help="Force-deliver a queued message after this many seconds even if the pane looks busy")
    ap.add_argument("--firehose", "--no-mention-filter", dest="firehose",
                    action="store_true",
                    help="See the full channel stream: inject every #channel message even if it @mentions only other agents")
    ap.add_argument("--no-receipts", dest="receipts", action="store_false",
                    help="Don't write a delivery receipt when a message is injected")
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
    idle_mode = pane is not None and not args.no_idle_wait
    print(f"[bridge] '{args.name}' connected (id={aid}, host={host}, pane={pane}). "
          f"Listening on #{args.channel} + direct inbox + broadcasts. "
          f"idle-wait={'on' if idle_mode else 'off'}.")

    start = 0.0 if args.history else time.time()
    chan_cursor = start
    inbox_cursor = start
    bcast_cursor = start

    # A1: messages wait here until the pane is idle. Each entry is
    # (enqueued_ts, where, message).
    pending: list[tuple[float, str, dict]] = []
    last_snapshot = ""
    idle_streak = 0

    def is_mine(m) -> bool:
        return is_self_message(m, aid, args.name)

    def deliver(where: str, m: dict) -> None:
        line = f"[HUB {where} from {m['author_name']}]: {m['text']}"
        if pane:
            inject(pane, line)
        else:
            print(line)
        # A2: confirm delivery back to the sender (only for real injections, and
        # never for our own messages — those are filtered out before delivery).
        if pane and args.receipts:
            to, text, meta = build_receipt(m, aid, where)
            if to and to != aid:
                try:
                    store.post_inbox(to, text, author=aid, author_name=args.name,
                                     author_kind="system", host=host, meta=meta)
                except Exception as e:
                    print(f"[bridge] receipt write failed: {e}", flush=True)

    try:
        while True:
            store.heartbeat(aid, activity=f"listening on #{args.channel}")

            # 1. Collect new messages from all sources into the queue (in order).
            fresh: list[tuple[str, dict]] = []
            for m in store.read_channel(args.channel, since_ts=chan_cursor):
                chan_cursor = max(chan_cursor, m["ts"])
                # A4: skip channel messages that @mention only other agents, so
                # we don't interrupt this agent with chatter that isn't for it.
                if (not is_mine(m)
                        and channel_msg_for_me(m["text"], aid, args.name,
                                               firehose=args.firehose)):
                    fresh.append((f"#{args.channel}", m))
            for m in store.read_inbox(aid, since_ts=inbox_cursor):
                inbox_cursor = max(inbox_cursor, m["ts"])
                if not is_mine(m):
                    fresh.append(("direct-to-you", m))
            for m in store.read_broadcast(since_ts=bcast_cursor):
                bcast_cursor = max(bcast_cursor, m["ts"])
                if not is_mine(m):
                    fresh.append(("broadcast-to-all", m))
            fresh.sort(key=lambda x: x[1]["ts"])
            now = time.time()
            pending.extend((now, where, m) for where, m in fresh)

            # 2. Try to flush the queue when the pane is ready.
            if pending:
                if not idle_mode:
                    # Old behaviour: inject everything immediately.
                    for _, where, m in pending:
                        deliver(where, m)
                    pending.clear()
                    idle_streak = 0
                else:
                    snap = capture_pane(pane)
                    busy = pane_busy(snap)
                    settled = (snap == last_snapshot)
                    last_snapshot = snap
                    idle_streak = idle_streak + 1 if (not busy and settled) else 0

                    head_age = time.time() - pending[0][0]
                    ready = idle_streak >= args.settle_checks
                    if ready_to_flush(idle_streak, args.settle_checks,
                                      head_age, args.max_wait):
                        _, where, m = pending.pop(0)
                        if not ready:
                            print(f"[bridge] max-wait hit ({head_age:.0f}s); "
                                  f"force-delivering despite busy pane.", flush=True)
                        deliver(where, m)
                        # Injecting makes the agent busy again; re-settle before
                        # the next queued message so we serialise cleanly.
                        idle_streak = 0
                        last_snapshot = ""

            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        store.set_agent_status(aid, "offline")


if __name__ == "__main__":
    main()
