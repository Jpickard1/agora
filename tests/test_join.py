"""Tests for `hubcli join` — one-command own-server onboarding (#89).

Always uses --no-pointer / a temp root so the real ~/.agent-hub-path and the live
hub are never touched, and --no-serve so no server is started."""

import io
import os
import sys
import tempfile
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub import cli
from agenthub.store import HubStore


def _tmp():
    d = tempfile.mkdtemp(prefix="agora-join-")
    return os.path.join(d, "mine"), os.path.join(d, "shared")


def test_join_hub_creates_own_hub_wired_to_shared():
    mine, shared = _tmp()
    info = cli.join_hub(mine, shared_root=shared)
    assert info["existed"] is False
    assert info["token"] and info["shared_root"] == shared
    # the private hub exists, and the shared store got the public #general
    assert HubStore(mine).config_path.exists()
    assert os.path.isdir(os.path.join(shared, "channels", "general"))


def test_join_hub_is_idempotent():
    mine, shared = _tmp()
    first = cli.join_hub(mine, shared_root=shared)
    second = cli.join_hub(mine, shared_root=shared)
    assert second["existed"] is True
    assert second["token"] == first["token"]      # same hub, same token


def test_join_hub_single_user_without_shared():
    mine, _ = _tmp()
    info = cli.join_hub(mine, shared_root=None)
    assert info["shared_root"] is None


def test_join_command_prints_own_server_model():
    mine, shared = _tmp()
    args = cli.build_parser().parse_args(
        ["--root", mine, "join", "--shared", shared, "--no-serve", "--no-pointer"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        args.func(args)
    out = buf.getvalue()
    assert "your OWN hub is ready" in out
    assert shared in out                                  # the shared store is shown
    assert "never reuse another user" in out              # own-token guidance
    assert "export AGENT_HUB_ROOT=" in out and "export AGENT_HUB_TOKEN=" in out
    assert "hubcli serve" in out                          # how to start it later


def test_join_command_reports_single_user_without_shared():
    mine, _ = _tmp()
    args = cli.build_parser().parse_args(
        ["--root", mine, "join", "--no-serve", "--no-pointer"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        args.func(args)
    assert "single-user" in buf.getvalue()


def test_bare_shared_flag_defaults_to_the_deployment_shared_hub():
    a = cli.build_parser().parse_args(["join", "--shared", "--no-serve"])
    assert a.shared == cli.DEFAULT_SHARED_ROOT
    b = cli.build_parser().parse_args(["join", "--shared", "/x/y", "--no-serve"])
    assert b.shared == "/x/y"
    c = cli.build_parser().parse_args(["join", "--no-serve"])
    assert c.shared is None


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
