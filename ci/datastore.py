#!/usr/bin/env python3
"""The data branch: commit, push, prune and squash. Python standard library and git only.

The orphan branch `data` is the pipeline's store (VM_STORE is a checkout of it). Two jobs write to it and
never touch the same file:

  capture   snapshots/<stamp>.json.gz, exec/<day>.jsonl, capture/<day>.log        every 30 minutes
            (raw snapshots stay 14 days: the builders read the last seven for the intraday series)
  publish   everything else (platform.jsonl, days/, source caches, reports.json, baselines/<slug>.json, onchain/)
            and the removal of raw snapshots older than 14 days                    every hour

    ci/datastore.py commit-push --dir .store -m "capture 20261122T1030Z"
    ci/datastore.py commit-push --dir .store -m "publish ..." --daily-if-new days/ --squash-if-due
    ci/datastore.py prune --dir .store            delete raw snapshots past retention whose UTC day is sealed
    ci/datastore.py status --dir .store           size of the checkout by folder, snapshot span, housekeeping state

commit-push never rebases. When the push is rejected (the other job pushed first, or the branch was squashed)
it fetches the new tip, resets to it and writes its own changed files again on top, then retries. That is safe
because each job owns its files, and it works in a depth-1 checkout and across a history rewrite, where
`git pull --rebase` does not. A .jsonl file that changed on both sides is merged line by line.

Growth control
  --daily-if-new days/   platform.jsonl and listings_seen.json are rewritten by every build but committed only
                         in the run that seals a UTC day (a new file under days/); between those runs the
                         builders re-read the raw snapshots, which are on the branch.
  --squash-if-due        once every SQUASH_DAYS the commit replaces the branch history with one commit
                         (force-with-lease), so expired snapshots and superseded on-chain state leave history.
"""
import argparse
import datetime as dt
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

