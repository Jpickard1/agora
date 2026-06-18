#!/usr/bin/env sh
# Agora hub container entrypoint (issue #58).
# On first run, initialise the hub at $AGENT_HUB_ROOT with $AGENT_HUB_TOKEN
# (idempotent — skipped if a config already exists), then serve the web UI.
# Uses `--no-pointer` so it never writes ~/.agent-hub-path (issue #39): a
# container must not hijack a shared pointer on a mounted volume.
set -eu

ROOT="${AGENT_HUB_ROOT:-/data}"
PORT="${AGORA_PORT:-8910}"

if [ ! -f "$ROOT/config.json" ]; then
  echo "[entrypoint] initialising hub at $ROOT"
  # NOTE: --root is a TOP-LEVEL flag — it must come BEFORE the subcommand.
  hubcli --root "$ROOT" init --no-pointer ${AGENT_HUB_TOKEN:+--token "$AGENT_HUB_TOKEN"}
else
  echo "[entrypoint] existing hub at $ROOT — leaving config as-is"
fi

echo "[entrypoint] serving on 0.0.0.0:$PORT  (UI: http://localhost:$PORT/)"
exec hubcli --root "$ROOT" serve --host 0.0.0.0 --port "$PORT"
