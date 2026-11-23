#!/bin/bash
# Paths for a run against a checkout of the data branch. Source it:   . ci/env.sh [data-checkout]
# The builders read these and fall back to the local layout (.cache/store, .cache/snapshots, data/onchain) without them.
#   VM_STORE          the store: the data branch checkout itself
#   VM_SNAPSHOTS      raw Omni snapshots (seed folder, then the live folder), separated by ":"
#   VM_ONCHAIN_DATA   the indexer's daily aggregates that build_chain.py adapts
#   VM_ONCHAIN_WORK   scratch for the on-chain increment, outside the checkout (its state subfolder is what
#                     the Actions cache keeps, and what `run.py ci --state-dir` is given)
_vm_dir="${1:-${VM_STORE:-$PWD/.store}}"
VM_STORE="$(cd "$_vm_dir" 2>/dev/null && pwd || echo "$_vm_dir")"
VM_SNAPSHOTS="$VM_STORE/snapshots_seed:$VM_STORE/snapshots"
VM_ONCHAIN_DATA="$VM_STORE/onchain/aggregates"
VM_ONCHAIN_WORK="${VM_ONCHAIN_WORK:-${RUNNER_TEMP:-${TMPDIR:-/tmp}}/vm-onchain}"
VM_ONCHAIN_STATE="$VM_ONCHAIN_WORK/state"
export VM_STORE VM_SNAPSHOTS VM_ONCHAIN_DATA VM_ONCHAIN_WORK VM_ONCHAIN_STATE
if [ -n "${GITHUB_ENV:-}" ]; then
  for _v in VM_STORE VM_SNAPSHOTS VM_ONCHAIN_DATA VM_ONCHAIN_WORK VM_ONCHAIN_STATE; do
    eval "printf '%s=%s\\n' \"$_v\" \"\$$_v\"" >> "$GITHUB_ENV"
  done
fi
unset _vm_dir _v
