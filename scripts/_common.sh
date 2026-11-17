# Shared helpers for the deployment scripts. Sourced, not executed.
# Configuration comes from the environment or the untracked file .deploy.env
# (copy .deploy.env.example). Nothing secret is stored or printed here.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$REPO_ROOT/.deploy.env" ]]; then
  # shellcheck disable=SC1091
  source "$REPO_ROOT/.deploy.env"
fi

HA_HOST="${HA_HOST:-homeassistant.local}"
HA_USER="${HA_USER:-homeassistant}"
HA_SSH_PORT="${HA_SSH_PORT:-22}"
REMOTE_SRC="${REMOTE_SRC:-/share/music-assistant-audd-local-recognizer}"
# Container name of the Music Assistant Nightly add-on. Empty = auto-detect.
MA_CONTAINER="${MA_CONTAINER:-}"
# How docker is invoked on the host (e.g. "sudo docker" if needed).
DOCKER="${DOCKER:-docker}"
# MA data dir inside the container (mass.storage_path); the add-on uses /data.
MA_DATA_DIR="${MA_DATA_DIR:-/data}"
PACKAGE="audd_local_recognizer"

SSH_OPTS=(-p "$HA_SSH_PORT" -o ConnectTimeout=10
  -o ControlMaster=auto -o "ControlPath=$HOME/.ssh/cm-%r@%h:%p" -o ControlPersist=120)

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

remote() { ssh "${SSH_OPTS[@]}" "$HA_USER@$HA_HOST" "$@"; }

# Resolve the MA container (configured name or the single running music_assistant one).
resolve_container() {
  if [[ -n "$MA_CONTAINER" ]]; then
    remote "$DOCKER inspect -f '{{.State.Running}}' '$MA_CONTAINER'" >/dev/null \
      || die "container '$MA_CONTAINER' not found on $HA_HOST"
    return
  fi
  local found
  found="$(remote "$DOCKER ps --format '{{.Names}}' | grep -i 'music_assistant' || true")"
  [[ -n "$found" ]] || die "no running music_assistant container found; set MA_CONTAINER"
  if [[ "$(printf '%s\n' "$found" | wc -l | tr -d ' ')" != "1" ]]; then
    die "several music_assistant containers found; set MA_CONTAINER to one of: $(echo $found)"
  fi
  MA_CONTAINER="$found"
}

# Python and provider directory inside the container.
resolve_runtime() {
  CONTAINER_PY="$(remote "$DOCKER exec '$MA_CONTAINER' sh -c 'command -v python3 || command -v python'")"
  [[ -n "$CONTAINER_PY" ]] || die "no python found in $MA_CONTAINER"
  PROVIDERS_DIR="$(remote "$DOCKER exec '$MA_CONTAINER' '$CONTAINER_PY' -c \
    'import os, music_assistant.providers as p; print(os.path.dirname(p.__file__))'")"
  [[ "$PROVIDERS_DIR" == */music_assistant/providers ]] \
    || die "unexpected MA providers dir: '$PROVIDERS_DIR'"
  PROVIDER_DEST="$PROVIDERS_DIR/$PACKAGE"
}
