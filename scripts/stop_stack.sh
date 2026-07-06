#!/usr/bin/env bash
# Stop the serving stack and free all VRAM.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

systemctl --user stop llama-swap.service 2>/dev/null && echo "llama-swap: stopped" \
  || echo "llama-swap: not running under systemd"

if command -v docker >/dev/null 2>&1 && [ -f "$REPO_ROOT/server/letta/.env" ]; then
  (cd "$REPO_ROOT/server/letta" && docker compose down)
  echo "letta: stopped"
fi
