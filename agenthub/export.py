"""Export & reporting (issue #21).

Dump hub activity — channels, tasks, decisions/alerts, and a per-agent standup
("what each agent did") — into shareable reports: JSON (machine-readable),
Markdown (readable/diff-able), and a self-contained printable HTML page.

Everything is derived from the existing store (no external creds). The data is
gathered ONCE into a plain snapshot dict; each renderer is a pure function of
that snapshot, so formats can't drift from each other.
"""

from __future__ import annotations

import html
import json
import time

# Window parsing -----------------------------------------------------------

_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_since(spec, now=None):
    """Turn a --since spec into an absolute epoch cutoff.
      None / "" / "all" / "0"  -> 0.0  (everything)
      "24h" / "7d" / "30m" / "2w" -> now - that duration
      a bare number            -> treated as an epoch seconds value
    """
    now = time.time() if now is None else now
    if spec is None:
        return 0.0
    s = str(spec).strip().lower()
    if s in ("", "all", "0"):
        return 0.0
    if s[-1] in _UNITS:
        try:
            return max(0.0, now - float(s[:-1]) * _UNITS[s[-1]])
        except ValueError:
            return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


# Gather -------------------------------------------------------------------

def _author_of(m):
    return m.get("author") or m.get("author_name") or "?"


def build_standup(channels, tasks, agents, since=0.0):
    """Per-agent 'what each agent did' summary over the window. Counts messages
    posted, tasks claimed, and tasks completed (from task event history)."""
    by = {}

    def row(key, name=None):
        r = by.get(key)
        if r is None:
            r = by[key] = {"agent": key, "name": name or key, "messages": 0,
                           "tasks_claimed": [], "tasks_done": [], "last_ts": 0.0}
        if name and r["name"] == key:
            r["name"] = name
        return r

    # known display names
    for a in agents:
        row(a["id"], a.get("name"))

    for ch in channels:
        for m in ch["messages"]:
            r = row(_author_of(m), m.get("author_name"))
            r["messages"] += 1
            r["last_ts"] = max(r["last_ts"], m.get("ts", 0.0))

    for t in tasks:
        for ev in t.get("events", []):
            if ev.get("ts", 0.0) < since:
                continue
            who = ev.get("by")
            if not who:
                continue
            r = row(who)
            r["last_ts"] = max(r["last_ts"], ev.get("ts", 0.0))
            if ev.get("status") == "claimed" or ev.get("status") == "running":
                if t["id"] not in r["tasks_claimed"]:
                    r["tasks_claimed"].append(t["id"])
            if ev.get("status") == "done":
                if t["id"] not in r["tasks_done"]:
                    r["tasks_done"].append(t["id"])
        # also credit the current claimer even if no event carried 'by'
        if t.get("claimed_by"):
            r = row(t["claimed_by"])
            if t["status"] == "done" and t["id"] not in r["tasks_done"]:
                r["tasks_done"].append(t["id"])
            elif t["status"] not in ("done", "open") and t["id"] not in r["tasks_claimed"]:
                r["tasks_claimed"].append(t["id"])

    # A finished task shouldn't also show as "in progress".
    for r in by.values():
        r["tasks_claimed"] = [t for t in r["tasks_claimed"] if t not in r["tasks_done"]]

    rows = sorted(by.values(),
                  key=lambda r: (-(r["messages"] + len(r["tasks_done"]) * 3), r["name"]))
    return rows


def gather(store, since=0.0, per_channel_limit=1000, now=None):
    """Collect a complete, render-ready snapshot of recent hub activity."""
    now = time.time() if now is None else now
    channels = []
    for c in store.list_channels():
        msgs = store.read_channel(c["name"], since_ts=since, limit=per_channel_limit)
        channels.append({"name": c["name"],
                         "description": c.get("description", ""),
                         "messages": msgs})
    tasks = store.list_tasks()
    agents = store.list_agents()

    # "Decisions & alerts": messages explicitly flagged as must-read alerts —
    # the closest durable signal we have for notable decisions.
    decisions = []
    for ch in channels:
        for m in ch["messages"]:
            if (m.get("meta") or {}).get("alert"):
                decisions.append({"channel": ch["name"], **m})
    decisions.sort(key=lambda m: m.get("ts", 0.0))

    standup = build_standup(channels, tasks, agents, since=since)
    total_msgs = sum(len(c["messages"]) for c in channels)
    return {
        "meta": {
            "generated_ts": now,
            "since_ts": since,
            "channels": len(channels),
            "messages": total_msgs,
            "tasks": len(tasks),
            "agents": len(agents),
        },
        "channels": channels,
        "tasks": tasks,
        "decisions": decisions,
        "agents": agents,
        "standup": standup,
    }


