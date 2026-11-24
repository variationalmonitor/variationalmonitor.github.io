#!/usr/bin/python3
"""Notice a biweekly operating update that the site did not take up. Python standard library only.

    /usr/bin/python3 ci/report_alert.py check [--store DIR] [--now 2026-11-30T00:00:00Z]
        print the alert, or that there is nothing to report
        exit 0 nothing to report, 1 an alert holds, 2 the store is unreadable
    /usr/bin/python3 ci/report_alert.py issue [--store DIR] [--now ...]
        keep one GitHub issue (label report-alert) in line with the alert, through `gh`: open it when an alert
        first holds, edit its body only when the text changed, close it when no alert holds
        exit 0 done, 1 a gh call failed, 2 the store is unreadable (the issue is left as it is)

Only full records (all of FULL_REPORT_FIELDS in pipeline/reports/parse.py) become a period on the Operating
results page, so two things leave that page on an old period without any other check failing:
  incomplete  a report-like record newer than the newest full record is not full (a core line renamed or
              dropped): the alert names the post's date and URL, the missing core lines and its warnings
  overdue     more than OVERDUE_DAYS since the newest full report's published_at (an update never read, for
              example one never mirrored to Telegram)
This file is the one definition of both. qa/gate.py loads it and shows alerts() as a WARNING in its freshness
group; it never blocks a deploy. The issue body is the alert text, which only changes when a record
changes or the overdue day count rises, so a run with nothing new makes no call beyond the listing.

--store defaults to $VM_STORE, then .cache/store (see ci/env.sh).
"""
import argparse
import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

OVERDUE_DAYS = 16          # full reports have come 13.0 to 14.9 days apart (Mar to Nov 2026)
LABEL = "report-alert"
LABEL_COLOR = "FBCA04"
LABEL_DESCRIPTION = "A biweekly operating update is overdue or was not fully read; the site keeps publishing"
TITLE = "Biweekly operating update overdue or not fully read"


def _parse_module():
    spec = importlib.util.spec_from_file_location("vm_report_parse", os.path.join(ROOT, "pipeline", "reports", "parse.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FULL_REPORT_FIELDS = tuple(_parse_module().FULL_REPORT_FIELDS)


def parse_time(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")


def _naive_utc(now):
    if now.tzinfo is not None:
        now = now.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return now


def _stamp(s):
    return parse_time(s).strftime("%Y-%m-%d %H:%M UTC")


def load_records(store):
    """<store>/reports.json as a list; raises OSError or ValueError when it cannot be read."""
    def refuse(token):
        raise ValueError("%s is not JSON" % token)
    with open(os.path.join(store, "reports.json"), encoding="utf-8") as fh:
        records = json.load(fh, parse_constant=refuse)
    if not isinstance(records, list):
        raise ValueError("reports.json is not a list")
    return records


def newest_full(records):
    full = [r for r in records if r.get("full") is True]
    return max(full, key=lambda r: r["published_at"]) if full else None


def alerts(records, now):
    """One line per condition that holds (an empty list: nothing to report). `now` is UTC."""
    now = _naive_utc(now)
    last = newest_full(records)
    if last is None:
        return ["Incomplete: reports.json holds %d report-like records and none is full." % len(records)]
    out = []
    for r in sorted(records, key=lambda r: r["published_at"]):
        if r["published_at"] <= last["published_at"] or r.get("full") is True:
            continue
        missing = [f for f in FULL_REPORT_FIELDS if r.get(f) is None]
        out.append("Incomplete: the report posted %s (%s) is not a full update, so Operating results stays on the "
                   "period ending %s. Core lines missing: %s. Warnings: %s."
                   % (_stamp(r["published_at"]), r.get("source_url"), last["published_at"][:10],
                      ", ".join(missing) or "none", "; ".join(r.get("warnings") or []) or "none"))
    age = now - parse_time(last["published_at"])
    if age > dt.timedelta(days=OVERDUE_DAYS):
        out.append("Overdue: %d days since the newest full report, posted %s (%s); the limit is %d days."
                   % (age.days, _stamp(last["published_at"]), last.get("source_url"), OVERDUE_DAYS))
    return out


def status_note(records, now):
    """One line for the gate's PASSED list (or None without a full record)."""
    last = newest_full(records)
    if last is None:
        return None
    return "newest full report %s, %d days ago" % (_stamp(last["published_at"]), (_naive_utc(now) - parse_time(last["published_at"])).days)


def _norm(text):
    return "\n".join(line.rstrip() for line in (text or "").replace("\r\n", "\n").strip().split("\n"))


def _gh(args, body=None):
    """Run gh; the body goes in on stdin (--body-file -). Returns the CompletedProcess."""
    try:
        return subprocess.run(["gh"] + args, input=body, capture_output=True, text=True)
    except OSError as exc:
        return subprocess.CompletedProcess(["gh"] + args, 127, "", str(exc))


def sync_issue(lines, now):
    """Bring the report-alert issue in line with `lines`. Returns the exit status."""
    listing = _gh(["issue", "list", "--label", LABEL, "--state", "open", "--json", "number,body", "--limit", "20"])
    try:
        if listing.returncode != 0:
            raise ValueError(listing.stderr.strip()[:300])
        issues = sorted(json.loads(listing.stdout or "[]"), key=lambda i: i["number"])
    except (ValueError, KeyError, TypeError) as exc:
        print("gh issue list failed, the issue is left as it is: %s" % exc)
        return 1
    body = "\n\n".join(lines) + "\n"
    if lines:
        if not issues:
            _gh(["label", "create", LABEL, "--color", LABEL_COLOR, "--description", LABEL_DESCRIPTION])   # exists: ignored
            res = _gh(["issue", "create", "--title", TITLE, "--label", LABEL, "--body-file", "-"], body)
            action = "opened %s" % res.stdout.strip()
        elif _norm(issues[0].get("body")) == _norm(body):
            print("issue #%d is open and its body is current" % issues[0]["number"])
            return 0
        else:
            res = _gh(["issue", "edit", str(issues[0]["number"]), "--body-file", "-"], body)
            action = "updated issue #%d" % issues[0]["number"]
        if res.returncode != 0:
            print("gh failed: %s" % res.stderr.strip()[:300])
            return 1
        print(action)
        return 0
    status = 0
    for issue in issues:
        res = _gh(["issue", "close", str(issue["number"]), "--comment",
                   "No report alert at %s UTC: the newest report post is full and the newest full report is at most %d days old."
                   % (_naive_utc(now).strftime("%Y-%m-%d %H:%M"), OVERDUE_DAYS)])
        print("closed issue #%d" % issue["number"] if res.returncode == 0 else "gh failed: %s" % res.stderr.strip()[:300])
        status = status or (0 if res.returncode == 0 else 1)
    return status


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["check", "issue"])
    ap.add_argument("--store", default=os.environ.get("VM_STORE") or os.path.join(ROOT, ".cache", "store"))
    ap.add_argument("--now", help="UTC time to judge against, e.g. 2026-11-30T00:00:00Z (testing)")
    args = ap.parse_args(argv)
    now = parse_time(args.now) if args.now else dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)
    try:
        records = load_records(args.store)
        lines = alerts(records, now)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("the report store is unreadable (%s): %s" % (args.store, str(exc)[:200]))
        return 2
    print("\n\n".join(lines) if lines else "Nothing to report: %s." % status_note(records, now))
    if args.command == "check":
        return 1 if lines else 0
    return sync_issue(lines, now)


if __name__ == "__main__":
    sys.exit(main())
