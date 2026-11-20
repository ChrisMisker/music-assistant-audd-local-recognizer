#!/usr/bin/env bash
# Delete learned tracks (and all their fingerprints) from the fingerprint database on the HA host.
#
#   scripts/delete-tracks.sh <id> [<id>...]   # asks you to type DELETE
#
# For a track AudD named wrongly (e.g. a content-farm upload) or a record to learn again from
# scratch. Track ids come from scripts/merge-tracks.sh (without arguments it lists all tracks). A
# copy of the database is saved next to it first (fingerprints.sqlite3.before-delete-<timestamp>).
# No restart needed: the provider reads tracks from the database on every lookup.

source "$(dirname "$0")/_common.sh"

# keep the confirmation input for read; ssh calls below must not swallow it
exec 3<&0 0</dev/null

[[ $# -ge 1 ]] || die "usage: $0 <track id> [<track id>...]"
for id in "$@"; do
  [[ "$id" =~ ^[0-9]+$ ]] || die "not a track id: $id"
done
IDS="$*"

resolve_container
resolve_runtime
DB="$MA_DATA_DIR/$PACKAGE/fingerprints.sqlite3"
[[ "$DB" == */audd_local_recognizer/fingerprints.sqlite3 ]] || die "refusing database $DB"

container_py() { remote "$DOCKER exec -i '$MA_CONTAINER' '$CONTAINER_PY' -" "$@"; }

describe() {
  container_py <<PY
import json, sqlite3
db = sqlite3.connect("file:$DB?mode=ro", uri=True)
ids = [int(i) for i in "$IDS".split()]
for track_id in ids:
    row = db.execute("SELECT metadata_json FROM tracks WHERE id=?", (track_id,)).fetchone()
    if row is None:
        print(f"MISSING {track_id}")
        continue
    m = json.loads(row[0])
    plays, refs = db.execute(
        "SELECT COUNT(DISTINCT passage_id), COUNT(*) FROM reference_windows WHERE track_id=?",
        (track_id,),
    ).fetchone()
    print(f"{track_id:>4} {plays:>5} plays {refs:>5} refs  {m.get('artist')} - {m.get('title')}")
PY
}

log "Tracks to delete:"
shown="$(describe)"
printf '%s\n' "$shown"
grep -q "^MISSING" <<<"$shown" && die "a track id does not exist; nothing changed"

read -r -u 3 -p "Type DELETE to delete these tracks and their fingerprints: " answer
[[ "$answer" == "DELETE" ]] || die "aborted, nothing changed"

STAMP="$(date +%Y%m%d-%H%M%S)"
container_py <<PY
import sqlite3
src = sqlite3.connect("file:$DB?mode=ro", uri=True)
dst = sqlite3.connect("$DB.before-delete-$STAMP")
src.backup(dst); dst.close(); src.close()
db = sqlite3.connect("$DB")
db.execute("PRAGMA foreign_keys=ON")  # references and landmarks go with their track
ids = [int(i) for i in "$IDS".split()]
with db:
    db.executemany("DELETE FROM tracks WHERE id=?", [(i,) for i in ids])
orphans = db.execute(
    "SELECT COUNT(*) FROM landmarks WHERE reference_id NOT IN (SELECT id FROM reference_windows)"
).fetchone()[0]
print(f"deleted {len(ids)} track(s); orphan landmarks: {orphans}; backup: $DB.before-delete-$STAMP")
PY
log "Done."
