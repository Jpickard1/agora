"""Tests for agent spawning (agora-1): plan construction, validation, and the
injection-safety of the shell-out path."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub import spawn


def _plan(**kw):
    base = dict(name="trainer", path="/work/dir", machine="", session="",
                tasks="do things", hub_root="/ewsc/jpickard/.agent-hub")
    base.update(kw)
    return spawn.build_spawn_plan(**base)


def test_local_plan_uses_argv_arrays():
    p = _plan(claude_bin="claude")
    assert p["local"] is True and p["target"] == "local"
    # claude starts in the right session + working dir
    assert p["immediate"][0] == ["tmux", "new-session", "-d", "-s", "trainer",
                                 "-c", "/work/dir", "claude"]
    # bridge runs in its own session and listens for this agent
    assert p["bridge_session"] == "trainer-bridge"
    assert "hubcli" in p["immediate"][1][-1] and "--name trainer" in p["immediate"][1][-1]
    # the seed prompt is typed, then Enter
    assert p["delayed"][0][:5] == ["tmux", "send-keys", "-t", "trainer", "-l"]
    assert p["delayed"][1] == ["tmux", "send-keys", "-t", "trainer", "Enter"]


def test_claude_bin_prefers_claude2():
    # Manual env/attr save+restore so this passes under BOTH pytest and the
    # repo's plain runner (which calls each test with no args — a monkeypatch
    # fixture param would TypeError there).
    saved_env = os.environ.get("AGORA_CLAUDE_BIN")
    saved_which = spawn.shutil.which
    try:
        # explicit override always wins
        os.environ["AGORA_CLAUDE_BIN"] = "/custom/claude"
        assert spawn._claude_bin() == "/custom/claude"
        os.environ.pop("AGORA_CLAUDE_BIN", None)
        # prefer claude2 when it's on PATH...
        spawn.shutil.which = lambda n: "/usr/bin/claude2" if n == "claude2" else None
        assert spawn._claude_bin() == "claude2"
        # ...and fall back to claude when claude2 is absent
        spawn.shutil.which = lambda n: None
        assert spawn._claude_bin() == "claude"
    finally:
        spawn.shutil.which = saved_which
        if saved_env is None:
            os.environ.pop("AGORA_CLAUDE_BIN", None)
        else:
            os.environ["AGORA_CLAUDE_BIN"] = saved_env


def test_session_defaults_to_name_and_is_sanitised():
    p = _plan(name="My Agent!", session="")
    assert p["name"] == "My-Agent"          # sanitised
    assert p["session"] == "My-Agent"       # defaults to the (sanitised) name


def test_missing_name_or_path_raises():
    for kw in (dict(name=""), dict(name="   "), dict(path=""), dict(path="  ")):
        try:
            _plan(**kw)
            assert False, f"expected ValueError for {kw}"
        except ValueError:
            pass


def test_bad_machine_rejected():
    try:
        _plan(machine="evil; rm -rf /")
        assert False, "expected ValueError for bad machine"
    except ValueError:
        pass


def test_path_with_control_chars_rejected():
    try:
        _plan(path="/tmp/x\nrm -rf /")
        assert False, "expected ValueError for control chars in path"
    except ValueError:
        pass


def test_injection_in_path_is_inert_as_single_argv():
    # A shell-metachar-laden path must remain ONE argv element (local exec has no
    # shell), so it can never run as a command.
    nasty = "/work/dir; touch /tmp/pwned"
    p = _plan(path=nasty)
    claude_step = p["immediate"][0]
    assert nasty in claude_step               # present verbatim
    assert claude_step.index(nasty) == claude_step.index("-c") + 1
    # not split into multiple tokens
    assert claude_step.count(nasty) == 1


def test_remote_plan_builds_quoted_ssh_command():
    p = _plan(machine="gpu07")
    assert p["local"] is False and p["target"] == "ssh:gpu07"
    argv = spawn._remote_argv(p["machine"], p["immediate"] + p["delayed"], p["seed_delay"])
    assert argv[0] == "ssh" and argv[1] == "gpu07"
    # whole remote command is a single shlex-quoted bash -lc string
    assert argv[2].startswith("bash -lc ")
    assert "new-session" in argv[2] and "send-keys" in argv[2]


def test_bootstrap_prompt_mentions_identity_and_announce():
    pr = spawn.bootstrap_prompt("trainer", "train models", "trainer-bridge")
    assert "trainer" in pr and "announce yourself" in pr.lower()
    assert "hubcli post" in pr


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
