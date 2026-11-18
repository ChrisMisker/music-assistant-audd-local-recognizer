#!/usr/bin/env bash
# Report recognition behaviour from the Music Assistant log on the HA host.
#
#   scripts/vote-report.sh [--since 3h]
#
# Per passage (track play): how and after how many seconds it was identified, AudD calls, and
# the votes of every local candidate; then totals and the vote distribution (real matches versus
# accidental collisions). Note: `docker logs` only covers the current container; a container
# restart or add-on update starts a new log.

source "$(dirname "$0")/_common.sh"

SINCE="3h"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --since) SINCE="$2"; shift ;;
    -h | --help) sed -n '2,9p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done

resolve_container
remote "$DOCKER logs --since '$SINCE' '$MA_CONTAINER' 2>&1" \
  | grep -iE 'AudD \+ Local|audd_local_recognizer' \
  | (cd "$REPO_ROOT" && uv run --quiet python scripts/vote_report.py)
