#!/usr/bin/env bash
# Run mypy against Music Assistant's *source code* without installing Music Assistant.
#
#   scripts/typecheck-with-ma-sources.sh
#
# CI installs the server and type-checks provider.py against its real types; locally MA is not
# installed (by choice), so plain `uv run mypy` sees its classes as Any. This script reproduces
# CI's mypy run: it fetches the pinned server commit (from pyproject.toml) and the matching
# music-assistant-models wheel into a cache folder as plain files and points MYPYPATH at them.
# Nothing is installed or executed.

set -euo pipefail
cd "$(dirname "$0")/.."

CACHE="${MA_TYPES_CACHE:-$HOME/.cache/audd-local-recognizer/ma-types}"
COMMIT="$(grep -o 'music-assistant/server@[0-9a-f]\{40\}' pyproject.toml | cut -d@ -f2)"
[[ -n "$COMMIT" ]] || { echo "no server pin in pyproject.toml" >&2; exit 1; }
SERVER="$CACHE/server-$COMMIT"

if [[ ! -d "$SERVER/music_assistant" ]]; then
  echo "==> fetching music-assistant/server@$COMMIT (source only)"
  mkdir -p "$SERVER"
  git -C "$SERVER" init -q
  git -C "$SERVER" fetch -q --depth 1 https://github.com/music-assistant/server.git "$COMMIT"
  git -C "$SERVER" checkout -q FETCH_HEAD
fi

MODELS_VERSION="$(grep -o 'music-assistant-models==[0-9.]*' "$SERVER/pyproject.toml" | cut -d= -f3)"
MODELS="$CACHE/models-$MODELS_VERSION"
if [[ ! -d "$MODELS/music_assistant_models" ]]; then
  echo "==> fetching music-assistant-models $MODELS_VERSION (wheel, unpacked as files)"
  mkdir -p "$MODELS"
  url="$(curl -fsSL "https://pypi.org/pypi/music-assistant-models/$MODELS_VERSION/json" \
    | python3 -c "import json,sys; print([u['url'] for u in json.load(sys.stdin)['urls'] if u['filename'].endswith('.whl')][0])")"
  curl -fsSL "$url" -o "$MODELS/models.whl"
  unzip -qo "$MODELS/models.whl" -d "$MODELS"
fi

echo "==> mypy with Music Assistant types (server $COMMIT, models $MODELS_VERSION)"
MYPYPATH="$SERVER:$MODELS" uv run mypy --no-incremental --follow-imports=silent 2>&1 \
  | grep -v "^$CACHE"
