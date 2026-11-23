#!/usr/bin/env python3
"""The on-chain increment for the publish job. Python standard library only.

    ci/onchain.py increment     run the indexer's CI increment when a new UTC day is complete, adopt its outputs
    ci/onchain.py status        where the aggregates and the two copies of the state stand

Paths come from ci/env.sh: VM_STORE (data branch checkout), VM_ONCHAIN_DATA (= <store>/onchain/aggregates),
VM_ONCHAIN_STATE (working state, outside the checkout, kept by the Actions cache).

What lives where
  <store>/onchain/aggregates/*.json   the indexer's daily aggregates (about 0.4 MB). Committed with every
                                      successful increment, so any publish run can build the on-chain files.
  $VM_ONCHAIN_STATE                   registry.bin.gz, agg_state.json.gz, anchors.json (about 12 MB). Saved to
                                      the Actions cache after every increment: the fast path.
  <store>/onchain/state/              the same three files on the data branch, rewritten once every
                                      STATE_SAVE_DAYS: the durable copy. If the cache is lost the increment
                                      starts from here and re-reads at most that many days of blocks.

The increment runs at most once per UTC day: only when the aggregates' last complete day is before yesterday.
A failed run changes nothing; the next hourly publish tries again. The indexer's exit code 4 (balances not
reconciled after repair) still adopts the outputs: the gate reads the reconciliation and decides.
"""
import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATE_FILES = ("registry.bin.gz", "agg_state.json.gz", "anchors.json")
INFO = "STATE_INFO.json"
STATE_SAVE_DAYS = 7
TIMEOUT_S = 40 * 60


def log(msg):
    print(msg, flush=True)


def read_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def last_complete_day(aggregates):
    doc = read_json(os.path.join(aggregates, "daily.json"))
    return ((doc or {}).get("coverage") or {}).get("last_complete_day")


def state_day(folder):
    """The last complete day a state folder was saved at, or None when the folder is not a full state."""
    if not all(os.path.exists(os.path.join(folder, f)) for f in STATE_FILES):
        return None
    return (read_json(os.path.join(folder, INFO)) or {}).get("last_complete_day") or "0000-00-00"


def copy_state(src, dst):
    os.makedirs(dst, exist_ok=True)
    for name in STATE_FILES + (INFO,):
        if os.path.exists(os.path.join(src, name)):
            shutil.copy2(os.path.join(src, name), os.path.join(dst, name))


def paths():
    store = os.environ.get("VM_STORE")
    if not store:
        raise SystemExit("VM_STORE is not set: source ci/env.sh first")
    work = os.environ.get("VM_ONCHAIN_WORK") or os.path.join(os.environ.get("RUNNER_TEMP", "/tmp"), "vm-onchain")
    return {
        "store": store,
        "aggregates": os.environ.get("VM_ONCHAIN_DATA") or os.path.join(store, "onchain", "aggregates"),
        "branch_state": os.path.join(store, "onchain", "state"),
        "cache_state": os.environ.get("VM_ONCHAIN_STATE") or os.path.join(work, "state"),
        "run": os.path.join(work, "run"),
    }


def output(key, value):
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a") as fh:
            fh.write("%s=%s\n" % (key, value))
    log("  output %s=%s" % (key, value))


def status(_args):
    p = paths()
    log("aggregates   %s  last complete day %s" % (p["aggregates"], last_complete_day(p["aggregates"])))
    log("cache state  %s  %s" % (p["cache_state"], state_day(p["cache_state"])))
    log("branch state %s  %s" % (p["branch_state"], state_day(p["branch_state"])))
    return 0


