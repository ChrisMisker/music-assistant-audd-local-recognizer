#!/usr/bin/env bash
# Copy finished input recordings (+ their session logs) and a database snapshot to this Mac.
#
#   scripts/fetch-recordings.sh [--delete] [--wav] [--idle-minutes N]
#
# Needs the provider setting "Record the input stream for debugging" (advanced). Only sessions
# whose files have not changed for --idle-minutes (default 2) are copied, so a recording that is
# still running is never cut off; fetch again later for it. Files land in
# $RECORDINGS_DIR/<timestamp>/ (default ./debug-recordings, git-ignored: audio of your own
# records, never commit it). Segments are stored as FLAC (lossless, ~60 % of the WAV size; each
# file is verified sample by sample before its WAV is deleted); --wav keeps WAV. Needs ffmpeg.
# --delete removes exactly the copied files on the HA host afterwards (never the live database).
# Replay them offline: uv run python scripts/replay_recording.py <folder> --report --oracle

source "$(dirname "$0")/_common.sh"

DELETE=0
KEEP_WAV=0
IDLE_MINUTES=2
while [[ $# -gt 0 ]]; do
  case "$1" in
    --delete) DELETE=1 ;;
    --wav) KEEP_WAV=1 ;;
    --idle-minutes) shift; IDLE_MINUTES="${1:-}" ;;
    -h | --help) sed -n '2,14p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done
[[ "$IDLE_MINUTES" =~ ^[0-9]+$ ]] || die "--idle-minutes needs a whole number"
if [[ $KEEP_WAV == 0 ]] && ! command -v ffmpeg >/dev/null; then
  die "ffmpeg not found (brew install ffmpeg), or use --wav"
fi

resolve_container
resolve_runtime
REC_DIR="$MA_DATA_DIR/$PACKAGE/recordings"
DB="$MA_DATA_DIR/$PACKAGE/fingerprints.sqlite3"
[[ "$REC_DIR" == */audd_local_recognizer/recordings ]] || die "refusing recordings dir $REC_DIR"

log "Looking for finished recording sessions on $MA_CONTAINER (idle >= ${IDLE_MINUTES} min)"
names="$(remote "$DOCKER exec -i '$MA_CONTAINER' '$CONTAINER_PY' -" <<PY
import os, re, time
from collections import defaultdict
folder = "$REC_DIR"
groups = defaultdict(list)
for name in os.listdir(folder) if os.path.isdir(folder) else []:
    if re.fullmatch(r"\d{8}-\d{6}(_\d\d\.wav|_db\.sqlite3|\.log)", name):
        groups[name[:15]].append(name)
now = time.time()
for started, files in sorted(groups.items()):
    newest = max(os.path.getmtime(os.path.join(folder, name)) for name in files)
    if now - newest >= $IDLE_MINUTES * 60:
        print("\n".join(sorted(files)))
PY
)"
if [[ -z "$names" ]]; then
  log "Nothing to fetch (no finished recordings; a running one is fetched after it ends)"
  exit 0
fi
while IFS= read -r name; do
  [[ "$name" =~ ^[0-9]{8}-[0-9]{6}(_[0-9]{2}\.wav|_db\.sqlite3|\.log)$ ]] || die "unexpected file '$name'"
done <<<"$names"
name_args="$(tr '\n' ' ' <<<"$names")"

BASE="${RECORDINGS_DIR:-$REPO_ROOT/debug-recordings}"
DEST="$BASE/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$DEST"

log "Copying $(wc -l <<<"$names" | tr -d ' ') files from $MA_CONTAINER:$REC_DIR"
remote "$DOCKER exec '$MA_CONTAINER' tar -C '$REC_DIR' -cf - $name_args" | tar -xf - -C "$DEST"

log "Copying a consistent snapshot of the fingerprint database"
remote "$DOCKER exec -i '$MA_CONTAINER' '$CONTAINER_PY' -" \
  > "$DEST/fingerprints.sqlite3" <<PY
import sqlite3, sys, tempfile, os
src = sqlite3.connect("file:$DB?mode=ro", uri=True)
fd, tmp = tempfile.mkstemp(suffix=".sqlite3"); os.close(fd)
dst = sqlite3.connect(tmp); src.backup(dst); dst.close(); src.close()
sys.stdout.buffer.write(open(tmp, "rb").read()); os.unlink(tmp)
PY

pcm_sum() { ffmpeg -nostdin -loglevel error -i "$1" -f s16le - | shasum | cut -d' ' -f1; }
if [[ $KEEP_WAV == 0 ]]; then
  log "Compressing to FLAC (lossless, verified)"
  for wav in "$DEST"/*.wav; do
    [[ -e "$wav" ]] || continue
    flac="${wav%.wav}.flac"
    ffmpeg -nostdin -loglevel error -y -i "$wav" -compression_level 8 "$flac"
    if [[ "$(pcm_sum "$wav")" == "$(pcm_sum "$flac")" ]]; then
      rm -f "$wav"
    else
      rm -f "$flac"
      warn "FLAC of $(basename "$wav") differs from the WAV; kept the WAV"
    fi
  done
fi

segments="$(find "$DEST" \( -name '*.wav' -o -name '*.flac' \) | wc -l | tr -d ' ')"
sessions="$(find "$DEST" -name '*_01.*' \( -name '*.wav' -o -name '*.flac' \) | wc -l | tr -d ' ')"
logs="$(find "$DEST" -name '*.log' | wc -l | tr -d ' ')"
log "$sessions sessions, $segments segments, $logs session logs + database snapshot in $DEST ($(du -sh "$DEST" | cut -f1))"

if [[ $DELETE == 1 ]]; then
  log "Deleting the copied files on the HA host"
  remote "$DOCKER exec '$MA_CONTAINER' sh -c 'cd $REC_DIR && rm -f $name_args'"
fi
log "Replay offline: uv run python scripts/replay_recording.py $DEST --report --oracle"
