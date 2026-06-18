"""Tests for the alert flag (issue #17) — meta.alert carried through the store
(and therefore through SSE + the UI). Run: python tests/test_alert.py"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub.store import HubStore  # noqa: E402


def fresh():
    s = HubStore(tempfile.mkdtemp(prefix="alert-"))
    s.init(token="t")
    return s


def test_alert_flag_roundtrips():
    s = fresh()
    s.post_channel("general", "evacuate now", "probe", "probe",
                   meta={"alert": True})
    msgs = s.read_channel("general")
    assert len(msgs) == 1
    assert (msgs[0].get("meta") or {}).get("alert") is True


def test_normal_message_is_not_an_alert():
    s = fresh()
    s.post_channel("general", "just chatting", "probe", "probe")
    m = s.read_channel("general")[0]
    assert not (m.get("meta") or {}).get("alert")


def test_alert_carried_into_firehose():
    s = fresh()
    s.post_channel("general", "must read", "probe", "probe", meta={"alert": True})
    fh = s.firehose()
    assert any((m.get("meta") or {}).get("alert") for m in fh)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1; print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
