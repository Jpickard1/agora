"""CLI-exec tests for 'hubcli mkchannel' visibility flags (#14 hotfix).

Regression guard: a prior bug referenced args.public (which the parser never
sets — --public is an alias of --shared via dest=shared), so 'mkchannel --private'
crashed with AttributeError. The store + --help tests missed it because they
never RAN the command through the parser. These do — parse argv, then call
args.func(args) against a temp hub. Uses HubStore(tmp).init() (never the 'init'
subcommand, which would clobber ~/.agent-hub-path)."""

import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub.cli import build_parser
from agenthub.store import HubStore


def _hub():
    d = tempfile.mkdtemp(prefix="cli-mk-")
    HubStore(d).init(token="t")              # init in-process; no pointer write
    return d


def _run(root, *argv):
    parser = build_parser()
    args = parser.parse_args(["--root", root, *argv])
    args.func(args)                          # this is what the bug crashed in


def _mode(root, name):
    return stat.S_IMODE(os.stat(os.path.join(root, "channels", name)).st_mode)


def test_mkchannel_private_runs_and_is_0700():
    d = _hub()
    _run(d, "mkchannel", "secret", "--private")     # used to AttributeError
    assert _mode(d, "secret") == 0o700
    vis = {c["name"]: c["visibility"] for c in HubStore(d).list_channels()}
    assert vis["secret"] == "private"


def test_mkchannel_default_is_private():
    d = _hub()
    _run(d, "mkchannel", "plain")                   # no flag -> private
    assert _mode(d, "plain") == 0o700


def test_mkchannel_shared_is_public():
    d = _hub()
    _run(d, "mkchannel", "townhall", "--shared")
    vis = {c["name"]: c["visibility"] for c in HubStore(d).list_channels()}
    assert vis["townhall"] == "public"


def test_mkchannel_public_alias_is_public():
    d = _hub()
    _run(d, "mkchannel", "townhall2", "--public")   # alias of --shared
    vis = {c["name"]: c["visibility"] for c in HubStore(d).list_channels()}
    assert vis["townhall2"] == "public"


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
