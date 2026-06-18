"""Tests for the SSRF guard on web fetch (issue #44): public/non-public IP
classification, host validation (literal IPs + DNS via an injected resolver,
incl. the cloud-metadata address and DNS-rebinding mixes), the allow_private
override, and fetch_url's integrated block — all without real network.
Run: python tests/test_ssrf.py"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenthub import web_access as w  # noqa: E402


def res(*ips):
    def _r(host):
        return list(ips)
    return _r


# -- ip_is_public ----------------------------------------------------------

def test_public_ips():
    for ip in ("8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"):
        assert w.ip_is_public(ip) is True, ip


def test_non_public_ips_blocked():
    for ip in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.5.5",
               "169.254.169.254",          # cloud metadata
               "0.0.0.0", "224.0.0.1",     # unspecified / multicast
               "::1", "fe80::1", "fc00::1"):
        assert w.ip_is_public(ip) is False, ip


def test_garbage_is_not_public():
    assert w.ip_is_public("not-an-ip") is False


# -- validate_host ---------------------------------------------------------

def test_validate_allows_public_dns():
    assert w.validate_host("example.com", resolver=res("93.184.216.34")) == ["93.184.216.34"]


def test_validate_blocks_private_dns():
    try:
        w.validate_host("evil.test", resolver=res("10.1.2.3"))
        assert False, "should have blocked"
    except w.SSRFBlocked as e:
        assert "10.1.2.3" in str(e)


def test_validate_blocks_metadata_literal():
    try:
        w.validate_host("169.254.169.254")
        assert False
    except w.SSRFBlocked:
        pass


def test_validate_blocks_if_any_resolved_ip_is_private():
    # DNS-rebinding style: one public + one internal -> refuse outright.
    try:
        w.validate_host("rebind.test", resolver=res("8.8.8.8", "127.0.0.1"))
        assert False
    except w.SSRFBlocked:
        pass


def test_validate_blocks_loopback_literal():
    try:
        w.validate_host("127.0.0.1")
        assert False
    except w.SSRFBlocked:
        pass


def test_validate_resolution_failure_is_blocked():
    def boom(host):
        raise OSError("nxdomain")
    try:
        w.validate_host("nope.test", resolver=boom)
        assert False
    except w.SSRFBlocked as e:
        assert "resolve" in str(e)


def test_allow_private_override():
    assert w.validate_host("box.lan", allow_private=True,
                           resolver=res("10.0.0.9")) == ["10.0.0.9"]


# -- fetch_url integration -------------------------------------------------

def test_fetch_blocks_private_host():
    r = w.fetch_url("http://metadata.internal/latest", resolver=res("169.254.169.254"))
    assert "error" in r and "SSRF" in r["error"]


def test_fetch_blocks_bad_scheme_before_resolving():
    r = w.fetch_url("file:///etc/passwd", resolver=res("8.8.8.8"))
    assert "error" in r and "scheme" in r["error"]


def test_fetch_allow_private_lets_internal_through_guard():
    # With allow_private the guard passes; a fake opener stands in for the network.
    class H:
        def get_content_type(self): return "text/plain"
        def get_content_charset(self): return "utf-8"

    class R:
        status = 200
        url = "http://box.lan/"
        headers = H()
        def read(self, n=-1): return b"intranet ok"

    r = w.fetch_url("http://box.lan/", allow_private=True,
                    resolver=res("10.0.0.5"),
                    urlopen=lambda req, timeout=None: R())
    assert r.get("text") == "intranet ok"


def test_fetch_with_fake_opener_and_no_resolver_skips_guard():
    # Existing behaviour: injected opener + no resolver => guard skipped, works.
    class H:
        def get_content_type(self): return "text/html"
        def get_content_charset(self): return "utf-8"

    class R:
        status = 200
        url = "http://example.com/"
        headers = H()
        def read(self, n=-1): return b"<title>T</title><p>hi</p>"

    r = w.fetch_url("http://example.com/", urlopen=lambda req, timeout=None: R())
    assert r["title"] == "T" and "hi" in r["text"]


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
