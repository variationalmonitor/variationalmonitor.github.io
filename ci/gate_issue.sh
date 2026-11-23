#!/bin/bash
# Keep exactly one GitHub issue about a failing gate.   ci/gate_issue.sh <pass|fail> <report.md>
# fail: open the issue, or update its body when it is already open.  pass: close it with a comment.
set -u
OUTCOME="$1"; REPORT="${2:-qa/out/report.md}"
LABEL="qa-gate"
TITLE="Deploy gate failing, site held at the last good build"
RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}"
gh label create "$LABEL" --color B60205 --description "The deploy gate is failing; the site is held at its last good build" 2>/dev/null || true
N=$(gh issue list --label "$LABEL" --state open --json number --jq '.[0].number // empty' 2>/dev/null)
if [ "$OUTCOME" = "fail" ]; then
  BODY="$(mktemp)"
  { if [ -s "$REPORT" ]; then head -c 60000 "$REPORT"; else echo "The gate did not produce a report."; fi
    printf '\n\nRun: %s\n' "$RUN_URL"; } > "$BODY"
  if [ -z "$N" ]; then gh issue create --title "$TITLE" --label "$LABEL" --body-file "$BODY"
  else gh issue edit "$N" --body-file "$BODY"; fi
elif [ -n "$N" ]; then
  gh issue close "$N" --comment "Gate passed at $(date -u +'%Y-%m-%d %H:%M') UTC; the site is current again. Run: $RUN_URL"
fi
