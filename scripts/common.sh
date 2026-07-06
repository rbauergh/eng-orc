# Shared helpers sourced by the setup/stack scripts. Not executable on its own.

# docker_ready: returns 0 when docker is installed, the daemon is up, and this
# user may talk to it. Otherwise prints the exact fix to stderr and returns 1.
docker_ready() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "docker is not installed — Letta is optional; install docker-ce (or Docker" >&2
    echo "Desktop with WSL integration for this distro) to run the memory server." >&2
    return 1
  fi
  local err
  if err="$(docker info 2>&1 >/dev/null)"; then
    return 0
  fi
  case "$err" in
    *"permission denied"*)
      echo "docker is installed but your user cannot reach the daemon socket. Fix:" >&2
      echo "    sudo usermod -aG docker \$USER" >&2
      echo "then open a NEW shell (group membership applies at login; 'newgrp docker'" >&2
      echo "works for the current shell, 'wsl --shutdown' applies it everywhere)." >&2
      ;;
    *"Cannot connect"*|*"daemon"*)
      echo "the docker daemon is not running. Fix:" >&2
      echo "    sudo systemctl enable --now docker" >&2
      ;;
    *)
      echo "docker is not usable: $err" >&2
      ;;
  esac
  return 1
}
