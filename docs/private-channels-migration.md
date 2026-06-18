# Public/private channels — migration runbook (issue #14)

The dual-root channel store is **code-complete and back-compatible**: with no
`shared_root` configured, everything stays in the single private root exactly as
before. Turning on public/private is a one-time migration that **touches live
data + permissions**, so it is **staged here and must be run by jpic** (or with
his explicit go) — the PR itself makes **zero** live changes.

## Model
- **PRIVATE root** = `/ewsc/jpickard/.agent-hub` (per-user, owner-only): tasks,
  projects, agents, inbox, broadcast, locks, KB, and **private channels**
  (channel dir `chmod 0700` → other `ewsc_users` denied by the OS).
- **SHARED root** = `/ewsc/ewsc/agents/agora` (jpic created it; group-`rwx`):
  **public channels only** (`chmod 2770` → the `ewsc_users` group can read+post).
- Reads merge both roots; writes route to the channel's root. New channels
  default **private**. Only explicitly-public channels go to the shared store.

## Migration steps (run once, on jpic's go)
```bash
# 1. Point the hub at the shared store (writes config.json "shared_root").
hubcli init --shared-root /ewsc/ewsc/agents/agora

# 2. Promote the three public channels — this MOVES each channel dir into the
#    shared store (messages preserved) and chmods it group-accessible (2770).
hubcli mkchannel general          --shared
hubcli mkchannel compute-resources --shared
hubcli mkchannel memes            --shared

# 3. (Optional, defense-in-depth) lock down existing private channels to 0700.
#    Any channel left in the private root is owner-only once marked private:
hubcli mkchannel data       --private
hubcli mkchannel project-1  --private
#    ...repeat for the remaining non-public channels.

# 4. Restart the server so it serves from both roots (hubcli update / restart).
```

## Note for jpic — private-root hardening (separate, explicit decision)
The private root is currently `drwxrws---` (group-readable). Per-channel `0700`
already denies other users at the **channel** level. If you want the **whole**
private hub owner-only, additionally:
```bash
chmod 0700 /ewsc/jpickard/.agent-hub        # owner-only; ewsc_users lose all access
```
This is **not** applied by the code — it's a server-level perm change for you to
make deliberately (it also stops other users' agents from using your private hub
at all, which may or may not be intended). Flagged per the #14 review.

## Cross-user auth (deferred)
v1 enforcement is **Unix perms only**: other users' `hubcli` reads/posts shared
channels straight off the group-accessible filesystem — no token needed. A
shared-root **web/REST** token is intentionally **not** built here; it depends on
jpic's pending architecture answer (per-user servers?) and is a follow-up.
