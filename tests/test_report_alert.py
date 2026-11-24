"""The report alert (ci/report_alert.py): a report post newer than the newest full report that is not full, or no
full report for over 16 days. Shown by the gate as a warning, and kept as one GitHub issue through a stubbed gh."""
from datetime import datetime, timedelta
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ci" / "report_alert.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ra = _load("report_alert_under_test", SCRIPT)
gate = _load("gate_for_report_alert", ROOT / "qa" / "gate.py")


def _collect():
    """The pipeline's reports.collect. Its `sources` and `reports` packages would be shadowed by tools/ on sys.path,
    so they are imported from pipeline/ and the modules other tests loaded are put back."""
    own = lambda k: k in ("sources", "reports") or k.startswith(("sources.", "reports."))
    saved_modules = {k: sys.modules.pop(k) for k in list(sys.modules) if own(k)}
    saved_path = list(sys.path)
    sys.path[:] = [str(ROOT / "pipeline")] + [p for p in sys.path if Path(p or ".").resolve() != ROOT / "tools"]
    try:
        from reports import collect
    finally:
        sys.path[:] = saved_path
        for k in [k for k in sys.modules if own(k)]:
            del sys.modules[k]
        sys.modules.update(saved_modules)
    return collect


collect = _collect()

CORE = {"spreads_paid": 4908930, "net_revenue": 3208936, "rewards": -217813, "net_profit": 2991123,
        "treasury_inflow": 981786, "olp_pnl": 2009337, "lifetime_olp_pnl": 26492418}
OCT30, NOV13 = "2026-10-30T17:40:12Z", "2026-11-13T18:51:03Z"


def record(published_at, post_id, drop=(), warnings=(), full=None, **extra):
    rec = {"published_at": published_at, "source_url": "https://t.me/variational_io/%d" % post_id,
           "cumulative_volume": 400e9 + post_id, "id": "telegram:%d" % post_id}
    rec.update({k: (None if k in drop else v) for k, v in CORE.items()})
    rec.update({"full": not drop if full is None else full, "filled_from_x": [], "warnings": list(warnings)})
    rec.update(extra)
    return rec


def today():
    return [record("2026-10-16T16:37:20Z", 430), record(OCT30, 445), record(NOV13, 459)]


def at(stamp):
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")


NOT_READ = "not read: 2W OLP Profit = $2,104,551; Lifetime OLP Profit = $28,596,969"
INCOMPLETE = record("2026-11-27T17:02:11Z", 470, drop=("olp_pnl", "lifetime_olp_pnl"), warnings=[NOT_READ])
INCOMPLETE_LINE = ("Incomplete: the report posted 2026-11-27 17:02 UTC (https://t.me/variational_io/470) is not a full "
                   "update, so Operating results stays on the period ending 2026-11-13. Core lines missing: olp_pnl, "
                   "lifetime_olp_pnl. Warnings: " + NOT_READ + ".")
OVERDUE_LINE = ("Overdue: 17 days since the newest full report, posted 2026-11-13 18:51 UTC "
                "(https://t.me/variational_io/459); the limit is 16 days.")


