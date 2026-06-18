"""Agent web access (issue #19) — no credentials required to be useful.

Two capabilities, both designed to be unit-testable without real network calls
(the side-effecting steps take an injectable opener/fetcher):

  fetch_url(url)   -- GET a URL with stdlib urllib, enforce a timeout + size cap,
                      and return readable text (basic HTML -> text). Works out of
                      the box, no keys.
  search(query)    -- pluggable search. Uses an API key from AGORA_SEARCH_KEY (and
                      AGORA_SEARCH_BACKEND, default 'brave'); if no key is set it
                      returns a clear 'configure a search backend' message instead
                      of hard-failing, so the command is always safe to call.
"""

from __future__ import annotations

import json
import http.client
import ipaddress
import os
import socket
import urllib.parse
import urllib.request
from html.parser import HTMLParser

DEFAULT_TIMEOUT = 12.0
DEFAULT_MAX_BYTES = 2_000_000          # 2 MB read cap
USER_AGENT = "agora-hub/1.0 (+https://github.com/Jpickard1/agora)"

# Block non-web schemes so 'fetch' can't be pointed at file://, ftp://, etc.
ALLOWED_SCHEMES = ("http", "https")


# -- SSRF guard (issue #44) ------------------------------------------------
# Prevent agent web access from reaching internal infrastructure: resolve the
# host and refuse private / loopback / link-local / reserved ranges (incl. the
# 169.254.169.254 cloud-metadata endpoint). To stop DNS-rebinding (TOCTOU), we
# resolve once, validate every returned address, then pin the actual connection
# to a validated IP. Overridable for trusted intranets via allow_private (or
# AGORA_FETCH_ALLOW_PRIVATE=1).

class SSRFBlocked(Exception):
    """Raised when a URL resolves to a non-public address."""


def _allow_private_default():
    return os.environ.get("AGORA_FETCH_ALLOW_PRIVATE", "").lower() in ("1", "true", "yes")


def ip_is_public(ip_str):
    """True if `ip_str` is a routable public address (not private/loopback/
    link-local/reserved/multicast/unspecified)."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _default_resolver(host):
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})


def validate_host(host, allow_private=False, resolver=None):
    """Resolve `host` and return its IPs if ALL are public; raise SSRFBlocked
    otherwise. A literal-IP host is checked directly (no DNS). `resolver` is
    injectable for tests."""
    if not host:
        raise SSRFBlocked("missing host")
    resolver = resolver or _default_resolver
    try:
        ipaddress.ip_address(host)           # literal IP?
        ips = [host]
    except ValueError:
        try:
            ips = resolver(host)
        except Exception as e:
            raise SSRFBlocked(f"could not resolve host '{host}': {e}")
    if not ips:
        raise SSRFBlocked(f"no addresses resolved for host '{host}'")
    if allow_private:
        return ips
    for ip in ips:
        if not ip_is_public(ip):
            raise SSRFBlocked(f"refusing non-public address {ip} for host '{host}'")
    return ips


def _pinned_opener(pinned_ip):
    """An opener that dials `pinned_ip` while keeping the original Host/SNI, so a
    second DNS lookup can't rebind us to an internal address."""
    class _HTTPConn(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.create_connection((pinned_ip, self.port),
                                                 self.timeout)

    class _HTTPSConn(http.client.HTTPSConnection):
        def connect(self):
            sock = socket.create_connection((pinned_ip, self.port), self.timeout)
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)

    class _HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(_HTTPConn, req)

    class _HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(_HTTPSConn, req)

    return urllib.request.build_opener(_HTTPHandler, _HTTPSHandler).open


# -- HTML -> text ----------------------------------------------------------

_SKIP_TAGS = {"script", "style", "noscript", "head", "template", "svg"}
_BLOCK_TAGS = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5",
               "h6", "section", "article", "header", "footer", "ul", "ol",
               "table", "blockquote", "pre"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts = []
        self._skip_depth = 0
        self._in_title = False
        self.title = ""

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        # capture <title> even though it lives inside the skipped <head>
        if self._in_title:
            self.title += data
        if self._skip_depth:
            return
        if data.strip():
            self._parts.append(data)

    def text(self):
        raw = "".join(self._parts)
        # collapse runs of blank lines / trailing spaces into tidy paragraphs
        lines = [ln.strip() for ln in raw.splitlines()]
        out, blank = [], False
        for ln in lines:
            if ln:
                out.append(ln)
                blank = False
            elif not blank:
                out.append("")
                blank = True
        return "\n".join(out).strip()


def html_to_text(html: str):
    """Return (title, readable_text) from an HTML document."""
    p = _TextExtractor()
    try:
        p.feed(html)
    except Exception:
        pass
    return p.title.strip(), p.text()


# -- fetch -----------------------------------------------------------------

