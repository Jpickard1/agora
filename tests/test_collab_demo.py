"""Test the collaborative multi-agent demo (#23): a hermetic run must produce a
working artifact + a transcript/report, with the multi-agent collaboration markers
present (atomic claims, a cross-review round, reactions). Temp dirs only — no live
hub, no network, ~/.agent-hub-path untouched."""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_spec = importlib.util.spec_from_file_location(
    "collab_demo", os.path.join(ROOT, "examples", "collab_demo", "demo.py"))
demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo)


def _run():
    base = tempfile.mkdtemp(prefix="collab-test-")
    return demo.run_demo(os.path.join(base, "out"),
                         hub_root=os.path.join(base, "hub"))


def test_demo_ships_working_artifact():
    res = _run()
    assert res["artifact_ok"] is True
    dist = Path(res["dist"])
    for f in ("index.html", "style.css", "app.js"):
        assert (dist / f).exists(), f"missing {f}"
    index = (dist / "index.html").read_text()
    assert 'href="style.css"' in index and 'src="app.js"' in index   # app composes
    app = (dist / "app.js").read_text()
    assert "function addItem" in app and "function render" in app     # it works


def test_all_tasks_completed_by_agents():
    res = _run()
    assert res["all_tasks_done"] is True
    owners = {t["id"]: t.get("claimed_by") for t in res["tasks"]}
    # the three build tasks were claimed + finished by distinct worker agents
    assert owners["demo-html"] == "ana"
    assert owners["demo-css"] == "ben"
    assert owners["demo-js"] == "cy"


def test_cross_review_round_happened():
    res = _run()
    assert res["review_round_happened"] is True
    # the requested change was actually applied to the shipped artifact
    assert "No items yet" in (Path(res["dist"]) / "app.js").read_text()


def test_report_has_transcript():
    res = _run()
    report = Path(res["report"]).read_text()
    assert "# Collaborative demo" in report
    assert "GOAL:" in report
    assert "Final task board" in report
    assert "approved" in report                 # reviewer sign-off captured
    assert "\U0001F44D×1" in report             # a real reaction count (rio's 👍, not a stray)


def test_demo_is_hermetic():
    pointer = Path("~/.agent-hub-path").expanduser()
    before = pointer.read_text() if pointer.exists() else None
    _run()
    after = pointer.read_text() if pointer.exists() else None
    assert after == before                      # live pointer untouched


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
