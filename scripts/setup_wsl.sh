#!/usr/bin/env bash
# eng-orc one-shot setup for WSL2 Ubuntu on a single NVIDIA GPU.
#
#   scripts/setup_wsl.sh [--profile balanced-12gb|max-64gb|classic-12gb]
#                        [--no-models] [--no-letta] [--no-services]
#                        [--with-optional]           # also fetch optional models
#                        [--llama-tag bNNNN]         # llama.cpp build to pin
#                        [--swap-version vNNN]       # llama-swap release to pin
#
# Idempotent: safe to re-run after failures or to switch profiles.
# Windows-side prerequisites (NVIDIA driver, .wslconfig) are in docs/OPERATIONS.md.

set -euo pipefail

PROFILE="balanced-12gb"
DO_MODELS=1
DO_LETTA=1
DO_SERVICES=1
WITH_OPTIONAL=""
LLAMA_TAG="${LLAMA_TAG:-b9878}"
SWAP_VERSION="${SWAP_VERSION:-v235}"
CUDA_ARCH="${CUDA_ARCH:-89}"           # 89 = Ada Lovelace (RTX 4070 Ti)
CUDA_PKG="${CUDA_PKG:-cuda-toolkit-13-3}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
MODELS_DIR="${MODELS_DIR:-$HOME/models}"
LLAMA_DIR="$HOME/llama.cpp"
BIN_DIR="$HOME/.local/bin"
SWAP_CONFIG_DIR="$HOME/.config/llama-swap"

log()  { printf '\033[1;34m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[setup] WARNING:\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[setup] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --profile)       PROFILE="$2"; shift 2 ;;
    --no-models)     DO_MODELS=0; shift ;;
    --no-letta)      DO_LETTA=0; shift ;;
    --no-services)   DO_SERVICES=0; shift ;;
    --with-optional) WITH_OPTIONAL="--with-optional"; shift ;;
    --llama-tag)     LLAMA_TAG="$2"; shift 2 ;;
    --swap-version)  SWAP_VERSION="$2"; shift 2 ;;
    -h|--help)       grep '^#' "$0" | head -12; exit 0 ;;
    *) die "unknown flag: $1" ;;
  esac
done

[ -d "$REPO_ROOT/server/profiles/$PROFILE" ] || die "unknown profile: $PROFILE"
[ "$(id -u)" != 0 ] || die "run as your normal user, not root (sudo is used where needed)"
grep -qi microsoft /proc/version 2>/dev/null || warn "this does not look like WSL — continuing anyway"

# --- 1. system packages -------------------------------------------------------
log "installing apt packages"
sudo apt-get update -y
sudo apt-get install -y --no-install-recommends \
  build-essential cmake ninja-build git libssl-dev pkg-config \
  python3 python3-venv python3-pip \
  ripgrep universal-ctags jq curl ca-certificates

# --- 2. GPU driver visibility ---------------------------------------------------
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
  log "GPU visible: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1)"
else
  warn "nvidia-smi not working. Install the NVIDIA driver ON WINDOWS (never inside WSL),"
  warn "then restart WSL (wsl --shutdown). Continuing so CPU-side setup completes."
fi

# --- 3. CUDA toolkit (build-time requirement; runtime driver comes from Windows) --
if [ -x /usr/local/cuda/bin/nvcc ] || command -v nvcc >/dev/null 2>&1; then
  log "CUDA toolkit already present"
else
  log "installing CUDA toolkit ($CUDA_PKG) from the wsl-ubuntu repo"
  keyring=/tmp/cuda-keyring_1.1-1_all.deb
  curl -fsSL -o "$keyring" \
    https://developer.download.nvidia.com/compute/cuda/repos/wsl-ubuntu/x86_64/cuda-keyring_1.1-1_all.deb
  sudo dpkg -i "$keyring"
  sudo apt-get update -y
  # NEVER install 'cuda' / 'cuda-drivers' meta-packages under WSL
  sudo apt-get install -y "$CUDA_PKG" || die "could not install $CUDA_PKG (set CUDA_PKG to an available cuda-toolkit-* version)"
fi
export PATH="/usr/local/cuda/bin:$PATH"

