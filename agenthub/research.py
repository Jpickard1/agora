"""Research agent pipeline (issue #24) — gather → analyze → sourced report.

Given a question, this:
  1. GATHERS evidence — web search (if AGORA_SEARCH_KEY is set) and/or a list of
     URLs you provide, each fetched to readable text.
  2. ANALYZES — synthesizes the evidence into findings. Synthesis is PLUGGABLE
     via an injected callable so a live agent can wire in an LLM; the built-in
     default is a deterministic, citation-preserving *extractive* summarizer that
     needs no model and no credentials.
  3. REPORTS — renders a sourced Markdown findings report (numbered citations +
     caveats) for saving to the KB (#25) and/or posting to a channel.

Everything is reproducible (deterministic default) and degrades gracefully with
no search key. All network + model steps are injectable, so tests need neither.
"""

from __future__ import annotations

import re
import time

from . import web_access


# -- gather ----------------------------------------------------------------

def gather_sources(question, urls=None, search_key=None, max_sources=5,
                   fetcher=None, searcher=None, timeout=12.0):
    """Collect readable sources for `question`.

    Strategy: if a search key is available, search and take the top result URLs;
    always include any explicitly-provided `urls`. Each URL is fetched to text.
    Returns (sources, notes) where sources is a list of
    {n, url, title, text, ok} and notes records how gathering went."""
    fetcher = fetcher or web_access.fetch_url
    searcher = searcher or web_access.search
    notes = []

    candidate_urls = list(urls or [])
    search_used = False
    if search_key:
        res = searcher(question, key=search_key, limit=max_sources)
        if res.get("configured") and res.get("results"):
            search_used = True
            for r in res["results"]:
                if r.get("url") and r["url"] not in candidate_urls:
                    candidate_urls.append(r["url"])
        elif res.get("error"):
            notes.append(f"search error: {res['error']}")
        elif not res.get("configured"):
            notes.append("search not configured")
    if not search_key:
        notes.append("no search key — using provided --url sources only")

    # de-dupe, cap
    seen, ordered = set(), []
    for u in candidate_urls:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    ordered = ordered[:max_sources]

    sources = []
    n = 0
    for u in ordered:
        r = fetcher(u, timeout=timeout)
        n += 1
        if "error" in r:
            notes.append(f"fetch failed [{n}] {u}: {r['error']}")
            sources.append({"n": n, "url": u, "title": "", "text": "",
                            "ok": False, "error": r["error"]})
        else:
            sources.append({"n": n, "url": r.get("final_url") or u,
                            "title": r.get("title", ""), "text": r.get("text", ""),
                            "ok": True})
    return sources, {"search_used": search_used, "notes": notes}


# -- analyze (default: deterministic extractive synthesis) ------------------

_STOP = set("the a an and or of to in on for with is are was were be been being "
            "this that these those it its as at by from how what why when which "
            "who whom does do did can could should would will may might into "
            "about over under between can't cannot you your we our they their".split())
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _terms(question):
    words = re.findall(r"[a-z0-9][a-z0-9\-']+", question.lower())
    return [w for w in words if w not in _STOP and len(w) > 2]


def extractive_synthesis(question, sources, max_findings=8):
    """Deterministic, no-model synthesis: rank sentences across sources by
    question-term overlap and return findings, each citing its source number.
    Returns {summary, findings:[{text, source_n}], method}."""
    terms = _terms(question)
    scored = []
    for s in sources:
        if not s.get("ok") or not s.get("text"):
            continue
        for sent in _SENT_SPLIT.split(s["text"]):
            sent = sent.strip()
            if len(sent) < 40 or len(sent) > 400:
                continue
            low = sent.lower()
            score = sum(1 for t in terms if t in low)
            if score:
                # stable tiebreak: higher score, then earlier/shorter
                scored.append((score, -len(sent), s["n"], sent))
    # deterministic order
    scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    findings, seen = [], set()
    for score, _, n, sent in scored:
        key = sent.lower()
        if key in seen:
            continue
        seen.add(key)
        findings.append({"text": sent, "source_n": n})
        if len(findings) >= max_findings:
            break
    summary = (findings[0]["text"] if findings
               else "No directly relevant statements were extracted from the "
                    "gathered sources.")
    return {"summary": summary, "findings": findings, "method": "extractive"}


# -- report ----------------------------------------------------------------

def _fmt_ts(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def render_report(question, sources, synthesis, gather_meta, now=None):
    """Render a sourced Markdown findings report with citations + caveats."""
    now = time.time() if now is None else now
    ok_sources = [s for s in sources if s.get("ok")]
    lines = []
    lines.append(f"# Research: {question}")
    lines.append("")
    lines.append(f"_Generated {_fmt_ts(now)} · method: {synthesis.get('method')} · "
                 f"{len(ok_sources)}/{len(sources)} sources fetched_")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append(synthesis.get("summary", ""))
    lines.append("")

    lines.append("## Findings")
    lines.append("")
    if synthesis.get("findings"):
        for f in synthesis["findings"]:
            lines.append(f"- {f['text']} [{f['source_n']}]")
    else:
        lines.append("_No findings extracted._")
    lines.append("")

    lines.append("## Sources")
    lines.append("")
    if sources:
        for s in sources:
            label = s.get("title") or s["url"]
            status = "" if s.get("ok") else "  ⚠️ (fetch failed)"
            lines.append(f"{s['n']}. [{label}]({s['url']}){status}")
    else:
        lines.append("_No sources gathered._")
    lines.append("")

    lines.append("## Caveats")
    lines.append("")
    caveats = list(gather_meta.get("notes", []))
    if synthesis.get("method") == "extractive":
        caveats.append("Findings are extracted verbatim from sources (no LLM "
                       "synthesis); verify claims against the cited sources.")
    if not ok_sources:
        caveats.append("No sources were successfully fetched — findings are empty.")
    if not caveats:
        caveats.append("None.")
    for c in caveats:
        lines.append(f"- {c}")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# -- orchestration ---------------------------------------------------------

def research(question, urls=None, search_key=None, max_sources=5,
             fetcher=None, searcher=None, synthesize=None, timeout=12.0,
             now=None):
    """Run the full pipeline and return a dict with the report + structured data.
    `synthesize(question, sources)` is injectable (default: extractive)."""
    synthesize = synthesize or extractive_synthesis
    sources, gmeta = gather_sources(
        question, urls=urls, search_key=search_key, max_sources=max_sources,
        fetcher=fetcher, searcher=searcher, timeout=timeout)
    synthesis = synthesize(question, sources)
    report_md = render_report(question, sources, synthesis, gmeta, now=now)
    return {
        "question": question,
        "sources": sources,
        "synthesis": synthesis,
        "report_md": report_md,
        "search_used": gmeta.get("search_used", False),
        "notes": gmeta.get("notes", []),
    }
