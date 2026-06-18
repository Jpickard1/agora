"""#95 web endpoints: /api/participants, /api/crossdm (GET+POST), all gated on a
configured shared_root. Uses FastAPI TestClient + a temp hub (no /ewsc writes)."""
import os, sys, getpass, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient
from agenthub.store import HubStore
from agenthub.server import create_app
from agenthub import participants

def _client(with_shared):
    root = tempfile.mkdtemp(prefix="hub-")
    s = HubStore(root)
    if with_shared:
        shared = tempfile.mkdtemp(prefix="shared-")
        s.init(shared_root=shared)          # persist shared_root in config
    else:
        s.init(); shared = None
    c = TestClient(create_app(root))
    if s.token:                              # auth is on by default
        c.headers.update({"X-Hub-Token": s.token})
    return c, shared

def test_off_when_no_shared_root():
    c, _ = _client(False)
    assert c.get("/api/participants").json()["enabled"] is False
    assert c.get("/api/crossdm").json()["enabled"] is False
    assert c.post("/api/crossdm", json={"to": "bob:x", "text": "hi"}).status_code == 409

def test_participants_listed_when_enabled():
    c, shared = _client(True)
    participants.register_participant(shared, "alice", "trainer", host="h1")
    body = c.get("/api/participants").json()
    assert body["enabled"] is True
    assert any(u["user"] == "alice" for u in body["users"])

def test_send_and_receive_cross_user_dm():
    c, shared = _client(True)
    me = getpass.getuser()
    r = c.post("/api/crossdm", json={"to": f"{me}:probe", "text": "ping me"})
    assert r.status_code == 200, r.text
    msgs = c.get("/api/crossdm").json()["messages"]
    assert any(m["text"] == "ping me" for m in msgs)

def test_send_requires_fields():
    c, _ = _client(True)
    assert c.post("/api/crossdm", json={"to": "", "text": "x"}).status_code == 400
    assert c.post("/api/crossdm", json={"to": "bob:x", "text": ""}).status_code == 400

def run():
    t=[v for k,v in sorted(globals().items()) if k.startswith("test_")]; p=0
    for f in t: f(); print("PASS", f.__name__); p+=1
    print(f"\n{p}/{len(t)} passed"); return p==len(t)
if __name__ == "__main__": sys.exit(0 if run() else 1)
