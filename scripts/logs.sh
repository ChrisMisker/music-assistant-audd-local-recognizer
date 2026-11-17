#!/usr/bin/env bash
# Show this provider's log lines from the Music Assistant container.
#
#   scripts/logs.sh [--follow] [--since 30m] [--all]
#
# --all shows every MA log line instead of only this provider's.

source "$(dirname "$0")/_common.sh"

FOLLOW="" SINCE="15m" FILTER=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    -f | --follow) FOLLOW="-f" ;;
    --since) SINCE="$2"; shift ;;
    --all) FILTER=0 ;;
    -h | --help) sed -n '2,6p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done

resolve_container
if [[ $FILTER == 1 ]]; then
  remote "$DOCKER logs $FOLLOW --since '$SINCE' '$MA_CONTAINER' 2>&1 \
    | grep --line-buffered -iE 'audd|local_recognizer|AudD \+ Local'"
else
  remote "$DOCKER logs $FOLLOW --since '$SINCE' '$MA_CONTAINER' 2>&1"
fi
