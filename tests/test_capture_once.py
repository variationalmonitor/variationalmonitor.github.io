"""ci/capture.py once: one boundary per run (tick.yml), with the loop's own sync, tick, dispatch and probe.

A fake clock and stubs drive the start cases, the publish and report dispatches, the retry and the shadow run; real
git repositories (a bare remote and depth-1 clones) drive the shadow's branch fetch and two writers pushing the same
snapshot (ci/datastore.py)."""
import argparse
import calendar
import contextlib
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    saved = list(sys.path)          # capture.py puts ci/ first on sys.path; ci/onchain.py must not shadow pipeline's
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path[:] = saved


capture = _load("capture_under_test", ROOT / "ci" / "capture.py")
ds = capture.datastore

T0 = calendar.timegm((2026, 11, 24, 10, 0, 0, 0, 0, 0))     # a :00 boundary, 10:00 UTC
HALF = T0 + 1800                                            # the :30 boundary after it
PERIOD = 1800


class FakeClock:
    def __init__(self, now):
        self.now = float(now)
        self.woke = []              # the moment every sleep ended

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        assert seconds >= 0, seconds
        self.now += seconds
        self.woke.append(self.now)


def make_args(store, **kw):
    args = dict(store=store, url="http://127.0.0.1:9/unused", no_push=False, skip_hl=True, period_s=PERIOD,
                max_wait_s=600.0, late_s=1200.0, shadow=False, no_probe=False, dry_run_dispatch=True)
    args.update(kw)
    return argparse.Namespace(**args)


def touch_snapshot(store, stamp):
    path = os.path.join(store, "snapshots", stamp + ".json.gz")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"x")


class Choose(unittest.TestCase):
    def pick(self, now, held=None, late_s=1200.0):
        asked = []

        def holding(boundary):
            asked.append(boundary)
            return held
        case, boundary, _ = capture.choose(now, PERIOD, 600.0, late_s, holding)
        return case, boundary, asked

    def test_a_when_the_next_boundary_is_at_most_max_wait_away(self):
        self.assertEqual(self.pick(T0 - 180), ("a", T0, []))
        self.assertEqual(self.pick(T0 - 600), ("a", T0, []))
        self.assertEqual(self.pick(T0 - 601)[:2], ("b", T0 - PERIOD))

    def test_b_when_the_boundary_that_passed_has_no_snapshot(self):
        self.assertEqual(self.pick(T0 + 300), ("b", T0, [T0]))

    def test_c_when_the_boundary_that_passed_has_a_snapshot(self):
        self.assertEqual(self.pick(T0 + 300, held="20261124T1000Z"), ("c", T0, [T0]))

    def test_c_when_the_boundary_passed_too_long_ago_without_asking_the_store(self):
        self.assertEqual(self.pick(T0 + 300, late_s=200.0), ("c", T0, []))

    def test_a_boundary_stamp_is_any_snapshot_from_the_boundary_to_the_next(self):
        names = ["20261124T0959Z.json.gz", "20261124T1015Z.json.gz", "20261124T1030Z.json.gz", "junk.json.gz"]
        self.assertEqual(capture.boundary_stamp(names, T0, PERIOD), "20261124T1015Z")
        self.assertIsNone(capture.boundary_stamp(names[:1], T0, PERIOD))
        self.assertEqual(capture.boundary_stamp(names + ["20261124T1000Z.json.gz"], T0, PERIOD), "20261124T1000Z")


