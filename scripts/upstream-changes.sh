#!/usr/bin/env bash
# Show what changed upstream in Music Assistant that may matter for this provider.
#
#   scripts/upstream-changes.sh            # since the last check (docs/upstream-checked.txt)
#   scripts/upstream-changes.sh --mark     # ... and record the current upstream state as checked
#   scripts/upstream-changes.sh --force    # check again although it was checked today
#
# Meant to run once a day (at the start of a session and before a deploy or release): it stops
# early when the last --mark was today. Lists recent MA server releases (nightly/beta/stable)
# and the commits on the server's dev branch since the last checked commit that touch what we
# depend on: the audio-analysis provider API, live-source analysis, Sendspin, provider loading
# and the community catalog. Also shows commits in the community-catalog repository (rules and
# the check-provider workflow), and the OPEN pull requests of the server that touch those paths
# (what is coming, before it is merged) and of the community catalog. Read-only apart from --mark;
# needs `gh`.

set -euo pipefail
cd "$(dirname "$0")/.."

STATE="docs/upstream-checked.txt"
SERVER="music-assistant/server"
CATALOG="music-assistant/community-catalog"
PATHS=(
  "music_assistant/models/audio_analysis_provider.py"
  "music_assistant/models/audio_analysis.py"
  "music_assistant/controllers/streams/audio_analysis.py"
  "music_assistant/controllers/community_catalog"
  "music_assistant/models/provider.py"
  "music_assistant/providers/sendspin"
  "music_assistant/providers/sendspin_source"
)

last_server=""
last_catalog=""
if [[ -f "$STATE" ]]; then
  if [[ " $* " != *" --force "* ]] && grep -q "^checked=$(date +%Y-%m-%d)" "$STATE"; then
    echo "Upstream already checked today ($STATE); use --force to check again."
    exit 0
  fi
  last_server="$(grep '^server=' "$STATE" | cut -d= -f2 || true)"
  last_catalog="$(grep '^catalog=' "$STATE" | cut -d= -f2 || true)"
fi

echo "== Music Assistant server releases (newest first)"
gh release list -R "$SERVER" -L 8 --json tagName,publishedAt,isPrerelease \
  -q '.[] | "  \(.publishedAt[0:10])  \(.tagName)\(if .isPrerelease then "  (pre-release)" else "" end)"'

head_server="$(gh api "repos/$SERVER/commits/dev" -q .sha)"  # MA server develops on "dev"
echo
echo "== Relevant server commits since ${last_server:0:7}${last_server:+ }(dev is ${head_server:0:7})"
since=""
if [[ -n "$last_server" ]]; then
  since="$(gh api "repos/$SERVER/commits/$last_server" -q .commit.committer.date)"
fi
for path in "${PATHS[@]}"; do
  gh api -X GET "repos/$SERVER/commits" -f sha=dev -f path="$path" -f per_page=30 ${since:+-f since="$since"} \
    -q '.[] | "\(.sha[0:7])  \(.commit.committer.date[0:10])  \(.commit.message | split("\n")[0])"' \
    | while read -r line; do echo "  [$path] $line"; done
done | sort -u -k2,2 | grep -v "^  \[[^]]*\] ${last_server:0:7} " || echo "  (none)"

head_catalog="$(gh api "repos/$CATALOG/commits/main" -q .sha)"
echo
echo "== Community catalog commits since ${last_catalog:0:7}${last_catalog:+ }(main is ${head_catalog:0:7})"
csince=""
if [[ -n "$last_catalog" ]]; then
  csince="$(gh api "repos/$CATALOG/commits/$last_catalog" -q .commit.committer.date)"
fi
gh api -X GET "repos/$CATALOG/commits" -f per_page=15 ${csince:+-f since="$csince"} \
  -q '.[] | "  \(.sha[0:7])  \(.commit.committer.date[0:10])  \(.commit.message | split("\n")[0])"' \
  | grep -v "^  ${last_catalog:0:7} " || echo "  (none)"

echo
echo "== Open server pull requests that touch those paths (not merged yet: what is coming)"
gh pr list -R "$SERVER" --state open --limit 200 --json number,title,updatedAt,isDraft,files \
  | PATHS="${PATHS[*]}" python3 -c '
import json, os, sys
paths = os.environ["PATHS"].split()
hits = []
for pr in json.load(sys.stdin):
    touched = sorted({p for f in pr["files"] for p in paths if f["path"].startswith(p)})
    if touched:
        draft = "  (draft)" if pr["isDraft"] else ""
        hits.append("  #%s  %s  %s%s  [%s]" % (pr["number"], pr["updatedAt"][:10], pr["title"], draft, ", ".join(touched)))
print("\n".join(hits) if hits else "  (none)")
'

echo
echo "== Open community catalog pull requests"
gh pr list -R "$CATALOG" --state open --limit 30 --json number,title,updatedAt \
  -q '.[] | "  #\(.number)  \(.updatedAt[0:10])  \(.title)"' | grep . || echo "  (none)"

echo
echo "== Watched discussions (comments, upvotes, last activity)"
# owner/repo#number of discussions we opened or follow
WATCHED_DISCUSSIONS=(
  "esphome/feature-requests#3973"  # usb_audio microphone input gain (Lenco +10 dB clipping)
)
for item in "${WATCHED_DISCUSSIONS[@]}"; do
  repo="${item%#*}" number="${item##*#}"
  gh api graphql -f query="query{repository(owner:\"${repo%/*}\",name:\"${repo#*/}\"){discussion(number:$number){title updatedAt upvoteCount comments{totalCount}}}}" \
    -q ".data.repository.discussion | \"  $item  \(.updatedAt[0:10])  \(.comments.totalCount) comments, \(.upvoteCount) upvotes  \(.title)\"" \
    || echo "  $item  (could not read)"
done

if [[ " $* " == *" --mark "* ]]; then
  printf 'server=%s\ncatalog=%s\nchecked=%s\n' "$head_server" "$head_catalog" "$(date +%Y-%m-%d)" > "$STATE"
  echo
  echo "Recorded as checked in $STATE"
fi
