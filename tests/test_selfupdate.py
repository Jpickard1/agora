"""Tests for self-update decision logic (#69), with git/pip/tmux mocked."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub import selfupdate as su


class _Patch:
    """Monkeypatch module-level helpers so no real git/pip/tmux runs."""
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
                restart_server=lambda s="agora-server": calls.append("restart") or True):
        r = su.do_update("/tmp/x")
    assert r["ok"] and r["changed"] is False
    assert "up to date" in r["message"]
    assert calls == []          # no pip / no restart on a no-op


def test_upstream_advanced_applies_and_restarts():
    commits = iter(["oldsha0000", "newsha1111"])   # before pull, then after
    calls = []
    with _Patch(is_git_checkout=lambda r: True,
                current_commit=lambda r: next(commits),
                git_pull=lambda r: (0, "Updated."),
                pip_install_editable=lambda r: calls.append("pip") or (0, ""),
                restart_server=lambda s="agora-server": calls.append("restart") or True):
        r = su.do_update("/tmp/x")
    assert r["ok"] and r["changed"] is True
    assert r["old"] == "oldsha0000" and r["new"] == "newsha1111"
    assert r["restarted"] is True and r["pip_ok"] is True
    assert calls == ["pip", "restart"]
    assert "oldsha00" in r["message"] and "newsha11" in r["message"]


def test_no_restart_flag_skips_restart():
    commits = iter(["old", "new"])
    calls = []
    with _Patch(is_git_checkout=lambda r: True,
                current_commit=lambda r: next(commits),
                git_pull=lambda r: (0, ""),
                pip_install_editable=lambda r: (0, ""),
                restart_server=lambda s="agora-server": calls.append("restart") or True):
        r = su.do_update("/tmp/x", restart=False)
    assert r["changed"] and r["restarted"] is False
    assert "restart the server to apply" in r["message"]


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
