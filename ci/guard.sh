#!/bin/bash
# Decide whether this run of a workflow should start its long job.   ci/guard.sh <workflow file>
# A dispatched run always starts: it is the previous loop handing over, or a person. A scheduled run is the
# watchdog: it starts only when no other run of the workflow is queued or in progress. Writes run=1|0 to
# $GITHUB_OUTPUT. If the API cannot be read it starts: the job's concurrency group still keeps loops apart.
set -u
WF="$1"
EVENT="${EVENT:-${GITHUB_EVENT_NAME:-workflow_dispatch}}"
run=1
if [ "$EVENT" = "schedule" ]; then
  others=$(gh api "repos/$GITHUB_REPOSITORY/actions/workflows/$WF/runs?per_page=40" \
    --jq "[.workflow_runs[] | select(.status != \"completed\") | select(.id != ${GITHUB_RUN_ID:-0})] | length" 2>/dev/null)
  if [ -z "$others" ]; then
    echo "could not list runs of $WF: starting (the concurrency group prevents a second loop)"
  elif [ "$others" -gt 0 ]; then
    echo "watchdog: $others other run(s) of $WF are active, nothing to do"
    run=0
  else
    echo "watchdog: no active run of $WF, starting one"
  fi
else
  echo "$EVENT: starting"
fi
echo "run=$run" >> "${GITHUB_OUTPUT:-/dev/stdout}"
