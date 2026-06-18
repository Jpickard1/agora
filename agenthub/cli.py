"""hubcli -- command line interface to the Agent Hub.

Works against the shared filesystem directly; the web server need not be
running. Designed to be trivially droppable into any agent or shell script.

Examples
--------
    hubcli init --root /ewsc/jpickard/.agent-hub      # one-time setup
    hubcli register --name trainer --caps gpu,train
    hubcli post -c general "training started on gpu01"
    hubcli read -c general --tail 20
    hubcli send <agent_id> "please pause and checkpoint"
    hubcli inbox --id trainer-gpu01-12345 --watch
    hubcli agents
    hubcli serve --host 0.0.0.0 --port 8787           # launch the web UI
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

from .config import resolve_root, resolve_token, write_pointer
from .store import HubStore
from .client import HubClient, default_agent_id


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _print_msg(m: dict) -> None:
    kind = m.get("author_kind", "agent")
    marker = {"human": "🧑", "system": "⚙", "agent": "🤖"}.get(kind, "•")
    dest = ""
    if m.get("to"):
        dest = f" → @{m['to']}"
    print(f"[{_fmt_ts(m['ts'])}] {marker} {m.get('author_name', m.get('author'))}{dest}: {m['text']}")


def _store(args) -> HubStore:
    return HubStore(resolve_root(getattr(args, "root", None)))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init(args):
    root = resolve_root(args.root)
    store = HubStore(root)
    cfg = store.init(token=args.token)
    write_pointer(root)
    print(f"Hub initialised at: {root}")
    print(f"Shared token:       {cfg['token']}")
    print(f"Pointer written to: ~/.agent-hub-path")
    print("\nShare these with your agents/servers:")
    print(f"  export AGENT_HUB_ROOT={root}")
    print(f"  export AGENT_HUB_TOKEN={cfg['token']}")


def cmd_register(args):
    store = _store(args)
    aid = args.id or default_agent_id(args.name)
    caps = [c.strip() for c in (args.caps or "").split(",") if c.strip()]
    rec = store.register_agent(
        aid, args.name, host=socket.gethostname().split(".")[0],
        kind=args.kind, capabilities=caps)
    print(f"Registered agent: {rec['id']}  (name={rec['name']}, host={rec['host']})")
    print("Use this id for inbox polling:")
    print(f"  hubcli inbox --id {rec['id']} --watch")


def cmd_post(args):
    store = _store(args)
    text = args.text if args.text is not None else sys.stdin.read().strip()
    if not text:
        print("Nothing to post (empty text).", file=sys.stderr)
        sys.exit(1)
    name = args.author or "cli"
    m = store.post_channel(args.channel, text, author=args.id or name,
                           author_name=name, author_kind=args.kind,
                           host=socket.gethostname().split(".")[0])
    print(f"Posted to #{m.channel} ({m.id[:8]})")


def cmd_send(args):
    store = _store(args)
    text = args.text if args.text is not None else sys.stdin.read().strip()
    if not text:
        print("Nothing to send (empty text).", file=sys.stderr)
        sys.exit(1)
    name = args.author or "human:cli"
    m = store.post_inbox(args.to, text, author=args.id or name,
                         author_name=name, author_kind=args.kind,
                         host=socket.gethostname().split(".")[0])
    print(f"Sent instruction to @{m.to} ({m.id[:8]})")


def cmd_broadcast(args):
    store = _store(args)
    text = args.text if args.text is not None else sys.stdin.read().strip()
    if not text:
        print("Nothing to broadcast (empty text).", file=sys.stderr)
        sys.exit(1)
    name = args.author or "human:cli"
    if args.cap:
        sent = store.broadcast_to_capability(
            args.cap, text, author=args.id or name, author_name=name,
            author_kind=args.kind, host=socket.gethostname().split(".")[0],
            online_only=args.online_only)
        print(f"Delivered to {len(sent)} agent(s) with capability '{args.cap}':")
        for m in sent:
            print(f"  → @{m.to}")
    else:
        m = store.post_broadcast(text, author=args.id or name, author_name=name,
                                 author_kind=args.kind,
                                 host=socket.gethostname().split(".")[0])
        print(f"Broadcast to all agents ({m.id[:8]})")


def cmd_firehose(args):
    store = _store(args)
    msgs = store.firehose(limit=args.tail or 200)
    if args.json:
        print(json.dumps(msgs, indent=2))
        return
    if not msgs:
        print("(no activity yet)")
    for m in msgs:
        where = f"#{m['channel']}" if m.get("channel") else ("→all" if m.get("to") == "*" else "")
        print(f"[{_fmt_ts(m['ts'])}] {where:12} {m.get('author_name')}: {m['text']}")


def cmd_prune(args):
    store = _store(args)
    max_age = args.max_age_days * 86400 if args.max_age_days else None
    if args.keep_last is None and max_age is None:
        print("Specify --keep-last and/or --max-age-days.", file=sys.stderr)
        sys.exit(1)
    archive = not args.no_archive
    if args.channel:
        n = store.prune_channel(args.channel, keep_last=args.keep_last,
                                max_age=max_age, archive=archive)
        print(f"Pruned {n} message(s) from #{args.channel}"
              + (" (archived)" if archive else " (deleted)"))
    else:
        result = store.prune_all(keep_last=args.keep_last, max_age=max_age,
                                 archive=archive)
        total = sum(result.values())
        if not total:
            print("Nothing to prune.")
        else:
            for target, n in result.items():
                print(f"  {target}: {n}")
            print(f"Pruned {total} message(s) total"
                  + (" (archived to archive.jsonl)" if archive else " (deleted)"))


def cmd_ask(args):
    text = args.text if args.text is not None else sys.stdin.read().strip()
    if not text:
        print("Nothing to ask (empty text).", file=sys.stderr)
        sys.exit(1)
    hub = HubClient(name=args.author or "asker", root=getattr(args, "root", None))
    print(f"Asking @{args.to} (timeout {args.timeout}s)…")
    reply = hub.request(args.to, text, timeout=args.timeout)
    if reply is None:
        print("(no reply before timeout)")
        sys.exit(2)
    print(f"Reply from {reply.get('author_name')}: {reply['text']}")


def cmd_read(args):
    store = _store(args)
    limit = args.tail if args.tail else None
    msgs = store.read_channel(args.channel, limit=limit)
    if args.json:
        print(json.dumps(msgs, indent=2))
        return
    if not msgs:
        print(f"(no messages in #{args.channel})")
    for m in msgs:
        _print_msg(m)


def cmd_inbox(args):
    store = _store(args)
    if not args.id:
        print("--id required (the agent id whose inbox to read).", file=sys.stderr)
        sys.exit(1)
    if args.watch:
        print(f"Watching inbox for {args.id} (Ctrl-C to stop)…")
        cursor = time.time() if not args.all else 0.0
        bcursor = cursor
        try:
            while True:
                if not args.no_heartbeat:
                    store.heartbeat(args.id)
                msgs = store.read_inbox(args.id, since_ts=cursor)
                bc = [] if args.no_broadcast else store.read_broadcast(since_ts=bcursor)
                for m in sorted(msgs + bc, key=lambda x: x["ts"]):
                    cursor = max(cursor, m["ts"]) if m.get("to") != "*" else cursor
                    if m.get("to") == "*":
                        bcursor = max(bcursor, m["ts"])
                    _print_msg(m)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped.")
        return
    msgs = store.read_inbox(args.id, limit=args.tail or None)
    if args.json:
        print(json.dumps(msgs, indent=2))
        return
    if not msgs:
        print("(inbox empty)")
    for m in msgs:
        _print_msg(m)


def cmd_agents(args):
    store = _store(args)
    agents = store.list_agents(online_window=args.window)
    if args.json:
        print(json.dumps(agents, indent=2))
        return
    if not agents:
        print("(no agents registered)")
        return
    print(f"{'STATUS':8} {'ID':40} {'HOST':16} {'LAST SEEN':10} CAPABILITIES")
    for a in agents:
        dot = "🟢 online" if a.get("online") else "⚪ offline"
        last = f"{int(a.get('age', 0))}s ago"
        caps = ",".join(a.get("capabilities", []))
        print(f"{dot:8} {a['id']:40} {a.get('host', ''):16} {last:10} {caps}")


def cmd_channels(args):
    store = _store(args)
    chans = store.list_channels()
    if args.json:
        print(json.dumps(chans, indent=2))
        return
    for c in chans:
        print(f"#{c['name']:20} {c.get('description', '')}")


def cmd_mkchannel(args):
    store = _store(args)
    name = store.ensure_channel(args.name, description=args.description or "")
    print(f"Channel ready: #{name}")


def cmd_doctor(args):
    root = resolve_root(args.root)
    store = HubStore(root)
    if not store.config_path.exists():
        print(f"✗ No hub found at {root}")
        print(f"  Run:  hubcli init --root {root}")
        sys.exit(1)
    s = store.stats()
    print(f"✓ Hub at {s['root']}")
    print(f"  config:           {'ok' if s['config_ok'] else 'MISSING'}")
    print(f"  auth:             {'shared token set' if s['auth_enabled'] else 'disabled'}")
    print(f"  channels:         {s['channels']} ({s['channel_messages_total']} messages)")
    for name, n in s["channel_message_counts"].items():
        print(f"      #{name}: {n}")
    print(f"  broadcasts:       {s['broadcast_messages']}")
    print(f"  inbox messages:   {s['inbox_messages_total']}")
    print(f"  agents:           {s['agents_online']} online / {s['agents_total']} total")
    ret = (store.get_config() or {}).get("retention") or {}
    if ret.get("keep_last") or ret.get("max_age_days"):
        print(f"  retention:        keep_last={ret.get('keep_last')}, "
              f"max_age_days={ret.get('max_age_days')}, every {ret.get('interval_sec')}s")
    else:
        print("  retention:        off (set a `retention` block in config.json to enable)")
    if args.json:
        print(json.dumps(s, indent=2))


SERVICE_TEMPLATE = """\
[Unit]
Description=Agent Hub server ({root})
After=network.target