class Conditions(unittest.TestCase):
    def test_nothing_to_report(self):
        self.assertEqual(ra.alerts(today(), at("2026-11-24T12:00:00Z")), [])
        # the limit is more than 16 days, not 16 days
        self.assertEqual(ra.alerts(today(), at(NOV13) + timedelta(days=16)), [])
        self.assertEqual(ra.status_note(today(), at("2026-11-24T12:00:00Z")), "newest full report 2026-11-13 18:51 UTC, 10 days ago")

    def test_incomplete_names_date_url_missing_lines_and_warnings(self):
        self.assertEqual(ra.alerts(today() + [INCOMPLETE], at("2026-11-27T18:00:00Z")), [INCOMPLETE_LINE])

    def test_incomplete_without_warnings_says_none(self):
        rec = record("2026-11-27T17:02:11Z", 470, drop=("net_profit",))
        line, = ra.alerts(today() + [rec], at("2026-11-27T18:00:00Z"))
        self.assertIn("Core lines missing: net_profit. Warnings: none.", line)

    def test_older_partial_records_are_not_alerts(self):
        legacy = record("2026-02-20T15:00:00Z", 300, drop=tuple(CORE))
        self.assertEqual(ra.alerts([legacy] + today(), at("2026-11-24T12:00:00Z")), [])

    def test_overdue_gives_date_and_days(self):
        self.assertEqual(ra.alerts(today(), at(NOV13) + timedelta(days=17)), [OVERDUE_LINE])
        line, = ra.alerts(today(), at(NOV13) + timedelta(days=16, seconds=1))
        self.assertTrue(line.startswith("Overdue: 16 days since the newest full report, posted 2026-11-13 18:51 UTC"))

    def test_both_at_once(self):
        self.assertEqual(ra.alerts(today() + [INCOMPLETE], at(NOV13) + timedelta(days=17)), [INCOMPLETE_LINE, OVERDUE_LINE])

    def test_aware_now_is_read_as_utc(self):
        from datetime import timezone
        now = (at(NOV13) + timedelta(days=17)).replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=9)))
        self.assertEqual(ra.alerts(today(), now), [OVERDUE_LINE])

    def test_no_full_record_is_an_alert(self):
        self.assertEqual(ra.alerts([record(NOV13, 459, drop=("olp_pnl",))], at(NOV13)),
                         ["Incomplete: reports.json holds 1 report-like records and none is full."])

    def test_record_made_full_from_the_x_post_counts_as_full(self):
        def post(pid, when, lifetime):
            return {"id": pid, "time": when, "edited": False, "text": "\n".join([
                "**Metrics**", "* Total Volume Traded: $%dB" % pid, "* Current Dual-Sided OI: $1.54B",
                "* 2W Spreads Paid (Gross Revenue): $4,908,930", "* 2W Net Revenue: $3,208,936",
                "* 2W Rewards: ($217,813)", "* 2W Net Profit: $2,991,123", "* 2W Protocol Treasury Inflow: $981,786",
                "* 2W OLP PnL: $2,009,337", "* Lifetime OLP PnL: %s" % lifetime])}
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            (store / "telegram").mkdir()
            posts = [post(445, OCT30, "$24,483,081"), post(459, NOV13, "")]   # Telegram printed the label, no figure
            (store / "telegram" / "posts.json").write_text(json.dumps({"channel": "variational_io", "posts": posts}))
            x = dict(post(459, "2026-11-13T19:02:44Z", "$26,492,418"), id="2121317860740063501",
                     url="https://x.com/variational_io/status/2121317860740063501")
            with patch.object(collect.xposts, "load", return_value=[]):
                alone = collect.collect(store)
            with patch.object(collect.xposts, "load", return_value=[x]):
                filled = collect.collect(store)
        # without the X post the Telegram record is not full, and that is an alert
        self.assertFalse(alone[-1]["full"])
        line, = ra.alerts(alone, at("2026-11-14T00:00:00Z"))
        self.assertIn("Core lines missing: lifetime_olp_pnl. Warnings: printed without a figure: lifetime_olp_pnl.", line)
        # the X post fills the figure: the record is full and nothing is reported
        self.assertTrue(filled[-1]["full"])
        self.assertEqual(filled[-1]["filled_from_x"], ["lifetime_olp_pnl"])
        self.assertEqual(ra.alerts(filled, at("2026-11-14T00:00:00Z")), [])


class Gate(unittest.TestCase):
    """The gate shows the alert as a warning in its freshness group and never as a blocking line."""

    def rows(self, records, now, alerts=None):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "telegram"))
            for name, doc in (("reports.json", records), ("telegram/posts.json", {"posts": []}),
                              ("telegram/state.json", {"checked_at": now})):
                with open(os.path.join(tmp, name), "w") as fh:
                    json.dump(doc, fh)
            rep = gate.Report()
            with patch.object(gate, "STORE", tmp), patch.object(gate, "ONCHAIN_DATA", os.path.join(tmp, "none")):
                if alerts:
                    with patch.object(gate, "load_report_alert", side_effect=alerts):
                        gate.check_freshness(rep, {}, at(now))
                else:
                    gate.check_freshness(rep, {}, at(now))
        return [r for r in rep.rows if r[1].startswith("operating update")]

    def test_no_alert_passes(self):
        (_, _, status, details), = self.rows(today(), "2026-11-24T12:00:00Z")
        self.assertEqual((status, details), ("pass", ["newest full report 2026-11-13 18:51 UTC, 10 days ago"]))

    def test_alerts_are_warnings(self):
        (section, _, status, details), = self.rows(today() + [INCOMPLETE], "2026-11-30T18:51:03Z")
        self.assertEqual((section, status, details), ("freshness", "warn", [INCOMPLETE_LINE, OVERDUE_LINE]))

    def test_a_check_that_cannot_run_is_still_only_a_warning(self):
        (_, _, status, details), = self.rows(today(), "2026-11-24T12:00:00Z", alerts=SyntaxError("invalid syntax"))
        self.assertEqual(status, "warn")
        self.assertIn("could not be judged: SyntaxError", details[0])


GH_STUB = r'''#!/usr/bin/python3
import json, os, sys
d = os.environ["GH_STUB_DIR"]
args = sys.argv[1:]
call = {"args": args}
if "--body-file" in args and args[args.index("--body-file") + 1] == "-":
    call["stdin"] = sys.stdin.read()
with open(os.path.join(d, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps(call) + "\n")
if args[:2] == ["issue", "list"]:
    rc = int(open(os.path.join(d, "list_rc")).read()) if os.path.exists(os.path.join(d, "list_rc")) else 0
    if rc:
        sys.stderr.write("HTTP 502\n"); sys.exit(rc)
    sys.stdout.write(open(os.path.join(d, "list.json")).read())
elif args[:2] == ["issue", "create"]:
    print("https://github.com/variationalmonitor/variationalmonitor.github.io/issues/12")
'''


