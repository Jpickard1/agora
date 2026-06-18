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


# Multi-user / own-server invariants (issue #105).
USER_DOCS = ("README.md", "SETUP.md", "QUICKSTART.md",
             "docs/multi-user.md", "docs/windows-quickstart.md")
SHARED_ROOT = "/ewsc/ewsc/agents/agora"


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


def test_no_hardcoded_ewsc_commands():
    """Portability (issue #68): docs must not tell users to run commands against a
    literal /ewsc path — those values are examples only, real commands use a
    placeholder / $HUB / AGENT_HUB_ROOT."""
    for rel in ("README.md", "SETUP.md", "QUICKSTART.md"):
        txt = _read(rel)
        assert "init --root /ewsc" not in txt, f"{rel}: hardcoded /ewsc in an init command"
        assert "AGENT_HUB_ROOT=/ewsc" not in txt, f"{rel}: hardcoded /ewsc export"


def test_portable_default_documented():
    rd, su = _read("README.md"), _read("SETUP.md")
    assert "~/.agent-hub" in rd and "~/.agent-hub" in su   # cross-platform default
    assert "AGENT_HUB_ROOT" in rd


def test_new_machine_section_present():
    assert "Installing on a new machine" in _read("README.md")
    assert "Installing on a new machine" in _read("SETUP.md")


def test_run_your_own_server_is_documented():
    """#105: every user-facing setup doc tells a setup agent to run its OWN server."""
    for rel in USER_DOCS:
        low = _read(rel).lower()
        assert "run your own" in low or "your own server" in low, \
            f"{rel}: missing 'run your own server' guidance"


def test_shared_root_path_is_explicit():
    """#105: the literal shared hub path is stated so a setup agent uses it verbatim."""
    for rel in USER_DOCS:
        assert SHARED_ROOT in _read(rel), f"{rel}: missing shared root {SHARED_ROOT}"


def test_no_topology_b_token_sharing():
    """#105: no 'share/reuse the token' guidance — each user runs their own hub
    with their own token. (The DON'T phrasing 'reuse another user's token' is fine.)"""
    bad = ("reuse its token", "shared token", "shared secret",
           "share these with every server", "reuse the token")
    for rel in ("README.md", "SETUP.md", "QUICKSTART.md", "docs/windows-quickstart.md"):
        low = _read(rel).lower()
        for phrase in bad:
            assert phrase not in low, f"{rel}: topology-B phrase present: {phrase!r}"


def test_init_uses_global_root_flag():
    """`--root` is a GLOBAL flag, so 'hubcli init --root ...' is invalid argparse;
    the correct order is 'hubcli --root <dir> init'. Guard docs + connect-help from
    regressing (this was a real pre-existing bug fixed in #105)."""
    from agenthub import cli
    targets = {rel: _read(rel) for rel in USER_DOCS}
    targets["CONNECT_PROMPT"] = cli.CONNECT_PROMPT
    for name, txt in targets.items():
        assert "init --root" not in txt, \
            f"{name}: wrong flag order 'init --root' (use '--root <dir> init')"


def test_connect_help_tells_agent_to_run_own_server():
    """#105: the connect-help paste-prompt (what a fresh setup agent reads) carries
    the own-server message + the shared root."""
    from agenthub import cli
    prompt = cli.CONNECT_PROMPT
    assert "RUN YOUR OWN SERVER" in prompt
    assert SHARED_ROOT in prompt


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
