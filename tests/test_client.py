"""Tests for HubClient, including the agent-to-agent request/response RPC.
Run: python tests/test_client.py   (or pytest)."""

import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub.client import HubClient  # noqa: E402


def fresh_root():
    d = tempfile.mkdtemp(prefix="hubclient-")
    HubClient(name="setup", root=d).store.init(token="t")
    return d


def test_post_and_read():
    root = fresh_root()
    a = HubClient(name="alice", root=root)
    a.register()
    a.post("general", "hi")
    assert any(m["text"] == "hi" for m in a.read("general"))


def test_broadcast_seen_by_poll_inbox():
    root = fresh_root()
    boss = HubClient(name="boss", root=root)
    worker = HubClient(name="worker", root=root)
    worker.register()
    time.sleep(0.01)
    boss.broadcast("everyone stop")
    got = worker.poll_inbox()
    assert any(m["text"] == "everyone stop" and m["to"] == "*" for m in got)


def test_request_response_roundtrip():
    root = fresh_root()
    responder = HubClient(name="responder", root=root)
    responder.register()
    asker = HubClient(name="asker", root=root)

    stop = threading.Event()

    def serve():
        # Minimal responder loop: reply "pong" to any request.
        while not stop.is_set():
            for m in responder.poll_inbox():
                if HubClient.is_request(m):
                    responder.reply(m, "pong")
            time.sleep(0.05)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        reply = asker.request(responder.id, "ping", timeout=5.0, poll=0.1)
    finally:
        stop.set()
        t.join(timeout=1)

    assert reply is not None, "no reply received"
    assert reply["text"] == "pong"
    assert reply["meta"]["msg_kind"] == "reply"
    assert reply["meta"]["in_reply_to"]  # correlation id present


def test_request_timeout():
    root = fresh_root()
    asker = HubClient(name="asker", root=root)
    reply = asker.request("nobody-home", "anyone?", timeout=0.6, poll=0.1)
    assert reply is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
