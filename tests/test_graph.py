"""Contract test for the comm-graph data the UI view consumes (issue #5).
The web view (🕸️ Comm graph) renders store.comm_graph() / GET /api/graph; this
pins the shape it relies on: sorted nodes + directed {source,target,count}
edges, self-messages excluded.
Run: python tests/test_graph.py"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub.store import HubStore  # noqa: E402


def fresh():
    s = HubStore(tempfile.mkdtemp(prefix="graph-"))
    s.init(token="t")
    return s


def dm(s, src, dst, n=1):
    for _ in range(n):
        s.post_inbox(dst, "hi", author=src, author_name=src)


def test_empty_graph_shape():
    g = fresh().comm_graph()
    assert g == {"nodes": [], "edges": []}


def test_edges_have_counts_and_direction():
    s = fresh()
    dm(s, "alice", "bob", 3)
    dm(s, "bob", "alice", 1)
    g = s.comm_graph()
    edges = {(e["source"], e["target"]): e["count"] for e in g["edges"]}
    assert edges[("alice", "bob")] == 3
    assert edges[("bob", "alice")] == 1


def test_nodes_are_sorted_union_of_endpoints():
    s = fresh()
    dm(s, "carol", "bob")
    dm(s, "alice", "carol")
    g = s.comm_graph()
    assert g["nodes"] == ["alice", "bob", "carol"]


def test_self_messages_excluded():
    s = fresh()
    dm(s, "alice", "alice", 5)
    dm(s, "alice", "bob", 1)
    g = s.comm_graph()
    assert all(e["source"] != e["target"] for e in g["edges"])
    assert ("alice", "bob") in {(e["source"], e["target"]) for e in g["edges"]}


def test_edge_fields_present():
    s = fresh()
    dm(s, "a", "b")
    e = s.comm_graph()["edges"][0]
    assert set(e) == {"source", "target", "count"}


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
