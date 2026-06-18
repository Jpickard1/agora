"""Tests for self-update (#69) + health-verified restart (#104), git/pip/tmux/http mocked."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub import selfupdate as su


class _Patch:
    """Monkeypatch module-level helpers so no real git/pip/tmux/http runs."""
    def __init__(self, **fns):
        self.fns = fns
        self.saved = {}

    def __enter__(self):
        for k, v in self.fns.items():
            self.saved[k] = getattr(su, k)
            setattr(su, k, v)
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            setattr(su, k, v)


# common mocks for the "upstream advanced" path
def _advanced(calls, *, restart_ok=True, healthy=True):
    commits = iter(["oldsha0000", "newsha1111"])   # before pull, then after
    return _Patch(
        is_git_checkout=lambda r: True,
        current_commit=lambda r: next(commits),
        git_pull=lambda r: (0, "Updated."),
        pip_install_editable=lambda r: calls.append("pip") or (0, ""),
        resolve_hub_root=lambda: "/hub",
        restart_server=lambda session=su.SERVER_SESSION, **kw:
            calls.append(("restart", kw.get("cwd"), kw.get("port"))) or restart_ok,
        wait_for_health=lambda port=su.DEFAULT_PORT, **kw:
            calls.append(("health", port)) or healthy)


def test_not_a_git_checkout():
    with _Patch(is_git_checkout=lambda r: False):
        r = su.do_update("/tmp/x")
    assert r["ok"] is False and r["git"] is False
    assert "not a git checkout" in r["message"]


def test_already_current_is_noop():
    calls = []
    with _Patch(is_git_checkout=lambda r: True,
                current_commit=lambda r: "abc123",
                git_pull=lambda r: (0, ""),
                pip_install_editable=lambda r: calls.append("pip") or (0, ""),
                restart_server=lambda *a, **k: calls.append("restart") or True,
                wait_for_health=lambda *a, **k: calls.append("health") or True):
        r = su.do_update("/tmp/x")
    assert r["ok"] and r["changed"] is False
    assert "up to date" in r["message"]
    assert calls == []          # no pip / no restart / no health probe on a no-op


def test_upstream_advanced_restarts_and_verifies_health():
    calls = []
    with _advanced(calls):
        r = su.do_update("/tmp/x", port=8910)
    assert r["ok"] and r["changed"] is True
    assert r["old"] == "oldsha0000" and r["new"] == "newsha1111"
    assert r["restarted"] is True and r["healthy"] is True and r["pip_ok"] is True
    # pip, then restart, then a health probe — in that order
    assert calls[0] == "pip"
    assert calls[1][0] == "restart" and calls[1][1] == "/tmp/x"   # launched from live-tree cwd
    assert calls[2][0] == "health"
    assert "health OK" in r["message"]


def test_restart_but_server_never_returns_fails_loudly():
    calls = []
    with _advanced(calls, healthy=False):
        r = su.do_update("/tmp/x", port=8910, health_timeout=5.0)
    assert r["ok"] is False           # only report success once it's actually serving
    assert r["restarted"] is True and r["healthy"] is False
    assert "did NOT return" in r["message"]
    assert "hubcli up" in r["message"] and "server.log" in r["message"]   # recovery steps


def test_restart_command_fails():
    calls = []
    with _advanced(calls, restart_ok=False):
        r = su.do_update("/tmp/x")
    assert r["ok"] is True and r["changed"] is True   # update applied
    assert r["restarted"] is False and r["healthy"] is None
    assert "could not restart" in r["message"] and "hubcli up" in r["message"]
    assert not any(c[0] == "health" for c in calls if isinstance(c, tuple))   # no health probe


def test_no_restart_flag_skips_restart_and_health():
    commits = iter(["old", "new"])
    calls = []
    with _Patch(is_git_checkout=lambda r: True,
                current_commit=lambda r: next(commits),
                git_pull=lambda r: (0, ""),
                pip_install_editable=lambda r: (0, ""),
                restart_server=lambda *a, **k: calls.append("restart") or True,
                wait_for_health=lambda *a, **k: calls.append("health") or True):
        r = su.do_update("/tmp/x", restart=False)
    assert r["changed"] and r["restarted"] is False and r["healthy"] is None
    assert "restart the server to apply" in r["message"]
    assert calls == []


def test_git_pull_failure():
    with _Patch(is_git_checkout=lambda r: True,
                current_commit=lambda r: "abc",
                git_pull=lambda r: (1, "fatal: conflict")):
        r = su.do_update("/tmp/x")
    assert r["ok"] is False and "git pull failed" in r["message"]


def test_check_only_reports_behind():
    with _Patch(is_git_checkout=lambda r: True,
                current_commit=lambda r: "abc",
                commits_behind=lambda r: 3):
        r = su.do_update("/tmp/x", check_only=True)
    assert r["check_only"] and r["behind"] == 3
    assert "3 new commit" in r["message"]


def test_check_only_up_to_date():
    with _Patch(is_git_checkout=lambda r: True,
                current_commit=lambda r: "abcdef99",
                commits_behind=lambda r: 0):
        r = su.do_update("/tmp/x", check_only=True)
    assert r["behind"] == 0 and "up to date" in r["message"]


# ---- unit tests for the new restart/health helpers (#104) ----

def test_serve_command_matches_normal_start():
    cmd = su.serve_command("/my/hub", port=8910, python="/usr/bin/python3")
    assert "AGENT_HUB_ROOT=/my/hub" in cmd
    assert "-m agenthub.cli serve" in cmd
    assert "--port 8910" in cmd
    assert "/my/hub/server.log" in cmd


def test_restart_server_starts_in_place_with_cwd():
    captured = {}
    def fake_tmux(session, command, cwd=None):
        captured.update(session=session, command=command, cwd=cwd)
    with _Patch(_tmux_restart=fake_tmux):
        ok = su.restart_server("agora-server", hub_root="/h", port=9999, cwd="/repo")
    assert ok is True
    assert captured["session"] == "agora-server"
    assert captured["cwd"] == "/repo"                       # launched from the live tree
    assert "--port 9999" in captured["command"] and "AGENT_HUB_ROOT=/h" in captured["command"]


def test_restart_server_resolves_hub_root_when_missing():
    captured = {}
    with _Patch(resolve_hub_root=lambda: "/derived",
                _tmux_restart=lambda s, c, cwd=None: captured.update(command=c)):
        ok = su.restart_server(port=8910)
    assert ok is True and "AGENT_HUB_ROOT=/derived" in captured["command"]


def test_wait_for_health_polls_until_ok():
    seq = iter([False, False, True])
    with _Patch(server_health_ok=lambda port=su.DEFAULT_PORT, timeout=3.0: next(seq)):
        assert su.wait_for_health(8910, timeout=5.0, interval=0.0) is True


def test_wait_for_health_times_out():
    with _Patch(server_health_ok=lambda port=su.DEFAULT_PORT, timeout=3.0: False):
        assert su.wait_for_health(8910, timeout=0.05, interval=0.01) is False


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