class Once(unittest.TestCase):
    """Real runs (not shadow) with every outside call stubbed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def run_once(self, start, ticks=(True,), publish=None, window=True, reports=(), probe_error=None,
                 sync_s=0.0, on_sync=None, git=False, unpushed=(), pushes=(), **kw):
        """git: the store is a git checkout, so settling looks at it; unpushed: per tick, True when the tick's own
        push failed and left its files in the checkout; pushes: what each commit_push made by settling does, a result
        or an exception to raise, the last one repeating."""
        clock = FakeClock(start)
        self.clock = clock
        events = []
        results = list(ticks)
        left_by_tick = list(unpushed)
        planned = list(pushes)
        checkout = []                   # paths not on the branch
        if git:
            os.makedirs(os.path.join(self.store, ".git"), exist_ok=True)

        def safe_tick(args):
            events.append(("tick", clock.now))
            if left_by_tick and left_by_tick.pop(0):
                checkout[:] = ["snapshots/x.json.gz", "exec/x.jsonl", "capture/x.log"]
            return results.pop(0)

        def changed_paths(store):
            return list(checkout), []

        def commit_push(store, message, **kw):
            events.append(("push", clock.now))
            outcome = planned.pop(0) if len(planned) > 1 else planned[0]
            if isinstance(outcome, Exception):
                raise outcome
            checkout[:] = []
            return outcome

        def sync(store):
            events.append(("sync", clock.now))
            clock.now += sync_s
            if on_sync:
                on_sync(store)

        def dispatch(workflow, fields, dry_run):
            events.append(("dispatch", clock.now, workflow, fields["reason"]))
            return True

        def probe(store):
            events.append(("probe", clock.now))
            if probe_error:
                raise probe_error
            return list(reports)

        def in_window(store, now=None):
            return window

        env = {k: v for k, v in os.environ.items() if k != "VM_PUBLISH"}
        if publish is not None:
            env["VM_PUBLISH"] = publish
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(capture, "safe_tick", safe_tick), mock.patch.object(capture, "sync", sync), \
                mock.patch.object(capture, "dispatch", dispatch), mock.patch.object(capture, "probe", probe), \
                mock.patch.object(capture, "in_report_window", in_window), \
                mock.patch.object(capture, "branch_names", side_effect=AssertionError("shadow only")), \
                mock.patch.object(ds, "changed_paths", changed_paths), mock.patch.object(ds, "commit_push", commit_push), \
                contextlib.redirect_stdout(out):
            rc = capture.once(make_args(self.store, **kw), clock=clock, sleep=clock.sleep)
        self.assertEqual(results, [], "every planned tick result was used")
        return rc, events, out.getvalue()

    @staticmethod
    def times(events, name):
        return [e[1] for e in events if e[0] == name]

    # ---- the three start cases

    def test_case_a_syncs_in_the_last_75_s_and_ticks_1_5_s_after_the_boundary(self):
        rc, events, _ = self.run_once(HALF - 180)
        self.assertEqual(rc, 0)
        self.assertEqual(self.times(events, "tick"), [HALF + 1.5])
        syncs = self.times(events, "sync")
        self.assertEqual(len(syncs), 1)
        self.assertTrue(HALF - 75 <= syncs[0] < HALF, syncs)

    def test_case_a_with_a_short_wait_syncs_at_once_and_does_not_oversleep(self):
        rc, events, _ = self.run_once(HALF - 30, sync_s=5.0)
        self.assertEqual(rc, 0)
        self.assertEqual(self.times(events, "sync"), [HALF - 30])
        self.assertEqual(self.times(events, "tick"), [HALF + 1.5])

    def test_case_a_with_no_push_does_not_sync(self):
        rc, events, _ = self.run_once(HALF - 180, no_push=True)
        self.assertEqual((rc, self.times(events, "sync"), self.times(events, "tick")), (0, [], [HALF + 1.5]))

    def test_case_b_syncs_then_captures_at_once(self):
        rc, events, out = self.run_once(T0 + 300)
        self.assertEqual(rc, 0)
        self.assertEqual([e[:2] for e in events], [("sync", T0 + 300), ("tick", T0 + 300)])
        self.assertIn("capturing the 10:00 UTC boundary late", out)

    def test_case_b_becomes_c_when_the_sync_brings_the_boundary_s_snapshot(self):
        rc, events, out = self.run_once(T0 + 300, ticks=(), on_sync=lambda s: touch_snapshot(s, "20261124T1000Z"))
        self.assertEqual((rc, self.times(events, "tick")), (0, []))
        self.assertIn("already has a snapshot (20261124T1000Z)", out)

    def test_case_c_logs_one_line_and_exits_0(self):
        touch_snapshot(self.store, "20261124T1001Z")
        rc, events, out = self.run_once(T0 + 300, ticks=(), publish="on", reports=(5,))
        self.assertEqual(rc, 0)
        self.assertEqual([e[0] for e in events], ["sync"])
        self.assertEqual(len(out.strip().splitlines()), 1, out)
        self.assertIn("nothing to do", out)

    def test_case_c_when_too_late_does_not_touch_the_store(self):
        rc, events, out = self.run_once(T0 + 300, ticks=(), max_wait_s=60.0, late_s=200.0)
        self.assertEqual((rc, events), (0, []))
        self.assertEqual(len(out.strip().splitlines()), 1, out)

    # ---- publish.yml after an on-the-hour tick

    def test_hourly_dispatch_after_a_00_tick_with_the_switch_on(self):
        rc, events, _ = self.run_once(T0 - 60, publish="on", window=False)
        self.assertEqual(rc, 0)
        self.assertEqual([e for e in events if e[0] == "dispatch"], [("dispatch", T0 + 1.5, "publish.yml", "hourly")])
        self.assertEqual([e[0] for e in events], ["sync", "tick", "dispatch"])

    def test_no_dispatch_after_a_30_tick(self):
        _, events, _ = self.run_once(HALF - 60, publish="on", window=False)
        self.assertEqual([e for e in events if e[0] == "dispatch"], [])

    def test_no_dispatch_with_the_switch_off_or_not_exactly_on(self):
        for value in (None, "", "off", "yes", "ON"):
            _, events, _ = self.run_once(T0 - 60, publish=value, window=True, reports=(9,))
            self.assertEqual([e[0] for e in events if e[0] in ("dispatch", "probe")], [], value)

    def test_a_late_capture_of_a_missed_00_dispatches_hourly_and_of_a_missed_30_does_not(self):
        _, events, _ = self.run_once(T0 + 300, publish="on", window=False)
        self.assertEqual([e[3] for e in events if e[0] == "dispatch"], ["hourly"])
        _, events, _ = self.run_once(HALF + 300, publish="on", window=False)
        self.assertEqual([e for e in events if e[0] == "dispatch"], [])

    def test_the_hourly_dispatch_follows_the_first_tick_even_when_it_failed(self):
        rc, events, _ = self.run_once(T0 - 60, ticks=(False, True), publish="on", window=False)
        self.assertEqual(rc, 0)
        self.assertEqual([e[0] for e in events], ["sync", "tick", "dispatch", "tick"])

    # ---- the retry

    def test_a_failed_tick_is_retried_once_at_half_a_period(self):
        rc, events, out = self.run_once(HALF - 120, ticks=(False, True))
        self.assertEqual(rc, 0)
        self.assertEqual(self.times(events, "tick"), [HALF + 1.5, HALF + 900])
        self.assertIn("retrying the failed tick", out)

    def test_two_failed_ticks_exit_1(self):
        rc, events, _ = self.run_once(HALF - 120, ticks=(False, False))
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.times(events, "tick")), 2)

    def test_a_late_capture_that_fails_is_retried_at_half_a_period(self):
        rc, events, _ = self.run_once(T0 + 300, ticks=(False, True))
        self.assertEqual((rc, self.times(events, "tick")), (0, [T0 + 300, T0 + 900]))

    def test_a_late_capture_that_fails_after_the_retry_time_gets_no_retry(self):
        rc, events, out = self.run_once(T0 + 1000, ticks=(False,))
        self.assertEqual((rc, self.times(events, "tick")), (1, [T0 + 1000]))
        self.assertIn("has passed: no retry", out)

    # ---- the report probe

    def test_a_new_report_dispatches_publish_before_the_wait_at_a_30_boundary(self):
        rc, events, _ = self.run_once(HALF - 180, publish="on", reports=(4321,))
        self.assertEqual(rc, 0)
        self.assertEqual([e[0] for e in events], ["probe", "dispatch", "sync", "tick"])
        self.assertEqual(events[1], ("dispatch", HALF - 180, "publish.yml", "report"))

    def test_a_new_report_at_a_00_boundary_waits_for_the_hourly_dispatch(self):
        _, events, out = self.run_once(T0 - 180, publish="on", reports=(4321,))
        self.assertEqual([e[0] for e in events], ["probe", "sync", "tick", "dispatch"])
        self.assertEqual([e[3] for e in events if e[0] == "dispatch"], ["hourly"])
        self.assertIn("the hourly dispatch after the 10:00 tick covers it", out)

    def test_no_probe_outside_the_report_window_or_with_no_probe(self):
        _, events, _ = self.run_once(HALF - 180, publish="on", window=False, reports=(1,))
        self.assertEqual([e[0] for e in events], ["sync", "tick"])
        _, events, _ = self.run_once(HALF - 180, publish="on", reports=(1,), no_probe=True)
        self.assertEqual([e[0] for e in events], ["sync", "tick"])

    def test_one_probe_per_run_after_the_tick_when_the_boundary_is_close_or_passed(self):
        _, events, _ = self.run_once(HALF - 90, publish="on", reports=(1,))
        self.assertEqual([e[0] for e in events], ["sync", "tick", "probe", "dispatch"])
        self.assertGreater(self.times(events, "probe")[0], HALF)
        _, events, _ = self.run_once(HALF + 300, publish="on", reports=())
        self.assertEqual([e[0] for e in events], ["sync", "tick", "probe"])

    def test_a_failing_probe_never_stops_the_capture(self):
        rc, events, out = self.run_once(HALF - 180, publish="on", probe_error=RuntimeError("telegram fetch failed"))
        self.assertEqual(rc, 0)
        self.assertEqual([e[0] for e in events], ["probe", "sync", "tick"])
        self.assertIn("telegram probe failed", out)

    # ---- settling: a reading the tick could not push has no next tick to take it along

    PUSH_FAILED = RuntimeError("could not push to data after 6 attempts")

    @staticmethod
    def lines(out):
        return [line[9:] for line in out.strip().splitlines()]     # without the HH:MM:SS of the log line

    def test_a_reading_the_tick_could_not_push_is_pushed_again_before_the_hourly_dispatch(self):
        rc, events, out = self.run_once(T0 - 60, publish="on", window=False, git=True, unpushed=(True,),
                                        pushes=(self.PUSH_FAILED, "pushed"))
        self.assertEqual(rc, 0)
        self.assertEqual(events, [("sync", T0 - 60), ("tick", T0 + 1.5), ("push", T0 + 21.5), ("push", T0 + 61.5),
                                  ("dispatch", T0 + 61.5, "publish.yml", "hourly")])
        self.assertEqual(self.lines(out)[-4:], [
            "  settle attempt 1 (3 paths not on the branch): failed, RuntimeError: could not push to data after 6 attempts",
            "  settle attempt 2 (3 paths not on the branch): pushed",
            "  settled after 2 attempts: nothing is left unpushed",
            "once done: a snapshot was saved"])

    def test_a_push_that_never_works_stops_at_the_deadline_dispatches_once_and_exits_1(self):
        rc, events, out = self.run_once(T0 - 60, publish="on", window=False, git=True, unpushed=(True,),
                                        pushes=(self.PUSH_FAILED,))
        deadline = T0 + capture.ONCE_SETTLE_S
        pushes = self.times(events, "push")
        self.assertEqual(rc, 1)
        self.assertEqual(pushes[:4], [T0 + 21.5, T0 + 61.5, T0 + 121.5, T0 + 181.5])
        self.assertEqual((len(pushes), pushes[-1]), (26, deadline))     # every 60 s, the last one at the deadline
        self.assertLessEqual(max(self.clock.woke), deadline)
        self.assertEqual(self.clock.now, deadline)
        self.assertEqual([e for e in events if e[0] == "dispatch"], [("dispatch", deadline, "publish.yml", "hourly")])
        self.assertEqual(events[-1][0], "dispatch")
        self.assertEqual(self.lines(out)[-2:], [
            "  settling stopped at 10:25:00 UTC: 3 paths not on the branch",
            "once done: a snapshot was saved, but the reading for the 10:00 UTC boundary could not be pushed and is lost"])

    def test_a_tick_that_pushed_adds_no_push_no_wait_and_no_line(self):
        _, plain_events, plain_out = self.run_once(T0 - 60, publish="on", window=False)    # not a git checkout
        rc, events, out = self.run_once(T0 - 60, publish="on", window=False, git=True, pushes=(self.PUSH_FAILED,))
        self.assertEqual(rc, 0)
        self.assertEqual(events, [("sync", T0 - 60), ("tick", T0 + 1.5), ("dispatch", T0 + 1.5, "publish.yml", "hourly")])
        self.assertEqual((events, self.lines(out)), (plain_events, self.lines(plain_out)))

    def test_no_push_does_not_settle(self):
        rc, events, out = self.run_once(T0 - 60, publish="on", window=False, git=True, unpushed=(True,),
                                        pushes=(self.PUSH_FAILED,), no_push=True)
        self.assertEqual(rc, 0)
        self.assertEqual(events, [("tick", T0 + 1.5), ("dispatch", T0 + 1.5, "publish.yml", "hourly")])
        self.assertNotIn("settl", out)

    def test_an_unexpected_error_in_a_push_does_not_end_the_settling(self):
        rc, events, out = self.run_once(HALF - 60, git=True, unpushed=(True,),
                                        pushes=(OSError("no space left"), self.PUSH_FAILED, "pushed"))
        self.assertEqual((rc, self.times(events, "push")), (0, [HALF + 21.5, HALF + 61.5, HALF + 121.5]))
        self.assertIn("settle attempt 1 (3 paths not on the branch): failed, OSError: no space left", out)

    def test_settling_after_a_failed_tick_stops_at_its_retry_time_and_the_retry_s_reading_goes_out(self):
        rc, events, out = self.run_once(HALF - 120, ticks=(False, True), git=True, unpushed=(True, True),
                                        pushes=[self.PUSH_FAILED] * 16 + ["pushed"])
        retry_at = HALF + 900
        pushes = self.times(events, "push")
        self.assertEqual(rc, 0)
        self.assertEqual(self.times(events, "tick"), [HALF + 1.5, retry_at])
        self.assertEqual(len(pushes), 17)
        self.assertEqual((pushes[15], pushes[16]), (retry_at, retry_at + 20))
        self.assertIn("settling stopped at 10:45:00 UTC", out)
        self.assertIn("retrying the failed tick", out)
        self.assertEqual(self.lines(out)[-1], "once done: a snapshot was saved")


def forbidden(name):
    def call(*a, **k):
        raise AssertionError("shadow called %s" % name)
    return call


def tree(path):
    found = []
    for root, dirs, files in os.walk(path):
        found += [os.path.join(root, f) for f in files]
    return sorted(found)


class Shadow(unittest.TestCase):
    """--shadow: no Omni, venue or Telegram call, no sync, snapshot, commit or dispatch; one branch fetch."""

    def run_shadow(self, store, start, names=("20261124T1000Z.json.gz",)):
        clock = FakeClock(start)
        fetches = []

        def branch_names(s):
            fetches.append(clock.now)
            return None if names is None else list(names)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"VM_PUBLISH": "on"}), \
                mock.patch.object(capture, "branch_names", branch_names), \
                contextlib.ExitStack() as stack, contextlib.redirect_stdout(out):
            for name in ("http_get", "fetch_stats", "tick", "safe_tick", "refresh", "sync", "dispatch", "probe",
                         "in_report_window", "write_log"):
                stack.enter_context(mock.patch.object(capture, name, forbidden(name)))
            stack.enter_context(mock.patch.object(ds, "commit_push", forbidden("commit_push")))
            rc = capture.once(make_args(store, shadow=True), clock=clock, sleep=clock.sleep)
        lines = out.getvalue().strip().splitlines()
        records = [json.loads(line) for line in lines if line.startswith("{")]
        self.assertEqual(len(records), 1, lines)
        return rc, records[0], fetches

    def test_case_a_waits_the_same_way_then_fetches_the_branch_once_150_s_after(self):
        with tempfile.TemporaryDirectory() as store:
            before = tree(store)
            rc, line, fetches = self.run_shadow(store, T0 - 152.25)
            self.assertEqual(tree(store), before)
        self.assertEqual(rc, 0)
        self.assertEqual(fetches, [T0 + 150])
        self.assertEqual(line, {"shadow": True, "case": "a", "boundary": "2026-11-24T10:00:00.000Z",
                                "started": "2026-11-24T09:57:27.750Z", "reached": "2026-11-24T10:00:01.500Z",
                                "checked": "2026-11-24T10:02:30.000Z", "branch_has_snapshot": True,
                                "branch_snapshot": "20261124T1000Z"})

    def test_case_b_reaches_at_once_and_c_does_not_reach(self):
        with tempfile.TemporaryDirectory() as store:
            rc, line, fetches = self.run_shadow(store, T0 + 300, names=())
            self.assertEqual((rc, line["case"], line["reached"], line["branch_has_snapshot"]),
                             (0, "b", "2026-11-24T10:05:00.000Z", False))
            self.assertEqual(fetches, [T0 + 300])
            touch_snapshot(store, "20261124T1000Z")
            before = tree(store)
            rc, line, _ = self.run_shadow(store, T0 + 300)
            self.assertEqual(tree(store), before)
        self.assertEqual((rc, line["case"], line["reached"], line["branch_snapshot"]), (0, "c", None, "20261124T1000Z"))

    def test_a_failed_fetch_is_reported_as_unknown_and_exits_0(self):
        with tempfile.TemporaryDirectory() as store:
            rc, line, _ = self.run_shadow(store, T0 - 100, names=None)
        self.assertEqual((rc, line["branch_has_snapshot"], line["branch_snapshot"]), (0, None, None))


FAKE_GH = """#!/bin/bash
echo "$*" >> "$GH_LOG"
case "$1 $2" in
  "workflow run")
    n=$(cat "$GH_RUNS" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$GH_RUNS"
    if [ "$n" -le "${GH_RUN_FAILS:-0}" ]; then echo "HTTP 422: Cannot trigger a 'workflow_dispatch' on a disabled workflow"; exit 1; fi
    exit 0 ;;
  "workflow enable") exit 0 ;;
  api*)
    if [ -n "${GH_STATE:-}" ]; then echo "$GH_STATE"; exit 0; fi
    echo "HTTP 404: Not Found"; exit 1 ;;
