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
    flagged_stale: set[str] = set()  # tasks we've already announced as stale
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