def increment(args):
    p = paths()
    today = dt.datetime.utcnow().date() if not args.today else dt.date.fromisoformat(args.today)
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    have = last_complete_day(p["aggregates"])
    if have and have >= yesterday and not args.force:
        log("on-chain: complete through %s, nothing to do until the next UTC day" % have)
        output("updated", 0)
        return 0
    cache_day, branch_day = state_day(p["cache_state"]), state_day(p["branch_state"])
    if cache_day is None and branch_day is None:
        log("::warning::on-chain: no indexer state in the cache or on the data branch; the on-chain files cannot advance "
            "(seed <data>/onchain/state from .cache/onchain/state)")
        output("updated", 0)
        return 0
    source = p["cache_state"] if (cache_day or "") >= (branch_day or "") else p["branch_state"]
    log("on-chain: aggregates complete through %s; state from %s (%s; cache %s, branch %s)"
        % (have, "the cache" if source == p["cache_state"] else "the data branch", state_day(source), cache_day, branch_day))
    run_state, run_out = os.path.join(p["run"], "state"), os.path.join(p["run"], "out")
    shutil.rmtree(p["run"], ignore_errors=True)
    copy_state(source, run_state)
    os.makedirs(run_out, exist_ok=True)
    os.makedirs(os.path.join(ROOT, ".cache", "onchain"), exist_ok=True)   # the aggregator writes a diagnostic there
    runner = args.runner or [sys.executable or "python3", os.path.join(ROOT, "pipeline", "onchain", "run.py")]
    cmd = list(runner) + ["ci", "--state-dir", run_state, "--out", run_out]
    log("+ " + " ".join(cmd))
    try:
        code = subprocess.run(cmd, timeout=TIMEOUT_S).returncode
    except subprocess.TimeoutExpired:
        code = "timeout"
    new_day = last_complete_day(run_out)
    if code not in (0, 4) or not new_day or (have and new_day < have) or state_day(run_state) is None:
        log("::warning::on-chain increment failed (exit %s, last complete day %s); keeping the files through %s"
            % (code, new_day, have))
        output("updated", 0)
        return 0
    if code == 4:
        log("::warning::on-chain balances did not reconcile after repair; outputs adopted, the gate reads the reconciliation")
    # adopt: aggregates into the checkout, state into the cache folder, and into the checkout when the durable copy is due
    os.makedirs(p["aggregates"], exist_ok=True)
    for name in sorted(os.listdir(run_out)):
        if name.endswith(".json"):
            shutil.copy2(os.path.join(run_out, name), os.path.join(p["aggregates"], name))
    info = {"last_complete_day": new_day, "saved_at": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "as_of_block": (read_json(os.path.join(run_out, "daily.json")) or {}).get("as_of_block")}
    with open(os.path.join(run_state, INFO), "w") as fh:
        json.dump(info, fh, indent=1, sort_keys=True)
        fh.write("\n")
    shutil.rmtree(p["cache_state"], ignore_errors=True)
    copy_state(run_state, p["cache_state"])
    hk_path = os.path.join(p["store"], "housekeeping.json")
    hk = read_json(hk_path) or {}
    saved = hk.get("onchain_state_saved")
    due = branch_day is None or saved is None or (today - dt.date.fromisoformat(saved)).days >= STATE_SAVE_DAYS
    if due:
        copy_state(run_state, p["branch_state"])
        hk["onchain_state_saved"] = today.isoformat()
        with open(hk_path, "w") as fh:
            json.dump(hk, fh, indent=1, sort_keys=True)
            fh.write("\n")
    log("on-chain: advanced %s -> %s; state saved to the cache%s"
        % (have, new_day, " and to the data branch (durable copy)" if due else "; durable copy on the branch is from %s" % saved))
    output("updated", 1)
    shutil.rmtree(p["run"], ignore_errors=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("increment")
    p.add_argument("--force", action="store_true", help="run even if the aggregates are complete through yesterday")
    p.add_argument("--today", help="UTC date to plan against (testing)")
    p.add_argument("--runner", nargs="+", help="command to run instead of pipeline/onchain/run.py (testing)")
    sub.add_parser("status")
    args = parser.parse_args()
    return increment(args) if args.cmd == "increment" else status(args)


if __name__ == "__main__":
    sys.exit(main())
