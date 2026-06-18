"""Deterministic manager dispatch helpers.

The manager turns a GitHub issue (filed via the **Agent Task** form) into a
dispatch decision: parse the issue body into structured fields, then route it to
a worker whose capabilities match. Doing this in code (instead of ad-hoc
parsing) makes the dispatch path testable and repeatable.

The Agent Task form (`.github/ISSUE_TEMPLATE/agent-task.yml`) renders an issue
body as Markdown sections — each field label becomes a ``### <label>`` heading
followed by its value:

    ### Summary

    Add retry logic to the loader

    ### Capability needed

    data
    ...

An unfilled optional field renders as ``_No response_``.
"""

from __future__ import annotations

import re

_HEADING_RE = re.compile(r"(?m)^#{2,3}\s+(.+?)\s*$")
_EMPTY_VALUES = {"_no response_", "_none_", "none", "n/a", ""}


def _clean(value: str) -> str:
    v = (value or "").strip()
    return "" if v.lower() in _EMPTY_VALUES else v


def parse_issue_form(body: str) -> dict:
    """Parse an Agent Task issue body into structured fields. Missing/empty
    sections come back as "". Tolerant of heading level (## or ###), label
    casing, and the older form's alternate labels."""
    sections: dict[str, str] = {}
    if body:
        parts = _HEADING_RE.split(body)
        # parts[0] is any preamble; then alternating (heading, content).
        it = iter(parts[1:])
        for heading, content in zip(it, it):
            sections[heading.strip().lower()] = content.strip()

    def g(*names: str) -> str:
        for n in names:
            if n in sections:
                return _clean(sections[n])
        return ""

    return {
        "summary": g("summary"),
        # accept both the current and the earlier form's label
        "capability": g("capability needed", "capability / skill needed",
                        "capability").lower(),
        "priority": g("priority").lower(),
        "details": g("details", "brief / context", "brief"),
        "acceptance": g("acceptance criteria", "definition of done"),
        "links": g("links / references", "links"),
    }


def route_by_capability(capability: str, agents: list[dict], *,
                        online_only: bool = True,
                        exclude: set[str] | None = None) -> str | None:
    """Pick a worker for a task needing `capability`. Returns the agent id, or
    None if nobody matches.

    Rules: skip humans and any id in `exclude` (e.g. the manager itself); with
    online_only, skip offline agents; a blank or 'general' capability matches any
    worker, otherwise the agent must advertise the capability. Deterministic:
    returns the first match in the given order."""
    cap = (capability or "").strip().lower()
    exclude = exclude or set()

    def eligible(a: dict) -> bool:
        if a.get("id") in exclude or a.get("kind") == "human":
            return False
        if online_only and not a.get("online"):
            return False
        if cap in ("", "general"):
            return True
        caps = [c.lower() for c in (a.get("capabilities") or [])]
        return cap in caps

    for a in agents:
        if eligible(a):
            return a.get("id")
    return None
