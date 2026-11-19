#!/usr/bin/env bash
# List learned tracks, or merge two tracks that are one recording (AudD named it in two ways).
#
#   scripts/merge-tracks.sh                     # list tracks: id, plays, references, length, name
#   scripts/merge-tracks.sh <keep_id> <drop_id> # merge drop into keep (asks you to type MERGE)
#
# The kept track keeps its name and metadata and only fills empty fields (e.g. the length) from
# the dropped one; all fingerprints are kept; the dropped name becomes an alias, so a later AudD
# hit with that name lands on the kept track. A copy of the database is saved next to it first
# (fingerprints.sqlite3.before-merge-<timestamp>). Needs the deployed provider (>= 0.9 dev).

source "$(dirname "$0")/_common.sh"

resolve_container
resolve_runtime
DB="$MA_DATA_DIR/$PACKAGE/fingerprints.sqlite3"
[[ "$DB" == */audd_local_recognizer/fingerprints.sqlite3 ]] || die "refusing database $DB"

container_py() { remote "$DOCKER exec -i '$MA_CONTAINER' '$CONTAINER_PY' -" "$@"; }

describe_tracks() {  # [ids...]
  container_py <<PY
import json, sqlite3
db = sqlite3.connect("file:$DB?mode=ro", uri=True)
ids = [int(i) for i in "$*".split()]
rows = db.execute("SELECT id, metadata_json FROM tracks ORDER BY id").fetchall()
print(f"{'id':>4} {'plays':>5} {'refs':>5} {'length':>6}  artist - title  [album]")
for track_id, meta in rows:
    if ids and track_id not in ids:
        continue
    m = json.loads(meta)
    plays, refs = db.execute(
        "SELECT COUNT(DISTINCT passage_id), COUNT(*) FROM reference_windows WHERE track_id=?",
        (track_id,),
    ).fetchone()
    length = f"{m['duration_seconds']:.0f}s" if m.get("duration_seconds") else "-"
    alias = f"  aliases={m['aliases']}" if m.get("aliases") else ""
    print(f"{track_id:>4} {plays:>5} {refs:>5} {length:>6}  {m.get('artist')} - {m.get('title')}  [{m.get('album')}]{alias}")
PY
}

if [[ $# -eq 0 ]]; then
  describe_tracks
  exit 0
fi
[[ $# -eq 2 && "$1" =~ ^[0-9]+$ && "$2" =~ ^[0-9]+$ && "$1" != "$2" ]] \
  || die "usage: $0 [<keep_id> <drop_id>] (two different track ids)"
KEEP="$1"
DROP="$2"

log "Keep track $KEEP, merge track $DROP into it:"
shown="$(describe_tracks "$KEEP" "$DROP")"
printf '%s\n' "$shown"
[[ "$(wc -l <<<"$shown")" -eq 3 ]] || die "track $KEEP or $DROP does not exist"

read -r -p "Type MERGE to merge track $DROP into track $KEEP: " answer
[[ "$answer" == "MERGE" ]] || die "aborted, nothing changed"

STAMP="$(date +%Y%m%d-%H%M%S)"
container_py <<PY
import sqlite3
from music_assistant.providers.audd_local_recognizer.database import FingerprintDatabase
from pathlib import Path
src = sqlite3.connect("file:$DB?mode=ro", uri=True)
dst = sqlite3.connect("$DB.before-merge-$STAMP")
src.backup(dst); dst.close(); src.close()
moved = FingerprintDatabase(Path("$DB")).merge_tracks($KEEP, $DROP)
print(f"moved {moved} references; backup: $DB.before-merge-$STAMP")
PY
describe_tracks "$KEEP"
log "Done. No restart needed: the provider reads tracks from the database on every lookup."
