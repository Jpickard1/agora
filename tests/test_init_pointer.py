"""Tests for the init pointer guard (issue #39): config.set_pointer must never
silently overwrite a pointer to a DIFFERENT hub. Fully isolated — every test
points POINTER_FILE at a temp file, so the real ~/.agent-hub-path is untouched.
Run: python tests/test_init_pointer.py"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub import config  # noqa: E402


def isolated():
    """A fresh temp POINTER_FILE; returns its path. Caller restores."""
    d = tempfile.mkdtemp(prefix="ptr-")
    config.POINTER_FILE = Path(d) / ".agent-hub-path"
    return config.POINTER_FILE


def test_writes_when_no_pointer_exists():
    isolated()
    action, prev = config.set_pointer("/hub/a")
    assert action == "written" and prev is None
    assert config.read_pointer() == str(Path("/hub/a").resolve())


def test_unchanged_when_same_root():
    isolated()
    config.set_pointer("/hub/a")
    action, prev = config.set_pointer("/hub/a")
    assert action == "unchanged"
    assert config.read_pointer() == str(Path("/hub/a").resolve())


def test_refuses_different_root_without_force():
    p = isolated()
    config.set_pointer("/hub/a")
    action, prev = config.set_pointer("/hub/b")
    assert action == "refused"
    assert prev == str(Path("/hub/a").resolve())
    # the existing pointer is left intact — the whole point of the fix
    assert config.read_pointer() == str(Path("/hub/a").resolve())


def test_overwrites_different_root_with_force():
    isolated()
    config.set_pointer("/hub/a")
    action, prev = config.set_pointer("/hub/b", force=True)
    assert action == "overwritten"
    assert prev == str(Path("/hub/a").resolve())
    assert config.read_pointer() == str(Path("/hub/b").resolve())


def test_read_pointer_none_when_absent():
    isolated()
    assert config.read_pointer() is None


def test_write_pointer_is_unconditional_low_level():
    isolated()
    config.write_pointer("/hub/a")
    config.write_pointer("/hub/b")     # low-level helper still overwrites
    assert config.read_pointer() == "/hub/b"


def run():
    saved = config.POINTER_FILE
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    try:
        for t in tests:
            t()
            print(f"PASS {t.__name__}")
            passed += 1
    finally:
        config.POINTER_FILE = saved   # never leave the real pointer redirected
    print(f"\n{passed}/{len(tests)} passed")
    return passed == len(tests)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
