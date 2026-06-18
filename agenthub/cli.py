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
import shutil
import socket
import subprocess
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
    meta = {"alert": True} if getattr(args, "alert", False) else None
    m = store.post_channel(args.channel, text, author=args.id or name,
                           author_name=name, author_kind=args.kind,
                           host=socket.gethostname().split(".")[0], meta=meta)
    print(f"{'🚨 Alert posted' if meta else 'Posted'} to #{m.channel} ({m.id[:8]})")


def cmd_alert(args):
    """Post a high-visibility alert: a channel message flagged meta.alert=true,
    which the UI renders white-bg / black-text / 🚨 and pins at top (issue #17).
    Any agent may raise one."""
    args.alert = True
    cmd_post(args)


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


def cmd_graph(args):
    store = _store(args)
    g = store.comm_graph()
    if args.json:
        print(json.dumps(g, indent=2))
        return
    if args.dot:
        print("digraph agora {")
        print("  rankdir=LR;")
        for e in g["edges"]:
            print(f'  "{e["source"]}" -> "{e["target"]}" [label="{e["count"]}"];')
        print("}")
        return
    if not g["edges"]:
        print("(no agent-to-agent messages yet)")
        return
    print("Agent communication (who DMs whom):")
    for e in g["edges"]:
        print(f"  {e['source']:20} → {e['target']:20} ({e['count']})")


def cmd_export(args):
    from . import export as exp
    store = _store(args)
    since = exp.parse_since(args.since)
    snap = exp.gather(store, since=since)

    fmts = list(exp.RENDERERS) if args.format == "all" else [args.format]

    # --stdout: print one format to stdout instead of writing files.
    if args.stdout:
        fmt = fmts[0]
        sys.stdout.write(exp.RENDERERS[fmt](snap))
        if not exp.RENDERERS[fmt](snap).endswith("\n"):
            sys.stdout.write("\n")
        return

    out_dir = args.out or os.path.join(str(resolve_root(args.root)), "exports")
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.fromtimestamp(snap["meta"]["generated_ts"]).strftime("%Y%m%d-%H%M%S")
    written = []
    for fmt in fmts:
        path = os.path.join(out_dir, f"agora-report-{stamp}.{exp.EXT[fmt]}")
        with open(path, "w") as f:
            f.write(exp.RENDERERS[fmt](snap))
        written.append(path)

    m = snap["meta"]
    print(f"Exported {m['messages']} messages / {m['tasks']} tasks / "
          f"{m['agents']} agents ({exp._window_label(snap)}):")
    for path in written:
        print(f"  {path}")

    if args.post_standup:
        text = exp.standup_text(snap)
        store.post_channel(args.post_standup, text,
                           author=args.author or "reporter", author_name=args.author or "reporter",
                           author_kind="system", host=socket.gethostname().split(".")[0])
        print(f"Posted standup summary to #{args.post_standup}")


def cmd_usage(args):
    store = _store(args)
    u = store.usage_stats(online_window=args.window)
    if args.json:
        print(json.dumps(u, indent=2))
        return
    t = u["totals"]
    h = u["host"]
    host_bits = [h.get("host", "?")]
    if "cpu_percent" in h:
        host_bits.append(f"cpu {h['cpu_percent']}%")
    if "mem_percent" in h:
        host_bits.append(f"mem {h['mem_percent']}% "
                         f"({h.get('mem_used_gb')}/{h.get('mem_total_gb')}G)")
    if "load1" in h:
        host_bits.append(f"load {h['load1']}")
    print("agora utilization")
    print(f"  agents: {t['agents']} ({t['online']} online)   "
          f"messages: {t['messages']}   "
          f"tasks: {t['tasks']} ({t['tasks_done']} done, "
          f"{t['tasks_per_agent']}/agent)")
    print(f"  host:   {'  '.join(host_bits)}")
    print()
    print(f"{'STATUS':8} {'AGENT':28} {'MSGS':>6} {'TASKS':>6} {'DONE':>5} {'RUN':>4}")
    for a in u["agents"]:
        dot = "🟢" if a.get("online") else "⚪"
        print(f"{dot:8} {a['name'][:28]:28} {a['messages']:>6} "
              f"{a['tasks_total']:>6} {a['tasks_done']:>5} {a['tasks_running']:>4}")
    if not u["agents"]:
        print("(no agents registered)")