BRANCH = "data"
REMOTE = "origin"
RETENTION_DAYS = 14
INTRADAY_DAYS = 7           # the builder's intraday series reads the last seven days of raw snapshots (VM_SNAPSHOTS)
KEEP_NEWEST = 384           # never prune the newest eight days of files (48 a day), whatever the clock says
SQUASH_DAYS = 7
DAILY_PATHS = ("platform.jsonl", "listings_seen.json")
HOUSEKEEPING = "housekeeping.json"
BOT = ["-c", "user.name=github-actions[bot]", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com"]


def log(msg):
    print(msg, flush=True)


def git(d, *args, check=True):
    proc = subprocess.run(["git", "-C", d] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True)
    if check and proc.returncode != 0:
        raise RuntimeError("git %s failed (%d): %s" % (" ".join(args), proc.returncode, proc.stderr.strip()[-400:]))
    return proc


def utcnow():
    return dt.datetime.utcnow().replace(microsecond=0)


def read_housekeeping(d):
    try:
        with open(os.path.join(d, HOUSEKEEPING)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def write_housekeeping(d, doc):
    with open(os.path.join(d, HOUSEKEEPING), "w") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
        fh.write("\n")


# ---------------------------------------------------------------- changes

def changed_paths(d):
    """(present, deleted): paths that differ from HEAD, untracked files included."""
    out = git(d, "status", "--porcelain", "-z", "--untracked-files=all").stdout
    present, deleted = [], []
    for entry in out.split("\0"):
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if "D" in code and not os.path.exists(os.path.join(d, path)):
            deleted.append(path)
        else:
            present.append(path)
    return present, deleted


def blob_at(d, rev, path):
    proc = subprocess.run(["git", "-C", d, "show", "%s:%s" % (rev, path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc.stdout if proc.returncode == 0 else None


def reapply(d, stash, present, deleted, old_base):
    """After a reset to the new remote tip, write this job's changes again."""
    for path in present:
        src, dst = os.path.join(stash, path), os.path.join(d, path)
        theirs_then, theirs_now = blob_at(d, old_base, path), blob_at(d, "HEAD", path)
        os.makedirs(os.path.dirname(dst) or d, exist_ok=True)
        if theirs_now is not None and theirs_now != theirs_then:
            # the same file changed on the branch while this job held it: not expected (each job owns its files)
            if path.endswith(".jsonl") or path.endswith(".log"):
                with open(src, "rb") as fh:
                    mine = fh.read().splitlines()
                have = theirs_now.splitlines()
                seen = set(have)
                merged = have + [line for line in mine if line not in seen]
                with open(dst, "wb") as fh:
                    fh.write(b"\n".join(merged) + b"\n")
                log("  merged by line: %s (%d lines kept from the branch, %d added)" % (path, len(have), len(merged) - len(have)))
                continue
            log("  WARNING %s also changed on the branch; this job's copy wins" % path)
        shutil.copy2(src, dst)
    for path in deleted:
        target = os.path.join(d, path)
        if os.path.exists(target):
            os.remove(target)


def commit_push(d, message, daily_if_new=None, squash_if_due=False, force_squash=False, retries=6, push=True):
    present, deleted = changed_paths(d)
    # paths committed once a day: put them back unless this run seals a day (or they were never committed)
    if daily_if_new is not None:
        sealing = any(p.startswith(daily_if_new) for p in present if blob_at(d, "HEAD", p) is None)
        if not sealing:
            for path in DAILY_PATHS:
                if path in present and blob_at(d, "HEAD", path) is not None:
                    git(d, "checkout", "-q", "HEAD", "--", path)
                    present.remove(path)
        log("  day sealed in this run: %s; %s" % ("yes" if sealing else "no",
            "platform.jsonl and listings_seen.json are committed" if sealing else "they wait for the next sealed day"))
    hk = read_housekeeping(d)
    today = utcnow().strftime("%Y-%m-%d")
    squash = force_squash
    if squash_if_due and not squash:
        last = hk.get("last_squash")
        squash = last is None or (dt.date.fromisoformat(today) - dt.date.fromisoformat(last)).days >= SQUASH_DAYS
    if any(p.startswith("onchain/state/") for p in present):
        squash = True       # a new durable copy of the on-chain state: drop the 12 MB it replaces from history at once
    if not present and not deleted and not squash:
        log("  nothing to commit")
        return "unchanged"
    if squash:
        hk["last_squash"] = today
        write_housekeeping(d, hk)
        if HOUSEKEEPING not in present:
            present.append(HOUSEKEEPING)
    stash = tempfile.mkdtemp(prefix="vm-datastore-")
    try:
        for path in present:
            dst = os.path.join(stash, path)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(d, path), dst)
        base = git(d, "rev-parse", "HEAD").stdout.strip()
        for attempt in range(1, retries + 1):
            git(d, "add", "-A")
            if git(d, "diff", "--cached", "--quiet", check=False).returncode == 0 and not squash:
                log("  nothing to commit after syncing with the branch")
                return "unchanged"
            git(d, *BOT, "commit", "-q", "--allow-empty", "-m", message)
            if squash:
                tree = git(d, "rev-parse", "HEAD^{tree}").stdout.strip()
                root = git(d, *BOT, "commit-tree", tree, "-m", "Squash history %s\n\n%s" % (today, message)).stdout.strip()
                git(d, "reset", "-q", "--soft", root)
            if not push:
                log("  committed %s (not pushed)" % git(d, "rev-parse", "--short", "HEAD").stdout.strip())
                return "committed"
            cmd = ["push", "-q", REMOTE, "HEAD:refs/heads/%s" % BRANCH]
            if squash:
                cmd.insert(1, "--force-with-lease=refs/heads/%s:%s" % (BRANCH, base))
            proc = git(d, *cmd, check=False)
            if proc.returncode == 0:
                log("  pushed %s to %s%s (attempt %d)" % (git(d, "rev-parse", "--short", "HEAD").stdout.strip(), BRANCH,
                                                          ", history squashed" if squash else "", attempt))
                return "pushed"
            log("  push rejected (attempt %d): %s" % (attempt, (proc.stderr.strip().splitlines() or ["?"])[-1][:200]))
            time.sleep(min(30, 2 * attempt))
            fetch = git(d, "fetch", "-q", "--depth", "1", REMOTE, BRANCH, check=False)
            if fetch.returncode != 0:
                log("  fetch failed: %s" % (fetch.stderr.strip().splitlines() or ["?"])[-1][:200])
                git(d, "reset", "-q", "--soft", base)      # drop the local commit, keep the files, try again
                continue
            new_base = git(d, "rev-parse", "FETCH_HEAD").stdout.strip()
            git(d, "reset", "-q", "--hard", new_base)
            reapply(d, stash, present, deleted, base)
            base = new_base
        raise RuntimeError("could not push to %s after %d attempts" % (BRANCH, retries))
    finally:
        shutil.rmtree(stash, ignore_errors=True)


# ---------------------------------------------------------------- prune

def stamp_time(name):
    return dt.datetime.strptime(name[:14], "%Y%m%dT%H%MZ")


def prune(d, days=RETENTION_DAYS, now=None, dry_run=False):
    """Delete raw snapshots older than `days` whose UTC day is sealed. Seed snapshots are never pruned.

    Retention can never reach into the intraday window: `days` is held above it, the newest KEEP_NEWEST files
    always stay, and age is measured from the newest snapshot, not from the clock, so a capture that has
    stopped for a while does not lose the week before it stopped.
    """
    now = now or utcnow()
    days = max(days, INTRADAY_DAYS + 1)
    files = sorted(glob.glob(os.path.join(d, "snapshots", "*.json.gz")))
    try:
        now = min(now, stamp_time(os.path.basename(files[-1]))) if files else now
    except ValueError:
        pass
    sealed = {os.path.basename(p)[:10] for p in glob.glob(os.path.join(d, "days", "*.json"))}
    removed, held = [], []
    for path in files[:-KEEP_NEWEST] if len(files) > KEEP_NEWEST else []:
        name = os.path.basename(path)
        try:
            t = stamp_time(name)
        except ValueError:
            continue
        if now - t <= dt.timedelta(days=days):
            continue
        # the builder assigns a snapshot to the UTC day whose end is nearest (pipeline/market/store.py)
        day = (t - dt.timedelta(hours=12)).date().isoformat()
        if day not in sealed:
            held.append(name)
            continue
        removed.append(name)
        if not dry_run:
            os.remove(path)
    logs = sorted(glob.glob(os.path.join(d, "capture", "*.log")))
    old_logs = [p for p in logs if os.path.basename(p)[:10] < (now - dt.timedelta(days=45)).strftime("%Y-%m-%d")]
    for path in old_logs:
        if not dry_run:
            os.remove(path)
    log("prune: %d snapshots on disk, %d removed (older than %d days, day sealed), %d held back (day not sealed), %d old capture logs removed"
        % (len(files), len(removed), days, len(held), len(old_logs)))
    if held:
        log("  held back: %s%s" % (", ".join(held[:6]), " ..." if len(held) > 6 else ""))
    return removed


# ---------------------------------------------------------------- status

def status(d):
    total = 0
    sizes = {}
    for root, dirs, files in os.walk(d):
        if ".git" in dirs:
            dirs.remove(".git")
        for name in files:
            size = os.path.getsize(os.path.join(root, name))
            rel = os.path.relpath(os.path.join(root, name), d)
            key = rel.split(os.sep)[0] if os.sep in rel else "(root files)"
            if key == "onchain":
                key = os.sep.join(rel.split(os.sep)[:2])
            sizes[key] = sizes.get(key, 0) + size
            total += size
    for key in sorted(sizes, key=lambda k: -sizes[k]):
        log("  %-22s %8.2f MB" % (key, sizes[key] / 1e6))
    log("  %-22s %8.2f MB" % ("total", total / 1e6))
    snaps = sorted(os.path.basename(p) for p in glob.glob(os.path.join(d, "snapshots", "*.json.gz")))
    if snaps:
        log("  snapshots: %d, %s to %s" % (len(snaps), snaps[0][:14], snaps[-1][:14]))
    log("  housekeeping: %s" % json.dumps(read_housekeeping(d), sort_keys=True))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("commit-push")
    p.add_argument("--dir", required=True)
    p.add_argument("-m", "--message", required=True)
    p.add_argument("--daily-if-new", metavar="PREFIX", help="commit platform.jsonl and listings_seen.json only when a new file appears under PREFIX")
    p.add_argument("--squash-if-due", action="store_true")
    p.add_argument("--squash", action="store_true", help="squash now")
    p.add_argument("--no-push", action="store_true")
    p = sub.add_parser("prune")
    p.add_argument("--dir", required=True)
    p.add_argument("--days", type=int, default=RETENTION_DAYS)
    p.add_argument("--now", help="UTC time to prune against (testing), e.g. 2026-12-10T00:00:00")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("status")
    p.add_argument("--dir", required=True)
    args = parser.parse_args()
    if args.cmd == "commit-push":
        try:
            commit_push(args.dir, args.message, daily_if_new=args.daily_if_new, squash_if_due=args.squash_if_due,
                        force_squash=args.squash, push=not args.no_push)
        except RuntimeError as exc:
            log("FAILED: %s" % exc)
            return 1
        return 0
    if args.cmd == "prune":
        now = dt.datetime.strptime(args.now, "%Y-%m-%dT%H:%M:%S") if args.now else None
        prune(args.dir, args.days, now, args.dry_run)
        return 0
    return status(args.dir)


if __name__ == "__main__":
    sys.exit(main())