# --- 4. llama.cpp (source build with CUDA — no official Linux CUDA binaries exist) --
build_llama() {
  log "building llama.cpp $LLAMA_TAG (CUDA, arch $CUDA_ARCH)"
  if [ ! -d "$LLAMA_DIR/.git" ]; then
    git clone https://github.com/ggml-org/llama.cpp "$LLAMA_DIR"
  fi
  git -C "$LLAMA_DIR" fetch --tags --quiet
  git -C "$LLAMA_DIR" checkout --quiet "$LLAMA_TAG"

  cache="$LLAMA_DIR/build/CMakeCache.txt"
  if [ -f "$cache" ]; then
    # Preserve the evidence of any custom flags a previous hand-tuned build
    # used (e.g. a GPU workaround) BEFORE the tree is reset, and surface them.
    flags_backup="$LLAMA_DIR/build-flags-backup.txt"
    grep -E '^(GGML_|LLAMA_|CMAKE_CUDA_ARCHITECTURES|CMAKE_CUDA_FLAGS|CMAKE_CXX_FLAGS|CMAKE_C_FLAGS)' "$cache" \
      | grep -vE '=(OFF|)$' > "$flags_backup" || true
    if [ -s "$flags_backup" ]; then
      log "previous build's non-default cmake settings (saved to $flags_backup):"
      sed 's/^/    /' "$flags_backup"
      log "carry any GPU-workaround flags forward via CMAKE_EXTRA_ARGS, e.g.:"
      log "    CMAKE_EXTRA_ARGS='-DGGML_CUDA_FORCE_MMQ=ON' scripts/setup_wsl.sh ..."
    fi
  fi
  # A build tree configured with another generator (e.g. the default Unix
  # Makefiles from an earlier manual build) cannot be reconfigured for Ninja.
  if [ -f "$cache" ] && ! grep -q '^CMAKE_GENERATOR:INTERNAL=Ninja$' "$cache"; then
    log "existing build tree uses a different CMake generator — resetting build/"
    rm -rf "$LLAMA_DIR/build"
  fi

  # shellcheck disable=SC2086  # CMAKE_EXTRA_ARGS is intentionally word-split
  cmake -S "$LLAMA_DIR" -B "$LLAMA_DIR/build" -G Ninja \
    -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES="$CUDA_ARCH" -DGGML_NATIVE=OFF \
    -DCMAKE_BUILD_TYPE=Release ${CMAKE_EXTRA_ARGS:-}
  cmake --build "$LLAMA_DIR/build" --config Release -j "$(nproc)" -t llama-server
  echo "$LLAMA_TAG" > "$LLAMA_DIR/build/.engorc-tag"

  # Prove the fresh binary can actually see the GPU before we invest in
  # 30 GB of model downloads (catches wrong-arch builds immediately).
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    if "$LLAMA_DIR/build/bin/llama-server" --list-devices 2>/dev/null | grep -qi cuda; then
      log "llama-server sees the GPU: $("$LLAMA_DIR/build/bin/llama-server" --list-devices 2>/dev/null | grep -i cuda | head -1)"
    else
      warn "llama-server does not report a CUDA device. Likely an arch mismatch —"
      warn "check 'nvidia-smi' works, confirm CUDA_ARCH=$CUDA_ARCH matches your GPU"
      warn "(4070 Ti = 89), and see $LLAMA_DIR/build-flags-backup.txt for flags your"
      warn "old working build used (re-apply via CMAKE_EXTRA_ARGS)."
    fi
  fi
}
if [ -x "$LLAMA_DIR/build/bin/llama-server" ] \
   && [ "$(cat "$LLAMA_DIR/build/.engorc-tag" 2>/dev/null || true)" = "$LLAMA_TAG" ]; then
  log "llama-server $LLAMA_TAG already built"
else
  build_llama
fi

# --- 5. llama-swap -----------------------------------------------------------------
mkdir -p "$BIN_DIR"
if [ -x "$BIN_DIR/llama-swap" ] && "$BIN_DIR/llama-swap" --version 2>/dev/null | grep -q "${SWAP_VERSION#v}"; then
  log "llama-swap $SWAP_VERSION already installed"
else
  log "installing llama-swap $SWAP_VERSION"
  tmp="$(mktemp -d)"
  for attempt in 1 2 3; do
    curl -fSL -o "$tmp/llama-swap.tar.gz" \
      "https://github.com/mostlygeek/llama-swap/releases/download/${SWAP_VERSION}/llama-swap_${SWAP_VERSION#v}_linux_amd64.tar.gz" \
      && break || { warn "download failed (attempt $attempt)"; sleep 3; }
  done
  tar -xzf "$tmp/llama-swap.tar.gz" -C "$tmp"
  install -m 0755 "$tmp/llama-swap" "$BIN_DIR/llama-swap"
  rm -rf "$tmp"