esac
exit 2
"""


class PublishDispatch(unittest.TestCase):
    """A failed dispatch of publish.yml undoes only GitHub's inactivity disable, through a stub gh on PATH."""

    def dispatch(self, run_fails, state):
        with tempfile.TemporaryDirectory() as tmp:
            gh = os.path.join(tmp, "gh")
            with open(gh, "w") as fh:
                fh.write(FAKE_GH)
            os.chmod(gh, 0o755)
            log = os.path.join(tmp, "gh.log")
            env = {"PATH": tmp + os.pathsep + "/usr/bin:/bin", "GITHUB_REPOSITORY": "owner/repo", "GH_LOG": log,
                   "GH_RUNS": os.path.join(tmp, "runs"), "GH_RUN_FAILS": str(run_fails), "GH_STATE": state or ""}
            out = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out):
                ok = capture.dispatch_publish("hourly", False)
            with open(log) as fh:
                calls = fh.read().splitlines()
        return ok, calls, out.getvalue()

    RUN = "workflow run publish.yml -f reason=hourly -R owner/repo"
    STATE = "api repos/owner/repo/actions/workflows/publish.yml --jq .state"

    def test_a_dispatch_that_works_reads_no_state(self):
        ok, calls, _ = self.dispatch(0, "disabled_inactivity")
        self.assertEqual((ok, calls), (True, [self.RUN]))

    def test_disabled_for_inactivity_is_enabled_and_the_dispatch_retried_once(self):
        ok, calls, out = self.dispatch(1, "disabled_inactivity")
        self.assertEqual((ok, calls), (True, [self.RUN, self.STATE, "workflow enable publish.yml -R owner/repo", self.RUN]))
        self.assertIn("publish.yml was disabled for inactivity: enabled again, dispatch retried: ok", out)

    def test_disabled_on_purpose_is_left_alone(self):
        ok, calls, out = self.dispatch(1, "disabled_manually")
        self.assertEqual((ok, calls), (False, [self.RUN, self.STATE]))
        self.assertIn("publish.yml state 'disabled_manually': left alone", out)

    def test_an_unreadable_state_is_left_alone(self):
        ok, calls, out = self.dispatch(1, None)
        self.assertEqual((ok, calls), (False, [self.RUN, self.STATE]))
        self.assertIn("publish.yml state could not be read: left alone", out)

    def test_the_retry_is_made_only_once(self):
        ok, calls, out = self.dispatch(5, "disabled_inactivity")
        self.assertEqual((ok, calls.count(self.RUN), calls.count("workflow enable publish.yml -R owner/repo")), (False, 2, 1))
        self.assertIn("dispatch retried: FAILED", out)