# Formatting helpers -------------------------------------------------------

def _ts(ts):
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _window_label(snapshot):
    s = snapshot["meta"]["since_ts"]
    return "all time" if not s else f"since {_ts(s)}"


# JSON ---------------------------------------------------------------------

def to_json(snapshot):
    return json.dumps(snapshot, indent=2, sort_keys=False)


# Markdown -----------------------------------------------------------------

def to_markdown(snapshot):
    m = snapshot["meta"]
    out = []
    out.append(f"# Agora activity report")
    out.append("")
    out.append(f"_Generated {_ts(m['generated_ts'])} · {_window_label(snapshot)}_")
    out.append("")
    out.append(f"**{m['agents']}** agents · **{m['channels']}** channels · "
               f"**{m['messages']}** messages · **{m['tasks']}** tasks")
    out.append("")

    # Standup
    out.append("## Standup — what each agent did")
    out.append("")
    if not snapshot["standup"]:
        out.append("_No activity in this window._")
    else:
        out.append("| Agent | Messages | Tasks done | Tasks in progress | Last seen |")
        out.append("|---|--:|---|---|---|")
        for r in snapshot["standup"]:
            done = ", ".join(r["tasks_done"]) or "—"
            wip = ", ".join(r["tasks_claimed"]) or "—"
            out.append(f"| {r['name']} | {r['messages']} | {done} | {wip} | {_ts(r['last_ts'])} |")
    out.append("")

    # Tasks
    out.append("## Tasks")
    out.append("")
    if not snapshot["tasks"]:
        out.append("_No tasks._")
    else:
        out.append("| Task | Status | Owner | Ref | Title |")
        out.append("|---|---|---|---|---|")
        for t in snapshot["tasks"]:
            out.append(f"| {t['id']} | {t['status']} | {t.get('claimed_by') or '—'} "
                       f"| {t.get('ref') or '—'} | {t.get('title') or ''} |")
    out.append("")

    # Decisions & alerts
    out.append("## Decisions & alerts")
    out.append("")
    if not snapshot["decisions"]:
        out.append("_None flagged._")
    else:
        for d in snapshot["decisions"]:
            out.append(f"- 🚨 **{d.get('author_name') or _author_of(d)}** "
                       f"in #{d['channel']} ({_ts(d.get('ts'))}): {d.get('text', '')}")
    out.append("")

    # Conversations
    out.append("## Conversations")
    out.append("")
    for ch in snapshot["channels"]:
        out.append(f"### #{ch['name']}"
                   + (f" — {ch['description']}" if ch["description"] else ""))
        if not ch["messages"]:
            out.append("")
            out.append("_No messages in this window._")
            out.append("")
            continue
        out.append("")
        for msg in ch["messages"]:
            who = msg.get("author_name") or _author_of(msg)
            txt = (msg.get("text") or "").replace("\n", " ")
            out.append(f"- `{_ts(msg.get('ts'))}` **{who}**: {txt}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# HTML ---------------------------------------------------------------------

_HTML_CSS = """
body{font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
  max-width:900px;margin:24px auto;padding:0 18px;color:#1b1f24}
h1{margin:0 0 4px} h2{margin-top:28px;border-bottom:2px solid #e6e8eb;padding-bottom:4px}
h3{margin-top:18px} .sub{color:#697}
table{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px}
th,td{border:1px solid #e0e3e7;padding:5px 8px;text-align:left;vertical-align:top}
th{background:#f5f6f8} .stat{display:inline-block;margin-right:14px}
.stat b{font-size:18px} .msg{margin:2px 0} .msg .t{color:#8a92a3;font-size:12px}
.msg .a{font-weight:600} .alert{background:#fff;border-left:3px solid #ed4245;padding:4px 8px;margin:3px 0}
.empty{color:#8a92a3;font-style:italic} @media print{body{margin:0}}
"""


def _h(s):
    return html.escape(str(s if s is not None else ""))


def to_html(snapshot):
    m = snapshot["meta"]
    p = [f"<!doctype html><html><head><meta charset='utf-8'>",
         f"<title>Agora activity report</title><style>{_HTML_CSS}</style></head><body>"]
    p.append(f"<h1>Agora activity report</h1>")
    p.append(f"<div class='sub'>Generated {_h(_ts(m['generated_ts']))} · "
             f"{_h(_window_label(snapshot))}</div>")
    p.append("<p>"
             f"<span class='stat'><b>{m['agents']}</b> agents</span>"
             f"<span class='stat'><b>{m['channels']}</b> channels</span>"
             f"<span class='stat'><b>{m['messages']}</b> messages</span>"
             f"<span class='stat'><b>{m['tasks']}</b> tasks</span></p>")

    p.append("<h2>Standup — what each agent did</h2>")
    if not snapshot["standup"]:
        p.append("<p class='empty'>No activity in this window.</p>")
    else:
        p.append("<table><tr><th>Agent</th><th>Messages</th><th>Tasks done</th>"
                 "<th>In progress</th><th>Last seen</th></tr>")
        for r in snapshot["standup"]:
            p.append(f"<tr><td>{_h(r['name'])}</td><td>{r['messages']}</td>"
                     f"<td>{_h(', '.join(r['tasks_done']) or '—')}</td>"
                     f"<td>{_h(', '.join(r['tasks_claimed']) or '—')}</td>"
                     f"<td>{_h(_ts(r['last_ts']))}</td></tr>")
        p.append("</table>")

    p.append("<h2>Tasks</h2>")
    if not snapshot["tasks"]:
        p.append("<p class='empty'>No tasks.</p>")
    else:
        p.append("<table><tr><th>Task</th><th>Status</th><th>Owner</th>"
                 "<th>Ref</th><th>Title</th></tr>")
        for t in snapshot["tasks"]:
            p.append(f"<tr><td>{_h(t['id'])}</td><td>{_h(t['status'])}</td>"
                     f"<td>{_h(t.get('claimed_by') or '—')}</td>"
                     f"<td>{_h(t.get('ref') or '—')}</td><td>{_h(t.get('title'))}</td></tr>")
        p.append("</table>")

    p.append("<h2>Decisions &amp; alerts</h2>")
    if not snapshot["decisions"]:
        p.append("<p class='empty'>None flagged.</p>")
    else:
        for d in snapshot["decisions"]:
            p.append(f"<div class='alert'>🚨 <b>{_h(d.get('author_name') or _author_of(d))}</b> "
                     f"in #{_h(d['channel'])} <span class='t'>{_h(_ts(d.get('ts')))}</span><br>"
                     f"{_h(d.get('text'))}</div>")

    p.append("<h2>Conversations</h2>")
    for ch in snapshot["channels"]:
        head = f"#{_h(ch['name'])}"
        if ch["description"]:
            head += f" — {_h(ch['description'])}"
        p.append(f"<h3>{head}</h3>")
        if not ch["messages"]:
            p.append("<p class='empty'>No messages in this window.</p>")
            continue
        for msg in ch["messages"]:
            who = msg.get("author_name") or _author_of(msg)
            p.append(f"<div class='msg'><span class='t'>{_h(_ts(msg.get('ts')))}</span> "
                     f"<span class='a'>{_h(who)}</span>: {_h(msg.get('text'))}</div>")
    p.append("</body></html>")
    return "".join(p)


# Standup-only text (for posting to a channel) -----------------------------

def standup_text(snapshot):
    """A compact plain-text standup suitable for posting into a channel."""
    lines = [f"📋 Standup ({_window_label(snapshot)})"]
    if not snapshot["standup"]:
        lines.append("  (no activity)")
        return "\n".join(lines)
    for r in snapshot["standup"]:
        if not (r["messages"] or r["tasks_done"] or r["tasks_claimed"]):
            continue
        bits = [f"{r['messages']} msgs"]
        if r["tasks_done"]:
            bits.append(f"done: {', '.join(r['tasks_done'])}")
        if r["tasks_claimed"]:
            bits.append(f"wip: {', '.join(r['tasks_claimed'])}")
        lines.append(f"  • {r['name']}: " + "; ".join(bits))
    return "\n".join(lines)


RENDERERS = {"json": to_json, "md": to_markdown, "html": to_html}
EXT = {"json": "json", "md": "md", "html": "html"}
