"""Integration tests for #86/#88 wired onto the #14 dual-root core: supervisor
drain -> local inbox, and `hubcli send <user>:<agent>` routing to the shared
DM area. Uses a real store + a stub shared_root (no /ewsc, no live writes)."""
import os, sys, tempfile, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agenthub.store import HubStore
from agenthub import crossdm, supervisor, cli

def _store_with_shared():
    root = tempfile.mkdtemp(prefix="hub-"); shared = tempfile.mkdtemp(prefix="shared-")
    s = HubStore(root); s.init(token="t", shared_root=shared)
    return s, shared

def test_store_exposes_shared_root():
    s, shared = _store_with_shared()
    assert str(s.shared_root()) == str(__import__("pathlib").Path(shared).resolve())

def test_supervisor_drain_delivers_to_local_inbox():
    s, shared = _store_with_shared()
    # alice (a different user) DMs bob's agent via the shared area
    r = crossdm.post_cross_user_dm(str(s.shared_root()), "bob:bobgent",
                                   "alice", "alice-agent", "hello bob", now=1.0)
    assert r["ok"]
    n, cur, seen = supervisor.drain_cross_user_inbox(s, s.shared_root(), "bob", 0.0, set())
    assert n == 1
    inbox = s.read_inbox("bobgent")
    assert any("hello bob" in m["text"] for m in inbox)
    assert any(m.get("author", "").startswith("alice") for m in inbox)   # from-identity
    # idempotent: second drain delivers nothing new
    n2, _, _ = supervisor.drain_cross_user_inbox(s, s.shared_root(), "bob", cur, seen)
    assert n2 == 0

def test_drain_noop_without_shared_root():
    s = HubStore(tempfile.mkdtemp()); s.init(token="t")   # single-root, no shared
    n, cur, seen = supervisor.drain_cross_user_inbox(s, s.shared_root(), "bob", 0.0, set())
    assert n == 0 and s.shared_root() is None

def test_cmd_send_cross_user_routes_to_shared():
    s, shared = _store_with_shared()
    args = types.SimpleNamespace(root=str(s.root), to="bob:bobgent", text="hi bob",
                                 author="alice-agent", id=None, kind="agent", user="alice")
    cli.cmd_send(args)
    msgs, _, _ = crossdm.drain_shared_dms(str(s.shared_root()), "bob")
    assert [m["text"] for m in msgs] == ["hi bob"]

def test_cmd_send_local_still_works():
    s, shared = _store_with_shared()
    args = types.SimpleNamespace(root=str(s.root), to="localagent", text="local msg",
                                 author="me", id=None, kind="agent", user="alice")
    cli.cmd_send(args)
    assert any(m["text"] == "local msg" for m in s.read_inbox("localagent"))

def run():
    t=[v for k,v in sorted(globals().items()) if k.startswith("test_")]; p=0
    for f in t:
        f(); print("PASS", f.__name__); p+=1
    print(f"\n{p}/{len(t)} passed"); return p==len(t)
if __name__ == "__main__": sys.exit(0 if run() else 1)
