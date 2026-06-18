"""agora supervisor — keep the hub alive and tick the manager.

This is the "dumb background daemon" half of the manager design: it never
thinks, it just keeps infrastructure running and pokes the manager on a
schedule. It is started by `hubcli up` in its own detached tmux session.

Responsibilities:
  1. Keep the web server responding — restart it (in tmux) if it dies.
  2. Keep the manager's bridge alive — if the manager drops off the roster, its
     bridge died, so restart it (needs the manager's tmux pane).
  3. Tick the manager — periodically drop a message into the manager's inbox so
     the (turn-based) manager wakes up and checks GitHub issues.

All the actual reasoning (reading issues, deciding assignments) is done by the
manager *agent* when it receives a tick — never here.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

from .config import resolve_root
from .ghsync import GitHubSyncer
from .store import HubStore

PYBIN = sys.executable


def _server_ok(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _tmux_start(session: str, command: str) -> None:
    """(Re)start a detached tmux session running `command`."""
    subprocess.run(["tmux", "kill-session", "-t", session],
                   stderr=subprocess.DEVNULL, check=False)
    subprocess.run(["tmux", "new-session", "-d", "-s", session, command], check=False)


def _agent_online(store: HubStore, agent_id: str, window: float = 30.0) -> bool:
    rec = store.get_agent(agent_id)
    if not rec:
        return False
    return (time.time() - rec.get("last_seen", 0)) <= window and rec.get("status") != "offline"


# -- testable loop units (issue #3) ---------------------------------------
# The decision logic of the watch loop is factored out so it can be unit-tested
# with mocks (no real tmux / network / sleeps). main() just wires them together.

def should_tick(now: float, last_tick: float, interval: float) -> bool:
    """True if it's time to poke the manager again."""
    return (now - last_tick) >= interval


def ensure_server(port, serve_cmd, *, server_ok=_server_ok,
                  tmux_start=_tmux_start) -> bool:
    """Restart the web server (in tmux) if its health check fails. Returns True
    if a restart was issued. server_ok/tmux_start are injectable for tests."""
    if server_ok(port):
        return False
    print("[supervisor] server not responding → restarting", flush=True)
    tmux_start("agora-server", serve_cmd)
    return True


def ensure_manager_bridge(store, manager, pane, bridge_cmd, *,
                          agent_online=_agent_online, tmux_start=_tmux_start) -> bool:
    """Restart the manager's bridge if the manager has dropped off the roster.
    No-op when there's no pane to restart into. Returns True if restarted."""
    if not pane:
        return False
    if agent_online(store, manager):
        return False
    print(f"[supervisor] manager '{manager}' offline → restarting its bridge", flush=True)
    tmux_start("agora-manager-bridge", bridge_cmd)
    return True


# -- task stall detector (issue #80) --------------------------------------
# Distinct from the stale-CLAIM check (#8, owner went OFFLINE): this watches
# tasks that are RUNNING with an ONLINE owner but show no progress — no new
# commit referencing the issue AND no hub message from the owner — for a while,
# i.e. likely stuck. One de-duped #general alert per stall; re-arms on activity.

def _issue_number(ref):
    m = re.search(r"#(\d+)", ref or "")
    return m.group(1) if m else None


def last_commit_ts(repo_dir, ref, *, run=subprocess.run):
    """Unix ts of the most recent commit whose message references the task's
    issue (#n) in `repo_dir`, or 0. Injectable `run` for tests (no real git)."""
    n = _issue_number(ref)
    if not repo_dir or not n:
        return 0
    try:
        proc = run(["git", "-C", str(repo_dir), "log", "-1", "--format=%ct",
                    "-E", "--grep", f"#{n}([^0-9]|$)"],
                   capture_output=True, text=True, timeout=5)
        return int((getattr(proc, "stdout", "") or "").strip() or 0)
    except Exception:
        return 0


def owner_hub_ts(store, owner):
    """Latest ts of a hub message authored by `owner` (channels + broadcast)."""
    if not owner:
        return 0
    latest = 0.0
    try:
        for ch in store.list_channels():
            for m in store.read_channel(ch["name"]):
                if m.get("author") == owner or m.get("author_name") == owner:
                    latest = max(latest, m.get("ts", 0) or 0)
        for m in store.read_broadcast():
            if m.get("author") == owner or m.get("author_name") == owner:
                latest = max(latest, m.get("ts", 0) or 0)
    except Exception:
        pass
    return latest


def detect_stalled(running, now, threshold, alerted, activity_ts):
    """Pure: from RUNNING tasks, return (to_alert, active_ids).
      to_alert  = owned tasks whose last activity is older than `threshold`
                  and not already in `alerted` (de-dup);
      active_ids = tasks with recent activity (caller clears them from `alerted`
                  so a task that goes quiet again re-alerts).
    `activity_ts(task)` -> latest progress timestamp for that task."""
    to_alert, active = [], set()
    for t in running:
        if not t.get("claimed_by"):
            continue
        if activity_ts(t) >= now - threshold:
            active.add(t["id"])
        elif t["id"] not in alerted:
            to_alert.append(t)
    return to_alert, active