def cmd_forget(args):
    store = _store(args)
    if store.forget_agent(args.agent):
        print(f"Forgot agent: {args.agent}")
        return 0
    print(f"No such agent: {args.agent}", file=sys.stderr)
    return 1


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


# The labels the Agent Task issue form expects to exist in the target repo.
# (name, color, description) — kept here so `labels-init` is the single source.
AGENT_TASK_LABELS = [
    ("ready", "0e8a16", "Task is ready for an agent to pick up now"),
    ("agent-task", "1d76db", "Filed via the Agent Task form; routed by the manager"),
    ("parked", "fbca04", "Filed but intentionally not dispatched yet"),
]


def cmd_labels_init(args):
    """Idempotently create the Agent Task form's labels in a GitHub repo via the
    `gh` CLI. Safe to re-run: labels that already exist are left untouched."""
    repo = args.repo or os.environ.get("AGORA_GH_REPO")
    if not repo:
        print("error: pass --repo owner/name (or set AGORA_GH_REPO)", file=sys.stderr)
        return 2
    if shutil.which("gh") is None:
        print("error: the GitHub CLI ('gh') is required and was not found on PATH",
              file=sys.stderr)
        return 2
    created, existed, failed = 0, 0, 0
    for name, color, desc in AGENT_TASK_LABELS:
        r = subprocess.run(
            ["gh", "label", "create", name, "--repo", repo,
             "--color", color, "--description", desc],
            capture_output=True, text=True)
        if r.returncode == 0:
            created += 1
            print(f"  ✓ created '{name}'")
        elif "already exists" in (r.stderr + r.stdout).lower():
            existed += 1
            print(f"  • '{name}' already exists")
        else:
            failed += 1
            print(f"  ✗ '{name}': {r.stderr.strip() or 'failed'}", file=sys.stderr)
    print(f"labels-init on {repo}: {created} created, {existed} existing, {failed} failed")
    return 1 if failed else 0


# -- tasks -----------------------------------------------------------------

def _print_task(t: dict, verbose: bool = False) -> None:
    badge = {"open": "○", "claimed": "◔", "running": "◑",
             "done": "●", "failed": "✗", "cancelled": "⊘"}.get(t["status"], "•")
    who = f" @{t['claimed_by']}" if t.get("claimed_by") else ""
    ref = f"  [{t['ref']}]" if t.get("ref") else ""
    cap = f"  cap={t['capability']}" if t.get("capability") else ""
    print(f"{badge} {t['status']:9} {t['id']}{who}{cap}{ref}  {t.get('title','')}")
    if verbose:
        for e in t.get("events", []):
            note = f" — {e['note']}" if e.get("note") else ""
            print(f"    [{_fmt_ts(e['ts'])}] {e['status']} by {e.get('by','?')}{note}")


def _print_kb_entry(e, verbose=False):
    tags = (" " + " ".join(f"#{t}" for t in e["tags"])) if e.get("tags") else ""
    kind = e.get("kind", "note")
    icon = {"note": "📝", "link": "🔗", "artifact": "📦"}.get(kind, "•")
    print(f"{icon} {e['id']}  {e.get('title','')}{tags}")
    if e.get("url"):
        print(f"    {e['url']}")
    if verbose and e.get("body"):
        print()
        for line in e["body"].splitlines():
            print(f"    {line}")
        print()


def cmd_kb_add(args):
    store = _store(args)
    body = args.body
    if body is None and not sys.stdin.isatty():
        body = sys.stdin.read()
    tags = [t for t in (args.tags or "").split(",") if t.strip()]
    name = args.author or "cli"
    e = store.kb_add(args.title, body=body or "", tags=tags, kind=args.kind,
                     url=args.url or "", author=name,
                     author_name=name, entry_id=args.id_arg)
    print(f"✓ Saved KB entry: {e['id']}" + (" (updated)" if args.id_arg else ""))


def cmd_kb_get(args):
    store = _store(args)
    e = store.kb_get(args.id_arg)
    if e is None:
        print(f"✗ No such KB entry: {args.id_arg}", file=sys.stderr)
        sys.exit(2)
    if args.json:
        print(json.dumps(e, indent=2))
        return
    _print_kb_entry(e, verbose=True)


