#!/usr/bin/env bash
# Start (or verify) the serving stack: llama-swap + Letta.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"

if systemctl --user list-unit-files llama-swap.service >/dev/null 2>&1; then
  systemctl --user start llama-swap.service
  echo "llama-swap: $(systemctl --user is-active llama-swap.service)"
else
  echo "llama-swap unit not installed; run scripts/setup_wsl.sh (or start manually:"
  echo "  ~/.local/bin/llama-swap --config ~/.config/llama-swap/config.yaml --listen 0.0.0.0:9292)"
fi

if [ ! -f "$REPO_ROOT/server/letta/.env" ]; then
  echo "letta: skipped (no server/letta/.env — copy .env.example; memory stays local)"
elif docker_ready; then
  (cd "$REPO_ROOT/server/letta" && docker compose up -d)
  echo "letta: starting on :8283"
else
  echo "letta: skipped until docker is usable (see above — memory falls back to local store)"
fi

echo
echo "check with: orc doctor"
