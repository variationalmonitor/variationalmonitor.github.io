#!/bin/bash
# Last step of the capture loop: start the next loop.   env: STRIKES, LOOP_OUTCOME, GH_TOKEN, GITHUB_REPOSITORY
# A loop that failed counts a strike; after three in a row the chain stops re-dispatching itself and the
# hourly watchdog cron restarts it, so a broken loop cannot spin. To stop the chain on purpose, disable the
# workflow first (gh workflow disable capture.yml), then cancel the run: the dispatch below then fails.
set -u
R="$GITHUB_REPOSITORY"
strikes="${STRIKES:-0}"
case "$strikes" in ''|*[!0-9]*) strikes=0 ;; esac
# GitHub disables workflows with a schedule after 60 days without repository activity. Undo only that state.
# publish.yml is looked at only when the loop dispatches it (VM_PUBLISH=on); otherwise it may not exist.
wfs="capture.yml"
[ "${VM_PUBLISH:-}" = "on" ] && wfs="$wfs publish.yml"
for wf in $wfs; do
  state=$(gh api "repos/$R/actions/workflows/$wf" --jq .state 2>/dev/null)
  if [ "$state" = "disabled_inactivity" ]; then
    echo "$wf was disabled for inactivity: enabling it again"
    gh workflow enable "$wf" -R "$R" || true
  fi
done
if [ "${LOOP_OUTCOME:-failure}" = "success" ]; then
  strikes=0
else
  strikes=$((strikes + 1))
  echo "the loop ended with '${LOOP_OUTCOME:-unknown}': strike $strikes"
  sleep 60
fi
if [ "$strikes" -ge 3 ]; then
  echo "three failed loops in a row: not re-dispatching; the watchdog cron starts the next one"
  exit 1
fi
for try in 1 2 3 4; do
  if gh workflow run capture.yml -R "$R" -f strikes="$strikes"; then
    echo "next loop dispatched (strikes=$strikes)"
    exit 0
  fi
  echo "dispatch failed (try $try)"
  sleep $((try * 20))
done
echo "could not dispatch the next loop; the watchdog cron starts it"
exit 1
