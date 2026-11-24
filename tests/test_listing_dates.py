"""Listing dates for listings not yet in classification.json: dated at build time by the rule --classify stores."""
from datetime import datetime
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def _pipeline():
    """The pipeline's `market` package and build_market.py. Both import the pipeline's `sources` (a namespace
    package), which a `sources` module on sys.path would shadow: tools/ is there for other tests, and their
    module is put back."""
    own = lambda k: k == "sources" or k.startswith("sources.")
    saved_modules = {k: sys.modules.pop(k) for k in list(sys.modules) if own(k)}
    saved_path = list(sys.path)
    sys.path[:] = [str(ROOT / "pipeline")] + [p for p in sys.path if Path(p or ".").resolve() != ROOT / "tools"]
    try:
        from market import classify, listings, outputs, store
        spec = importlib.util.spec_from_file_location("build_market_under_test", ROOT / "pipeline" / "build_market.py")
        build_market = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build_market)
    finally:
        sys.path[:] = saved_path
        for k in [k for k in sys.modules if own(k)]:
            del sys.modules[k]
        sys.modules.update(saved_modules)
    return classify, listings, outputs, store, build_market


classify, listings, outputs, store, build_market = _pipeline()

SPEC = importlib.util.spec_from_file_location("gate", ROOT / "qa" / "gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

POP = "Pop Mart International Group Limited"
# the store's listing register: BTC in the first stored snapshot, POPMART first seen by the half-hourly capture
SEEN = {
    "BTC-PERP": {"name": "Bitcoin", "type": "perp", "first": "2026-01-22T23:15:00Z", "last": "2026-11-24T10:00:00Z", "marks": ["floor"]},
    "HUT-PERP": {"name": "Hut 8 Corp.", "type": "perp", "first": "2026-11-20T10:08:42Z", "last": "2026-11-24T10:00:00Z", "marks": ["zero"]},
    "POPMART-PERP": {"name": POP, "type": "perp", "first": "2026-11-24T09:30:00Z", "last": "2026-11-24T10:00:00Z", "marks": ["zero"]},
}
PLATFORM = {"2026-11-24T09:30:00Z": {"src": "snapshots/20261124T0930Z.json.gz"}}
# sealed days: the first stored snapshot closes 22 Jan; HUT is first in the day the 20 Nov snapshot closes
DAYS = {"2026-01-22": {"rows": {"BTC-PERP": {}}, "raw": {"boundary_src": "snapshots_seed/20260122T2315Z.json.gz"}},
        "2026-11-19": {"rows": {"BTC-PERP": {}, "HUT-PERP": {}}, "raw": {"boundary_src": "legacy:2026-11-20.json"}}}
CONFIG = {"swap_financing": {}, "listings": {
    "BTC-PERP": {"group": "crypto_majors", "cls": "crypto", "name": "Bitcoin", "basis": "major",
                 "listed": {"date": "2026-01-22", "evidence": "snapshot", "source": "snapshots_seed/20260122T2315Z.json.gz"}},
    "HUT-PERP": {"group": "tradfi_perp", "cls": "equity", "name": "Hut 8 Corp.", "basis": "funding_zero",
                 "listed": {"date": "2026-11-19", "evidence": "announcement", "source": "https://t.me/variational_io/462"}},
}}


def post(pid, time, symbol, name):
    return {"id": pid, "time": time, "text": "New announcement from 333cas333 in Discord:\n\n%s`$%s` perps are now live with zero fees and "
            "up to 20x leverage.\n\nX Post\n\nhttps://omni.variational.io/perpetual/%s\n\n@ here" % (name, symbol, symbol)}


HUT_POST = post(462, "2026-11-19T20:58:16Z", "HUT", "Hut 8")


class Fixture(unittest.TestCase):
    """A classification file, the store's register, posts and platform rows, all in memory or a temporary folder."""
    posts = [HUT_POST]
    config = CONFIG

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "classification.json")
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self.config, fh)
        for target, attr, value in ((classify, "PATH", self.path), (listings, "load_posts", lambda: (self.posts, "test")),
                                    (store, "load_platform", lambda: PLATFORM)):
            p = patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def dates(self):
        return listings.listing_dates(classify.Classifier(SEEN), DAYS, SEEN)


