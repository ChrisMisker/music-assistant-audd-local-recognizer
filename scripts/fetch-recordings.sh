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
# Every file is checked against a SHA-256 taken on the host and fetched again if it differs
# (up to 3 times). --delete removes exactly the copied files on the HA host afterwards, and only
# when every file arrived intact (never the live database).
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
    -h | --help) sed -n '2,15p' "$0"; exit 0 ;;
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

ATTEMPTS=3
container_py() { remote "$DOCKER exec -i '$MA_CONTAINER' '$CONTAINER_PY' -"; }
local_sha() { shasum -a 256 "$1" | cut -d' ' -f1; }

# Sizes and SHA-256 of the files on the HA host, computed inside the container: every copy is
# checked against them, and a copy that differs (a dropped SSH stream, 2026-11-22/23) is fetched
# again, at most $ATTEMPTS times.
log "Checksumming $(wc -l <<<"$names" | tr -d ' ') files on $MA_CONTAINER"
sums="$(container_py <<PY
import hashlib, os
for name in """$names""".split():
    h = hashlib.sha256()
    with open(os.path.join("$REC_DIR", name), "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    print(name, h.hexdigest())
PY
)"
[[ "$(wc -l <<<"$sums")" == "$(wc -l <<<"$names")" ]] || die "could not checksum the recordings"

# fetch_verified <local file> <expected sha256> <command that writes the file to stdout...>
fetch_verified() {
  local dest="$1" expected="$2" try
  shift 2
  for ((try = 1; try <= ATTEMPTS; try++)); do
    if "$@" >"$dest" && [[ "$(local_sha "$dest")" == "$expected" ]]; then
      return 0
    fi
    warn "$(basename "$dest"): copy $try/$ATTEMPTS incomplete or different, retrying"
  done
  rm -f "$dest"
  return 1
}

log "Copying and verifying the files"
failed=()
while read -r name sha; do
  fetch_verified "$DEST/$name" "$sha" remote "$DOCKER exec '$MA_CONTAINER' cat '$REC_DIR/$name'" ||
    failed+=("$name")
done <<<"$sums"

log "Copying a consistent, verified snapshot of the fingerprint database"
SNAP="$REC_DIR/.snapshot-$(date +%s).sqlite3"
snap_sha="$(container_py <<PY
import hashlib, sqlite3
src = sqlite3.connect("file:$DB?mode=ro", uri=True)
dst = sqlite3.connect("$SNAP"); src.backup(dst); dst.close(); src.close()
print(hashlib.sha256(open("$SNAP", "rb").read()).hexdigest())
PY
)"
fetch_verified "$DEST/fingerprints.sqlite3" "$snap_sha" \
  remote "$DOCKER exec '$MA_CONTAINER' cat '$SNAP'" || failed+=("database snapshot")
remote "$DOCKER exec '$MA_CONTAINER' rm -f '$SNAP'" || warn "could not remove $SNAP on the host"

if [[ ${#failed[@]} -gt 0 ]]; then
  warn "Not copied after $ATTEMPTS tries: ${failed[*]}"
  warn "Nothing was deleted on the HA host; run the script again for a complete copy"
  DELETE=0
fi

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
  log "Deleting the copied (verified) files on the HA host"
  remote "$DOCKER exec '$MA_CONTAINER' sh -c 'cd $REC_DIR && rm -f $name_args'"
fi
log "Replay offline: uv run python scripts/replay_recording.py $DEST --report --oracle"
