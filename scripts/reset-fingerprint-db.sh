#!/usr/bin/env bash
# DESTRUCTIVE, development only: delete this provider's local fingerprint database.
#
#   scripts/reset-fingerprint-db.sh
#
# Deletes only fingerprint*.sqlite3 (+ -wal/-shm) inside
# $MA_DATA_DIR/audd_local_recognizer in the MA container, after showing the files and
# asking you to type RESET. Then restarts MA so the provider recreates an empty schema.
# Everything learned is lost; AudD will be needed again for every track.

source "$(dirname "$0")/_common.sh"

resolve_container
DB_DIR="$MA_DATA_DIR/$PACKAGE"
[[ "$DB_DIR" == */audd_local_recognizer ]] || die "refusing DB dir $DB_DIR"

log "Container: $MA_CONTAINER"
log "Fingerprint DB files in $DB_DIR:"
FILES="$(remote "$DOCKER exec '$MA_CONTAINER' sh -c 'ls -la $DB_DIR/fingerprint*.sqlite3* 2>/dev/null'" || true)"
if [[ -z "$FILES" ]]; then
  log "No fingerprint database found; nothing to do."
  exit 0
fi
printf '%s\n' "$FILES"

read -r -p "Type RESET to permanently delete these files and restart MA: " answer
[[ "$answer" == "RESET" ]] || die "aborted, nothing deleted"

remote "$DOCKER exec '$MA_CONTAINER' sh -c 'rm -f $DB_DIR/fingerprint*.sqlite3 $DB_DIR/fingerprint*.sqlite3-wal $DB_DIR/fingerprint*.sqlite3-shm'"
log "Deleted. Restarting $MA_CONTAINER"
remote "$DOCKER restart '$MA_CONTAINER'" >/dev/null
log "Done. The provider starts with an empty database."
