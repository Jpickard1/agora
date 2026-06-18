"""Docs-consistency guard (issue #63). Cheap checks that the user docs don't go
stale: every hubcli command referenced in README's feature/command sections is a
real subcommand, and the port references are consistent. Pure text + the real
argparse parser — no network.
Run: python tests/test_docs.py"""

import io
import os
import re
import sys
from contextlib import redirect_stdout, redirect_stderr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _real_subcommands():
    """The actual top-level hubcli subcommands, scraped from argparse help."""
    from agenthub import cli
    buf = io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            cli.main(["--help"])
    except SystemExit:
        pass
    help_text = buf.getvalue()
    m = re.search(r"\{([a-z0-9,\-]+)\}", help_text)
    assert m, "could not parse subcommand list from --help"
    return set(m.group(1).split(","))


def _code_fragments(md):
    """Each inline-code span and ``` fenced block as a SEPARATE fragment, so a
    'hubcli <cmd>' match can't span two unrelated snippets (and prose like
    'hubcli on any server' is ignored entirely)."""
    frags = re.findall(r"```[a-z]*\n(.*?)```", md, flags=re.DOTALL)
    frags += re.findall(r"`([^`\n]+)`", md)
    # drop ASCII-art diagram blocks (box-drawing chars) — they're prose, not shell
    return [f for f in frags if not re.search(r"[─│└┐┘┌├┤►◄┼▲▼]", f)]


def test_readme_feature_commands_are_real():
    subs = _real_subcommands()
    referenced = set()
    for frag in _code_fragments(_read("README.md")):
        referenced.update(re.findall(r"\bhubcli\s+([a-z][a-z0-9-]+)", frag))
    bogus = {c for c in referenced if c not in subs}
    assert not bogus, f"README references unknown hubcli commands: {sorted(bogus)}"


def test_key_new_features_are_documented():
    readme = _read("README.md").lower()
    for term in ("search", "mentions", "knowledge base", "advisory locks",
                 "research", "docker", "liveness", "projects"):
        assert term in readme, f"README missing feature mention: {term}"


def test_port_references_consistent():
    # we standardised user-facing examples on 8910 (the up/Docker default)
    assert "8787" not in _read("README.md")
    assert "8787" not in _read("SETUP.md")


def test_docker_is_first_class_install():
    readme = _read("README.md")
    assert "docker compose up" in readme
    assert "Option B" in readme            # presented as a co-equal install path


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
