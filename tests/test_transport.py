"""Tests for cross-platform bridge transport + spawn guard (issue #15).
Headless and OS-independent — Windows behaviour is simulated by monkeypatching
is_windows, so these run anywhere.
Run: python tests/test_transport.py"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub import transport as t  # noqa: E402


# -- transport selection ---------------------------------------------------

def test_choose_tmux_when_available_with_pane():
    assert t.choose_transport("auto", has_tmux=True, has_pane=True) == "tmux"


def test_choose_file_when_no_tmux():
    assert t.choose_transport("auto", has_tmux=False, has_pane=False) == "file"


def test_choose_file_when_tmux_but_no_pane():
    assert t.choose_transport("auto", has_tmux=True, has_pane=False) == "file"


def test_explicit_choice_overrides_auto():
    assert t.choose_transport("file", has_tmux=True, has_pane=True) == "file"
    assert t.choose_transport("stdout", has_tmux=True, has_pane=True) == "stdout"
    assert t.choose_transport("tmux", has_tmux=False, has_pane=False) == "tmux"


# -- file transport --------------------------------------------------------

def test_file_transport_appends_lines():
    p = os.path.join(tempfile.mkdtemp(), "inbox.txt")
    ft = t.FileTransport(p)
    ft.deliver("[HUB #general from a]: one")
    ft.deliver("two\n")                      # trailing newline normalised
    assert open(p, encoding="utf-8").read() == "[HUB #general from a]: one\ntwo\n"


def test_file_transport_is_never_busy():
    p = os.path.join(tempfile.mkdtemp(), "inbox.txt")
    assert t.FileTransport(p).busy() is False


def test_file_transport_creates_parent_dir():
    d = os.path.join(tempfile.mkdtemp(), "nested", "deep")
    p = os.path.join(d, "inbox.txt")
    t.FileTransport(p).deliver("x")
    assert os.path.exists(p)


def test_default_inbox_path():
    p = t.default_inbox_path("/hub/root", "bot")
    assert str(p).endswith(os.path.join("root", "inbox-bot.txt"))


# -- tmux transport (no real tmux needed) ----------------------------------

def test_tmux_transport_delegates():
    sent = []
    tr = t.TmuxTransport("%1", lambda pane, line: sent.append((pane, line)),
                         lambda: True)
    tr.deliver("hello")
    assert sent == [("%1", "hello")]
    assert tr.busy() is True


# -- hostname is portable --------------------------------------------------

def test_hostname_is_nonempty_string():
    h = t.hostname()
    assert isinstance(h, str) and h and "." not in h


# -- spawn guard on Windows ------------------------------------------------

def test_spawn_blocked_on_windows(monkeypatch=None):
    from agenthub import spawn
    orig = t.is_windows
    t.is_windows = lambda: True
    try:
        raised = False
        try:
            spawn.build_spawn_plan("bot", "C:/work", "", "bot", "tasks",
                                   hub_root="Z:/hub")
        except ValueError as e:
            raised = True
            assert "Windows" in str(e)
        assert raised, "expected ValueError on Windows"
    finally:
        t.is_windows = orig


def test_spawn_works_when_not_windows():
    from agenthub import spawn
    orig = t.is_windows
    t.is_windows = lambda: False
    try:
        plan = spawn.build_spawn_plan("bot", "/work", "", "bot", "tasks",
                                      hub_root="/hub")
        assert plan["name"] == "bot"
        assert plan["immediate"]            # tmux argv steps present
    finally:
        t.is_windows = orig


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in tests:
        fn()
        print(f"PASS {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(tests)} passed")
    return passed == len(tests)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
