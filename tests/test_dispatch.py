"""Tests for the manager dispatch helpers: issue-form parsing + capability routing."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub import dispatch


FORM_BODY = """\
### Summary

Add retry logic to the data loader

### Capability needed

Data

### Priority

High

### Details

The loader dies on transient network errors. Add bounded retries.

### Acceptance criteria

Retries 3x with backoff; a test covers the failure path.

### Links / references

_No response_

### Ready to dispatch

- [x] This task is ready for an agent to pick up now.
"""


def test_parse_extracts_all_fields():
    p = dispatch.parse_issue_form(FORM_BODY)
    assert p["summary"] == "Add retry logic to the data loader"
    assert p["capability"] == "data"          # lower-cased
    assert p["priority"] == "high"
    assert "transient network errors" in p["details"]
    assert "backoff" in p["acceptance"]
    assert p["links"] == ""                    # _No response_ -> empty


def test_parse_missing_sections_are_blank():
    p = dispatch.parse_issue_form("### Summary\n\njust a summary")
    assert p["summary"] == "just a summary"
    assert p["capability"] == "" and p["priority"] == "" and p["details"] == ""


def test_parse_empty_body():
    p = dispatch.parse_issue_form("")
    assert all(v == "" for v in p.values())


def test_parse_accepts_alternate_labels():
    body = "### Capability / skill needed\n\ngpu\n\n### Definition of done\n\nworks"
    p = dispatch.parse_issue_form(body)
    assert p["capability"] == "gpu"
    assert p["acceptance"] == "works"


# -- routing ---------------------------------------------------------------

AGENTS = [
    {"id": "manager", "capabilities": ["claude-code"], "online": True, "kind": "agent"},
    {"id": "worker1", "capabilities": ["claude-code", "data"], "online": True, "kind": "agent"},
    {"id": "gpubox", "capabilities": ["gpu"], "online": False, "kind": "agent"},
    {"id": "jpic", "capabilities": [], "online": True, "kind": "human"},
]


def test_route_matches_capability():
    assert dispatch.route_by_capability("data", AGENTS) == "worker1"


def test_route_skips_offline():
    # only gpubox has gpu, but it's offline -> no match
    assert dispatch.route_by_capability("gpu", AGENTS) is None
    # unless we allow offline
    assert dispatch.route_by_capability("gpu", AGENTS, online_only=False) == "gpubox"


def test_route_general_matches_any_worker_not_human():
    # 'general' matches the first eligible non-human agent (manager first here)
    assert dispatch.route_by_capability("general", AGENTS) == "manager"
    # excluding the manager, it falls to worker1
    assert dispatch.route_by_capability("", AGENTS, exclude={"manager"}) == "worker1"


def test_route_never_returns_human():
    only_human = [{"id": "jpic", "capabilities": [], "online": True, "kind": "human"}]
    assert dispatch.route_by_capability("general", only_human) is None


def test_route_no_match_returns_none():
    assert dispatch.route_by_capability("welding", AGENTS) is None


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
