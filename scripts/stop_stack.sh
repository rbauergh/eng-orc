#!/usr/bin/env bash
# Stop the serving stack and free all VRAM.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"

systemctl --user stop llama-swap.service 2>/dev/null && echo "llama-swap: stopped" \
  || echo "llama-swap: not running under systemd"

if [ -f "$REPO_ROOT/server/letta/.env" ] && docker_ready; then
  (cd "$REPO_ROOT/server/letta" && docker compose down)
  echo "letta: stopped"
fi
