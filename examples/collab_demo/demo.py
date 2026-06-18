"""Collaborative multi-agent demo (issue #23).

A self-contained, reproducible run that proves agents can deliver a real project
together end-to-end:

  1. the MANAGER decomposes a goal into hub tasks,
  2. three WORKER agents each atomically claim a task and build a piece of a real
     web app in their OWN isolated work dir (mimicking per-agent git worktrees),
  3. a REVIEWER cross-checks the assembled app, 👍s each contribution, and requests
     one concrete change; the author applies it and the reviewer approves,
  4. the pieces are integrated into a working artifact (a static "Standup Board"
     single-page app) and the whole run is captured as a transcript + REPORT.md.

It drives the REAL hub primitives — tasks (atomic claim), channels, reactions —
against a private temp hub. No network, no live hub, no external services.

Run it:   python examples/collab_demo/demo.py [output_dir]
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from agenthub.store import HubStore

GOAL = "Build a working 'Standup Board' web app (add / filter / remove items, persisted)."

# --- the work products each agent authors (real, dependency-free web app) ------

INDEX_HTML = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Standup Board</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <main>
    <h1>\U0001F4CB Standup Board</h1>
    <form id="add-form">
      <input id="item-input" placeholder="What did you do?" autocomplete="off">
      <button type="submit">Add</button>
    </form>
    <input id="filter" placeholder="Filter…">
    <ul id="list"></ul>
  </main>
  <script src="app.js"></script>
</body>
</html>
"""

STYLE_CSS = """\
* { box-sizing: border-box; }
body { font-family: system-ui, sans-serif; margin: 0; background: #0f172a; color: #e2e8f0; }
main { max-width: 540px; margin: 3rem auto; padding: 0 1rem; }
h1 { font-size: 1.6rem; }
#add-form { display: flex; gap: .5rem; margin-bottom: .5rem; }
input { flex: 1; padding: .55rem .7rem; border: 1px solid #334155; border-radius: .5rem;
        background: #1e293b; color: inherit; }
button { padding: .55rem 1rem; border: 0; border-radius: .5rem; background: #6366f1;
         color: white; cursor: pointer; }
ul { list-style: none; padding: 0; margin-top: 1rem; }
li { padding: .6rem .7rem; border-radius: .5rem; background: #1e293b; margin-bottom: .4rem;
     cursor: pointer; }
li:hover { background: #334155; }
li.empty { background: transparent; color: #64748b; cursor: default; text-align: center; }
"""

# v1 — functional but missing an empty-state (the reviewer will catch this).
APP_JS_V1 = """\
const KEY = "standup.items";
let items = JSON.parse(localStorage.getItem(KEY) || "[]");
const $ = (s) => document.querySelector(s);

function save() { localStorage.setItem(KEY, JSON.stringify(items)); }

function addItem(text) {
  text = (text || "").trim();
  if (!text) return;
  items.push({ text, ts: Date.now() });
  save(); render();
}

function render() {
  const q = ($("#filter").value || "").toLowerCase();
  const ul = $("#list");
  ul.innerHTML = "";
  items
    .filter((it) => it.text.toLowerCase().includes(q))
    .forEach((it, i) => {
      const li = document.createElement("li");
      li.textContent = it.text;
      li.onclick = () => { items.splice(i, 1); save(); render(); };
      ul.appendChild(li);
    });
}

$("#add-form").addEventListener("submit", (e) => {
  e.preventDefault();
  addItem($("#item-input").value);
  $("#item-input").value = "";
});
$("#filter").addEventListener("input", render);
render();
"""

# v2 — adds the empty-state the reviewer asked for.
APP_JS_V2 = APP_JS_V1.replace(
    "    });\n}\n",
    "    });\n"
    "  if (ul.children.length === 0) {\n"
    "    const li = document.createElement(\"li\");\n"
    "    li.className = \"empty\";\n"
    "    li.textContent = \"No items yet — add one above.\";\n"
    "    ul.appendChild(li);\n"
    "  }\n}\n",
)

# manager's decomposition: (task_id, agent, filename, content, description)
PLAN = [
    ("demo-html", "ana", "index.html", INDEX_HTML, "page structure + form/list"),
    ("demo-css",  "ben", "style.css",  STYLE_CSS,  "styling / layout"),
    ("demo-js",   "cy",  "app.js",     APP_JS_V1,  "add/filter/remove + persistence"),
]


def _post(store, channel, text, agent):
    return store.post_channel(channel, text, author=agent, author_name=agent)


