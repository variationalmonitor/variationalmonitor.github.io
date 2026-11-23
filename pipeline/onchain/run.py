#!/usr/bin/env python3
"""One-command runs for the on-chain pipeline. Standard library only.

  python3 run.py local     incremental index of the full local store, then rebuild every output
                           (resumes from .cache/onchain/state when it exists, otherwise full rebuild)
  python3 run.py ci        CI run from the compact state directory only (no full SQLite needed):
                           bootstrap a small store from <state-dir>, index the new blocks, fold them
                           into the saved aggregator state, write data/onchain/*.json and the new state
  python3 run.py rebuild   full rebuild of outputs + state from the local store (no indexing)
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import common as C  # noqa: E402

PY = sys.executable or "python3"


def sh(*cmd):
    print("+", " ".join(cmd), flush=True)
    subprocess.run(list(cmd), check=True)


def run_aggregate(agg, idx, db_args, out, state_in, state_out, reconcile):
    """Fold + reconcile. When pools fail to reconcile, fetch their direct transfers and fold again."""
    tmp = state_out + ".next"
    base = [PY, agg] + db_args + ["--out", out, "--state-out", tmp]
    if state_in:
        base += ["--state-in", state_in]
    if reconcile:
        base.append("--reconcile")
    print("+", " ".join(base), flush=True)
    code = subprocess.run(base).returncode
    if code == 3:
        sh(PY, idx, *db_args, "repair")
        print("+", " ".join(base), flush=True)
        code = subprocess.run(base).returncode
    if code not in (0, 3):
        raise SystemExit("aggregate failed with exit code %d" % code)
    os.replace(tmp, state_out)
    if code == 3:
        print("WARNING: balances still do not reconcile after repair; see reconciliation in data/onchain/pools_snapshot.json", flush=True)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("local", "ci", "rebuild"))
    parser.add_argument("--state-dir", default=os.path.join(C.CACHE_DIR, "state"))
    parser.add_argument("--out", default=C.DATA_DIR)
    parser.add_argument("--tenderly", default="3")
    parser.add_argument("--arbitrum", default="2")
    parser.add_argument("--sequence", default="1")
    parser.add_argument("--no-reconcile", action="store_true")
    args = parser.parse_args()
    idx, agg = os.path.join(HERE, "indexer.py"), os.path.join(HERE, "aggregate.py")
    state = os.path.join(args.state_dir, "agg_state.json.gz")
    os.makedirs(args.state_dir, exist_ok=True)
    workers = ["--tenderly", args.tenderly, "--arbitrum", args.arbitrum, "--sequence", args.sequence]

    if args.mode == "ci":
        db = os.path.join(args.state_dir, "ci_window.sqlite")
        for suffix in ("", "-wal", "-shm", ".lock"):
            if os.path.exists(db + suffix):
                os.remove(db + suffix)
        sh(PY, idx, "--db", db, "bootstrap", "--state-dir", args.state_dir)
        sh(PY, idx, "--db", db, "update", *workers)
        code = run_aggregate(agg, idx, ["--db", db], args.out, state, state, not args.no_reconcile)
        sh(PY, idx, "--db", db, "export-registry", "--state-dir", args.state_dir)
        for suffix in ("", "-wal", "-shm", ".lock"):
            if os.path.exists(db + suffix):
                os.remove(db + suffix)
        return 4 if code == 3 else 0
    if args.mode == "local":
        sh(PY, idx, "update", *workers)
    resume = state if (args.mode == "local" and os.path.exists(state)) else None
    code = run_aggregate(agg, idx, [], args.out, resume, state, not args.no_reconcile)
    sh(PY, idx, "export-registry", "--state-dir", args.state_dir)
    return 4 if code == 3 else 0


if __name__ == "__main__":
    sys.exit(main())
