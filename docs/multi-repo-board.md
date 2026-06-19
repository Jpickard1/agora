# Multi-repo task board (issue #123)

The agora task board can pull issues/PRs from **any number of GitHub repos** and
show them on one board, grouped and color-coded by source repo. This gives a
single view of what every team is doing across `Jpickard1/agora`,
`Jpickard1/Control4AI`, `Jpickard1/MGB-main`, … instead of one hard-wired repo.

It generalizes the old single `AGORA_GH_REPO` to a **list** of repos and keeps
single-repo setups working unchanged.

## How it works

- The board's repos live in `config.json` under `task_board.repos` (a list).
- The supervisor runs a periodic, incremental, rate-limit-aware sync
  (`BoardSyncer`): one paged `gh issue list` per repo (one auth), advancing a
  per-repo cursor so each poll only fetches the changed slice.
- The synced cards are persisted to `board_state.json` in the hub root and
  served read-only at `GET /api/board` (no live `gh` call per request).
- Each card links back to its GitHub issue/PR and carries its `state`, `labels`,
  `author`, and source `repo` for grouping/filtering.

## Register a repo (at runtime — no restart)

```bash
hubcli board add-repo Jpickard1/Control4AI
hubcli board add-repo Jpickard1/MGB-main
hubcli board list-repos
hubcli board remove-repo Jpickard1/MGB-main
hubcli board sync            # trigger an immediate incremental sync
```

`add-repo` / `remove-repo` edit `config.json` and take effect on the next
supervisor sync cycle (it re-reads the list every cycle) — no server restart.

Equivalently, set the list directly in `config.json`:

```json
{
  "task_board": {
    "repos": ["Jpickard1/agora", "Jpickard1/Control4AI"],
    "interval_sec": 300
  }
}
```

Single-repo back-compat: if `task_board.repos` is absent, the board falls back
to the single `$AGORA_GH_REPO` environment variable.

## Read the board

```bash
curl -s localhost:8910/api/board               | jq        # all repos
curl -s 'localhost:8910/api/board?repo=Jpickard1/agora'      # one repo
curl -s 'localhost:8910/api/board?state=open'                # open only
```

Response: `{ repos, colors (per-repo, deterministic), count, cards, groups }`.

## Auth / token scope

Sync uses the **GitHub CLI (`gh`)** with whatever account `gh auth status`
reports — one auth for all repos.

- **Public repos:** no extra scope; `gh auth login` is enough.
- **Private repos:** the authenticated token needs the **`repo`** scope (read
  access to issues). Verify with `gh auth status`; if a private repo's issues
  don't appear, re-auth with `gh auth refresh -s repo`.
- A repo that errors (no access / typo) is **skipped, not fatal** — the rest of
  the board still syncs.

## Notes / scope

- Polling only (no webhooks) in v1 — the interval is `task_board.interval_sec`
  (default 300s).
- Cross-repo dependency links are out of scope for v1.
