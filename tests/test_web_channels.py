"""#102: POST /api/channels honors the public/private selector; list reflects it."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient
from agenthub.store import HubStore
from agenthub.server import create_app

def _client():
    root = tempfile.mkdtemp(prefix="hub-")
    s = HubStore(root); s.init()
    c = TestClient(create_app(root))
    if s.token:
        c.headers.update({"X-Hub-Token": s.token})
    return c

def test_create_public_and_private():
    c = _client()
    assert c.post("/api/channels", json={"name": "planning", "visibility": "public"}).json()["visibility"] == "public"
    assert c.post("/api/channels", json={"name": "secret", "visibility": "private"}).json()["visibility"] == "private"
    vis = {ch["name"]: ch.get("visibility") for ch in c.get("/api/channels").json()}
    assert vis["planning"] == "public" and vis["secret"] == "private"

def test_default_visibility_is_public():
    c = _client()
    assert c.post("/api/channels", json={"name": "noflag"}).json()["visibility"] == "public"

def test_invalid_visibility_falls_back_public():
    c = _client()
    assert c.post("/api/channels", json={"name": "weird", "visibility": "banana"}).json()["visibility"] == "public"

def run():
    t = [v for k, v in sorted(globals().items()) if k.startswith("test_")]; p = 0
    for f in t: f(); print("PASS", f.__name__); p += 1
    print(f"\n{p}/{len(t)} passed"); return p == len(t)
if __name__ == "__main__": sys.exit(0 if run() else 1)
