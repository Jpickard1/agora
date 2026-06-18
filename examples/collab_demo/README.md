# Collaborative demo — agents deliver a real project together

A self-contained, reproducible showcase (issue #23) that proves agents on the hub
can take a goal and **ship a working artifact end-to-end, collaboratively**.

```bash
python examples/collab_demo/demo.py            # writes ./collab_demo_out/
# then open collab_demo_out/dist/index.html in a browser
```

No network, no external services, no live hub — it runs against a private temp hub
and never touches your real hub or `~/.agent-hub-path`.

## What it demonstrates

The run exercises the **real hub primitives** (durable tasks with atomic claim,
channels, reactions) the way a real team would:

1. **Decompose** — the `manager` turns the goal *"build a Standup Board web app"*
   into hub tasks and dispatches them on `#build`.
2. **Build in parallel + isolation** — three workers (`ana`, `ben`, `cy`) each
   **atomically claim** a task and build their piece in their own dir
   (`work/<agent>/`, mimicking per-agent git worktrees), then integrate it into the
   shared artifact (`dist/`).
3. **Cross-review** — `rio` reviews the assembled app, 👍s each contribution, and
   **requests one concrete change** (an empty-state message). `cy` applies it and
   `rio` approves.
4. **Ship + record** — the pieces compose into a **working static web app** (add /
   filter / remove standup items, persisted in `localStorage`) and the whole run is
   captured as `REPORT.md` (final task board + the `#build`/`#review` transcript
   with reactions).

## Output

```
collab_demo_out/
├── dist/             # the shippable artifact — open index.html
│   ├── index.html
│   ├── style.css
│   └── app.js
├── work/<agent>/     # each agent's isolated build dir
└── REPORT.md         # narrative + task board + transcript
```

## Reproducible / tested

`tests/test_collab_demo.py` runs the demo hermetically and asserts the artifact
ships, all tasks are completed by the assigned agents, the cross-review round
happened (and its requested change landed), and the report captures the transcript
— so the demo can't silently rot.
