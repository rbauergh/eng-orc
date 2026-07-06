#!/usr/bin/env bash
# Download the GGUF models for a profile manifest (resumable, verified).
#
#   scripts/download_models.sh --profile balanced-12gb [--models-dir ~/models]
#                              [--venv .venv] [--with-optional]
#
# Manifest lines: "<hf-repo>|<include-pattern>", optionally prefixed
# "optional " (skipped unless --with-optional). Downloads land flat in the
# models dir. Re-runs skip completed files (hf download resumes partials).

set -euo pipefail

PROFILE="balanced-12gb"
MODELS_DIR="$HOME/models"
WITH_OPTIONAL=0
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
VENV="$REPO_ROOT/.venv"

log()  { printf '\033[1;36m[models]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[models] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --profile)       PROFILE="$2"; shift 2 ;;
    --models-dir)    MODELS_DIR="$2"; shift 2 ;;
    --venv)          VENV="$2"; shift 2 ;;
    --with-optional) WITH_OPTIONAL=1; shift ;;
    -h|--help)       grep '^#' "$0" | head -8; exit 0 ;;
    *) die "unknown flag: $1" ;;
  esac
done

MANIFEST="$REPO_ROOT/server/profiles/$PROFILE/models.txt"
[ -f "$MANIFEST" ] || die "no manifest for profile $PROFILE"
mkdir -p "$MODELS_DIR"

# The hf CLI ships with huggingface_hub (newer releases install `hf`,
# older ones `huggingface-cli`) — ensure it exists inside the venv.
if [ ! -x "$VENV/bin/hf" ] && [ ! -x "$VENV/bin/huggingface-cli" ]; then
  log "installing huggingface_hub CLI into the venv"
  "$VENV/bin/pip" install -q -U "huggingface_hub[cli]"
fi
HF="$VENV/bin/hf"
[ -x "$HF" ] || HF="$VENV/bin/huggingface-cli"
[ -x "$HF" ] || die "huggingface CLI not found in $VENV"

free_gb="$(df -BG --output=avail "$MODELS_DIR" | tail -1 | tr -dc '0-9' || echo 0)"
log "downloading profile '$PROFILE' to $MODELS_DIR (${free_gb}G free on that filesystem)"
case "$MODELS_DIR" in /mnt/*) die "models must live on ext4 (e.g. \$HOME), not /mnt/* — 9P I/O cripples model loads" ;; esac

failures=0
while IFS= read -r raw_line; do
  line="${raw_line%%#*}"
  line="$(echo "$line" | xargs || true)"
  [ -n "$line" ] || continue
  optional=0
  case "$line" in optional\ *) optional=1; line="${line#optional }" ;; esac
  if [ "$optional" = 1 ] && [ "$WITH_OPTIONAL" != 1 ]; then
    log "skipping optional: $line (use --with-optional)"
    continue
  fi
  repo="${line%%|*}"
  pattern="${line##*|}"

  # exact filenames that already exist are done
  case "$pattern" in
    *\**) ;;
    *) if [ -s "$MODELS_DIR/$pattern" ]; then log "present: $pattern"; continue; fi ;;
  esac

  log "fetching $repo :: $pattern"
  ok=0
  for attempt in 1 2 3; do
    if "$HF" download "$repo" --include "$pattern" --local-dir "$MODELS_DIR"; then
      ok=1; break
    fi
    log "attempt $attempt failed for $repo; retrying in 5s"
    sleep 5
  done
  if [ "$ok" != 1 ]; then
    printf '\033[1;31m[models] FAILED:\033[0m %s\n' "$repo :: $pattern" >&2
    failures=$((failures + 1))
    continue
  fi
  case "$pattern" in
    *\**)
      # a wildcard matching zero files "succeeds" silently — catch that here
      matched=0
      for f in "$MODELS_DIR"/$pattern; do
        [ -e "$f" ] && matched=1 && break
      done
      if [ "$matched" != 1 ]; then
        printf '\033[1;31m[models] pattern matched NOTHING:\033[0m %s (check the filename on the HF repo)\n' "$pattern" >&2
        failures=$((failures + 1))
      fi
      ;;
    *) [ -s "$MODELS_DIR/$pattern" ] || { printf '\033[1;31m[models] MISSING after download:\033[0m %s\n' "$pattern" >&2; failures=$((failures + 1)); } ;;
  esac
done < "$MANIFEST"

# hf download may leave .cache metadata next to the files; harmless but noisy
rm -rf "$MODELS_DIR/.cache" 2>/dev/null || true

if [ "$failures" -gt 0 ]; then
  die "$failures download(s) failed — re-run this script (downloads resume)"
fi
log "all models for '$PROFILE' are present"
ls -lh "$MODELS_DIR" | grep -Ei '\.gguf$' || true
