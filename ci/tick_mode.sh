#!/bin/bash
# Decide whether this run of tick.yml captures for real or runs as a shadow.   env: VM_CAPTURE, GH_TOKEN, GITHUB_REPOSITORY
# Real only when the repository variable VM_CAPTURE is exactly "tick" and no run of capture.yml (the loop) is queued
# or in progress, so that the loop and the ticks never both call the Omni API. If the runs cannot be listed it
# captures: a missed boundary is lost for good, a doubled one is not (ci/datastore.py keeps the first snapshot).
# Writes shadow=0|1 to $GITHUB_OUTPUT.
set -u
shadow=1
if [ "${VM_CAPTURE:-}" != "tick" ]; then
  echo "VM_CAPTURE is '${VM_CAPTURE:-}', not 'tick': shadow run (no Omni call, nothing written)"
else
  active=$(gh api "repos/$GITHUB_REPOSITORY/actions/workflows/capture.yml/runs?per_page=40" \
    --jq '[.workflow_runs[] | select(.status != "completed")] | length' 2>/dev/null)
  case "$active" in
    ''|*[!0-9]*)
      echo "VM_CAPTURE=tick; could not list the runs of capture.yml: capturing"
      shadow=0 ;;
    0)
      echo "VM_CAPTURE=tick and no run of capture.yml is queued or in progress: capturing"
      shadow=0 ;;
    *)
      echo "VM_CAPTURE=tick, but $active run(s) of capture.yml are queued or in progress: shadow run, the loop captures" ;;
  esac
fi
echo "shadow=$shadow" >> "${GITHUB_OUTPUT:-/dev/stdout}"
