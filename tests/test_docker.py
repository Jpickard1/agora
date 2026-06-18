"""Tests for the one-command Docker install (issue #58). Headless: validates the
Dockerfile / compose / entrypoint / Makefile are present and internally
consistent (no docker daemon or yaml dependency needed).
Run: python tests/test_docker.py"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_artifacts_exist():
    for rel in ("Dockerfile", "docker-compose.yml", ".dockerignore",
                "Makefile", "deploy/docker-entrypoint.sh"):
        assert os.path.exists(os.path.join(ROOT, rel)), rel


def test_dockerfile_installs_and_uses_entrypoint():
    df = _read("Dockerfile")
    assert "pip install" in df and "." in df
    assert "EXPOSE 8910" in df
    assert 'VOLUME ["/data"]' in df
    assert "docker-entrypoint.sh" in df
    assert "HEALTHCHECK" in df and "/api/health" in df


def test_entrypoint_inits_then_serves_safely():
    ep = _read("deploy/docker-entrypoint.sh")
    # init only when missing (idempotent), and serve on all interfaces
    assert "config.json" in ep
    assert "init" in ep
    assert "--no-pointer" in ep            # issue #39: never hijack the shared pointer
    assert "serve" in ep
    assert "0.0.0.0" in ep


def test_entrypoint_passes_root_before_subcommand():
    """Regression guard (issue #58 bug): --root is a TOP-LEVEL flag, so every
    hubcli invocation must place it BEFORE the subcommand, else the container
    crashes with 'unrecognized arguments: --root'."""
    ep = _read("deploy/docker-entrypoint.sh")
    subcommands = {"init", "serve", "listen", "post", "send", "agents"}
    checked = 0
    for raw in ep.splitlines():
        line = raw.strip()
        if line.startswith("#") or "hubcli" not in line:
            continue
        toks = line.split()
        try:
            start = toks.index("hubcli")
        except ValueError:
            continue
        args = toks[start + 1:]
        sub_idx = next((i for i, t in enumerate(args) if t in subcommands), None)
        if sub_idx is None:
            continue
        checked += 1
        if "--root" in args:
            assert args.index("--root") < sub_idx, (
                f"--root must precede the subcommand in: {line}")
    assert checked >= 2, "expected to check the init + serve invocations"


def test_compose_has_volume_port_and_token():
    yml = _read("docker-compose.yml")
    assert "/data" in yml                  # hub-root volume mount
    assert "AGENT_HUB_TOKEN" in yml        # token env
    assert "8910" in yml
    assert "AGENT_HUB_DIR" in yml          # shared-FS path override
    assert "AGENT_HUB_ROOT: /data" in yml


def test_makefile_one_command_targets():
    mk = _read("Makefile")
    for target in ("up:", "down:", "logs:"):
        assert target in mk, target
    assert "docker compose up" in mk


def test_readme_documents_docker():
    rd = _read("README.md")
    assert "docker compose up" in rd
    assert "AGENT_HUB_TOKEN" in rd
    # shared-FS workflow is preserved / documented as intact
    assert "shared filesystem" in rd.lower() or "shared-fs" in rd.lower()


def test_entrypoint_command_matches_packaged_cli():
    # The image invokes `hubcli` — make sure that console script is declared.
    assert 'hubcli = "agenthub.cli:main"' in _read("pyproject.toml")


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
