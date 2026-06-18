"""Tests for the orphaned-spawn-worker reaper (#119). Pure selection logic on
synthetic ps data — no real ps, no real kills. The destructive --kill path is
exercised only via an injected killer (records pids, never signals anything)."""

import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "reap_orphan_workers", os.path.join(ROOT, "scripts", "reap_orphan_workers.py"))
reap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reap)

SPAWN = ('python -c "from multiprocessing.spawn import spawn_main; '
         'spawn_main(tracker_fd=4, pipe_handle=6)" --multiprocessing-fork')
ME = "worker1"


def _procs():
    return [
        {"pid": 100, "ppid": 1, "user": ME, "cmdline": SPAWN},           # orphan (init)
        {"pid": 101, "ppid": 999, "user": ME, "cmdline": SPAWN},         # parent gone
        {"pid": 102, "ppid": 50, "user": ME, "cmdline": SPAWN},          # live parent
        {"pid": 50, "ppid": 1, "user": ME, "cmdline": "python train.py"},
        {"pid": 103, "ppid": 1, "user": "other", "cmdline": SPAWN},      # other user
        {"pid": 104, "ppid": 1, "user": ME, "cmdline": "python -m agenthub.cli serve"},
    ]


def test_selects_only_orphaned_spawn_workers_owned_by_me():
    pids = sorted(p["pid"] for p in reap.select_orphans(_procs(), me=ME))
    assert pids == [100, 101]


def test_never_touches_a_live_parent():
    assert 102 not in {p["pid"] for p in reap.select_orphans(_procs(), me=ME)}


def test_never_touches_other_users():
    assert 103 not in {p["pid"] for p in reap.select_orphans(_procs(), me=ME)}


def test_ignores_non_spawn_processes():
    got = {p["pid"] for p in reap.select_orphans(_procs(), me=ME)}
    assert 104 not in got and 50 not in got


def test_is_spawn_worker_matches_real_patterns():
    assert reap.is_spawn_worker(SPAWN)
    assert reap.is_spawn_worker("python -c 'from multiprocessing.spawn import spawn_main'")
    assert not reap.is_spawn_worker("python train.py")
    assert not reap.is_spawn_worker("")


def test_dry_run_kills_nothing():
    killed = []
    reap.reap(_procs(), do_kill=False, me=ME, killer=lambda pid, s: killed.append(pid))
    assert killed == []


def test_kill_hits_only_orphans():
    killed = []
    reap.reap(_procs(), do_kill=True, me=ME, killer=lambda pid, s: killed.append(pid))
    assert sorted(killed) == [100, 101]      # live-parent / other-user / non-worker spared


def test_kill_is_gated_behind_yes():
    # --kill without --yes must refuse (exit 2) and signal nothing
    calls = []
    fake = [{"pid": 100, "ppid": 1, "user": ME, "cmdline": SPAWN}]
    reap.list_processes = lambda *a, **k: fake          # avoid real ps
    reap.getpass.getuser = lambda: ME
    orig_kill = reap.os.kill
    reap.os.kill = lambda *a, **k: calls.append(a)
    try:
        rc = reap.main(["--kill"])           # no --yes
    finally:
        reap.os.kill = orig_kill
    assert rc == 2 and calls == []           # refused, nothing killed


def test_selftest_passes():
    assert reap._selftest() is True


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
        passed += 1
    print(f"\n{passed}/{len(tests)} passed")
    return passed == len(tests)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