class MissingFromConfig(Fixture):
    def test_dated_by_its_first_raw_snapshot(self):
        every, dated, computed = self.dates()[:3]
        self.assertEqual(every["POPMART-PERP"], {"group": "tradfi_perp", "cls": "equity", "name": POP, "basis": "funding_zero"})
        self.assertEqual(dated["POPMART-PERP"], {"date": "2026-11-24", "evidence": "snapshot", "source": "snapshots/20261124T0930Z.json.gz"})
        self.assertEqual(listings.listed_date(dated["POPMART-PERP"], min(DAYS)), "2026-11-24")

    def test_the_raw_snapshot_day_not_the_sealed_day(self):
        # first seen at 00:05 UTC: that snapshot closes the day before, but the listing's date is its own UTC day
        seen = dict(SEEN, **{"NEW-PERP": dict(SEEN["POPMART-PERP"], name="New Corp.", first="2026-11-25T00:05:00Z")})
        days = dict(DAYS, **{"2026-11-24": {"rows": {"NEW-PERP": {}}, "raw": {"boundary_src": "snapshots/20261125T0005Z.json.gz"}}})
        dated = listings.listing_dates(classify.Classifier(seen), days, seen)[1]
        self.assertEqual(dated["NEW-PERP"]["date"], "2026-11-25")

    def test_a_markets_latest_row_carries_the_date(self):
        class Ref:
            key, t = "2026-11-24T10:00:00Z", datetime(2026, 11, 24, 10)

            def load(self):
                quotes = {"updated_at": "2026-11-24T09:59:00Z", "size_1k": {"bid": "99.9", "ask": "100.1"}}
                return {"platform": {}, "listings": [
                    {"ticker": tk, "name": SEEN[tk]["name"], "type": "perp", "vol24h": "1", "oi_long": "1", "oi_short": "0",
                     "funding": {"last": {"annualized_rate": "0"}}, "quotes": quotes} for tk in ("BTC-PERP", "POPMART-PERP")]}

            def drop(self):
                pass

        dated = self.dates()[1]
        with patch.object(store, "fin_ref_for", return_value={}):
            latest = outputs.latest_json(Ref(), classify.Classifier(SEEN), min(DAYS), dated)[0]
        self.assertEqual({r["ticker"]: r["listed"] for r in latest["rows"]}, {"BTC-PERP": None, "POPMART-PERP": "2026-11-24"})


class EarlierAnnouncement(Fixture):
    posts = [HUT_POST, post(470, "2026-11-23T20:00:00Z", "POPMART", "Pop Mart")]

    def test_an_earlier_announcement_wins(self):
        self.assertEqual(self.dates()[1]["POPMART-PERP"],
                         {"date": "2026-11-23", "evidence": "announcement", "source": "https://t.me/variational_io/470"})


class OldAnnouncement(Fixture):
    # the same symbol announced months before the capture first saw it: an earlier listing, not this one
    posts = [HUT_POST, post(350, "2026-07-15T12:00:00Z", "POPMART", "Pop Mart")]

    def test_a_post_long_before_the_first_snapshot_does_not_date_it(self):
        self.assertEqual(self.dates()[1]["POPMART-PERP"],
                         {"date": "2026-11-24", "evidence": "snapshot", "source": "snapshots/20261124T0930Z.json.gz"})


class AnnouncementAtTheLeadLimit(Fixture):
    posts = [HUT_POST, post(469, "2026-11-22T00:10:00Z", "POPMART", "Pop Mart")]

    def test_a_post_two_days_before_still_dates_it(self):
        self.assertEqual(self.dates()[1]["POPMART-PERP"]["date"], "2026-11-22")
        self.assertEqual(self.dates()[1]["POPMART-PERP"]["evidence"], "announcement")


