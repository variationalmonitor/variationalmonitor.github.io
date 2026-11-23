#!/usr/bin/python3
"""Run every builder in contract order. Python standard library only.

    /usr/bin/python3 pipeline/build.py              market, execution, chain, reports. Reports runs last: it reads
                                                    treasury.json from the chain builder (on-chain inflow per period,
                                                    spreads_daily.json) and writes the research list from its configs.
    /usr/bin/python3 pipeline/build.py --offline    the same without network (passed to the market and reports builders)
    /usr/bin/python3 pipeline/build.py --verbose    also print each builder's own output
    /usr/bin/python3 pipeline/build.py --only chain reports     run a subset, still in contract order

Stops at the first builder that fails (exit 1) and prints one line per step. Paths come from the environment,
with the local layout as default: VM_STORE, VM_SNAPSHOTS, VM_ONCHAIN_DATA, VM_SITE_DATA.

A new report. The market and chain builders read <store>/reports.json, which the reports builder rewrites at
the end of the run. When that file changes during a run (a biweekly report has just been read from Telegram),
every builder runs once more, offline, so that all files are built from the same reports: otherwise the first
build after a report would be inconsistent and the gate would hold it for an hour.
"""
import argparse
import hashlib
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# name, script, takes --offline
STEPS = [
    ("market", "build_market.py", True),
    ("execution", "build_execution.py", False),
    ("chain", "build_chain.py", False),
    ("reports", "build_reports.py", True),
]
TAIL = 40


def last_line(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return lines[-1][:150] if lines else ""


def reports_digest():
    """Digest of <store>/reports.json, or None when it does not exist yet."""
    store = os.environ.get("VM_STORE") or os.path.join(os.path.dirname(HERE), ".cache", "store")
    try:
        with open(os.path.join(store, "reports.json"), "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def run_pass(args, offline, label=""):
    """One pass over the builders in contract order. Returns False at the first failure."""
    for name, script, has_offline in STEPS:
        if args.only and name not in args.only:
            continue
        cmd = [sys.executable or "python3", os.path.join(HERE, script)]
        if offline and has_offline:
            cmd.append("--offline")
        t0 = time.time()
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        if args.verbose:
            sys.stdout.write(proc.stdout)
            if proc.stdout and not proc.stdout.endswith("\n"):
                sys.stdout.write("\n")
        status = "ok" if proc.returncode == 0 else "FAILED (exit %d)" % proc.returncode
        print("build %-9s %-16s %5.1fs  %s%s" % (name, status, time.time() - t0, label, last_line(proc.stdout)), flush=True)
        if proc.returncode != 0:
            if not args.verbose:
                print("\n".join("    " + l for l in proc.stdout.splitlines()[-TAIL:]), flush=True)
            print("build stopped at %s; later builders did not run" % name, flush=True)
            return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="no network: rebuild from the store")
    parser.add_argument("--verbose", action="store_true", help="print each builder's output")
    parser.add_argument("--only", nargs="+", choices=[s[0] for s in STEPS], help="run only these builders")
    args = parser.parse_args()
    started = time.time()
    before = reports_digest()
    if not run_pass(args, args.offline):
        return 1
    after = reports_digest()
    if not args.only and after != before:
        print("reports.json changed during the run (a new or revised report): second pass, offline, so every file is built from it", flush=True)
        if not run_pass(args, True, "(second pass) "):
            return 1
        if reports_digest() != after:
            print("build stopped: reports.json changed again during the second pass", flush=True)
            return 1
    print("build done in %.1fs" % (time.time() - started), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
