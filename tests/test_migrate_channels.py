"""Tests for the safe channel migration command (#92): dry-run, idempotency,
relocation, perms, message preservation."""

import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub.store import HubStore


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def _legacy_two_root():
    """A hub where channels exist in the PRIVATE root (pre-migration), with a
    shared_root now configured — mimics the live state before migrating."""
    priv = tempfile.mkdtemp(prefix="priv-")
    shared = tempfile.mkdtemp(prefix="shared-")
    s = HubStore(priv)
    s.init(token="t")                       # single-root first (no shared)
    # create channels the old way (all land in the private root)
    for ch in ("general", "compute-resources", "memes", "data", "project-1"):
        s.ensure_channel(ch, visibility="public")   # legacy: everything public-ish
        s.post_channel(ch, f"msg in {ch}", author="a", author_name="a")
    # now point at a shared root (as the migration would)
    s.init(shared_root=shared)
    return s, priv, shared


def test_requires_shared_root():
    d = tempfile.mkdtemp(prefix="noshared-")
    s = HubStore(d); s.init()
    try:
        s.migrate_channels()
        assert False, "expected ValueError without shared_root"
    except ValueError:
        pass


def test_dry_run_changes_nothing():
    s, priv, shared = _legacy_two_root()
    plan = s.migrate_channels(dry_run=True)
    # plan is non-empty (channels need moving) but NOTHING moved
    assert plan
    assert os.path.isdir(os.path.join(priv, "channels", "general"))   # still private
    assert not os.path.isdir(os.path.join(shared, "channels", "general"))


def test_migrate_moves_public_and_privatizes_rest():
    s, priv, shared = _legacy_two_root()
    s.migrate_channels()
    # public channels moved to the shared store, group-accessible (2770)
    for ch in ("general", "compute-resources", "memes"):
        d = os.path.join(shared, "channels", ch)
        assert os.path.isdir(d) and _mode(d) == 0o2770
        assert not os.path.isdir(os.path.join(priv, "channels", ch))
    # everything else is private, owner-only (0700), stays in the private root
    for ch in ("data", "project-1"):
        d = os.path.join(priv, "channels", ch)
        assert os.path.isdir(d) and _mode(d) == 0o700


def test_messages_preserved_across_migration():
    s, priv, shared = _legacy_two_root()
    s.migrate_channels()
    assert [m["text"] for m in s.read_channel("general")] == ["msg in general"]
    assert [m["text"] for m in s.read_channel("data")] == ["msg in data"]


def test_idempotent_second_run_is_noop():
    s, priv, shared = _legacy_two_root()
    s.migrate_channels()
    second = s.migrate_channels(dry_run=True)   # already migrated
    assert second == []                          # nothing left to do


def test_custom_public_list():
    s, priv, shared = _legacy_two_root()
    s.migrate_channels(public=["data"])          # only 'data' is public now
    assert os.path.isdir(os.path.join(shared, "channels", "data"))
    assert _mode(os.path.join(priv, "channels", "general")) == 0o700   # now private


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