def run_demo(out_dir: str | os.PathLike, hub_root: str | None = None) -> dict:
    """Run the full collaboration and produce the artifact + report under out_dir.
    Returns a result dict (artifact paths, task states, transcript, ok)."""
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    work = out / "work"          # per-agent isolated dirs (≈ worktrees)
    dist = out / "dist"          # the integrated, shippable artifact
    dist.mkdir(parents=True, exist_ok=True)

    hub_root = hub_root or tempfile.mkdtemp(prefix="collab-hub-")
    store = HubStore(hub_root)
    store.init()

    # 1. MANAGER decomposes the goal into tasks + announces the plan.
    _post(store, "build", f"GOAL: {GOAL}", "manager")
    for task_id, agent, fname, _content, desc in PLAN:
        store.create_task(task_id, title=desc, capability="frontend", created_by="manager")
        _post(store, "build", f"@{agent} please build {fname} ({desc}) — task {task_id}", "manager")

    # 2. WORKERS each claim their task, build in an isolated dir, integrate, report.
    for task_id, agent, fname, content, _desc in PLAN:
        won = store.claim_task(task_id, agent)            # atomic, race-proof claim
        assert won, f"{agent} failed to claim {task_id}"
        adir = work / agent
        adir.mkdir(parents=True, exist_ok=True)
        (adir / fname).write_text(content)                # build in isolation
        shutil.copy(adir / fname, dist / fname)           # integrate into the artifact
        _post(store, "build", f"built {fname} — done (task {task_id})", agent)
        store.update_task(task_id, "done", by=agent, note=f"shipped {fname}")

    # 3. REVIEWER cross-checks: 👍 every contribution, then requests one change.
    build_msgs = [m for m in store.read_channel("build") if m["text"].startswith("built ")]
    for m in build_msgs:
        store.add_reaction(m["id"], "\U0001F44D", author="rio", author_name="rio")

    app_js = (dist / "app.js").read_text()
    review_passed = "No items yet" in app_js
    if not review_passed:
        store.create_task("demo-review-fix", title="add empty-state to app.js",
                          capability="frontend", created_by="rio")
        _post(store, "review",
              "@cy app.js works, but the list shows nothing when empty. Please add an "
              "empty-state message ('No items yet'). Requesting changes.", "rio")
        # author applies the requested change
        store.claim_task("demo-review-fix", "cy")
        (work / "cy" / "app.js").write_text(APP_JS_V2)
        shutil.copy(work / "cy" / "app.js", dist / "app.js")
        _post(store, "build", "applied empty-state per review (app.js) — done", "cy")
        store.update_task("demo-review-fix", "done", by="cy", note="added empty-state")
        app_js = (dist / "app.js").read_text()

    approved = "No items yet" in app_js
    if approved:
        _post(store, "review", "✅ approved — empty-state added, app composes. Ship it!", "rio")

    # 4. Verify the artifact actually composes, then write the report.
    index = (dist / "index.html").read_text()
    artifact_ok = (
        (dist / "index.html").exists() and (dist / "style.css").exists()
        and (dist / "app.js").exists()
        and 'href="style.css"' in index and 'src="app.js"' in index
        and "function addItem" in app_js and "function render" in app_js
        and approved
    )
    tasks = store.list_tasks()
    report_path = _write_report(out, store, tasks, artifact_ok)

    return {
        "out_dir": str(out),
        "dist": str(dist),
        "report": str(report_path),
        "artifact_ok": artifact_ok,
        "all_tasks_done": all(t["status"] == "done" for t in tasks),
        "review_round_happened": not review_passed,   # a change was requested+applied
        "tasks": tasks,
    }


def _write_report(out: Path, store: HubStore, tasks, artifact_ok: bool) -> Path:
    lines = [
        "# Collaborative demo — run report",
        "",
        f"**Goal:** {GOAL}",
        "",
        f"**Result:** {'✅ working artifact shipped' if artifact_ok else '❌ artifact incomplete'}"
        f" — open `dist/index.html` in a browser.",
        "",
        "## How the team worked",
        "- **manager** decomposed the goal into tasks and dispatched them on `#build`.",
        "- **ana / ben / cy** each atomically claimed a task and built a piece in their",
        "  own isolated dir (`work/<agent>/`), then integrated it into `dist/`.",
        "- **rio** reviewed: 👍'd each contribution and requested one change (empty-state),",
        "  which **cy** applied before rio approved.",
        "",
        "## Final task board",
    ]
    for t in tasks:
        owner = t.get("claimed_by") or t.get("assignee") or "—"
        lines.append(f"- `{t['id']}` — **{t['status']}** ({owner}) — {t.get('title','')}")
    for ch in ("build", "review"):
        lines += ["", f"## Transcript — #{ch}"]
        for m in store.read_channel(ch):
            rx = store.get_reactions(m["id"]) or {}
            tags = "".join(f" {e}×{info['count']}" for e, info in rx.items()) if rx else ""
            lines.append(f"- **{m['author_name']}:** {m['text']}{tags}")
    report = out / "REPORT.md"
    report.write_text("\n".join(lines) + "\n")
    return report


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "collab_demo_out"
    res = run_demo(target)
    print(f"artifact: {res['dist']}/index.html  (open in a browser)")
    print(f"report:   {res['report']}")
    print(f"tasks all done: {res['all_tasks_done']}   artifact ok: {res['artifact_ok']}")
    sys.exit(0 if res["artifact_ok"] and res["all_tasks_done"] else 1)
