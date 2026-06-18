#!/usr/bin/env python3
"""Reap orphaned multiprocessing spawn-workers that hold GPU contexts (issue #119).

Python `multiprocessing` *spawn* workers
(`python -c "from multiprocessing.spawn import spawn_main ..."`) can survive a
parent `pkill` — they get reparented to PID 1 — and keep holding GPU contexts,
starving other jobs on shared hosts. This finds them and, ONLY when explicitly
asked, kills them.

SAFE by default: with no flags it just REPORTS candidates (a dry run — pure
read). Killing is gated behind `--kill` AND `--yes`, because it is a DESTRUCTIVE
operation on shared GPU hosts used by other EWSC users and must not be run
without the host owner's explicit OK.

Conservative guards — a process is a candidate only if ALL hold:
  - its cmdline is a multiprocessing spawn-worker, AND
  - it is orphaned (parent is PID 1, or its ppid is not among live processes), AND
  - it is owned by the current user.
It NEVER targets a process whose parent is still alive, nor another user's process.

Usage:
  python scripts/reap_orphan_workers.py             # dry-run: list orphaned workers
  python scripts/reap_orphan_workers.py --json
  python scripts/reap_orphan_workers.py --kill --yes   # GATED — needs host-owner OK
  python scripts/reap_orphan_workers.py --selftest
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import signal
import subprocess
import sys

# Matches the cmdline of a multiprocessing spawn worker (and the fork shim).
SPAWN_RE = re.compile(r"multiprocessing\.spawn|spawn_main|--multiprocessing-fork")


def list_processes(run=subprocess.run):
    """Snapshot of processes as [{pid, ppid, user, cmdline}]. `run` is injectable
    for tests so no real `ps` is needed."""
    p = run(["ps", "-eo", "pid=,ppid=,user=,args="], capture_output=True, text=True)
    out = []
    for line in (getattr(p, "stdout", "") or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 3)          # pid ppid user "args (may contain spaces)"
        if len(parts) < 4:
            continue
        pid, ppid, user, cmdline = parts
        try:
            out.append({"pid": int(pid), "ppid": int(ppid), "user": user, "cmdline": cmdline})
        except ValueError:
            continue
    return out


def is_spawn_worker(cmdline):
    return bool(SPAWN_RE.search(cmdline or ""))


def select_orphans(procs, me=None):
    """PURE: the orphaned mp spawn-workers owned by `me`. Orphaned = parent is PID
    1, or the parent pid is not among the live processes. A process whose parent
    is alive (and not init) is NEVER selected."""
    me = me or getpass.getuser()
    live = {p["pid"] for p in procs}
    orphans = []
    for p in procs:
        if p.get("user") != me:
            continue                          # never touch other users' processes
        if not is_spawn_worker(p.get("cmdline", "")):
            continue                          # only spawn-workers
        ppid = p.get("ppid")
        if ppid == 1 or ppid not in live:     # reparented to init, or parent gone
            orphans.append(p)
    return orphans


def reap(procs, *, do_kill=False, me=None, sig=signal.SIGTERM, killer=os.kill):
    """Return the orphan candidates; if do_kill, also signal them. `killer` is
    injectable for tests. do_kill is OFF by default."""
    orphans = select_orphans(procs, me=me)
    if do_kill:
        for p in orphans:
            try:
                killer(p["pid"], sig)
                p["killed"] = True
            except Exception as e:            # noqa: BLE001
                p["killed"] = False
                p["error"] = str(e)
    return orphans


def _report(orphans):
    if not orphans:
        print("No orphaned spawn-workers found. ✓")
        return
    print(f"Found {len(orphans)} orphaned spawn-worker(s) (owned by you, parent dead):")
    for p in orphans:
        cl = p["cmdline"]
        print(f"  pid {p['pid']:>7}  ppid {p['ppid']:>7}  {cl[:100]}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Reap orphaned mp spawn-workers (#119)")
    ap.add_argument("--kill", action="store_true",
                    help="GATED: kill the orphans (destructive on shared hosts; also needs --yes)")
    ap.add_argument("--yes", action="store_true",
                    help="Required with --kill to confirm you have host-owner approval")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="synthetic-data assertions")
    args = ap.parse_args(argv)
    if args.selftest:
        return 0 if _selftest() else 1

    procs = list_processes()
    orphans = select_orphans(procs)
    if args.kill and not args.yes:
        print("⛔ Refusing to --kill without --yes. Killing runs on SHARED GPU hosts "
              "used by other EWSC users — get the host owner's explicit OK first, then "
              "re-run with --kill --yes.", file=sys.stderr)
        _report(orphans)
        return 2
    if args.kill:                              # --kill --yes
        reap(procs, do_kill=True)
    if args.json:
        print(json.dumps(orphans, indent=2))
    else:
        _report(orphans)
        if orphans and not args.kill:
            print("\n(dry run — nothing killed. To clean up: --kill --yes, "
                  "only with host-owner approval.)")
    return 0


def _selftest():
    me = "worker1"
    spawn = 'python -c "from multiprocessing.spawn import spawn_main; spawn_main(...)" --multiprocessing-fork'
    procs = [
        {"pid": 100, "ppid": 1, "user": me, "cmdline": spawn},          # orphan (ppid 1) -> select
        {"pid": 101, "ppid": 999, "user": me, "cmdline": spawn},        # parent 999 gone -> select
        {"pid": 102, "ppid": 50, "user": me, "cmdline": spawn},         # parent 50 ALIVE -> skip
        {"pid": 50, "ppid": 1, "user": me, "cmdline": "python train.py"},  # the live parent (not a worker)
        {"pid": 103, "ppid": 1, "user": "someone_else", "cmdline": spawn},  # other user -> skip
        {"pid": 104, "ppid": 1, "user": me, "cmdline": "python -m agenthub.cli serve"},  # not a worker -> skip
    ]
    orphans = select_orphans(procs, me=me)
    pids = sorted(p["pid"] for p in orphans)
    assert pids == [100, 101], pids                       # only orphaned spawn-workers, mine
    assert 102 not in pids                                # live parent -> never touched
    assert 103 not in pids and 104 not in pids            # other user / non-worker excluded

    # do_kill uses the injected killer and only hits the orphans
    killed = []
    reap(procs, do_kill=True, me=me, killer=lambda pid, s: killed.append(pid))
    assert sorted(killed) == [100, 101], killed
    # dry run kills nothing
    killed2 = []
    reap(procs, do_kill=False, me=me, killer=lambda pid, s: killed2.append(pid))
    assert killed2 == [], killed2

    assert is_spawn_worker(spawn) and not is_spawn_worker("python train.py")
    print("selftest: PASS (orphan selection, live-parent/other-user/non-worker excluded, "
          "kill hits only orphans, dry-run kills nothing)")
    return True


if __name__ == "__main__":
    sys.exit(main())