def cmd_kb_list(args):
    store = _store(args)
    entries = store.kb_list(tag=args.tag, limit=args.limit)
    if args.json:
        print(json.dumps(entries, indent=2))
        return
    if not entries:
        print("(knowledge base is empty)" + (f" for tag #{args.tag}" if args.tag else ""))
        return
    for e in entries:
        _print_kb_entry(e)


def cmd_kb_search(args):
    store = _store(args)
    entries = store.kb_search(args.query, tag=args.tag, limit=args.limit)
    if args.json:
        print(json.dumps(entries, indent=2))
        return
    if not entries:
        print(f"(no KB entries match '{args.query}')")
        return
    for e in entries:
        _print_kb_entry(e)


def cmd_kb_rm(args):
    store = _store(args)
    if store.kb_delete(args.id_arg):
        print(f"✓ Deleted KB entry: {args.id_arg}")
    else:
        print(f"✗ No such KB entry: {args.id_arg}", file=sys.stderr)
        sys.exit(2)


def cmd_task_new(args):
    store = _store(args)
    t = store.create_task(
        args.id, title=args.title or "", ref=args.ref or "",
        brief=args.brief or "", capability=args.cap or "",
        created_by=args.author or "manager",
        labels=[c.strip() for c in (args.labels or "").split(",") if c.strip()])
    print(f"Task ready: {t['id']} (status={t['status']})")
    _print_task(t)


def cmd_task_claim(args):
    store = _store(args)
    won = store.claim_task(args.id, args.author, note=args.note or "")
    if won:
        print(f"✓ Claimed {args.id} as @{args.author}")
    else:
        t = store.get_task(args.id)
        if t is None:
            print(f"✗ No such task: {args.id}", file=sys.stderr)
            sys.exit(2)
        print(f"✗ Already claimed by @{t.get('claimed_by')} — not yours.",
              file=sys.stderr)
        sys.exit(1)


def cmd_task_update(args):
    store = _store(args)
    t = store.update_task(args.id, args.status, by=args.author or "",
                          note=args.note or "")
    if t is None:
        print(f"✗ No such task: {args.id}", file=sys.stderr)
        sys.exit(2)
    print(f"Task {t['id']} -> {t['status']}")


def cmd_task_list(args):
    store = _store(args)
    tasks = store.list_tasks(status=args.status)
    if args.json:
        print(json.dumps(tasks, indent=2))
        return
    if not tasks:
        print("(no tasks)" + (f" with status={args.status}" if args.status else ""))
        return
    for t in tasks:
        _print_task(t)


def cmd_task_show(args):
    store = _store(args)
    t = store.get_task(args.id)
    if t is None:
        print(f"✗ No such task: {args.id}", file=sys.stderr)
        sys.exit(2)
    if args.json:
        print(json.dumps(t, indent=2))
        return
    _print_task(t, verbose=True)


def cmd_task_release(args):
    store = _store(args)
    if store.get_task(args.id) is None:
        print(f"✗ No such task: {args.id}", file=sys.stderr)
        sys.exit(2)
    if store.release_task(args.id, by=args.author, force=args.force):
        print(f"✓ Released {args.id} → open (now reclaimable)")
    else:
        t = store.get_task(args.id)
        print(f"✗ {args.id} is claimed by @{t.get('claimed_by')}, not you. "
              f"Use --force to override (or 'task reassign').", file=sys.stderr)
        sys.exit(1)