class Issue(unittest.TestCase):
    """`report_alert.py issue` against a fake gh on PATH that records its arguments."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "bin").mkdir()
        gh = self.dir / "bin" / "gh"
        gh.write_text(GH_STUB)
        gh.chmod(0o755)
        self.store = self.dir / "store"
        self.store.mkdir()
        self.open_issues([])

    def tearDown(self):
        self.tmp.cleanup()

    def open_issues(self, issues):
        (self.dir / "list.json").write_text(json.dumps(issues))

    def run_issue(self, records, now):
        if records is not None:
            (self.store / "reports.json").write_text(json.dumps(records))
        env = dict(os.environ, PATH="%s%s%s" % (self.dir / "bin", os.pathsep, os.environ.get("PATH", "")),
                   GH_STUB_DIR=str(self.dir))
        env.pop("VM_STORE", None)
        res = subprocess.run([sys.executable, str(SCRIPT), "issue", "--store", str(self.store), "--now", now],
                             capture_output=True, text=True, env=env)
        log = self.dir / "calls.jsonl"
        calls = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
        return res, calls

    def verbs(self, calls):
        return [c["args"][:2] for c in calls]

    def test_first_alert_opens_the_issue_with_the_alert_text(self):
        res, calls = self.run_issue(today() + [INCOMPLETE], "2026-11-27T18:00:00Z")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.verbs(calls), [["issue", "list"], ["label", "create"], ["issue", "create"]])
        self.assertEqual(calls[0]["args"], ["issue", "list", "--label", "report-alert", "--state", "open",
                                            "--json", "number,body", "--limit", "20"])
        create = calls[2]
        self.assertEqual(create["args"][create["args"].index("--label") + 1], "report-alert")
        self.assertEqual(create["stdin"], INCOMPLETE_LINE + "\n")

    def test_both_conditions_make_one_body(self):
        res, calls = self.run_issue(today() + [INCOMPLETE], "2026-11-30T18:51:03Z")
        self.assertEqual(calls[-1]["stdin"], INCOMPLETE_LINE + "\n\n" + OVERDUE_LINE + "\n")

    def test_body_unchanged_means_no_edit(self):
        # GitHub may hand the body back with CRLF line ends and without the final newline
        self.open_issues([{"number": 12, "body": (INCOMPLETE_LINE + "\n\n" + OVERDUE_LINE).replace("\n", "\r\n")}])
        res, calls = self.run_issue(today() + [INCOMPLETE], "2026-11-30T20:00:00Z")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.verbs(calls), [["issue", "list"]])

    def test_changed_text_edits_the_open_issue(self):
        self.open_issues([{"number": 12, "body": INCOMPLETE_LINE + "\n"}])
        res, calls = self.run_issue(today() + [INCOMPLETE], "2026-11-30T18:51:03Z")
        self.assertEqual(self.verbs(calls), [["issue", "list"], ["issue", "edit"]])
        self.assertEqual(calls[1]["args"][:3], ["issue", "edit", "12"])
        self.assertEqual(calls[1]["stdin"], INCOMPLETE_LINE + "\n\n" + OVERDUE_LINE + "\n")

    def test_resolved_means_close(self):
        self.open_issues([{"number": 12, "body": OVERDUE_LINE + "\n"}])
        nxt = record("2026-11-27T17:02:11Z", 470)
        res, calls = self.run_issue(today() + [nxt], "2026-11-27T18:00:00Z")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.verbs(calls), [["issue", "list"], ["issue", "close"]])
        self.assertEqual(calls[1]["args"][:4], ["issue", "close", "12", "--comment"])

    def test_nothing_open_and_nothing_to_report_is_one_listing(self):
        res, calls = self.run_issue(today(), "2026-11-24T12:00:00Z")
        self.assertEqual(res.returncode, 0)
        self.assertEqual(self.verbs(calls), [["issue", "list"]])

    def test_a_failed_listing_opens_nothing(self):
        (self.dir / "list_rc").write_text("1")
        res, calls = self.run_issue(today() + [INCOMPLETE], "2026-11-27T18:00:00Z")
        self.assertEqual(res.returncode, 1)
        self.assertEqual(self.verbs(calls), [["issue", "list"]])

    def test_an_unreadable_store_leaves_the_issue_alone(self):
        res, calls = self.run_issue(None, "2026-11-27T18:00:00Z")
        self.assertEqual(res.returncode, 2)
        self.assertIn("the report store is unreadable", res.stdout)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
