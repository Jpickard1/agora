"""Tests for the dual-root channel store (#14): public->shared, private->private,
with the 'only public channels go shared' allowlist + back-compat."""

import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub.store import HubStore


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def _two_root():
    priv = tempfile.mkdtemp(prefix="priv-")
    shared = tempfile.mkdtemp(prefix="shared-")
    s = HubStore(priv)
    s.init(token="t", shared_root=shared)
    return s, priv, shared


# -- routing ---------------------------------------------------------------

def test_general_is_public_in_shared_root():
    s, priv, shared = _two_root()
    g = [c for c in s.list_channels() if c["name"] == "general"][0]
    assert g["visibility"] == "public"
    assert os.path.isdir(os.path.join(shared, "channels", "general"))
    assert not os.path.isdir(os.path.join(priv, "channels", "general"))


def test_new_channel_defaults_private_in_private_root():
    s, priv, shared = _two_root()
    s.ensure_channel("secret")
    cdir = os.path.join(priv, "channels", "secret")
    assert os.path.isdir(cdir)
    assert not os.path.isdir(os.path.join(shared, "channels", "secret"))
    assert _mode(cdir) == 0o700                      # owner-only


def test_public_channel_in_shared_root_group_accessible():
    s, priv, shared = _two_root()
    s.ensure_channel("townhall", visibility="public")
    cdir = os.path.join(shared, "channels", "townhall")
    assert os.path.isdir(cdir)
    m = _mode(cdir)
    assert m & 0o070 == 0o070 and m & stat.S_ISGID and m & 0o007 == 0


def test_post_read_route_to_correct_root():
    s, priv, shared = _two_root()
    s.ensure_channel("townhall", visibility="public")
    s.post_channel("townhall", "hi all", author="a", author_name="a")
    s.ensure_channel("secret")
    s.post_channel("secret", "hush", author="a", author_name="a")
    assert [m["text"] for m in s.read_channel("townhall")] == ["hi all"]
    assert [m["text"] for m in s.read_channel("secret")] == ["hush"]
    # the public message physically lives in the shared root
    assert os.listdir(os.path.join(shared, "channels", "townhall", "messages"))


def test_visibility_change_moves_between_roots_preserving_messages():
    s, priv, shared = _two_root()
    s.ensure_channel("data")                          # private
    s.post_channel("data", "rows", author="a", author_name="a")
    s.set_channel_visibility("data", "public")        # -> shared
    assert os.path.isdir(os.path.join(shared, "channels", "data"))
    assert not os.path.isdir(os.path.join(priv, "channels", "data"))
    assert [m["text"] for m in s.read_channel("data")] == ["rows"]


def test_list_channels_merges_and_tags():
    s, priv, shared = _two_root()
    s.ensure_channel("secret")
    s.ensure_channel("townhall", visibility="public")
    by = {c["name"]: c["visibility"] for c in s.list_channels()}
    assert by["general"] == "public" and by["townhall"] == "public"
    assert by["secret"] == "private"


# -- allowlist: ONLY public channels go shared; everything else private ----

def test_locks_tasks_agents_stay_in_private_root():
    s, priv, shared = _two_root()
    # these stores are rooted at the PRIVATE root, never the shared one
    assert str(s.locks_dir).startswith(priv)
    assert str(s.tasks_dir).startswith(priv)
    assert str(s.agents_dir).startswith(priv)
    assert str(s.inbox_dir).startswith(priv)
    assert str(s.kb_dir).startswith(priv)
    # and the shared root contains ONLY a channels dir (no locks/tasks/agents)
    shared_entries = set(os.listdir(shared))
    assert shared_entries <= {"channels"}
    # creating a lock + task writes under the private root, not shared
    s.acquire_lock("agenthub/bridge.py", owner="worker1") if hasattr(s, "acquire_lock") else None
    s.create_task("t1", title="x")
    assert os.path.isdir(os.path.join(priv, "tasks", "t1"))
    assert not os.path.isdir(os.path.join(shared, "tasks"))


# -- back-compat: no shared root => everything private, channels still work --

def test_back_compat_single_root():
    d = tempfile.mkdtemp(prefix="single-")
    s = HubStore(d)
    s.init(token="t")                                 # no shared_root
    assert s.shared_channels_dir() is None
    s.ensure_channel("general", visibility="public")
    s.post_channel("general", "hello", author="a", author_name="a")
    assert [m["text"] for m in s.read_channel("general")] == ["hello"]
    # general lives in the only (private) root
    assert os.path.isdir(os.path.join(d, "channels", "general"))


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
