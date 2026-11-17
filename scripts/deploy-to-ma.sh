#!/usr/bin/env bash
# Deploy the local working tree to the Music Assistant Nightly add-on on Home Assistant.
#
#   scripts/deploy-to-ma.sh [--skip-checks] [--restart] [--logs]
#
# 1. run local tests + ruff (unless --skip-checks)
# 2. copy the source to $REMOTE_SRC on the HA host
# 3. replace ONLY the audd_local_recognizer provider directory inside the MA container
# 4. py_compile + import the provider with the container's own Python (real MA runtime)
# 5. optionally restart the MA container and follow the provider logs
#
# Never touches the fingerprint database, MA core, other providers, or GitHub.
# NOTE: the provider lives in the container's site-packages, so an add-on update or
# container re-creation removes it. Just run this script again afterwards.

source "$(dirname "$0")/_common.sh"

SKIP_CHECKS=0 RESTART=0 LOGS=0
for arg in "$@"; do
  case "$arg" in
    --skip-checks) SKIP_CHECKS=1 ;;
    --restart) RESTART=1 ;;
    --logs) LOGS=1 ;;
    -h | --help) sed -n '2,15p' "$0"; exit 0 ;;
    *) die "unknown option: $arg" ;;
  esac
done

cd "$REPO_ROOT"

if [[ $SKIP_CHECKS == 0 ]]; then
  log "Local checks (pytest, ruff)"
  uv run --quiet pytest -q
  uv run --quiet ruff check audd_local_recognizer tests
  uv run --quiet ruff format --check audd_local_recognizer tests
fi

VERSION="$(grep -m1 '^VERSION' "$PACKAGE/provider.py" | cut -d'"' -f2)"
COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo none)"
DIRTY="$(git status --porcelain 2>/dev/null | grep -q . && echo '+dirty' || true)"
log "Deploying v$VERSION ($COMMIT$DIRTY) to $HA_USER@$HA_HOST"

resolve_container
resolve_runtime
log "Container: $MA_CONTAINER  provider dir: $PROVIDER_DEST"

log "Syncing source to $HA_HOST:$REMOTE_SRC"
[[ "$REMOTE_SRC" == /share/* ]] || die "REMOTE_SRC must live under /share"
# Replace the package dir there (scoped delete), keep everything else in REMOTE_SRC.
COPYFILE_DISABLE=1 tar -czf - \
  --exclude '__pycache__' --exclude '*.pyc' --exclude '*.sqlite3*' \
  "$PACKAGE" scripts tests pyproject.toml README.md CLAUDE.md ROADMAP.md \
  | remote "$REMOTE_SUDO mkdir -p '$REMOTE_SRC' && $REMOTE_SUDO rm -rf '$REMOTE_SRC/$PACKAGE' \
      && $REMOTE_SUDO tar -xzf - -o -C '$REMOTE_SRC' \
      && echo '$VERSION $COMMIT$DIRTY' | $REMOTE_SUDO tee '$REMOTE_SRC/DEPLOYED_VERSION' >/dev/null"

log "Installing provider into the container"
[[ "$PROVIDER_DEST" == */music_assistant/providers/$PACKAGE ]] || die "refusing dest $PROVIDER_DEST"
remote "$DOCKER exec '$MA_CONTAINER' rm -rf '$PROVIDER_DEST' \
  && $DOCKER cp '$REMOTE_SRC/$PACKAGE' '$MA_CONTAINER:$PROVIDER_DEST'"

log "Compile + import check in the MA runtime"
remote "$DOCKER exec -i '$MA_CONTAINER' '$CONTAINER_PY' -" <<PY
import importlib, pathlib, py_compile, sys
dest = pathlib.Path("$PROVIDER_DEST")
for path in sorted(dest.glob("*.py")):
    py_compile.compile(str(path), doraise=True)
mod = importlib.import_module("music_assistant.providers.$PACKAGE.provider")
print(f"provider v{mod.VERSION} compiles and imports OK on Python {sys.version.split()[0]}")
try:
    import audalign  # noqa: F401
except ImportError:
    print("WARNING: audalign not installed yet (MA installs manifest requirements on load)")
PY

if [[ $RESTART == 1 ]]; then
  log "Restarting $MA_CONTAINER"
  remote "$DOCKER restart '$MA_CONTAINER'" >/dev/null
else
  log "Not restarted. Run with --restart (or restart the add-on) to load the new code."
fi

if [[ $LOGS == 1 ]]; then
  exec "$(dirname "$0")/logs.sh" --follow
fi
log "Done."
