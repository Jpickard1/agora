"""Tests for the configurable display name (issue #67): server.os_username()
defaults to the OS account and never hard-codes a person.
Run: python tests/test_whoami.py"""

import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub import server  # noqa: E402


def test_returns_os_username_normally():
    assert server.os_username() == getpass.getuser()


def test_fallback_to_user_on_exception():
    orig = getpass.getuser
    getpass.getuser = lambda: (_ for _ in ()).throw(OSError("no user"))
    try:
        assert server.os_username() == "user"
    finally:
        getpass.getuser = orig


def test_fallback_to_user_when_blank():
    orig = getpass.getuser
    getpass.getuser = lambda: "   "
    try:
        assert server.os_username() == "user"
    finally:
        getpass.getuser = orig


def test_strips_whitespace():
    orig = getpass.getuser
    getpass.getuser = lambda: "  bob  "
    try:
        assert server.os_username() == "bob"
    finally:
        getpass.getuser = orig


def test_no_hardcoded_person_in_source():
    # Guard against a hardcoded display name creeping back into the UI/server.
    js = open(os.path.join(os.path.dirname(__file__), "..",
                           "agenthub", "web", "app.js"), encoding="utf-8").read()
    assert '"jpic"' not in js and "'jpic'" not in js, "hardcoded name in app.js"


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
