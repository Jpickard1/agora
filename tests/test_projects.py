"""Tests for the Projects abstraction (issue #22): create/get/list, attaching
tasks/channels/milestones, and the task-status progress rollup.
Run: python tests/test_projects.py"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub.store import HubStore  # noqa: E402


def fresh():
    s = HubStore(tempfile.mkdtemp(prefix="proj-"))
    s.init(token="t")
    return s


def test_new_is_idempotent():
    s = fresh()
    a = s.project_new("launch", name="Launch", goal="ship", owner="probe")
    b = s.project_new("launch", name="Different")   # must NOT clobber
    assert a["id"] == "launch"
    assert b["name"] == "Launch"
    assert len(s.project_list()) == 1


def test_get_unknown_is_none():
    s = fresh()
    assert s.project_get("nope") is None


def test_add_task_channel_milestone():
    s = fresh()
    s.project_new("p")
    s.create_task("t1")
    s.project_add_task("p", "t1")
    s.project_add_channel("p", "general")
    s.project_add_milestone("p", "MVP")
    p = s.project_get("p", rollup=False)
    assert p["task_ids"] == ["t1"]
    assert p["channels"] == ["general"]
    assert p["milestones"] == [{"name": "MVP", "done": False}]


def test_attachments_are_deduped():
    s = fresh()
    s.project_new("p")
    s.create_task("t1")
    s.project_add_task("p", "t1")
    s.project_add_task("p", "t1")
    s.project_add_milestone("p", "MVP")
    s.project_add_milestone("p", "MVP")
    p = s.project_get("p", rollup=False)
    assert p["task_ids"] == ["t1"]
    assert len(p["milestones"]) == 1


def test_set_milestone_done():
    s = fresh()
    s.project_new("p")
    s.project_add_milestone("p", "MVP")
    s.project_set_milestone("p", "MVP", True)
    p = s.project_get("p", rollup=False)
    assert p["milestones"][0]["done"] is True


def test_progress_rollup_from_task_status():
    s = fresh()
    s.project_new("p")
    for tid, status in [("t1", "done"), ("t2", "done"), ("t3", "running"), ("t4", None)]:
        s.create_task(tid)
        if status:
            s.claim_task(tid, "probe")
            s.update_task(tid, status, by="probe")
        s.project_add_task("p", tid)
    pr = s.project_get("p")["progress"]
    assert pr["total_tasks"] == 4
    assert pr["done"] == 2
    assert pr["percent"] == 50
    assert pr["by_status"]["done"] == 2
    assert pr["by_status"]["running"] == 1


def test_progress_counts_milestones():
    s = fresh()
    s.project_new("p")
    s.project_add_milestone("p", "a")
    s.project_add_milestone("p", "b")
    s.project_set_milestone("p", "a", True)
    pr = s.project_get("p")["progress"]
    assert pr["milestones_total"] == 2
    assert pr["milestones_done"] == 1


def test_progress_empty_is_zero_not_error():
    s = fresh()
    s.project_new("p")
    pr = s.project_get("p")["progress"]
    assert pr["total_tasks"] == 0 and pr["percent"] == 0


def test_missing_task_counts_as_unknown():
    s = fresh()
    s.project_new("p")
    s.project_add_task("p", "ghost")        # never created
    pr = s.project_get("p")["progress"]
    assert pr["by_status"].get("unknown") == 1


def test_list_newest_first_and_delete():
    s = fresh()
    s.project_new("a")
    s.project_new("b")
    ids = [p["id"] for p in s.project_list()]
    assert ids[0] == "b"                     # most-recently-created/updated first
    assert s.project_delete("a") is True
    assert [p["id"] for p in s.project_list()] == ["b"]
    assert s.project_delete("missing") is False


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