def cmd_task_reassign(args):
    store = _store(args)
    t = store.reassign_task(args.id, args.agent, by=args.author or "manager",
                            note=args.note or "")
    if t is None:
        print(f"✗ No such task: {args.id}", file=sys.stderr)
        sys.exit(2)
    print(f"✓ Reassigned {args.id} → @{t.get('claimed_by')}")


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
    if getattr(args, "channels", None):
        argv += ["--channels", args.channels]
    if getattr(args, "all_channels", False):
        argv.append("--all-channels")
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
   nohup hubcli listen --name {name}{listen_flags} > /tmp/hub-{name}.log 2>&1 &
   (If `hubcli` isn't on PATH, use: nohup python -m agenthub.bridge --name {name} ...)
   (To follow several channels: --channels general,dev,alerts ; or every channel: --all-channels)

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
    # Mirror whatever channel selection the caller requested into the paste-ready
    # listen command, and pick a sensible channel for the "reply here" examples.
    if getattr(args, "all_channels", False):
        listen_flags = " --all-channels"
        channel = args.channel
    elif getattr(args, "channels", None):
        listen_flags = f" --channels {args.channels}"
        channel = args.channels.split(",")[0].strip() or args.channel
    else:
        listen_flags = ""
        channel = args.channel
    print(CONNECT_PROMPT.format(name=args.name, channel=channel,
                                listen_flags=listen_flags))


def _tmux_has(session):
    import subprocess
    return subprocess.run(["tmux", "has-session", "-t", session],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def _tmux_start(session, command):
    import subprocess
    subprocess.run(["tmux", "kill-session", "-t", session], stderr=subprocess.DEVNULL, check=False)
    subprocess.run(["tmux", "new-session", "-d", "-s", session, command], check=False)


def cmd_up(args):
    """One-command bootstrap: bring up the server, the manager's own bridge, and
    the supervisor — all in durable detached tmux sessions. Idempotent."""
    import subprocess, sys, time, urllib.request
    root = resolve_root(args.root)
    store = HubStore(root)
    store.init()
    pybin = sys.executable
    pane = args.pane or os.environ.get("TMUX_PANE", "")
    manager, port = args.manager, args.port
    tick = 30.0 if args.dev else args.tick
    watch = 10.0 if args.dev else args.watch

    def server_ok():
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as r:
                return r.status == 200
        except Exception:
            return False

    print(f"agora up{' (dev mode)' if args.dev else ''}:")

    # 1. web server
    if server_ok():
        print(f"  ✓ server already running on :{port}")
    else:
        _tmux_start("agora-server",
                    f"AGENT_HUB_ROOT={root} {pybin} -m agenthub.cli serve "
                    f"--host 127.0.0.1 --port {port} > {root}/server.log 2>&1")
        for _ in range(12):
            time.sleep(1)
            if server_ok():
                break
        print(f"  {'✓' if server_ok() else '✗'} server on :{port}  (tmux: agora-server)")

    # 2. manager's own bridge (so the manager agent is connected + heartbeating)
    if not pane:
        print("  ! no tmux pane detected — run `hubcli up` from inside the manager's "
              "tmux pane, or pass --pane <id>. Skipping manager bridge.")
    else:
        _tmux_start("agora-manager-bridge",
                    f"AGENT_HUB_ROOT={root} {pybin} -m agenthub.cli listen "
                    f"--name {manager} --pane {pane} --all-channels "
                    f"> {root}/manager-bridge.log 2>&1")
        print(f"  ✓ manager bridge  (agent '{manager}', pane {pane}, "
              f"all channels, tmux: agora-manager-bridge)")

    # 3. supervisor (keep-alive + issue ticker)
    _tmux_start("agora-supervisor",
                f"AGENT_HUB_ROOT={root} {pybin} -m agenthub.supervisor "
                f"--manager {manager} --manager-pane {pane} --port {port} "
                f"--tick-interval {tick} --watch-interval {watch} > {root}/supervisor.log 2>&1")
    print(f"  ✓ supervisor  (tick={int(tick)}s, watch={int(watch)}s, tmux: agora-supervisor)")

    cfg = store.get_config() or {}
    print(f"\n  UI:    http://127.0.0.1:{port}/")
    if cfg.get("token"):
        print(f"  token: {cfg['token']}")
    print(f"  Manager '{manager}' is live; it will be ticked to check GitHub issues every {int(tick)}s.")
    print("  Stop everything with:  hubcli down")


def cmd_down(args):
    import subprocess
    for s in ("agora-supervisor", "agora-manager-bridge", "agora-server"):
        if _tmux_has(s):
            subprocess.run(["tmux", "kill-session", "-t", s], stderr=subprocess.DEVNULL, check=False)
            print(f"  stopped {s}")
        else:
            print(f"  (not running) {s}")


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
    sp.add_argument("--alert", action="store_true", help="High-visibility alert (white bg/black text/🚨, pinned)")
    sp.set_defaults(func=cmd_post)

    sp = sub.add_parser("alert", help="Post a high-visibility alert (must-read; pinned + 🚨)")
    sp.add_argument("text", nargs="?", help="Alert text (or stdin)")
    sp.add_argument("-c", "--channel", default="general")
    sp.add_argument("--author", help="Display name")
    sp.add_argument("--id", help="Author id")
    sp.add_argument("--kind", default="agent")
    sp.set_defaults(func=cmd_alert)

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

    sp = sub.add_parser("usage", help="System utilization: per-agent + host metrics")
    sp.add_argument("--window", type=float, default=30.0, help="Online window (s)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_usage)

    sp = sub.add_parser("graph", help="Agent communication graph (who DMs whom)")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--dot", action="store_true", help="Emit Graphviz DOT")
    sp.set_defaults(func=cmd_graph)

    sp = sub.add_parser("export", help="Export channels/tasks/decisions + a standup report")
    sp.add_argument("--format", choices=["all", "md", "json", "html"], default="all",
                    help="Report format(s) to write (default: all)")
    sp.add_argument("--out", help="Output directory (default: <root>/exports)")
    sp.add_argument("--since", help="Limit to recent activity, e.g. 24h, 7d, 30m, 2w (default: all)")
    sp.add_argument("--stdout", action="store_true",
                    help="Print one format to stdout instead of writing files")
    sp.add_argument("--post-standup", dest="post_standup", metavar="CHANNEL",
                    help="Also post the standup summary to this channel")
    sp.add_argument("--author", help="Author name for the posted standup")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("forget", help="Remove an agent record from the roster")
    sp.add_argument("agent", help="Agent id to forget")
    sp.set_defaults(func=cmd_forget)

    sp = sub.add_parser("channels", help="List channels")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_channels)

    sp = sub.add_parser("mkchannel", help="Create a channel")
    sp.add_argument("name")
    sp.add_argument("--description", "-d")
    sp.set_defaults(func=cmd_mkchannel)

    sp = sub.add_parser("labels-init",
                        help="Create the Agent Task form's labels in a GitHub repo (idempotent)")
    sp.add_argument("--repo", help="owner/name (defaults to $AGORA_GH_REPO)")
    sp.set_defaults(func=cmd_labels_init)

    # tasks: durable work dispatch (manager creates, workers claim/update)
    sp = sub.add_parser("task", help="Durable work tasks (new/claim/update/list/show)")
    tsub = sp.add_subparsers(dest="task_cmd", required=True)

    tp = tsub.add_parser("new", help="Create/dispatch a task (idempotent)")
    tp.add_argument("id", help="Task id, e.g. 'MGB-main#42'")
    tp.add_argument("--title")
    tp.add_argument("--ref", help="External reference, e.g. 'Jpickard1/MGB-main#42'")
    tp.add_argument("--brief", help="Short brief for the worker")
    tp.add_argument("--cap", help="Capability/skill the task needs")
    tp.add_argument("--labels", help="Comma-separated labels")
    tp.add_argument("--author", help="Who created it (default: manager)")
    tp.set_defaults(func=cmd_task_new)

    tp = tsub.add_parser("claim", help="Atomically claim a task (first-wins)")
    tp.add_argument("id")
    tp.add_argument("--author", required=True, help="Claiming agent id")
    tp.add_argument("--note")
    tp.set_defaults(func=cmd_task_claim)

    tp = tsub.add_parser("update", help="Append a status event to a task")
    tp.add_argument("id")
    tp.add_argument("--status", required=True,
                    choices=["running", "done", "failed", "cancelled", "open"])
    tp.add_argument("--author", help="Agent reporting the update")
    tp.add_argument("--note")
    tp.set_defaults(func=cmd_task_update)

    tp = tsub.add_parser("release", help="Drop your claim so a task can be reclaimed")
    tp.add_argument("id")
    tp.add_argument("--author", required=True, help="Agent releasing (must be the owner unless --force)")
    tp.add_argument("--force", action="store_true", help="Manager override: release regardless of owner")
    tp.set_defaults(func=cmd_task_release)

    tp = tsub.add_parser("reassign", help="Manager: move a task's claim to another agent")
    tp.add_argument("id")
    tp.add_argument("agent", help="New owner agent id")
    tp.add_argument("--author", help="Who reassigned it (default: manager)")
    tp.add_argument("--note")
    tp.set_defaults(func=cmd_task_reassign)

    tp = tsub.add_parser("list", help="List tasks (durable dispatch state)")
    tp.add_argument("--status", help="Filter by current status")
    tp.add_argument("--json", action="store_true")
    tp.set_defaults(func=cmd_task_list)

    tp = tsub.add_parser("show", help="Show one task + its event history")
    tp.add_argument("id")
    tp.add_argument("--json", action="store_true")
    tp.set_defaults(func=cmd_task_show)

    # knowledge base: shared, searchable notes/links/artifacts (issue #25)
    sp = sub.add_parser("kb", help="Shared knowledge base (add/get/search/list/rm)")
    ksub = sp.add_subparsers(dest="kb_cmd", required=True)

    kp = ksub.add_parser("add", help="Add or update a KB entry (body via arg or stdin)")
    kp.add_argument("title", help="Entry title")
    kp.add_argument("--body", help="Markdown body (else read from stdin)")
    kp.add_argument("--tags", help="Comma-separated tags")
    kp.add_argument("--kind", choices=["note", "link", "artifact"], default="note")
    kp.add_argument("--url", help="URL (for links/artifacts)")
    kp.add_argument("--id", dest="id_arg", help="Update an existing entry by id")
    kp.add_argument("--author", help="Author display name")
    kp.set_defaults(func=cmd_kb_add)

    kp = ksub.add_parser("get", help="Show one KB entry (full body)")
    kp.add_argument("id_arg", metavar="id", help="Entry id")
    kp.add_argument("--json", action="store_true")
    kp.set_defaults(func=cmd_kb_get)

    kp = ksub.add_parser("list", help="List KB entries (newest first)")
    kp.add_argument("--tag", help="Filter by tag")
    kp.add_argument("--limit", type=int)
    kp.add_argument("--json", action="store_true")
    kp.set_defaults(func=cmd_kb_list)

    kp = ksub.add_parser("search", help="Full-text search the KB")
    kp.add_argument("query", help="Search terms")
    kp.add_argument("--tag", help="Restrict to a tag")
    kp.add_argument("--limit", type=int)
    kp.add_argument("--json", action="store_true")
    kp.set_defaults(func=cmd_kb_search)

    kp = ksub.add_parser("rm", help="Delete a KB entry")
    kp.add_argument("id_arg", metavar="id", help="Entry id")
    kp.set_defaults(func=cmd_kb_rm)

    sp = sub.add_parser("listen", help="Connect a Claude Code agent (in tmux) and listen")
    sp.add_argument("--name", required=True, help="Agent name (also its hub id)")
    sp.add_argument("--channel", default="general", help="Single channel to follow (default)")
    sp.add_argument("--channels", help="Comma-separated channels to follow, e.g. general,dev,alerts")
    sp.add_argument("--all-channels", dest="all_channels", action="store_true",
                    help="Follow EVERY channel, including ones created later")
    sp.add_argument("--pane", help="tmux pane id to inject into (else auto-detect this pane)")
    sp.add_argument("--root", help="Hub root (else env/pointer)")
    sp.add_argument("--history", action="store_true", help="Deliver pre-existing messages too")
    sp.set_defaults(func=cmd_listen)

    sp = sub.add_parser("connect-help", help="Print a paste-ready prompt to connect an agent")
    sp.add_argument("--name", required=True)
    sp.add_argument("--channel", default="general")
    sp.add_argument("--channels", help="Comma-separated channels to follow")
    sp.add_argument("--all-channels", dest="all_channels", action="store_true",
                    help="Follow every channel")
    sp.set_defaults(func=cmd_connect_help)

    sp = sub.add_parser("up", help="One-command bootstrap: server + manager bridge + supervisor")
    sp.add_argument("--manager", default="manager", help="Manager agent name/id")
    sp.add_argument("--pane", help="Manager's tmux pane (else auto-detect this pane)")
    sp.add_argument("--port", type=int, default=8910)
    sp.add_argument("--root", help="Hub root (else env/pointer)")
    sp.add_argument("--tick", type=float, default=180.0, help="Issue-check cadence (s)")
    sp.add_argument("--watch", type=float, default=15.0, help="Liveness-check cadence (s)")
    sp.add_argument("--dev", action="store_true", help="Fast cadences for development (tick 30s, watch 10s)")
    sp.set_defaults(func=cmd_up)

    sp = sub.add_parser("down", help="Stop the server + manager bridge + supervisor")
    sp.set_defaults(func=cmd_down)

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