[Service]
Type=simple
Environment=AGENT_HUB_ROOT={root}
ExecStart={python} -m agenthub.cli serve --host {host} --port {port}
WorkingDirectory={workdir}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
"""


def cmd_install_service(args):
    root = resolve_root(args.root)
    unit = SERVICE_TEMPLATE.format(
        root=root, python=sys.executable, host=args.host, port=args.port,
        workdir=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if args.system:
        dest = Path("/etc/systemd/system/agent-hub.service")
        enable_cmd = "sudo systemctl daemon-reload && sudo systemctl enable --now agent-hub"
    else:
        dest = Path("~/.config/systemd/user/agent-hub.service").expanduser()
        enable_cmd = ("systemctl --user daemon-reload && "
                      "systemctl --user enable --now agent-hub")
    if args.print_only:
        print(unit)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(unit, encoding="utf-8")
    print(f"Wrote systemd unit: {dest}\n")
    print("Enable and start it with:")
    print(f"  {enable_cmd}")
    if not args.system:
        print("\nTip: `loginctl enable-linger $USER` keeps it running after you log out.")


def cmd_listen(args):
    from .bridge import main as bridge_main
    argv = ["--name", args.name, "--channel", args.channel]
    if args.root:
        argv += ["--root", args.root]
    if args.pane:
        argv += ["--pane", args.pane]
    if args.history:
        argv.append("--history")
    bridge_main(argv)


CONNECT_PROMPT = """\
You are now a connected agent on our "agent hub" (a shared-filesystem chat for
my agents). Your hub name/id is: {name}

