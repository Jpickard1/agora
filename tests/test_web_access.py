"""Tests for agent web access (issue #19): HTML->text, fetch with a mocked
network opener (timeout/size-cap/scheme guard), and the key-optional pluggable
search. No real network calls — every test injects a fake opener.
Run: python tests/test_web_access.py"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub import web_access as web  # noqa: E402


class FakeHeaders:
    def __init__(self, ctype="text/html", charset="utf-8"):
        self._ctype, self._charset = ctype, charset

    def get_content_type(self):
        return self._ctype

    def get_content_charset(self):
        return self._charset


class FakeResp:
    def __init__(self, body, status=200, ctype="text/html", charset="utf-8",
                 url="http://example.com/final"):
        self._body = body if isinstance(body, bytes) else body.encode()
        self.status = status
        self.url = url
        self.headers = FakeHeaders(ctype, charset)

    def read(self, n=-1):
        return self._body if n is None or n < 0 else self._body[:n]


def opener_returning(resp):
    def _open(req, timeout=None):
        return resp
    return _open


def opener_raising(exc):
    def _open(req, timeout=None):
        raise exc
    return _open


# -- html_to_text ----------------------------------------------------------

def test_html_to_text_extracts_title_and_strips_scripts():
    title, text = web.html_to_text(
        "<html><head><title>My Page</title><style>a{}</style></head>"
        "<body><h1>Heading</h1><p>Hello.</p><script>evil()</script>"
        "<p>World.</p></body></html>")
    assert title == "My Page"
    assert "Heading" in text and "Hello." in text and "World." in text
    assert "evil()" not in text and "a{}" not in text


def test_html_to_text_collapses_blank_lines():
    _, text = web.html_to_text("<p>a</p><p></p><p></p><p>b</p>")
    assert "\n\n\n" not in text


# -- fetch_url -------------------------------------------------------------

def test_fetch_html_returns_readable_text():
    resp = FakeResp("<title>T</title><p>Body text here.</p>")
    r = web.fetch_url("http://example.com", urlopen=opener_returning(resp))
    assert r["status"] == 200
    assert r["title"] == "T"
    assert "Body text here." in r["text"]
    assert r["content_type"] == "text/html"


def test_fetch_plain_text_passthrough():
    resp = FakeResp("just text", ctype="text/plain")
    r = web.fetch_url("http://x", urlopen=opener_returning(resp))
    assert r["text"] == "just text"
    assert r["title"] == ""


def test_fetch_enforces_size_cap_and_flags_truncated():
    resp = FakeResp("x" * 100, ctype="text/plain")
    r = web.fetch_url("http://x", max_bytes=10, urlopen=opener_returning(resp))
    assert r["truncated"] is True
    assert r["bytes"] == 10


def test_fetch_rejects_non_http_scheme():
    r = web.fetch_url("file:///etc/passwd", urlopen=opener_returning(FakeResp("x")))
    assert "error" in r and "scheme" in r["error"]


def test_fetch_handles_network_error_gracefully():
    r = web.fetch_url("http://x", urlopen=opener_raising(OSError("boom")))
    assert "error" in r and "boom" in r["error"]


# -- search ----------------------------------------------------------------

def test_search_without_key_is_friendly_not_error():
    r = web.search("cats", key=None)
    assert r["configured"] is False
    assert "AGORA_SEARCH_KEY" in r["message"]
    assert r["results"] == []


def test_search_unknown_backend():
    r = web.search("cats", key="k", backend="nope")
    assert r["configured"] is False
    assert "Unknown search backend" in r["message"]


def test_search_brave_parses_results():
    payload = {"web": {"results": [
        {"title": "A", "url": "http://a", "description": "da"},
        {"title": "B", "url": "http://b", "description": "db"},
    ]}}
    resp = FakeResp(json.dumps(payload), ctype="application/json")
    r = web.search("cats", key="k", backend="brave", urlopen=opener_returning(resp))
    assert r["configured"] and r["backend"] == "brave"
    assert [x["title"] for x in r["results"]] == ["A", "B"]
    assert r["results"][0]["snippet"] == "da"


def test_search_tavily_parses_results():
    payload = {"results": [{"title": "T", "url": "http://t", "content": "ct"}]}
    resp = FakeResp(json.dumps(payload), ctype="application/json")
    r = web.search("cats", key="k", backend="tavily", urlopen=opener_returning(resp))
    assert r["results"][0]["title"] == "T"
    assert r["results"][0]["snippet"] == "ct"


def test_search_respects_limit():
    payload = {"web": {"results": [{"title": str(i), "url": "u", "description": ""}
                                   for i in range(10)]}}
    resp = FakeResp(json.dumps(payload), ctype="application/json")
    r = web.search("x", key="k", backend="brave", limit=3,
                   urlopen=opener_returning(resp))
    assert len(r["results"]) == 3


def test_search_network_error_reported():
    r = web.search("cats", key="k", backend="brave",
                   urlopen=opener_raising(OSError("down")))
    assert r["configured"] is True and "down" in r["error"]


def test_search_env_key_enables(monkeypatch=None):
    os.environ["AGORA_SEARCH_KEY"] = "envkey"
    try:
        resp = FakeResp(json.dumps({"web": {"results": []}}), ctype="application/json")
        r = web.search("cats", urlopen=opener_returning(resp))
        assert r["configured"] is True
    finally:
        del os.environ["AGORA_SEARCH_KEY"]


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
