"""A biweekly report read on the day it is posted: the open-interest series must not gain that unfinished day."""
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


def _history():
    """The pipeline's `market.history`, imported past the `sources` module that tools/ puts on sys.path for other tests."""
    own = lambda k: k == "sources" or k.startswith("sources.")
    saved_modules = {k: sys.modules.pop(k) for k in list(sys.modules) if own(k)}
    saved_path = list(sys.path)
    sys.path[:] = [str(ROOT / "pipeline")] + [p for p in sys.path if Path(p or ".").resolve() != ROOT / "tools"]
    try:
        from market import history
    finally:
        sys.path[:] = saved_path
        for k in [k for k in sys.modules if own(k)]:
            del sys.modules[k]
        sys.modules.update(saved_modules)
    return history


history = _history()


def report(day, hour, dual_oi):
    return {"t": datetime.strptime("%sT%02d:00:00Z" % (day, hour), "%Y-%m-%dT%H:%M:%SZ"), "oi": Decimal(dual_oi),
            "cum": None, "markets": None, "tokens": None, "url": None}


# DefiLlama's daily open interest: two finished days, the newest being yesterday
DEFILLAMA = {"2026-11-22": 641700000.0, "2026-11-23": 640000000.0}


class ReportPostedToday(unittest.TestCase):
    def test_a_report_dated_after_the_last_figure_adds_no_row(self):
        oi, _ = history.build_oi([report("2026-11-24", 17, "1290000000")], DEFILLAMA, {})
        self.assertEqual(oi["dates"], ["2026-11-22", "2026-11-23"])
        self.assertIsNotNone(oi["total"][-1])
        self.assertEqual(oi["src"], [1, 1])

    def test_the_series_is_the_same_with_and_without_that_report(self):
        early = report("2026-11-20", 9, "1200000000")
        with_today, _ = history.build_oi([early, report("2026-11-24", 17, "1290000000")], DEFILLAMA, {})
        without, _ = history.build_oi([early], DEFILLAMA, {})
        self.assertEqual(with_today, without)

    def test_a_report_before_defillama_begins_still_dates_the_series(self):
        oi, _ = history.build_oi([report("2026-11-20", 9, "1200000000")], DEFILLAMA, {})
        self.assertEqual(oi["dates"], ["2026-11-20", "2026-11-21", "2026-11-22", "2026-11-23"])
        self.assertEqual(oi["src"], [2, None, 1, 1])
        self.assertEqual(oi["total"][0], 60000000000)          # half the dual-sided figure, in cents
        self.assertEqual(oi["dual"][0], 120000000000)          # the figure as reported
        self.assertIsNone(oi["total"][1])


if __name__ == "__main__":
    unittest.main()
