#!/usr/bin/env bash
# Give a learned track another artist and title (AudD named it oddly, e.g. "The Bee Gees - I Want
# Home (false)").
#
#   scripts/rename-track.sh <id> "<artist>" "<title>"   # asks you to type RENAME
#
# Track ids come from scripts/merge-tracks.sh (without arguments it lists all tracks). The old name
# becomes an alias, so a later AudD hit with it lands on this track. When the new name is already
# another track's, nothing changes: merge the two with scripts/merge-tracks.sh instead. A copy of
# the database is saved next to it first (fingerprints.sqlite3.before-rename-<timestamp>). No
# restart needed: the provider reads tracks from the database on every lookup.

source "$(dirname "$0")/_common.sh"

# keep the confirmation input for read; ssh calls below must not swallow it
exec 3<&0 0</dev/null

[[ $# -eq 3 && "$1" =~ ^[0-9]+$ && -n "$2" && -n "$3" ]] \
  || die "usage: $0 <track id> \"<artist>\" \"<title>\""
ID="$1"
# as Python string literals (JSON strings are valid ones), so any quote in a name is safe
ARTIST="$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$2")"
TITLE="$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$3")"

resolve_container
resolve_runtime
DB="$MA_DATA_DIR/$PACKAGE/fingerprints.sqlite3"
[[ "$DB" == */audd_local_recognizer/fingerprints.sqlite3 ]] || die "refusing database $DB"

container_py() { remote "$DOCKER exec -i '$MA_CONTAINER' '$CONTAINER_PY' -" "$@"; }

# $1: "check" (read-only) or "apply"; prints the current and the new name, or an error
rename() {
  container_py <<PY
import json, sqlite3
from music_assistant.providers.audd_local_recognizer.database import ALIASES, track_key
mode, track_id, artist, title = "$1", $ID, $ARTIST, $TITLE
uri = "file:$DB" + ("?mode=ro" if mode == "check" else "")
db = sqlite3.connect(uri, uri=True)
row = db.execute("SELECT track_key, metadata_json FROM tracks WHERE id=?", (track_id,)).fetchone()
if row is None:
    print(f"ERROR: no track {track_id}")
    raise SystemExit(0)
old_key, metadata = row[0], json.loads(row[1])
renamed = {**metadata, "artist": artist, "title": title}
new_key = track_key(renamed)
other = db.execute("SELECT id FROM tracks WHERE track_key=? AND id<>?", (new_key, track_id)).fetchone()
if other:
    print(f"ERROR: track {other[0]} already has that name: merge-tracks.sh {other[0]} {track_id}")
    raise SystemExit(0)
print(f"track {track_id}: {metadata.get('artist')} - {metadata.get('title')}  ->  {artist} - {title}")
if mode == "apply":
    aliases = set(metadata.get(ALIASES, [])) | ({old_key} - {new_key})
    if aliases:
        renamed[ALIASES] = sorted(aliases)
    with db:
        db.execute(
            "UPDATE tracks SET track_key=?, metadata_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (new_key, json.dumps(renamed, ensure_ascii=False, separators=(",", ":")), track_id),
        )
    print("renamed; the old name is an alias now")
PY
}

shown="$(rename check)"
printf '%s\n' "$shown"
[[ "$shown" != *ERROR* ]] || die "nothing changed"

read -r -u 3 -p "Type RENAME to rename track $ID: " answer
[[ "$answer" == "RENAME" ]] || die "aborted, nothing changed"

STAMP="$(date +%Y%m%d-%H%M%S)"
container_py <<PY
import sqlite3
src = sqlite3.connect("file:$DB?mode=ro", uri=True)
dst = sqlite3.connect("$DB.before-rename-$STAMP")
src.backup(dst); dst.close(); src.close()
print("backup: $DB.before-rename-$STAMP")
PY
rename apply
log "Done. No restart needed: the provider reads tracks from the database on every lookup."
