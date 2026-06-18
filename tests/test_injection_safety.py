"""Security regression tests for shell-injection via hub message content (#121).

Proves (1) the bridge injects message content LITERALLY — tmux `send-keys -l --`,
list-form subprocess, never shell=True — so backticks / $() / ;rm in a posted
message are typed verbatim into the agent's pane and never executed in any
listening agent; and (2) `hubcli post --body-file` delivers content verbatim (the
shell-safe sender path), so a future refactor can't silently reintroduce a shell
on either side."""

import contextlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub import bridge, cli
from agenthub.store import HubStore

# a message crafted to run commands IF anything evaluated it via a shell
MALICIOUS = "hi `touch /tmp/pwned` $(rm -rf x) ; rm -rf y && echo z"


def _capture_inject(text):
    calls = []
    orig = bridge.subprocess.run
    bridge.subprocess.run = lambda args, **kw: calls.append((args, kw)) or None
    try:
        bridge.inject("%1", text)
    finally:
        bridge.subprocess.run = orig
    return calls


def test_inject_is_literal_list_form_no_shell():
    calls = _capture_inject(MALICIOUS)
    args0, kw0 = calls[0]
    # list-form argv (not a shell string), with -l (literal) and -- (end opts)
    assert args0 == ["tmux", "send-keys", "-t", "%1", "-l", "--", MALICIOUS]
    assert kw0.get("shell") is not True            # never shell=True
    # the dangerous content is a single literal argv element, unmodified
    assert args0[-1] == MALICIOUS and "`" in args0[-1] and "$(" in args0[-1]
    # second call just submits (Enter) — no content in it
    assert calls[1][0] == ["tmux", "send-keys", "-t", "%1", "Enter"]


def test_inject_flattens_newlines_no_early_submit():
    # newlines must not become extra key-presses that could submit a partial line
    sent = _capture_inject("line1\nrm -rf z\nline3")[0][0][-1]
    assert "\n" not in sent and sent == "line1 rm -rf z line3"


def test_post_body_file_delivers_content_verbatim():
    root = tempfile.mkdtemp(prefix="inj-")
    HubStore(root).init(token="t")
    p = os.path.join(root, "body.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write(MALICIOUS + "\n")
    args = cli.build_parser().parse_args(
        ["--root", root, "post", "-c", "general", "--author", "yi", "--body-file", p])
    with contextlib.redirect_stdout(io.StringIO()):
        args.func(args)
    m = HubStore(root).read_channel("general")[-1]
    assert m["text"] == MALICIOUS                  # stored verbatim; nothing substituted


def test_posted_then_injected_round_trips_literally():
    # end-to-end: a malicious message stored via --body-file, then what the bridge
    # WOULD inject for it is the literal text (list-form, -l) — executes nothing.
    root = tempfile.mkdtemp(prefix="inj-")
    HubStore(root).init(token="t")
    p = os.path.join(root, "b.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write(MALICIOUS)
    args = cli.build_parser().parse_args(
        ["--root", root, "post", "-c", "general", "--author", "yi", "--body-file", p])
    with contextlib.redirect_stdout(io.StringIO()):
        args.func(args)
    stored = HubStore(root).read_channel("general")[-1]["text"]
    injected = _capture_inject(stored)[0][0][-1]
    assert injected == MALICIOUS                   # literal end to end


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