fi
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) warn "add $BIN_DIR to your PATH" ;; esac

# --- 6. llama-swap config for the chosen profile ---------------------------------------
mkdir -p "$SWAP_CONFIG_DIR" "$MODELS_DIR"
cp "$REPO_ROOT/server/profiles/$PROFILE/llama-swap.yaml" "$SWAP_CONFIG_DIR/config.yaml"
log "llama-swap config installed for profile $PROFILE"

# --- 7. python venv + orc -----------------------------------------------------------------
VENV="$REPO_ROOT/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  log "creating venv"
  python3 -m venv "$VENV"
fi
log "installing eng-orc (editable, with dev extras)"
"$VENV/bin/pip" install --upgrade pip -q
"$VENV/bin/pip" install -e "$REPO_ROOT[dev]" -q
ORC="$VENV/bin/orc"

# --- 8. models ---------------------------------------------------------------------------------
if [ "$DO_MODELS" = 1 ]; then
  "$SCRIPT_DIR/download_models.sh" --profile "$PROFILE" --models-dir "$MODELS_DIR" \
    --venv "$VENV" $WITH_OPTIONAL
else
  log "skipping model downloads (--no-models)"
fi

# --- 9. orc home config -----------------------------------------------------------------------------
"$ORC" init
log "merging profile model settings into ~/.eng-orc/config.yaml"
"$VENV/bin/python" - "$REPO_ROOT/server/profiles/$PROFILE/orc-models.yaml" <<'PYEOF'
import sys
from pathlib import Path
import yaml

from engorc.config import load_config

profile_path = Path(sys.argv[1])
config_path = load_config().config_path
data = yaml.safe_load(config_path.read_text()) or {}
profile = yaml.safe_load(profile_path.read_text())
for key in ("models", "review"):
    if key in profile:
        data[key] = profile[key]
config_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
print(f"models/review sections set from {profile_path.name}")
PYEOF

# --- 10. systemd user service for llama-swap ------------------------------------------------------------
if [ "$DO_SERVICES" = 1 ] && command -v systemctl >/dev/null 2>&1 \
   && systemctl is-system-running >/dev/null 2>&1; then
  log "installing systemd user unit for llama-swap"
  mkdir -p "$HOME/.config/systemd/user"
  cp "$SCRIPT_DIR/systemd/llama-swap.service" "$HOME/.config/systemd/user/llama-swap.service"
  systemctl --user daemon-reload
  systemctl --user enable --now llama-swap.service
  loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER" || \
    warn "could not enable linger; llama-swap will only run while you are logged in"
else
  [ "$DO_SERVICES" = 1 ] && warn "systemd not active — enable it in /etc/wsl.conf ([boot] systemd=true), run 'wsl --shutdown', and re-run; starting llama-swap manually for now:"
  warn "  $BIN_DIR/llama-swap --config $SWAP_CONFIG_DIR/config.yaml --listen 0.0.0.0:9292"
fi

# --- 11. Letta memory server ------------------------------------------------------------------------------------
if [ "$DO_LETTA" = 1 ]; then
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    log "starting Letta (docker compose)"
    [ -f "$REPO_ROOT/server/letta/.env" ] || cp "$REPO_ROOT/server/letta/.env.example" "$REPO_ROOT/server/letta/.env"
    (cd "$REPO_ROOT/server/letta" && docker compose up -d)
    log "letta starting at http://127.0.0.1:8283 (first boot runs migrations — give it ~2 min)"
  else
    warn "docker compose not available — skipping Letta. eng-orc degrades to its local memory"
    warn "store automatically; start Letta later with: cd server/letta && docker compose up -d"
  fi
fi

# --- 12. prove it ------------------------------------------------------------------------------------------------------
log "running the GPU-less selftest"
"$ORC" selftest || die "selftest failed — the orchestrator itself is unhealthy"

log "setup complete. Next:"
echo "    source $VENV/bin/activate"
echo "    orc doctor                       # verify servers + models end-to-end"
echo "    orc chat utility 'say hi'        # first real token through the GPU"
echo "    orc new \"build me ...\" && orc run --watch"
