"""Tests for the research pipeline (issue #24): gather (search-key vs provided
URLs), extractive synthesis with citations, the sourced report, reproducibility,
and graceful degradation. No real network/LLM — fetcher/searcher/synthesize are
all injected.
Run: python tests/test_research.py"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub import research as R  # noqa: E402

DOCS = {
    "http://a": {"final_url": "http://a", "title": "Photosynthesis",
                 "text": "Photosynthesis converts light energy into chemical "
                         "energy. Chlorophyll absorbs sunlight. Oxygen is "
                         "released as a byproduct of photosynthesis."},
    "http://b": {"final_url": "http://b", "title": "Light reactions",
                 "text": "The light reactions occur in the thylakoid membrane. "
                         "Water is split to supply electrons during "
                         "photosynthesis."},
}


def fake_fetch(url, timeout=12.0):
    d = DOCS.get(url)
    return dict(d) if d else {"error": "404 not found"}


def fake_search(query, key=None, limit=5):
    return {"configured": True, "query": query,
            "results": [{"title": "Photosynthesis", "url": "http://a", "snippet": "x"}]}


Q = "How does photosynthesis convert light energy?"


# -- gather ----------------------------------------------------------------

def test_gather_uses_provided_urls_without_key():
    sources, meta = R.gather_sources(Q, urls=["http://a", "http://b"],
                                     fetcher=fake_fetch)
    assert meta["search_used"] is False
    assert [s["url"] for s in sources] == ["http://a", "http://b"]
    assert any("no search key" in n for n in meta["notes"])


def test_gather_uses_search_when_key_present():
    sources, meta = R.gather_sources(Q, search_key="k", fetcher=fake_fetch,
                                     searcher=fake_search)
    assert meta["search_used"] is True
    assert sources[0]["url"] == "http://a"


def test_gather_dedupes_and_caps():
    sources, _ = R.gather_sources(Q, urls=["http://a", "http://a", "http://b"],
                                  max_sources=1, fetcher=fake_fetch)
    assert len(sources) == 1


def test_gather_records_fetch_failure():
    sources, meta = R.gather_sources(Q, urls=["http://missing"], fetcher=fake_fetch)
    assert sources[0]["ok"] is False
    assert any("fetch failed" in n for n in meta["notes"])


# -- synthesis -------------------------------------------------------------

def test_extractive_synthesis_cites_sources():
    sources, _ = R.gather_sources(Q, urls=["http://a", "http://b"], fetcher=fake_fetch)
    syn = R.extractive_synthesis(Q, sources)
    assert syn["method"] == "extractive"
    assert syn["findings"]
    assert all("source_n" in f and f["text"] for f in syn["findings"])
    # findings are single-line (whitespace collapsed)
    assert all("\n" not in f["text"] for f in syn["findings"])


def test_synthesis_empty_when_no_sources():
    syn = R.extractive_synthesis(Q, [])
    assert syn["findings"] == []
    assert "No directly relevant" in syn["summary"]


# -- report ----------------------------------------------------------------

def test_report_has_sections_and_citations():
    out = R.research(Q, urls=["http://a", "http://b"], fetcher=fake_fetch, now=1.0)
    md = out["report_md"]
    for h in ("# Research:", "## Summary", "## Findings", "## Sources", "## Caveats"):
        assert h in md, h
    assert "[1]" in md or "[2]" in md
    assert "http://a" in md
    assert "extracted verbatim" in md   # the no-LLM caveat


def test_report_lists_failed_sources_in_caveats():
    out = R.research(Q, urls=["http://missing"], fetcher=fake_fetch, now=1.0)
    assert "fetch failed" in out["report_md"]
    assert "No sources were successfully fetched" in out["report_md"]


# -- reproducibility + degradation -----------------------------------------

def test_pipeline_is_reproducible():
    a = R.research(Q, urls=["http://a", "http://b"], fetcher=fake_fetch, now=1.0)
    b = R.research(Q, urls=["http://a", "http://b"], fetcher=fake_fetch, now=1.0)
    assert a["report_md"] == b["report_md"]


def test_degrades_without_search_key():
    out = R.research(Q, urls=["http://a"], search_key=None, fetcher=fake_fetch)
    assert out["search_used"] is False
    assert out["report_md"]                 # still produces a report


def test_pluggable_synthesizer_is_used():
    def fake_llm(question, sources):
        return {"summary": "LLM SAYS HI", "findings":
                [{"text": "synth finding", "source_n": 1}], "method": "llm"}
    out = R.research(Q, urls=["http://a"], fetcher=fake_fetch,
                     synthesize=fake_llm, now=1.0)
    assert "LLM SAYS HI" in out["report_md"]
    assert "method: llm" in out["report_md"]


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