def _whoami():
    import os as _os
    try:
        import getpass
        return _os.environ.get("AGORA_USER") or getpass.getuser()
    except Exception:
        return _os.environ.get("AGORA_USER") or "user"


def drain_cross_user_inbox(store, shared_root, my_user, cursor, seen):
    """Cross-user DM delivery (#88): pull new DMs for `my_user` from the shared
    area and post_inbox() each into the local (recipient) inbox. Returns
    (n_delivered, new_cursor, seen). No-op without a shared_root."""
    from . import crossdm
    if not shared_root:
        return 0, cursor, seen
    msgs, new_cursor, seen = crossdm.drain_shared_dms(str(shared_root), my_user,
                                                      since_ts=cursor, seen=seen)
    for m in msgs:
        store.post_inbox(m.get("to"), m.get("text", ""),
                         author=f"{m.get('from_user')}:{m.get('author')}",
                         author_name=m.get("author_name") or m.get("author"),
                         author_kind="agent", host="cross-user")
    return len(msgs), new_cursor, seen


def main(argv=None):
    ap = argparse.ArgumentParser(prog="agora-supervisor")
    ap.add_argument("--manager", default="manager", help="Manager agent id to keep alive + tick")
    ap.add_argument("--manager-pane", default="", help="tmux pane of the manager (to restart its bridge)")
    ap.add_argument("--port", type=int, default=8910)
    ap.add_argument("--root", default=None)
    ap.add_argument("--channel", default="general")
    ap.add_argument("--watch-interval", type=float, default=15.0, help="liveness check cadence (s)")
    ap.add_argument("--tick-interval", type=float, default=180.0, help="manager issue-check cadence (s)")
    ap.add_argument("--stale-after", type=float, default=300.0,
                    help="flag a task stale if its owner has been offline this long (s)")
    ap.add_argument("--stall-after", type=float, default=1800.0,
                    help="alert if a RUNNING task shows no commit/hub activity this long (s)")
    ap.add_argument("--repo-dir", default=None,
                    help="git checkout to probe for task commits (default: this install)")
    ap.add_argument("--gh-sync", dest="gh_sync", action="store_true", default=True,
                    help="mirror task status changes to linked GitHub issues (default on)")
    ap.add_argument("--no-gh-sync", dest="gh_sync", action="store_false",
                    help="disable the GitHub issue syncer")
    ap.add_argument("--gh-sync-dry-run", dest="gh_dry_run", action="store_true",
                    help="plan GitHub sync actions but don't run gh (for testing)")
    args = ap.parse_args(argv)

    root = str(resolve_root(args.root))
    store = HubStore(root)
    store.init()

    # GitHub <-> task syncer (issue #9): comments linked issues on status change
    # and closes them on done. Disabled automatically if `gh` isn't installed; a
    # task with no/invalid ref is a silent no-op. Env override: AGORA_GH_SYNC=0.
    gh_enabled = args.gh_sync and os.environ.get("AGORA_GH_SYNC", "1") != "0"
    if gh_enabled and not args.gh_dry_run and not shutil.which("gh"):
        print("[supervisor] gh not found → GitHub sync disabled", flush=True)
        gh_enabled = False
    syncer = GitHubSyncer(store, os.path.join(root, "ghsync_state.json"),
                          enabled=gh_enabled, dry_run=args.gh_dry_run)
    print(f"[supervisor] github sync: {'on' if gh_enabled else 'off'}"
          f"{' (dry-run)' if args.gh_dry_run else ''}", flush=True)

    # Optional self-update (issue #69): periodically pull+apply the latest Agora.
    # OFF by default — enable via a `selfupdate` block in config.json:
    #   "selfupdate": {"enabled": true, "interval_sec": 3600}
    su_cfg = (store.get_config() or {}).get("selfupdate") or {}
    su_enabled = bool(su_cfg.get("enabled"))
    su_interval = float(su_cfg.get("interval_sec") or 3600)
    print(f"[supervisor] self-update: "
          f"{'on, every ' + str(int(su_interval)) + 's' if su_enabled else 'off'}",
          flush=True)

    serve_cmd = (f"AGENT_HUB_ROOT={root} {PYBIN} -m agenthub.cli serve "
                 f"--host 127.0.0.1 --port {args.port} > {root}/server.log 2>&1")
    bridge_cmd = (f"AGENT_HUB_ROOT={root} {PYBIN} -m agenthub.cli listen "
                  f"--name {args.manager} --pane {args.manager_pane} "
                  f"> {root}/manager-bridge.log 2>&1")

    print(f"[supervisor] up — port={args.port} manager={args.manager} "
          f"watch={args.watch_interval}s tick={args.tick_interval}s", flush=True)
    last_tick = 0.0
    last_update = time.time()  # wait one interval before the first auto-update
    flagged_stale: set[str] = set()  # tasks we've already announced as stale (offline owner)
    stall_alerted: set[str] = set()  # RUNNING tasks already flagged as stalled (#80)
    repo_dir = args.repo_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print(f"[supervisor] stall-detector: alert after {int(args.stall_after)}s "
          f"of no commit/hub activity (repo {repo_dir})", flush=True)
    cu_cursor, cu_seen, cu_user = 0.0, set(), _whoami()   # cross-user DM drain (#88)
    if store.shared_root():
        print(f"[supervisor] cross-user DM drain on for user '{cu_user}' "
              f"(shared_root {store.shared_root()})", flush=True)
    while True:
        try:
            if ensure_server(args.port, serve_cmd):
                time.sleep(3)

            ensure_manager_bridge(store, args.manager, args.manager_pane, bridge_cmd)

            now = time.time()
            if should_tick(now, last_tick, args.tick_interval):
                last_tick = now
                store.post_inbox(
                    args.manager,
                    "⏰ tick: check GitHub issues (label 'ready') and dispatch any new "
                    "ones to available workers. Skip issues already assigned/claimed.",
                    author="system:supervisor", author_name="supervisor",
                    author_kind="system", host="supervisor")
                print("[supervisor] ticked manager", flush=True)

            # Stale-claim recovery: a task whose owner went offline is likely
            # abandoned — tell the manager once so it can reassign (issue #8).
            stale = store.stale_tasks(offline_window=args.stale_after)
            stale_ids = {t["id"] for t in stale}
            for t in stale:
                if t["id"] not in flagged_stale:
                    store.post_inbox(
                        args.manager,
                        f"⚠️ stale task {t['id']} — owner @{t.get('claimed_by')} has been "
                        f"offline >{int(args.stale_after)}s. Reassign with: "
                        f"hubcli task reassign {t['id']} <agent> --author manager",
                        author="system:supervisor", author_name="supervisor",
                        author_kind="system", host="supervisor")
                    print(f"[supervisor] flagged stale task {t['id']}", flush=True)
            flagged_stale = stale_ids  # forget tasks that recovered/were reassigned

            # Task stall detector (issue #80): a RUNNING task with no commit
            # referencing its issue AND no hub message from its owner in
            # --stall-after seconds is likely stuck. One de-duped #general alert
            # tagging owner+manager; re-arms when the task shows activity again.
            running = [t for t in store.list_tasks() if t.get("status") == "running"]
            _hub_ts_cache: dict = {}

            def _activity_ts(t):
                owner = t.get("claimed_by")
                if owner not in _hub_ts_cache:
                    _hub_ts_cache[owner] = owner_hub_ts(store, owner)
                return max(t.get("updated_ts", 0) or 0,
                           _hub_ts_cache[owner],
                           last_commit_ts(repo_dir, t.get("ref")))

            to_alert, active = detect_stalled(running, now, args.stall_after,
                                              stall_alerted, _activity_ts)
            for tid in active:
                stall_alerted.discard(tid)       # activity resumed -> re-arm
            for t in to_alert:
                owner = t.get("claimed_by")
                mins = int(args.stall_after // 60)
                store.post_channel(
                    "general",
                    f"\U0001F6A8 stall: task {t['id']} (@{owner}) has shown no commit or hub "
                    f"activity in >{mins}m \u2014 @{owner} still on it? @{args.manager} may "
                    f"want to check in or reassign (hubcli task reassign {t['id']} <agent> "
                    f"--author {args.manager}).",
                    author="system:supervisor", author_name="supervisor",
                    author_kind="system", host="supervisor", meta={"alert": True})
                stall_alerted.add(t["id"])
                print(f"[supervisor] flagged STALLED task {t['id']}", flush=True)

            # Cross-user DM delivery (#88): drain the shared DM area into local
            # inboxes. No-op unless a shared_root is configured (gated on jpic).
            _n, cu_cursor, cu_seen = drain_cross_user_inbox(
                store, store.shared_root(), cu_user, cu_cursor, cu_seen)
            if _n:
                print(f"[supervisor] delivered {_n} cross-user DM(s)", flush=True)

            # GitHub <-> task sync (issue #9): push any new status transitions to
            # the linked issues (idempotent; no-op for tasks without a ref).
            synced = syncer.tick()
            for tid in synced:
                print(f"[supervisor] synced task {tid} to GitHub", flush=True)

            # Optional self-update (issue #69): pull+apply latest Agora on a
            # schedule so pushed changes reach this install. Off unless enabled.
            if su_enabled and should_tick(now, last_update, su_interval):
                last_update = now
                from . import selfupdate
                res = selfupdate.do_update(restart=True)
                print(f"[supervisor] self-update: {res.get('message')}", flush=True)
                if res.get("changed"):
                    store.post_inbox(
                        args.manager, f"🔄 self-update applied: {res['message']}",
                        author="system:supervisor", author_name="supervisor",
                        author_kind="system", host="supervisor")
        except Exception as e:
            print(f"[supervisor] error: {e}", flush=True)
        time.sleep(args.watch_interval)


if __name__ == "__main__":
    main()
