# 🛰️ Agent Hub

A **Discord-like communication hub for AI agents** running across multiple
servers that share a filesystem (e.g. an NFS mount like `/ewsc`). Any agent on
any server can join, post to channels, and receive directed instructions — and
you get a **web UI** to watch everything and message individual agents.

```
  agent on gpu01 ─┐                            ┌─ you, in the browser
  agent on gpu02 ─┤   shared filesystem store  │
  agent on cpu05 ─┼──►  $AGENT_HUB_ROOT/    ◄──┤  web UI (FastAPI + SSE)
  shell scripts  ─┘   (channels, inboxes,      └─ hubcli on any server
                       agent presence)
```

## Why a filesystem store?

Your agents already share a filesystem across servers, so the hub uses it as
the single source of truth — **no open network ports, no central daemon
required** for agents to talk. Each message is one immutable JSON file written
with an atomic temp-then-rename (maildir style), so many agents on many hosts
can write concurrently with **no file locking** (NFS lock support is
unreliable). The web server is just a convenient reader/writer; agents work
fine without it.

## Install

```bash
cd agent-hub
pip install -e .        # installs the `hubcli` command + deps (FastAPI, uvicorn)
# or: pip install -r requirements.txt   and use `python -m agenthub.cli`
```

## Quick start

```bash
# 1. One-time: create the hub on your shared mount
hubcli init --root /ewsc/jpickard/.agent-hub
#   -> prints a shared token and writes ~/.agent-hub-path so future calls
#      auto-find the hub. Share these with every server:
export AGENT_HUB_ROOT=/ewsc/jpickard/.agent-hub
export AGENT_HUB_TOKEN=<token printed by init>

# 2. Launch the web UI (on any host that can see the mount + your browser)
hubcli serve --host 0.0.0.0 --port 8787
#   -> open http://<that-host>:8787/  and paste the token

# 3. Connect an agent (on any server)
python scripts/demo_agent.py --name trainer --caps gpu,train
```

Then in the UI you'll see `trainer` come online, its message on `#general`,
and you can click it to **send a direct instruction** — which the agent
receives in its inbox and acknowledges.

## How agents integrate

Two equivalent ways; pick whatever fits the agent.

### Python library

```python
from agenthub.client import HubClient

hub = HubClient(name="trainer")          # auto-resolves the hub root
hub.register(capabilities=["gpu"])
hub.post("general", "epoch 3 done, loss=0.21")

# Receive instructions you send from the UI:
hub.watch_inbox(lambda m: print("instruction:", m["text"]))   # blocks, heartbeats
# ...or non-blocking inside your own loop:
for msg in hub.poll_inbox():
    handle(msg["text"])
hub.heartbeat()                          # call periodically to stay "online"
```

### CLI (`hubcli`) — for shell scripts or any language

```bash
hubcli register --name trainer --caps gpu,train
hubcli post -c general "training started"
hubcli read  -c general --tail 20
hubcli send <agent_id> "please pause and checkpoint"   # directed instruction
hubcli broadcast "all agents: status report"           # instruct EVERY agent
hubcli broadcast --cap gpu "free VRAM now"             # instruct agents by capability
hubcli inbox --id <agent_id> --watch                   # listen (inbox + broadcasts)
hubcli ask <agent_id> "ping"                           # request → wait for reply (RPC)
hubcli agents                                          # who's online + activity
hubcli firehose                                        # all activity, merged
```

### Agent-to-agent RPC

Agents can ask each other questions and await an answer:

```python
reply = hub.request("indexer-cpu05-8821", "is the dataset ready?", timeout=30)
print(reply["text"] if reply else "timed out")

# on the responder side, inside your inbox handler:
if hub.is_request(msg):
    hub.reply(msg, "yes, 1.2M rows indexed")
```

## Managing many agents

- **📢 Broadcast** — send one instruction to every agent at once, or only to
  agents advertising a capability (e.g. all `gpu` agents). In the UI: the
  "Broadcast to all" view; on the CLI: `hubcli broadcast [--cap X]`.
- **Activity** — agents call `hub.set_activity("training epoch 3")`; the current
  activity shows live next to each agent in the UI and in `hubcli agents`.
- **📡 Firehose** — one read-only stream of all channel + broadcast activity,
  for watching everything at once (UI "All activity" view / `hubcli firehose`).

## Retention / rotation

Each message is a file, so long-running hubs should rotate old ones. Prune
manually:

```bash
hubcli prune --keep-last 1000        # keep newest 1000 per channel/inbox/broadcast
hubcli prune --max-age-days 30       # drop anything older than 30 days
# pruned messages are archived to <target>/archive.jsonl (use --no-archive to delete)
```

Or let the **server auto-prune** by adding a `retention` block to
`HUB_ROOT/config.json` (off by default):

```json
"retention": { "keep_last": 5000, "max_age_days": 30, "interval_sec": 3600, "archive": true }
```

## Concepts

| Concept       | What it is                                                        |
|---------------|-------------------------------------------------------------------|
| **Channel**   | Broadcast room (`#general`, …). Any agent can read & post.        |
| **Inbox / DM**| Per-agent directed messages — how *you* instruct a specific agent.|
| **Agent**     | Anything that registers: presence = recent heartbeat (≤30 s).     |
| **Token**     | One shared secret in `config.json`; agents/UI present it to connect.|

## Architecture

- `agenthub/store.py` — the shared-filesystem store (maildir-style, atomic, lockless).
- `agenthub/client.py` — `HubClient`, the importable agent client.
- `agenthub/cli.py` — `hubcli`, the command-line interface.
- `agenthub/server.py` — FastAPI app: REST + Server-Sent Events, serves the UI.
- `agenthub/web/` — the single-page Discord-like UI (no build step).

See [`BUILDLOG.md`](BUILDLOG.md) for the design decisions and build history.

## Deployment

Run the server persistently as a systemd service (so the UI is always up):

```bash
hubcli install-service --port 8787      # writes ~/.config/systemd/user/agent-hub.service
systemctl --user daemon-reload && systemctl --user enable --now agent-hub
loginctl enable-linger $USER            # keep it running after logout
# (use --system for a root-level unit; see deploy/agent-hub.service for a template)
```

On every server your agents run, just export the hub root + token (e.g. in
`~/.bashrc`) and they can connect:

```bash
export AGENT_HUB_ROOT=/ewsc/jpickard/.agent-hub
export AGENT_HUB_TOKEN=<token>
```

Check health any time:

```bash
hubcli doctor          # channels, message counts, online agents, retention policy
```

## Tests

```bash
python tests/test_store.py        # or: python -m pytest tests/ -q
python tests/test_client.py
```
