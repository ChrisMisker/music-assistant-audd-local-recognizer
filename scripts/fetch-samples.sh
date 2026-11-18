#!/usr/bin/env bash
# Copy the AudD debug samples from the MA container to ./debug-samples/ on this Mac.
#
#   scripts/fetch-samples.sh
#
# Needs the provider setting "Save AudD samples for debugging" (advanced) to be on. The samples
# are audio of your own records: ./debug-samples/ is git-ignored and must never be committed.
# Listen to them, or upload one in the AudD dashboard ("manually upload a file") to compare.

source "$(dirname "$0")/_common.sh"

resolve_container
DEBUG_DIR="$MA_DATA_DIR/$PACKAGE/debug"
DEST="$REPO_ROOT/debug-samples"
mkdir -p "$DEST"
log "Fetching $DEBUG_DIR from $MA_CONTAINER"
remote "$DOCKER exec '$MA_CONTAINER' sh -c 'test -d $DEBUG_DIR && tar -C $DEBUG_DIR -cf - .'" \
  | tar -xf - -C "$DEST" \
  || die "no debug samples found (is 'Save AudD samples for debugging' on?)"
count="$(find "$DEST" -name '*.wav' | wc -l | tr -d ' ')"
log "$count samples in $DEST"
for json in $(ls -t "$DEST"/*.json 2>/dev/null | head -10); do
  uv run --quiet python - "$json" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1]); d = json.loads(p.read_text())
print(f"{p.stem:38} {d.get('outcome','')[:40]:40} rms={d.get('rms_dbfs')} peak={d.get('peak_dbfs')} "
      f"clip={d.get('clipped_ratio')} L/R={d.get('lr_correlation')}")
PY
done
