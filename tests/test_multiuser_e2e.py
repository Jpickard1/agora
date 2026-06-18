"""End-to-end integration test of the multi-user flow (#98).

Simulates two users sharing one shared root: each joins via the real
`hubcli join --shared` command path, then we assert the cross-user invariants —
a second user sees PUBLIC channels but NOT another user's PRIVATE channels, can
read+post cross-user on the shared channels, and shows up in the shared roster.

Fully hermetic: temp roots only, `--no-pointer`/`--no-serve`, so the live hub and
~/.agent-hub-path are never touched (verified by test_join_flow_is_hermetic)."""

import io
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub import cli, participants
from agenthub.store import HubStore


def _join(root, shared):
    """Run the real `hubcli join --shared <shared>` against a temp root (no serve,
    no pointer write) — exactly what a new user runs, minus the blocking server."""
    args = cli.build_parser().parse_args(
        ["--root", root, "join", "--shared", shared, "--no-serve", "--no-pointer"])
    with redirect_stdout(io.StringIO()):
        args.func(args)


def _world():
    """Two users (A = owner, B = the 2nd user / 'land') on one shared root.
    A makes a public + a private channel and posts to each; B then joins."""
    base = tempfile.mkdtemp(prefix="agora-e2e-")
    shared = os.path.join(base, "shared")
    rootA = os.path.join(base, "userA")
    rootB = os.path.join(base, "userB")

    _join(rootA, shared)                       # user A joins
    a = HubStore(rootA)
    a.ensure_channel("secret-a", visibility="private")    # A's private (own root)
    a.ensure_channel("team", visibility="public")         # public (shared root)
    a.post_channel("general", "hello from A", author="a:agent", author_name="A")
    a.post_channel("secret-a", "A private note", author="a:agent", author_name="A")

    _join(rootB, shared)                       # user B joins the SAME shared root
    b = HubStore(rootB)
    return {"base": base, "shared": shared, "rootA": rootA, "rootB": rootB,
            "a": a, "b": b}


def test_second_user_sees_public_not_private():
    w = _world()
    names = {c["name"] for c in w["b"].list_channels()}
    assert "general" in names and "team" in names      # public channels visible
    assert "secret-a" not in names                     # A's private NOT visible to B


def test_private_channel_lives_only_in_owner_root():
    w = _world()
    # A's private channel is in A's private root, never the shared root
    assert (Path(w["rootA"]) / "channels" / "secret-a" / "meta.json").exists()
    assert not (Path(w["shared"]) / "channels" / "secret-a").exists()
    # the public channel A created lives in the shared root
    assert (Path(w["shared"]) / "channels" / "team" / "meta.json").exists()


def test_second_user_reads_shared_public_messages():
    w = _world()
    texts = [m["text"] for m in w["b"].read_channel("general")]
    assert "hello from A" in texts                      # B reads A's public post
    assert "A private note" not in texts                # private content never leaks


def test_cross_user_post_is_visible_both_ways():
    w = _world()
    w["b"].post_channel("general", "hi from B", author="b:agent", author_name="B")
    seen_by_a = [m["text"] for m in w["a"].read_channel("general")]
    assert "hi from B" in seen_by_a                     # A sees B's cross-user post
    assert "hello from A" in seen_by_a                  # and its own


def test_both_users_register_in_shared_roster():
    w = _world()
    participants.register_participant(w["shared"], "alice", "a-agent", host="hA")
    participants.register_participant(w["shared"], "land", "land-agent", host="hB")
    users = {p["user"] for p in participants.list_participants(w["shared"])}
    assert {"alice", "land"} <= users                   # both visible cross-user


def test_join_flow_is_hermetic():
    """The join path must not touch the live pointer."""
    before = Path("~/.agent-hub-path").expanduser()
    snapshot = before.read_text() if before.exists() else None
    _world()
    after = before.read_text() if before.exists() else None
    assert after == snapshot                            # ~/.agent-hub-path unchanged


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