def fetch_url(url, timeout=DEFAULT_TIMEOUT, max_bytes=DEFAULT_MAX_BYTES,
              urlopen=None, allow_private=None, resolver=None):
    """Fetch `url` and return a dict with readable text. Network access goes
    through `urlopen` (defaults to urllib) so tests can inject a fake.

    SSRF guard (issue #44): unless a fake `urlopen` is injected, the host is
    resolved and refused if it maps to a private/loopback/link-local/reserved
    address; the connection is then pinned to a validated IP to defeat DNS
    rebinding. Set allow_private=True (or AGORA_FETCH_ALLOW_PRIVATE=1) for
    trusted intranets.

    Returns: {url, final_url, status, content_type, title, text, bytes,
              truncated} or {url, error} on failure."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        return {"url": url, "error": f"unsupported scheme '{parsed.scheme}' "
                f"(only {'/'.join(ALLOWED_SCHEMES)} allowed)"}

    if allow_private is None:
        allow_private = _allow_private_default()

    # Guard only the real network path. When a fake `urlopen` is injected (tests)
    # there is no real socket, so validation is unnecessary — but if a `resolver`
    # is supplied we still validate, so the guard itself stays testable.
    if urlopen is None or resolver is not None:
        try:
            ips = validate_host(parsed.hostname, allow_private=allow_private,
                                resolver=resolver)
        except SSRFBlocked as e:
            return {"url": url, "error": f"blocked (SSRF guard): {e}"}
        if urlopen is None and not allow_private:
            # pin the connection to a validated public IP (anti-rebinding)
            urlopen = _pinned_opener(ips[0])

    if urlopen is None:
        urlopen = urllib.request.urlopen
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        resp = urlopen(req, timeout=timeout)
    except Exception as e:
        return {"url": url, "error": f"{type(e).__name__}: {e}"}

    raw = resp.read(max_bytes + 1)
    truncated = len(raw) > max_bytes
    raw = raw[:max_bytes]

    headers = getattr(resp, "headers", None)
    ctype = headers.get_content_type() if headers else "text/plain"
    charset = (headers.get_content_charset() if headers else None) or "utf-8"
    body = raw.decode(charset, errors="replace")

    if "html" in ctype:
        title, text = html_to_text(body)
    else:
        title, text = "", body

    return {
        "url": url,
        "final_url": getattr(resp, "url", None) or (resp.geturl() if hasattr(resp, "geturl") else url),
        "status": getattr(resp, "status", None),
        "content_type": ctype,
        "title": title,
        "text": text,
        "bytes": len(raw),
        "truncated": truncated,
    }


# -- search (pluggable, key-optional) --------------------------------------

def _brave_request(query, key, limit):
    qs = urllib.parse.urlencode({"q": query, "count": limit})
    req = urllib.request.Request(
        "https://api.search.brave.com/res/v1/web/search?" + qs,
        headers={"X-Subscription-Token": key, "Accept": "application/json",
                 "User-Agent": USER_AGENT})
    return req


def _brave_parse(data, limit):
    out = []
    for r in (data.get("web", {}) or {}).get("results", [])[:limit]:
        out.append({"title": r.get("title", ""), "url": r.get("url", ""),
                    "snippet": r.get("description", "")})
    return out


def _tavily_request(query, key, limit):
    body = json.dumps({"api_key": key, "query": query,
                       "max_results": limit}).encode()
    req = urllib.request.Request(
        "https://api.tavily.com/search", data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    return req


def _tavily_parse(data, limit):
    out = []
    for r in (data.get("results") or [])[:limit]:
        out.append({"title": r.get("title", ""), "url": r.get("url", ""),
                    "snippet": r.get("content", "")})
    return out


SEARCH_BACKENDS = {
    "brave": (_brave_request, _brave_parse),
    "tavily": (_tavily_request, _tavily_parse),
}


def search(query, key=None, backend=None, limit=5, timeout=DEFAULT_TIMEOUT,
           urlopen=None):
    """Search the web via a pluggable, key-gated backend.

    No key (AGORA_SEARCH_KEY unset) -> a friendly 'not configured' result rather
    than an error, so calling it is always safe. `urlopen` is injectable for
    tests."""
    key = key or os.environ.get("AGORA_SEARCH_KEY")
    backend = (backend or os.environ.get("AGORA_SEARCH_BACKEND") or "brave").lower()

    if not key:
        return {
            "configured": False,
            "query": query,
            "results": [],
            "message": (
                "Web search isn't configured. Set AGORA_SEARCH_KEY (and optionally "
                "AGORA_SEARCH_BACKEND=" + "|".join(sorted(SEARCH_BACKENDS)) +
                ", default brave) to enable it. 'hubcli web fetch <url>' works "
                "without any key."),
        }
    if backend not in SEARCH_BACKENDS:
        return {"configured": False, "query": query, "results": [],
                "message": f"Unknown search backend '{backend}'. "
                           f"Choose one of: {', '.join(sorted(SEARCH_BACKENDS))}."}

    build, parse = SEARCH_BACKENDS[backend]
    if urlopen is None:
        urlopen = urllib.request.urlopen
    try:
        resp = urlopen(build(query, key, limit), timeout=timeout)
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
        results = parse(data, limit)
    except Exception as e:
        return {"configured": True, "query": query, "results": [],
                "backend": backend, "error": f"{type(e).__name__}: {e}"}
    return {"configured": True, "query": query, "backend": backend,
            "results": results}
