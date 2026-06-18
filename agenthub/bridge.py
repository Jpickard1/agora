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

from . import transport as transport_mod
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


def detect_session(pane: str | None) -> str | None:
    """The tmux session name containing `pane` (for the roster panel)."""
    if not pane:
        return None
    try:
        return subprocess.check_output(
            ["tmux", "display-message", "-p", "-t", pane, "#{session_name}"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip() or None
    except Exception:
        return None


def compute_status(has_pane: bool, busy: bool, pending: int) -> str:
    """A5 live status for the roster: working (mid-turn) > waiting (idle with
    queued messages about to deliver) > listening (idle, nothing queued).
    Without a pane we can't tell, so we report 'listening'."""
    if has_pane and busy:
        return "working"
    if pending:
        return "waiting"
    return "listening"


def classify_liveness(has_pane: bool, busy: bool, unchanged_for: float, *,
                      wedged_after: float = 20.0, idle_after: float = 45.0) -> str:
    """Liveness sub-status (issue #53): is the agent actually keeping up, beyond
    just heartbeating online? `unchanged_for` = seconds since the pane's output
    last changed.
      - no pane: can't judge -> 'responsive' (the bridge is alive)
      - busy + output frozen for >= wedged_after -> 'wedged' (stuck mid-turn —
        input injected but nothing is being produced; the case that had the
        manager pinging an "online" agent that wasn't consuming input)
      - busy + output moving -> 'busy' (actively producing)
      - idle pane, changed recently (< idle_after) -> 'responsive' (just finished)
      - idle pane, quiet a while -> 'idle' (free for work)
    """
    if not has_pane:
        return "responsive"
    if busy:
        return "wedged" if unchanged_for >= wedged_after else "busy"
    return "responsive" if unchanged_for < idle_after else "idle"


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


def parse_channels(spec: str | None) -> list[str]:
    """Parse a comma-separated --channels value into a clean, de-duplicated list
    (order preserved). A leading '#' on any name is tolerated."""
    seen: set[str] = set()
    out: list[str] = []
    for c in (spec or "").split(","):
        c = c.strip().lstrip("#")
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def resolve_channels(store, all_channels: bool, channels_spec: str | None,
                     default_channel: str) -> list[str]:
    """The channel names this bridge should currently follow:
      --all-channels -> every channel that exists (re-resolved live so new ones
                        created later are picked up automatically),
      --channels a,b -> exactly those,
      else           -> [default_channel] (single-channel back-compat)."""
    if all_channels:
        return [c["name"] for c in store.list_channels()]
    if channels_spec:
        return parse_channels(channels_spec)
    return [default_channel]


def channels_activity(names: list[str]) -> str:
    """Roster 'activity' string describing the set of followed channels."""
    if not names:
        return "no channels"
    if len(names) == 1:
        return f"on #{names[0]}"
    if len(names) <= 3:
        return "on " + ", ".join("#" + n for n in names)
    return f"on {len(names)} channels"


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
    ap.add_argument("--channel", default="general",
                    help="Channel to listen to (single-channel default)")
    ap.add_argument("--channels", default=None,
                    help="Comma-separated channels to follow, e.g. general,dev,alerts")
    ap.add_argument("--all-channels", dest="all_channels", action="store_true",
                    help="Follow EVERY channel, including ones created later")
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
    ap.add_argument("--wedged-after", type=float, default=20.0,
                    help="Liveness: pane busy with no output change for this many seconds => 'wedged'")
    ap.add_argument("--idle-after", type=float, default=45.0,
                    help="Liveness: idle pane quiet for this many seconds => 'idle' (else 'responsive')")
    ap.add_argument("--firehose", "--no-mention-filter", dest="firehose",
                    action="store_true",
                    help="See the full channel stream: inject every #channel message even if it @mentions only other agents")
    ap.add_argument("--no-receipts", dest="receipts", action="store_false",
                    help="Don't write a delivery receipt when a message is injected")
    ap.add_argument("--transport", choices=["auto", "tmux", "file", "stdout"],
                    default="auto",
                    help="Delivery transport: tmux injection (Unix), a polled "
                         "inbox file (Windows/no-tmux), or stdout. Default: auto.")
    ap.add_argument("--inbox-file", default=None,
                    help="Path for the 'file' transport (default <root>/inbox-<name>.txt)")
    args = ap.parse_args(argv)

    store = HubStore(resolve_root(args.root))
    store.init()
    aid = args.name  # keep it simple: the agent's name IS its hub id (use unique names)
    host = transport_mod.hostname()

    # Choose a cross-platform delivery transport (issue #15). tmux when usable;
    # otherwise a file inbox the agent tails — the Windows / no-tmux path.
    pane = detect_pane(args.pane)
    has_tmux = bool(shutil.which("tmux"))
    kind = transport_mod.choose_transport(args.transport, has_tmux, bool(pane))
    if kind == "tmux":
        transport = transport_mod.TmuxTransport(
            pane, inject, lambda: pane_busy(capture_pane(pane)))
    elif kind == "file":
        inbox = args.inbox_file or transport_mod.default_inbox_path(store.root, aid)
        transport = transport_mod.FileTransport(inbox)
        if args.transport == "auto" and not has_tmux:
            print(f"[bridge] tmux not found → file transport: delivering to {inbox}")
        elif args.transport == "auto" and not pane:
            print(f"[bridge] no tmux pane → file transport: delivering to {inbox}")
        pane = None   # no tmux idle-detection in file mode
    else:  # stdout
        transport = transport_mod.StdoutTransport()
        pane = None
    session = detect_session(pane)
    # A5: stash the tmux session + transport in `extra` (no store schema change)
    # so the roster panel can show name / server / tmux session / status.
    store.register_agent(aid, args.name, host=host, pid=os.getpid(),
                         capabilities=["claude-code"],
                         extra={"tmux_session": session, "transport": transport.kind})
    channels = resolve_channels(store, args.all_channels, args.channels,
                                args.channel)
    store.heartbeat(aid, status="listening", activity=channels_activity(channels))
    # Idle-wait (settle detection) is a tmux-only concept; file/stdout deliver now.
    idle_mode = transport.kind == "tmux" and not args.no_idle_wait
    chans_desc = ("ALL channels (auto-following new ones)" if args.all_channels
                  else ", ".join("#" + c for c in channels) or "(none)")
    print(f"[bridge] '{args.name}' connected (id={aid}, host={host}, "
          f"transport={transport.kind}). "
          f"Listening on {chans_desc} + direct inbox + broadcasts. "
          f"idle-wait={'on' if idle_mode else 'off'}.")

    start = 0.0 if args.history else time.time()
    # Per-channel cursors so each channel is tracked independently. New channels
    # discovered later (in --all-channels mode) start from 'now' (or 0 with
    # --history) so we don't replay their whole backlog on first sight.
    chan_cursors: dict[str, float] = {c: start for c in channels}
    inbox_cursor = start
    bcast_cursor = start

    # A1: messages wait here until the pane is idle. Each entry is
    # (enqueued_ts, where, message).
    pending: list[tuple[float, str, dict]] = []
    last_snapshot = ""
    idle_streak = 0
    # Liveness tracking (issue #53): when the pane output last changed, so we can
    # tell a 'wedged' agent (busy but frozen) from a 'busy' one (still producing).
    last_live_snap = ""
    last_change_ts = time.time()

    def is_mine(m) -> bool:
        return is_self_message(m, aid, args.name)

    def deliver(where: str, m: dict) -> None:
        line = f"[HUB {where} from {m['author_name']}]: {m['text']}"
        transport.deliver(line)
        # A2: confirm delivery back to the sender for real deliveries (tmux/file),
        # never for our own messages (filtered out before delivery) or stdout echo.
        if args.receipts and transport.kind != "stdout":
            to, text, meta = build_receipt(m, aid, where)
            if to and to != aid:
                try:
                    store.post_inbox(to, text, author=aid, author_name=args.name,
                                     author_kind="system", host=host, meta=meta)
                except Exception as e:
                    print(f"[bridge] receipt write failed: {e}", flush=True)

    try:
        while True:
            # Sample the pane once per loop: drives both the live roster status
            # (A5) and the idle-delivery gate (A1).
            snap = capture_pane(pane) if pane else ""
            busy = pane_busy(snap) if pane else False
            if idle_mode:
                settled = (snap == last_snapshot)
                last_snapshot = snap
                idle_streak = idle_streak + 1 if (not busy and settled) else 0
            # Liveness (#53): track when the pane output last changed.
            if snap != last_live_snap:
                last_live_snap = snap
                last_change_ts = time.time()
            liveness = classify_liveness(
                pane is not None, busy, time.time() - last_change_ts,
                wedged_after=args.wedged_after, idle_after=args.idle_after)

            # 1. Collect new messages from all sources into the queue (in order).
            # In --all-channels mode, re-resolve the live channel set each loop so
            # channels created after we started are followed automatically.
            if args.all_channels:
                added = False
                for c in resolve_channels(store, True, None, args.channel):
                    if c not in chan_cursors:
                        chan_cursors[c] = start if args.history else time.time()
                        print(f"[bridge] now following new channel #{c}", flush=True)
                        added = True
                if added:
                    channels = list(chan_cursors.keys())
                    store.heartbeat(aid, activity=channels_activity(channels))

            fresh: list[tuple[str, dict]] = []
            for ch in list(chan_cursors.keys()):
                for m in store.read_channel(ch, since_ts=chan_cursors[ch]):
                    chan_cursors[ch] = max(chan_cursors[ch], m["ts"])
                    # A4: skip channel messages that @mention only other agents,
                    # so we don't interrupt this agent with chatter not for it.
                    if (not is_mine(m)
                            and channel_msg_for_me(m["text"], aid, args.name,
                                                   firehose=args.firehose)):
                        fresh.append((f"#{ch}", m))
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

            # A5 status + #53 liveness for the roster (preserve free-text activity).
            store.heartbeat(aid, status=compute_status(pane is not None, busy,
                                                       len(pending)),
                            liveness=liveness)

            # 2. Try to flush the queue when the pane is ready.
            if pending:
                if not idle_mode:
                    # Old behaviour: inject everything immediately.
                    for _, where, m in pending:
                        deliver(where, m)
                    pending.clear()
                    idle_streak = 0
                else:
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