# ---------------------------------------------------------------- real git

GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@example.invalid"]


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd] + GIT_ID + list(args), check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True).stdout


def write(d, rel, data, mode="wb"):
    path = os.path.join(d, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode) as fh:
        fh.write(data)


def gz(data):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as fh:
        fh.write(data)
    return buf.getvalue()


class GitCase(unittest.TestCase):
    """A bare remote with a `data` branch, and depth-1 clones of it, isolated from the user's git config."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        empty = os.path.join(self.tmp.name, "gitconfig")
        open(empty, "w").close()
        env = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": empty, "GIT_CONFIG_NOSYSTEM": "1",
                                           "GIT_TERMINAL_PROMPT": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.remote = os.path.join(self.tmp.name, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "data", self.remote], check=True)
        seed = os.path.join(self.tmp.name, "seed")
        subprocess.run(["git", "init", "-q", "-b", "data", seed], check=True)
        write(seed, "exec/2026-11-24.jsonl", b'{"snap":"20261124T0930Z","src":"live"}\n')
        write(seed, "capture/2026-11-24.log", b'{"t":"2026-11-24T09:30:01Z"}\n')
        write(seed, "snapshots/20261124T0930Z.json.gz", gz(b"0930"))
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "seed")
        git(seed, "push", "-q", "file://" + self.remote, "data")

    def clone(self, name):
        dest = os.path.join(self.tmp.name, name)
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", "data", "file://" + self.remote, dest],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return dest

    def remote_file(self, rel):
        return subprocess.run(["git", "-C", self.remote, "show", "data:" + rel], check=True,
                              stdout=subprocess.PIPE).stdout

    def capture_into(self, d, who, snap_bytes=None):
        """What one tick leaves in the checkout: the snapshot, its book samples and its log line."""
        write(d, "snapshots/20261124T1000Z.json.gz", snap_bytes or gz(b'{"who":"%s"}' % who.encode()))
        write(d, "exec/2026-11-24.jsonl", b'{"snap":"20261124T1000Z","src":"live","who":"%s"}\n' % who.encode(), "ab")
        write(d, "capture/2026-11-24.log", b'{"stamp":"20261124T1000Z","who":"%s"}\n' % who.encode(), "ab")


class SameSnapshotPush(GitCase):
    """Two writers capture the same boundary (the loop and a tick): the second push is rejected."""

    def push_second(self, d):
        out = io.StringIO()
        with mock.patch.object(ds.time, "sleep"), contextlib.redirect_stdout(out):
            result = ds.commit_push(d, "capture 20261124T1000Z")
        return result, out.getvalue()

    def test_the_branch_s_snapshot_is_kept_and_the_rest_still_goes_out(self):
        loop, tick = self.clone("loop"), self.clone("tick")
        self.capture_into(loop, "loop")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ds.commit_push(loop, "capture 20261124T1000Z"), "pushed")
        self.capture_into(tick, "tick")
        result, out = self.push_second(tick)
        self.assertEqual(result, "pushed", out)
        self.assertIn("push rejected (attempt 1)", out)
        self.assertIn("snapshots/20261124T1000Z.json.gz is already on the branch; the branch's copy is kept, "
                      "this job's reading is dropped", out)
        snap = "snapshots/20261124T1000Z.json.gz"
        self.assertEqual(self.remote_file(snap), gz(b'{"who":"loop"}'))
        with open(os.path.join(tick, snap), "rb") as fh:
            self.assertEqual(fh.read(), gz(b'{"who":"loop"}'))
        self.assertEqual(self.remote_file("exec/2026-11-24.jsonl").decode().splitlines(), [
            '{"snap":"20261124T0930Z","src":"live"}',
            '{"snap":"20261124T1000Z","src":"live","who":"loop"}',
            '{"snap":"20261124T1000Z","src":"live","who":"tick"}'])
        self.assertEqual(len(self.remote_file("capture/2026-11-24.log").decode().splitlines()), 3)
        self.assertEqual(git(tick, "status", "--porcelain"), "")
        self.assertEqual(git(tick, "rev-parse", "HEAD"), git(self.remote, "rev-parse", "data"))

    def test_an_identical_snapshot_is_kept_quietly(self):
        loop, tick = self.clone("loop"), self.clone("tick")
        same = gz(b'{"same":true}')
        self.capture_into(loop, "loop", same)
        with contextlib.redirect_stdout(io.StringIO()):
            ds.commit_push(loop, "capture 20261124T1000Z")
        self.capture_into(tick, "tick", same)
        result, out = self.push_second(tick)
        self.assertEqual(result, "pushed", out)
        self.assertIn("is already on the branch; the branch's copy is kept\n", out)
        self.assertEqual(self.remote_file("snapshots/20261124T1000Z.json.gz"), same)


class ShadowAgainstGit(GitCase):
    """The shadow's one fetch reads the branch tip, not the checkout, and leaves the checkout as it was."""

    def test_one_fetch_sees_the_loop_s_push_and_the_checkout_is_unchanged(self):
        store, loop = self.clone("store"), self.clone("loop")
        self.capture_into(loop, "loop")
        with contextlib.redirect_stdout(io.StringIO()):
            ds.commit_push(loop, "capture 20261124T1000Z")
        head, files = git(store, "rev-parse", "HEAD"), tree(os.path.join(store, "snapshots"))
        calls = []
        real_git = ds.git

        def recording_git(d, *args, **kw):
            calls.append(args[0])
            return real_git(d, *args, **kw)
        clock = FakeClock(T0 - 150)
        out = io.StringIO()
        with mock.patch.object(ds, "git", recording_git), contextlib.ExitStack() as stack, \
                contextlib.redirect_stdout(out):
            for name in ("http_get", "fetch_stats", "tick", "safe_tick", "sync", "dispatch", "probe"):
                stack.enter_context(mock.patch.object(capture, name, forbidden(name)))
            rc = capture.once(make_args(store, shadow=True), clock=clock, sleep=clock.sleep)
        line = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertEqual((rc, line["case"], line["branch_has_snapshot"], line["branch_snapshot"]),
                         (0, "a", True, "20261124T1000Z"))
        self.assertEqual(calls, ["fetch", "ls-tree"])
        self.assertEqual(git(store, "rev-parse", "HEAD"), head)
        self.assertEqual(git(store, "status", "--porcelain"), "")
        self.assertEqual(tree(os.path.join(store, "snapshots")), files)