1. Connect and start listening by running this ONCE, in the background:
   nohup hubcli listen --name {name} > /tmp/hub-{name}.log 2>&1 &
   (If `hubcli` isn't on PATH, use: nohup python -m agenthub.bridge --name {name} ...)

2. From now on, lines that look like `[HUB ... from someone]: ...` are messages
   from me or another agent, injected into your terminal. When you get one,
   reply with a normal shell command:
     - to everyone on the channel:  hubcli post -c {channel} --author {name} "your reply"
     - directly to one agent:       hubcli send <their-id> --author {name} "your reply"
   See who else is online with:     hubcli agents

3. Stay connected. I may talk to you here in the CLI or from the web UI; both
   arrive the same way. You remain "online" as long as this tmux session runs.

Acknowledge by posting a quick hello to the channel, then wait for messages.
"""


def cmd_connect_help(args):
    print(CONNECT_PROMPT.format(name=args.name, channel=args.channel))


def cmd_serve(args):
    # Imported lazily so the CLI works without FastAPI installed.
    from .server import run_server
    run_server(root=resolve_root(args.root), host=args.host, port=args.port)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hubcli", description="Agent Hub CLI")
    p.add_argument("--root", help="Hub root dir (overrides env/pointer)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="Initialise a hub directory")
    sp.add_argument("--token", help="Shared token (generated if omitted)")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("register", help="Register/announce an agent")
    sp.add_argument("--name", required=True)
    sp.add_argument("--id", help="Stable agent id (auto-derived if omitted)")
    sp.add_argument("--kind", default="agent")
    sp.add_argument("--caps", help="Comma-separated capabilities")
    sp.set_defaults(func=cmd_register)

    sp = sub.add_parser("post", help="Post a message to a channel")
    sp.add_argument("text", nargs="?", help="Message text (or stdin)")
    sp.add_argument("-c", "--channel", default="general")
    sp.add_argument("--author", help="Display name")
    sp.add_argument("--id", help="Author id")
    sp.add_argument("--kind", default="agent")
    sp.set_defaults(func=cmd_post)

    sp = sub.add_parser("send", help="Send a directed instruction to an agent")
    sp.add_argument("to", help="Target agent id")
    sp.add_argument("text", nargs="?", help="Message text (or stdin)")
    sp.add_argument("--author", help="Display name")
    sp.add_argument("--id", help="Author id")
    sp.add_argument("--kind", default="human")
    sp.set_defaults(func=cmd_send)

    sp = sub.add_parser("broadcast", help="Send an instruction to ALL agents (or by capability)")
    sp.add_argument("text", nargs="?", help="Message text (or stdin)")
    sp.add_argument("--cap", help="Only agents advertising this capability")
    sp.add_argument("--online-only", action="store_true", help="With --cap, only online agents")
    sp.add_argument("--author", help="Display name")
    sp.add_argument("--id", help="Author id")
    sp.add_argument("--kind", default="human")
    sp.set_defaults(func=cmd_broadcast)

    sp = sub.add_parser("prune", help="Archive/rotate old messages (retention)")
    sp.add_argument("--keep-last", type=int, help="Keep only the last N messages per target")
    sp.add_argument("--max-age-days", type=float, help="Remove messages older than D days")
    sp.add_argument("--channel", help="Only prune this channel (default: everything)")
    sp.add_argument("--no-archive", action="store_true", help="Delete instead of archiving")
    sp.set_defaults(func=cmd_prune)

    sp = sub.add_parser("ask", help="Send a request to an agent and wait for its reply")
    sp.add_argument("to", help="Target agent id")
    sp.add_argument("text", nargs="?", help="Request text (or stdin)")
    sp.add_argument("--timeout", type=float, default=30.0)
    sp.add_argument("--author", help="Your display name")
    sp.set_defaults(func=cmd_ask)

    sp = sub.add_parser("firehose", help="Show all channel + broadcast activity merged")
    sp.add_argument("--tail", type=int, help="Only the last N items")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_firehose)

    sp = sub.add_parser("read", help="Read a channel")
    sp.add_argument("-c", "--channel", default="general")
    sp.add_argument("--tail", type=int, help="Only the last N messages")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_read)

    sp = sub.add_parser("inbox", help="Read/watch an agent's directed messages")
    sp.add_argument("--id", help="Agent id whose inbox to read")
    sp.add_argument("--watch", action="store_true")
    sp.add_argument("--all", action="store_true", help="When watching, include history")
    sp.add_argument("--interval", type=float, default=2.0)
    sp.add_argument("--no-heartbeat", action="store_true")
    sp.add_argument("--no-broadcast", action="store_true", help="Ignore broadcasts to all agents")
    sp.add_argument("--tail", type=int)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_inbox)

    sp = sub.add_parser("agents", help="List registered agents + presence")
    sp.add_argument("--window", type=float, default=30.0, help="Online window (s)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_agents)

    sp = sub.add_parser("channels", help="List channels")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_channels)

    sp = sub.add_parser("mkchannel", help="Create a channel")
    sp.add_argument("name")
    sp.add_argument("--description", "-d")
    sp.set_defaults(func=cmd_mkchannel)

    sp = sub.add_parser("listen", help="Connect a Claude Code agent (in tmux) and listen")
    sp.add_argument("--name", required=True, help="Agent name (also its hub id)")
    sp.add_argument("--channel", default="general")
    sp.add_argument("--pane", help="tmux pane id to inject into (else auto-detect this pane)")
    sp.add_argument("--root", help="Hub root (else env/pointer)")
    sp.add_argument("--history", action="store_true", help="Deliver pre-existing messages too")
    sp.set_defaults(func=cmd_listen)

    sp = sub.add_parser("connect-help", help="Print a paste-ready prompt to connect an agent")
    sp.add_argument("--name", required=True)
    sp.add_argument("--channel", default="general")
    sp.set_defaults(func=cmd_connect_help)

    sp = sub.add_parser("serve", help="Run the web UI server")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8787)
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("doctor", help="Health check: hub status, counts, presence")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("install-service", help="Generate a systemd unit for the server")
    sp.add_argument("--host", default="0.0.0.0")
    sp.add_argument("--port", type=int, default=8787)
    sp.add_argument("--system", action="store_true", help="System unit (needs root) instead of --user")
    sp.add_argument("--print-only", action="store_true", help="Print the unit, don't write it")
    sp.set_defaults(func=cmd_install_service)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
