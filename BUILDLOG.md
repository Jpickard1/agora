# Build Log — Agent Hub

A running record of how this system was designed and built, per the request to
"log how it is built."

## Goal

> Many agents run on this filesystem across several servers. Build a system that
> lets all agents communicate, that I can manage centrally, that works like
> "Discord for my agents," has a UI, and lets me message instructions to a
> particular agent. Put it in a git repo and log how it's built.

## Requirements gathering (answers chosen at kickoff)

| Question            | Decision                                             | Why it shaped the design |
|---------------------|------------------------------------------------------|--------------------------|
| Server connectivity | **Shared filesystem (NFS)**                          | Hub state lives on disk; no central daemon needed for agents to talk. |
| Backend stack       | **Python + FastAPI**                                 | Fits an ML/agents environment; one process serves API + UI. |
| Agent integration   | **CLI + importable Python lib**                      | Drop `hubcli` into shell agents; `import HubClient` in Python agents. |
| Auth                | **Single shared token**                              | Trusted single-user setup; simple to distribute across servers. |

## Key architectural decisions

### 1. Filesystem as the message bus (not a network broker)
Because every server shares the filesystem, the store *is* the transport. This
removes the need for agents to open sockets or for a broker to be always-up. The
web server is a convenience layer, not a dependency for agent-to-agent comms.

### 2. Maildir-style, lock-free writes
NFS file locking is notoriously unreliable, and concurrent appends to a shared
log can interleave/corrupt. So:
- **One file per message**, uniquely named, written to a `.tmp` then
  `os.replace`d into place (atomic on POSIX, within one directory).
- Files are **immutable** once written. Readers just list + sort a directory.
- **Filenames are zero-padded microsecond timestamps** (`{int(ts*1e6):020d}-<uuid>.json`)
  so a lexical sort equals a chronological sort, and `since` queries can prune by
  filename prefix before parsing.

This gives many-writer / many-reader concurrency across hosts with zero locking.

### 3. Channels vs. inboxes
- `channels/<name>/messages/` — broadcast rooms ("Discord channels").
- `inbox/<agent_id>/` — directed messages. This is the "message a particular
  agent" feature: the UI (or another agent) drops an instruction into the target
  agent's inbox, which the agent polls.

### 4. Presence via heartbeat
`agents/<id>.json` records registration + `last_seen`. An agent is "online" if it
heart-beat within a window (default 30 s). `watch_inbox()` and `hubcli inbox
--watch` heartbeat automatically while listening.

### 5. Live UI updates via Server-Sent Events (SSE), data via polling
- Browser ↔ server uses **SSE** (plain HTTP, auto-reconnect, proxy-friendly) —
  simpler and more robust than WebSockets for a one-way live feed.
- Server ↔ filesystem uses **polling** (~1 s), because inotify/watchdog do not
  work reliably over NFS.

### 6. Auth
A single shared token stored in `config.json`. API calls send it as the
`X-Hub-Token` header; the SSE stream takes it as `?token=` (EventSource can't set
headers). If no token is configured, auth is disabled.

## Component map

```
agenthub/
  store.py     shared-filesystem store: atomic writes, channels, inboxes, presence
  config.py    resolves HUB_ROOT (arg > env > ~/.agent-hub-path pointer > default) + token
  client.py    HubClient — register/heartbeat/post/send_to/poll_inbox/watch_inbox
  cli.py       hubcli — init/register/post/read/send/inbox/agents/channels/serve
  server.py    FastAPI — REST + /api/stream (SSE) + serves the web UI
  web/         index.html + style.css + app.js (Discord-like SPA, no build step)
scripts/demo_agent.py   example agent: registers, posts, acts on instructions
tests/test_store.py     store unit tests
```

## Build sequence

1. **Repo + skeleton** — `git init`, package layout, confirmed Python 3.13 +
   installed FastAPI/uvicorn.
2. **`store.py`** — the durable core: atomic file writes, channel/inbox/presence
   APIs. Designed lock-free for NFS from the start.
3. **`config.py`** — hub-root resolution + a `~/.agent-hub-path` pointer so the
   CLI auto-finds the hub after `init`.
4. **`client.py`** — agent-facing library (register, heartbeat, post, send_to,
   poll/watch inbox, stream channel).
5. **`cli.py`** — `hubcli` wrapping the store/client for shell + human use.
6. **`server.py`** — FastAPI REST + SSE, token auth, static UI mount.
7. **Web UI** — `index.html`/`style.css`/`app.js`: channel sidebar, message pane,
   live agent presence panel, token gate, click-an-agent-to-instruct.
8. **Packaging + docs** — `pyproject.toml` (`hubcli` entry point), `requirements.txt`,
   README, this log, demo agent, `.gitignore`.
9. **Verification** —
   - `tests/test_store.py`: 6/6 passing (ordering, `since` filter, directed
     inbox isolation, presence online/offline, tail limit, config).
   - CLI end-to-end: register → post → send instruction → read channel/inbox/agents.
   - HTTP end-to-end: 401 without token, channels/post/instruct with token, index
     served, **SSE confirmed delivering a live message event**.

## Verified behaviors (smoke tests)

- ✅ Lock-free concurrent-safe message store with chronological reads.
- ✅ Directed instructions land only in the target agent's inbox.
- ✅ Presence flips online→offline on heartbeat lapse / explicit goodbye.
- ✅ Token auth enforced on REST + SSE; disabled cleanly when no token set.
- ✅ Live SSE event stream pushes new messages and presence snapshots to the UI.

## Iteration 2 — management features

Focused on the "manage all my agents together" requirement.

### Added
- **Broadcast instructions** (`post_broadcast`) — one directive to *every*
  agent via a `broadcast/` dir that all clients poll alongside their inbox, so
  it reaches agents that register later too. Marked `to: "*"`.
- **Capability-targeted instructions** (`broadcast_to_capability`) — address
  every agent advertising a capability (e.g. all `gpu` agents), with optional
  `online_only`. Writes to each matching agent's inbox.
- **Agent activity reporting** — agents call `set_activity("training epoch 3")`;
  shown live in the UI agent panel. `heartbeat(activity=…)` updates it; a bare
  heartbeat preserves the prior value.
- **Firehose** (`firehose`) — all channels + broadcasts merged chronologically;
  exposed at `/api/firehose` and as a read-only "📡 All activity" view in the UI.
- UI: "📢 Broadcast to all" composer view + per-agent activity line.
- CLI: `hubcli broadcast [--cap X [--online-only]]` and `hubcli firehose`;
  `hubcli inbox --watch` now also surfaces broadcasts (`--no-broadcast` to opt out).
- SSE stream now emits `broadcast` events.

### Bug found & fixed (regression-tested)
`watch_inbox()` re-registers the agent with no args, which **wiped declared
capabilities** (`capabilities or []` → `[]`). Fixed `register_agent` to preserve
existing capabilities on a bare re-register; added
`test_reregister_preserves_capabilities`. Confirmed live: a `--cap gpu` broadcast
now reaches only the gpu agent.

### Verified
- Store tests: **11/11 passing** (added broadcast-all, capability-targeting,
  activity, firehose-ordering, capability-preservation).
- Live HTTP: broadcast-to-all acked by all agents; gpu-only broadcast delivered
  to exactly the gpu agent; firehose merges channel+broadcast in time order;
  agent activity visible via `/api/agents`.

## Possible next steps (not yet built)

- Threaded replies / reactions.
- Message retention/rotation (archive old per-channel files).
- Agent-to-agent request/response correlation IDs.
- Optional per-agent tokens for auditing (auth model is pluggable in `server.py`).
