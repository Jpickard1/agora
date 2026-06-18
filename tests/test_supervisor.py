"""Tests for the supervisor daemon (issue #3 — close the core-tests gap).
Covers the watch-loop decision logic with everything mocked: no real tmux,
no real HTTP, no spawns, no sleeps.
Run: python tests/test_supervisor.py"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub import supervisor as S  # noqa: E402
from agenthub.store import HubStore  # noqa: E402


def fresh():
    s = HubStore(tempfile.mkdtemp(prefix="sup-"))
    s.init(token="t")
    return s


# -- _server_ok (health check, mocked HTTP) --------------------------------

def test_server_ok_true_on_200(monkeypatch=None):
    import agenthub.supervisor as mod

    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    orig = mod.urllib.request.urlopen
    mod.urllib.request.urlopen = lambda url, timeout=None: FakeResp()
    try:
        assert mod._server_ok(8910) is True
    finally:
        mod.urllib.request.urlopen = orig


def test_server_ok_false_on_exception():
    import agenthub.supervisor as mod
    orig = mod.urllib.request.urlopen

    def boom(url, timeout=None):
        raise OSError("refused")
    mod.urllib.request.urlopen = boom
    try:
        assert mod._server_ok(8910) is False
    finally:
        mod.urllib.request.urlopen = orig


# -- _agent_online (roster liveness) ---------------------------------------

def test_agent_online_true_when_recent():
    s = fresh()
    s.register_agent("manager", "manager")
    s.heartbeat("manager")
    assert S._agent_online(s, "manager") is True


def test_agent_online_false_when_missing():
    s = fresh()
    assert S._agent_online(s, "ghost") is False


def test_agent_online_false_when_stale():
    s = fresh()
    s.register_agent("manager", "manager")
    import json
    p = os.path.join(str(s.agents_dir), "manager.json")
    rec = json.load(open(p)); rec["last_seen"] = time.time() - 999; json.dump(rec, open(p, "w"))
    assert S._agent_online(s, "manager", window=30) is False


def test_agent_online_false_when_offline_status():
    s = fresh()
    s.register_agent("manager", "manager")
    s.heartbeat("manager")
    s.set_agent_status("manager", "offline")
    assert S._agent_online(s, "manager") is False


# -- should_tick (scheduling) ----------------------------------------------

def test_should_tick_after_interval():
    assert S.should_tick(now=200.0, last_tick=0.0, interval=180.0) is True


def test_should_tick_not_before_interval():
    assert S.should_tick(now=100.0, last_tick=0.0, interval=180.0) is False


def test_should_tick_exactly_at_interval():
    assert S.should_tick(now=180.0, last_tick=0.0, interval=180.0) is True


# -- ensure_server (restart-a-dead-process) --------------------------------

def test_ensure_server_no_restart_when_healthy():
    calls = []
    restarted = S.ensure_server(8910, "serve",
                                server_ok=lambda p: True,
                                tmux_start=lambda s, c: calls.append(s))
    assert restarted is False and calls == []


def test_ensure_server_restarts_when_down():
    calls = []
    restarted = S.ensure_server(8910, "serve-cmd",
                                server_ok=lambda p: False,
                                tmux_start=lambda s, c: calls.append((s, c)))
    assert restarted is True
    assert calls == [("agora-server", "serve-cmd")]


# -- ensure_manager_bridge (restart-a-dead-process) ------------------------

def test_ensure_bridge_noop_without_pane():
    calls = []
    out = S.ensure_manager_bridge(fresh(), "manager", "", "bcmd",
                                  agent_online=lambda st, m: False,
                                  tmux_start=lambda s, c: calls.append(s))
    assert out is False and calls == []


def test_ensure_bridge_noop_when_manager_online():
    calls = []
    out = S.ensure_manager_bridge(fresh(), "manager", "%3", "bcmd",
                                  agent_online=lambda st, m: True,
                                  tmux_start=lambda s, c: calls.append(s))
    assert out is False and calls == []


def test_ensure_bridge_restarts_when_offline():
    calls = []
    out = S.ensure_manager_bridge(fresh(), "manager", "%3", "bcmd",
                                  agent_online=lambda st, m: False,
                                  tmux_start=lambda s, c: calls.append((s, c)))
    assert out is True
    assert calls == [("agora-manager-bridge", "bcmd")]


# -- wedged-agent detection (#111), pure logic -----------------------------

def _agent(aid, *, online=True, liveness="responsive", queued=0,
           last_delivered_ts=0.0, kind="agent"):
    return {"id": aid, "name": aid, "online": online, "liveness": liveness,
            "kind": kind, "delivery": {"queued": queued,
                                       "last_delivered_ts": last_delivered_ts}}


def test_offline_agent_is_never_wedged():
    a = _agent("x", online=False, liveness="wedged", queued=5)
    assert S.is_wedged(a, now=1000.0, stale_after=60.0) is False


def test_busy_agent_is_never_wedged():
    # legitimately working (long turn) — must NOT be flagged
    a = _agent("x", liveness="busy", queued=3, last_delivered_ts=0.0)
    assert S.is_wedged(a, now=1000.0, stale_after=60.0) is False


def test_bridge_flagged_wedged_is_wedged():
    a = _agent("x", liveness="wedged")
    assert S.is_wedged(a, now=1000.0, stale_after=60.0) is True


def test_stale_backlog_is_wedged():
    # online, idle, has a queue that hasn't drained in a while
    a = _agent("x", liveness="idle", queued=2, last_delivered_ts=100.0)
    assert S.is_wedged(a, now=1000.0, stale_after=60.0) is True


def test_fresh_backlog_is_not_wedged():
    # queued, but a message was delivered recently → still draining → healthy
    a = _agent("x", liveness="idle", queued=2, last_delivered_ts=980.0)
    assert S.is_wedged(a, now=1000.0, stale_after=60.0) is False


def test_no_queue_is_not_wedged():
    a = _agent("x", liveness="idle", queued=0)
    assert S.is_wedged(a, now=1000.0, stale_after=60.0) is False


def test_detect_wedged_only_after_threshold():
    a = _agent("x", liveness="wedged")
    # first sighting: starts the clock, not yet flagged
    to_handle, recovered, since = S.detect_wedged(
        [a], now=100.0, threshold=60.0, handled=set(), wedged_since={}, stale_after=60.0)
    assert to_handle == [] and since == {"x": 100.0}
    # still wedged 70s later: now flagged
    to_handle, recovered, since = S.detect_wedged(
        [a], now=170.0, threshold=60.0, handled=set(), wedged_since=since, stale_after=60.0)
    assert [x["id"] for x in to_handle] == ["x"]


def test_detect_wedged_dedups():
    a = _agent("x", liveness="wedged")
    to_handle, _, since = S.detect_wedged(
        [a], now=200.0, threshold=60.0, handled={"x"}, wedged_since={"x": 100.0},
        stale_after=60.0)
    assert to_handle == []          # already handled -> no repeat alert


def test_detect_wedged_recovers_and_rearms():
    healthy = _agent("x", liveness="responsive")
    to_handle, recovered, since = S.detect_wedged(
        [healthy], now=300.0, threshold=60.0, handled={"x"},
        wedged_since={"x": 100.0}, stale_after=60.0)
    assert to_handle == [] and "x" in recovered and "x" not in since


def test_detect_wedged_vanished_agent_recovers():
    # agent we were tracking dropped off the roster entirely
    to_handle, recovered, since = S.detect_wedged(
        [], now=300.0, threshold=60.0, handled={"x"}, wedged_since={"x": 100.0},
        stale_after=60.0)
    assert "x" in recovered and "x" not in since


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
