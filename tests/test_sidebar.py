"""Structural guards for the sidebar UX (issue #76): one scroll container (no
nested per-list scrollbars) + collapsible sections persisted to localStorage.
Headless text checks on the served assets — no browser needed.
Run: python tests/test_sidebar.py"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "agenthub", "web")


def _read(name):
    with open(os.path.join(WEB, name), encoding="utf-8") as f:
        return f.read()


def test_single_scroll_wrapper_exists():
    html, css = _read("index.html"), _read("style.css")
    assert 'class="sidebar-scroll"' in html
    assert ".sidebar-scroll" in css and "overflow-y: auto" in css


def test_lists_do_not_scroll_independently():
    # the .list rule must NOT carry its own overflow-y (that made 3 scrollbars)
    css = _read("style.css")
    for line in css.splitlines():
        if line.strip().startswith(".list {"):
            assert "overflow-y" not in line, "per-list scrollbar reintroduced"
            break
    else:
        raise AssertionError(".list rule not found")


def test_three_collapsible_sections_with_bodies():
    html = _read("index.html")
    for key in ("manage", "channels", "dm"):
        assert f'data-section="{key}"' in html, f"missing section head {key}"
        assert f'data-section-body="{key}"' in html, f"missing section body {key}"
    assert html.count("section-head collapsible") == 3
    assert 'class="chev"' in html


def test_collapse_state_persisted():
    js = _read("app.js")
    assert "agenthub.collapsed." in js          # localStorage key prefix
    assert "data-section-body" in js            # toggles the matching body
    # clicking the + (new channel) must not collapse the section
    assert 'e.target.closest("button")' in js


def test_roster_and_settings_preserved():
    html = _read("index.html")
    assert 'id="agent-list"' in html            # roster intact
    assert 'id="settings-btn"' in html          # Settings/display-name control intact
    assert 'id="lock-list"' in html             # locks panel intact


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