class AnnouncementPastTheLeadLimit(Fixture):
    posts = [HUT_POST, post(468, "2026-11-21T23:50:00Z", "POPMART", "Pop Mart")]

    def test_a_post_three_days_before_does_not(self):
        self.assertEqual(self.dates()[1]["POPMART-PERP"]["date"], "2026-11-24")
        self.assertEqual(self.dates()[1]["POPMART-PERP"]["evidence"], "snapshot")


class LaterAnnouncement(Fixture):
    posts = [HUT_POST, post(471, "2026-11-25T08:00:00Z", "POPMART", "Pop Mart")]

    def test_a_later_announcement_does_not_move_it(self):
        self.assertEqual(self.dates()[1]["POPMART-PERP"]["date"], "2026-11-24")
        self.assertEqual(self.dates()[1]["POPMART-PERP"]["evidence"], "snapshot")


class SameDayAnnouncement(Fixture):
    posts = [HUT_POST, post(472, "2026-11-24T12:00:00Z", "POPMART", "Pop Mart")]

    def test_a_post_the_same_day_is_the_evidence(self):
        self.assertEqual(self.dates()[1]["POPMART-PERP"], {"date": "2026-11-24", "evidence": "announcement",
                                                           "source": "https://t.me/variational_io/472"})


class ConfigEntry(Fixture):
    # a stored date the rule would no longer give: the first sealed day, a day before the raw snapshot's
    config = {"swap_financing": {}, "listings": dict(CONFIG["listings"], **{
        "POPMART-PERP": {"group": "tradfi_perp", "cls": "equity", "name": POP, "basis": "funding_zero",
                         "listed": {"date": "2026-11-23", "evidence": "snapshot", "source": "manual"}}})}
    posts = [HUT_POST, post(473, "2026-11-22T08:00:00Z", "POPMART", "Pop Mart")]

    def test_a_config_entry_keeps_its_stored_value(self):
        every, dated, computed = self.dates()[:3]
        self.assertEqual(dated["POPMART-PERP"], self.config["listings"]["POPMART-PERP"]["listed"])
        self.assertEqual(computed["POPMART-PERP"]["date"], "2026-11-22")          # what the rule gives, not used
        self.assertEqual(every["POPMART-PERP"], self.config["listings"]["POPMART-PERP"])
        self.assertEqual(dated["HUT-PERP"], CONFIG["listings"]["HUT-PERP"]["listed"])
        self.assertEqual(dated["BTC-PERP"], CONFIG["listings"]["BTC-PERP"]["listed"])
        self.assertIsNone(listings.listed_date(dated["BTC-PERP"], min(DAYS)))      # live when capture began


class Classify(Fixture):
    def run_classify(self):
        with patch.object(build_market.store, "list_refs", return_value=["ref"]), \
                patch.object(build_market.store, "ingest", return_value={"snapshots": 1, "added": 0, "duplicates": 0, "listings_seen": 3}), \
                patch.object(build_market.store, "load_seen", return_value=SEEN), \
                patch.object(build_market.history, "load_days", return_value=DAYS), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(build_market.main(["--classify"]), 0)
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)["listings"]

    def test_classify_stores_the_date_shown_at_build_time(self):
        shown = self.dates()[1]["POPMART-PERP"]
        stored = self.run_classify()
        self.assertEqual(stored["POPMART-PERP"], dict(classify.entry_by_rule("POPMART-PERP", SEEN["POPMART-PERP"]), listed=shown))
        self.assertEqual({tk: stored[tk] for tk in CONFIG["listings"]}, CONFIG["listings"])
        # the build after --classify shows the same date, now from the file
        self.assertEqual(self.dates()[1]["POPMART-PERP"], shown)


