#!/usr/bin/env bash
# Copy the input recordings and a snapshot of the fingerprint database to this Mac.
#
#   scripts/fetch-recordings.sh [--delete]
#
# Needs the provider setting "Record the input stream for debugging" (advanced). Files land in
# ./debug-recordings/<timestamp>/ (git-ignored: audio of your own records, never commit it).
# --delete removes the recordings on the HA host after a successful copy (only the WAV files and
# their start-of-recording DB copies in the provider's recordings/ folder; the live database is
# never touched).
# Replay them offline: uv run python scripts/replay_recording.py debug-recordings/<timestamp>

source "$(dirname "$0")/_common.sh"

DELETE=0
for arg in "$@"; do
  case "$arg" in
    --delete) DELETE=1 ;;
    -h | --help) sed -n '2,11p' "$0"; exit 0 ;;
    *) die "unknown option: $arg" ;;
  esac
done

resolve_container
resolve_runtime
REC_DIR="$MA_DATA_DIR/$PACKAGE/recordings"
DB="$MA_DATA_DIR/$PACKAGE/fingerprints.sqlite3"
[[ "$REC_DIR" == */audd_local_recognizer/recordings ]] || die "refusing recordings dir $REC_DIR"
DEST="$REPO_ROOT/debug-recordings/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$DEST"

log "Copying recordings from $MA_CONTAINER:$REC_DIR"
remote "$DOCKER exec '$MA_CONTAINER' sh -c 'test -d $REC_DIR && tar -C $REC_DIR -cf - .'" \
  | tar -xf - -C "$DEST" || die "no recordings found (is 'Record the input stream' on?)"

log "Copying a consistent snapshot of the fingerprint database"
remote "$DOCKER exec -i '$MA_CONTAINER' '$CONTAINER_PY' -" \
  > "$DEST/fingerprints.sqlite3" <<PY
import sqlite3, sys, tempfile, os
src = sqlite3.connect("file:$DB?mode=ro", uri=True)
fd, tmp = tempfile.mkstemp(suffix=".sqlite3"); os.close(fd)
dst = sqlite3.connect(tmp); src.backup(dst); dst.close(); src.close()
sys.stdout.buffer.write(open(tmp, "rb").read()); os.unlink(tmp)
PY

wavs="$(find "$DEST" -name '*.wav' | wc -l | tr -d ' ')"
size="$(du -sh "$DEST" | cut -f1)"
log "$wavs recording segments + database snapshot in $DEST ($size)"

if [[ $DELETE == 1 ]]; then
  [[ "$wavs" -gt 0 ]] || die "nothing copied; not deleting anything"
  log "Deleting the recordings on the HA host"
  remote "$DOCKER exec '$MA_CONTAINER' sh -c 'rm -f $REC_DIR/*.wav $REC_DIR/*_db.sqlite3'"
fi
log "Replay offline: uv run python scripts/replay_recording.py $DEST"
