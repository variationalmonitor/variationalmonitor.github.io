"""A listing on its first UTC day: a market file from the intraday window alone, and the checks that accept it."""
from datetime import datetime
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def _market():
    """The pipeline's `market` package. It imports the pipeline's `sources` (a namespace package), which a
    `sources` module on sys.path would shadow: tools/ is there for other tests, and their module is put back."""
    own = lambda k: k == "sources" or k.startswith("sources.")
    saved_modules = {k: sys.modules.pop(k) for k in list(sys.modules) if own(k)}
    saved_path = list(sys.path)
    sys.path[:] = [str(ROOT / "pipeline")] + [p for p in sys.path if Path(p or ".").resolve() != ROOT / "tools"]
    try:
        from market import check, intraday, outputs, store
    finally:
        sys.path[:] = saved_path
        for k in [k for k in sys.modules if own(k)]:
            del sys.modules[k]
        sys.modules.update(saved_modules)
    return check, intraday, outputs, store


check, intraday, outputs, store = _market()

SPEC = importlib.util.spec_from_file_location("gate", ROOT / "qa" / "gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


class Ref:
    """A snapshot in memory, read the way store.Ref is read."""

    def __init__(self, key, listings):
        self.key, self.t = key, datetime.strptime(key, "%Y-%m-%dT%H:%M:%SZ")
        self.snap = {"listings": listings}

    def load(self):
        return self.snap

    def drop(self):
        pass


class Classifier:
    """Classifies by name, so that the test can tell which snapshot's name a market file was derived from."""
    fin_cfg = {}
    by_name = {"Bitcoin": ("crypto_majors", "crypto", None), "Pop Mart": ("tradfi_perp", "equity", None),
               "Pop Mart International Group Limited": ("tradfi_perp", "equity", None)}

    def get(self, ticker, typ, name):
        return self.by_name.get(name, ("other_crypto", "crypto", None))


def listing(ticker, name, vol="10", long_="6", short="4"):
    return {"ticker": ticker, "name": name, "type": "perp", "vol24h": vol, "oi_long": long_, "oi_short": short,
            "funding": {"last": {"annualized_rate": "0.05"}},
            "quotes": {"updated_at": "2026-11-24T09:59:00Z", "size_1k": {"bid": "99.9", "ask": "100.1"}}}


def sealed_day(rows):
    """One sealed day as history.load_days returns it."""
    out = {}
    for tk, (name, group, cls) in rows.items():
        out[tk] = {"name": name, "type": "perp", "group": group, "cls": cls, "sgroup": group,
                   "vol_c": 100000, "long_c": 60000, "short_c": 40000, "funding_ann": 0.05,
                   "fin_long_ann": None, "fin_short_ann": None, "ba": {"1k": [2.0, 1.5, 2.5, 48]}}
    return {"rows": out, "approx": False, "raw": {}, "boundary": datetime(2026, 11, 24)}


def collect(snaps):
    refs = [Ref(k, ls) for k, ls in snaps]
    with patch.object(store, "fin_ref_for", return_value={}):
        return intraday.collect(refs, {}, Classifier())


class FirstDayMarketFile(unittest.TestCase):
    def setUp(self):
        # BTC has a sealed day; POPMART is first seen at 09:30 and renamed at 10:00; the last ticker is unsafe
        self.intra = collect([
            ("2026-11-24T09:00:00Z", [listing("BTC-PERP", "Bitcoin")]),
            ("2026-11-24T09:30:00Z", [listing("BTC-PERP", "Bitcoin"), listing("POPMART-PERP", "Pop Mart", "0.16", "0.16", "0"),
                                      listing("BAD/TICKER", "Bad")]),
            ("2026-11-24T10:00:00Z", [listing("BTC-PERP", "Bitcoin"),
                                      listing("POPMART-PERP", "Pop Mart International Group Limited", "0.16", "0.16", "0"),
                                      listing("BAD/TICKER", "Bad")]),
        ])
        self.days = {"2026-11-23": sealed_day({"BTC-PERP": ("Bitcoin", "crypto_majors", "crypto")})}
        self.files, self.index = outputs.market_files(self.days, ["BTC-PERP", "POPMART-PERP", "BAD/TICKER"], self.intra)

    def test_collect_describes_each_listing_by_its_newest_snapshot(self):
        self.assertEqual(self.intra["listings"]["POPMART-PERP"],
                         {"name": "Pop Mart International Group Limited", "type": "perp", "group": "tradfi_perp", "cls": "equity"})
        self.assertEqual(self.intra["markets"]["POPMART-PERP"]["ts"], ["2026-11-24T09:30Z", "2026-11-24T10:00Z"])

    def test_a_listing_seen_only_in_the_intraday_window_gets_a_file(self):
        f = self.files["POPMART-PERP"]
        self.assertEqual(list(f), list(self.files["BTC-PERP"]))       # the same keys, in the same order
        self.assertEqual({k: f[k] for k in ("ticker", "name", "type", "group", "cls")},
                         {"ticker": "POPMART-PERP", "name": "Pop Mart International Group Limited", "type": "perp",
                          "group": "tradfi_perp", "cls": "equity"})
        self.assertEqual({k: v for k, v in f.items() if isinstance(v, list)}, {k: [] for k in f if isinstance(f[k], list)})
        self.assertEqual(f["intraday"], self.intra["markets"]["POPMART-PERP"])
        self.assertEqual(f["intraday"]["vol24h"], [0.16, 0.16])
        self.assertEqual(self.index["tickers"], ["BTC-PERP", "POPMART-PERP"])

    def test_derivation_matches_the_daily_rows(self):
        # the same listing sealed into a day gives the same name, type, group and class
        snap = self.intra["listings"]["POPMART-PERP"]
        row = store.listing_row(listing("POPMART-PERP", "Pop Mart International Group Limited"),
                                datetime(2026, 11, 25), Classifier(), {}, {})
        sealed, _ = outputs.market_files(
            {"2026-11-24": sealed_day({"POPMART-PERP": (row["name"], row["group"], row["cls"])})}, [], None)
        self.assertEqual({k: sealed["POPMART-PERP"][k] for k in snap}, snap)

    def test_a_listing_with_a_sealed_day_is_unchanged(self):
        f = self.files["BTC-PERP"]
        self.assertEqual(f["dates"], ["2026-11-23"])
        self.assertEqual(f["vol24h"], [1000.0])
        self.assertEqual(f["intraday"]["ts"], ["2026-11-24T09:00Z", "2026-11-24T09:30Z", "2026-11-24T10:00Z"])

    def test_an_unsafe_ticker_still_gets_no_file(self):
        self.assertIn("BAD/TICKER", self.intra["listings"])
        self.assertNotIn("BAD/TICKER", self.files)

    def test_a_missing_name_falls_back_to_the_ticker_stem(self):
        intra = collect([("2026-11-24T10:00:00Z", [listing("NEW-PERP", None)])])
        files, _ = outputs.market_files({}, ["NEW-PERP"], intra)
        self.assertEqual(files["NEW-PERP"]["name"], "NEW")


class FirstDayChecks(unittest.TestCase):
    def daily(self):
        return {"dates": ["2026-11-23"], "vol24h": [1.0], "intraday": {"ts": ["2026-11-24T10:00Z"], "vol24h": [1.0]}}

    def first_day(self):
        return {"dates": [], "vol24h": [], "intraday": {"ts": ["2026-11-24T09:30Z", "2026-11-24T10:00Z"], "vol24h": [0.16, 0.16]}}

    def test_builder_check_needs_a_daily_date_or_an_intraday_point(self):
        self.assertTrue(check.has_history(self.daily()))
        self.assertTrue(check.has_history(self.first_day()))
        self.assertTrue(check.has_history({"dates": ["2026-11-23"]}))
        self.assertFalse(check.has_history({"dates": [], "intraday": {"ts": [], "vol24h": []}}))
        self.assertFalse(check.has_history({"dates": []}))
        self.assertFalse(check.has_history({"dates": [], "intraday": None}))
        self.assertFalse(check.has_history({"dates": [], "intraday": {"ts": None}}))

    def schema_problems(self, docs):
        rep = gate.Report()
        gate.check_schema(rep, docs, datetime(2026, 11, 24, 10, 15))
        row = [r for r in rep.rows if r[1] == "dates ascending, every daily array as long as its dates"]
        self.assertEqual(len(row), 1)
        return row[0][3] if row[0][2] == "fail" else []

    def test_gate_accepts_a_first_day_market_file_only(self):
        no_points = dict(self.first_day(), intraday={"ts": [], "vol24h": []})
        stray = dict(self.first_day(), vol24h=[0.16])         # a daily value without a date is still caught
        problems = self.schema_problems({"market/BTC-PERP": self.daily(), "market/POPMART-PERP": self.first_day(),
                                         "market/EMPTY-PERP": no_points, "market/STRAY-PERP": stray,
                                         "oi_daily": {"dates": [], "total": []}})
        self.assertEqual(sorted(problems), ["market/EMPTY-PERP.json: no dates", "market/STRAY-PERP.json: vol24h has 1 values for 0 dates",
                                            "oi_daily.json: no dates"])


if __name__ == "__main__":
    unittest.main()