class SettleAgainstGit(GitCase):
    """The real tick and commit_push: a push that fails (the remote cannot be reached) leaves the reading in the
    checkout, and once pushes it when the remote is back instead of exiting with it unpushed."""

    def test_the_reading_reaches_the_branch_with_a_later_attempt(self):
        store = self.clone("store")
        away = self.remote + ".away"
        os.rename(self.remote, away)            # every push and fetch fails until it is back
        clock = FakeClock(T0 - 60)

        def sleep(seconds):
            clock.sleep(seconds)
            if clock.now >= T0 + 60 and os.path.exists(away):
                os.rename(away, self.remote)

        def fetch_stats(url, budget_s):
            return b'{"reading":"1000"}', {"attempts": 1}
        env = {k: v for k, v in os.environ.items() if k != "VM_PUBLISH"}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(ds.time, "sleep"), \
                mock.patch.object(capture, "fetch_stats", fetch_stats), \
                mock.patch.object(capture, "utcnow", lambda: capture.dt.datetime(2026, 11, 24, 10, 0, 1)), \
                contextlib.ExitStack() as stack, contextlib.redirect_stdout(out):
            for name in ("http_get", "refresh", "dispatch", "probe"):
                stack.enter_context(mock.patch.object(capture, name, forbidden(name)))
            rc = capture.once(make_args(store), clock=clock, sleep=sleep)
        log = out.getvalue()
        self.assertEqual(rc, 0, log)
        self.assertIn("commit or push failed, will retry with the next tick", log)
        self.assertIn("settle attempt 1 (2 paths not on the branch): failed, RuntimeError: could not push", log)
        self.assertIn("settle attempt 2 (2 paths not on the branch): pushed", log)
        self.assertEqual(gzip.decompress(self.remote_file("snapshots/20261124T1000Z.json.gz")), b'{"reading":"1000"}')
        self.assertEqual(len(self.remote_file("capture/2026-11-24.log").decode().splitlines()), 2)
        self.assertEqual(git(store, "status", "--porcelain"), "")
        self.assertEqual(git(store, "rev-parse", "HEAD"), git(self.remote, "rev-parse", "data"))


if __name__ == "__main__":
    unittest.main()