def snapshot(folder, stamp, tickers):
    with gzip.open(os.path.join(folder, stamp + ".json.gz"), "wt", encoding="utf-8") as fh:
        json.dump({"listings": [{"ticker": tk} for tk in tickers]}, fh)


class GateCheck(unittest.TestCase):
    """The gate's check of rows not in classification.json, against raw snapshots it reads itself."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.raw, self.store = os.path.join(tmp.name, "snapshots"), os.path.join(tmp.name, "store")
        os.makedirs(self.raw)
        os.makedirs(os.path.join(self.store, "telegram"))
        snapshot(self.raw, "20260122T2315Z", ["BTC-PERP"])
        snapshot(self.raw, "20261124T0900Z", ["BTC-PERP"])
        snapshot(self.raw, "20261124T0930Z", ["BTC-PERP", "POPMART-PERP"])
        snapshot(self.raw, "20261124T1000Z", ["BTC-PERP", "POPMART-PERP"])
        self.seen({"POPMART-PERP": {"first": "2026-11-24T09:30:00Z"}})
        self.posts([])
        for attr, value in (("SNAPSHOT_DIRS", [self.raw]), ("STORE", self.store)):
            p = patch.object(gate, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def seen(self, d):
        with open(os.path.join(self.store, "listings_seen.json"), "w") as fh:
            json.dump(d, fh)

    def posts(self, posts):
        with open(os.path.join(self.store, "telegram", "posts.json"), "w") as fh:
            json.dump({"posts": posts}, fh)

    def problems(self, listed, data_day="2026-11-25"):
        return gate.unfiled_listing_dates([{"ticker": "POPMART-PERP", "name": POP, "listed": listed}], data_day)

    def test_the_right_date_passes(self):
        self.assertEqual(self.problems("2026-11-24"), [])
        self.assertEqual(self.problems("2026-11-24", "2026-11-24"), [])

    def test_a_wrong_date_fails(self):
        self.assertEqual(len(self.problems("2026-11-23")), 1)            # earlier, and no post names it
        self.assertEqual(len(self.problems("2026-11-25")), 1)            # after the first raw snapshot that lists it
        self.assertEqual(len(self.problems("2026-11-24", "2026-11-23")), 1)   # after the data day
        self.assertEqual(len(self.problems("2026-01-21")), 1)            # before the first stored snapshot
        self.assertEqual(len(self.problems(None)), 1)
        self.assertEqual(len(self.problems("24 Nov 2026")), 1)

    def test_an_earlier_date_needs_a_post_that_day_naming_it(self):
        self.posts([post(470, "2026-11-23T20:00:00Z", "POPMART", "Pop Mart")])
        self.assertEqual(self.problems("2026-11-23"), [])
        self.posts([post(470, "2026-11-23T20:00:00Z", "HUT", "Hut 8")])
        self.assertEqual(len(self.problems("2026-11-23")), 1)

    def test_the_index_must_agree_with_the_raw_files(self):
        self.seen({"POPMART-PERP": {"first": "2026-11-24T10:00:00Z"}})
        self.assertEqual(len(self.problems("2026-11-24")), 1)
        self.seen({"POPMART-PERP": {"first": "2026-11-24T09:00:00Z"}})   # that file is on disk and does not list it
        self.assertEqual(len(self.problems("2026-11-24")), 1)

    def test_a_removed_first_snapshot_falls_back_to_the_index(self):
        os.remove(os.path.join(self.raw, "20261124T0930Z.json.gz"))
        self.assertEqual(self.problems("2026-11-24"), [])
        self.seen({"POPMART-PERP": {"first": "2026-11-23T22:00:00Z"}})   # removed too: the index's day stands
        self.assertEqual(len(self.problems("2026-11-24")), 1)
        self.assertEqual(self.problems("2026-11-23"), [])


if __name__ == "__main__":
    unittest.main()
