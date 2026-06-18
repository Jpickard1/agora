"""Tests for the participants registry (issue #86), against a stub shared_root."""
import os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agenthub import participants as P

def sr(): return tempfile.mkdtemp(prefix="parts-")

def test_register_and_list():
    s = sr()
    P.register_participant(s, "jpic", "probe", host="exxact07", now=1000.0)
    P.register_participant(s, "jpic", "manager", host="exxact07", now=1000.0)
    P.register_participant(s, "zhang", "zbot", host="exxact07", now=1000.0)
    parts = P.list_participants(s, online_window=30.0, now=1000.0)
    users = {p["user"]: p for p in parts}
    assert set(users) == {"jpic", "zhang"}
    assert {a["name"] for a in users["jpic"]["agents"]} == {"probe", "manager"}
    assert users["jpic"]["online_agents"] == 2 and users["jpic"]["online"]

def test_per_agent_files_no_clobber():
    # two agents of the same user register "concurrently" -> both survive
    s = sr()
    P.register_participant(s, "jpic", "a1", now=1.0)
    P.register_participant(s, "jpic", "a2", now=1.0)
    p = P.list_participants(s, now=1.0)[0]
    assert {a["name"] for a in p["agents"]} == {"a1", "a2"}

def test_online_window():
    s = sr()
    P.register_participant(s, "jpic", "old", now=1000.0)
    parts = P.list_participants(s, online_window=30.0, now=1000.0 + 999)
    assert parts[0]["agents"][0]["online"] is False
    assert parts[0]["online"] is False

def test_lookup_user():
    s = sr()
    P.register_participant(s, "jpic", "probe", now=1.0)
    P.register_participant(s, "zhang", "zbot", now=1.0)
    assert P.lookup_user(s, "probe") == "jpic"
    assert P.lookup_user(s, "zbot") == "zhang"
    assert P.lookup_user(s, "ghost") is None

def test_heartbeat_updates_last_seen():
    s = sr()
    P.register_participant(s, "jpic", "probe", now=1.0)
    P.register_participant(s, "jpic", "probe", now=500.0)   # heartbeat
    a = P.list_participants(s, now=500.0)[0]["agents"][0]
    assert a["last_seen"] == 500.0

def test_empty():
    assert P.list_participants(sr()) == []

def run():
    t=[v for k,v in sorted(globals().items()) if k.startswith("test_")]; p=0
    for f in t:
        f(); print("PASS", f.__name__); p+=1
    print(f"\n{p}/{len(t)} passed"); return p==len(t)
if __name__ == "__main__": sys.exit(0 if run() else 1)
