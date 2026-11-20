"""Date integrity, append-only capture, public-field and no-lookahead tests."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import research_baselines as b
from tools import financials as f

DATA = Path(__file__).resolve().parents[1] / "public/data"


def article(**changes):
    value = {"id": "robinhood-with-a-citadel-inside", "status": "scheduled",
             "scheduled_date": "2026-11-24", "published_at": None, "canonical_url": None,
             "body": "PRIVATE_DRAFT_CANARY", "thesis": "PRIVATE_THESIS_CANARY"}
    value.update(changes)
    return value


def snapshot(when="2026-11-23T19:15:00Z", **changes):
    value = copy.deepcopy(json.loads((DATA / "markets.json").read_text())["latest"])
    value["observed_at"] = when
    value["date"] = when[:10]
    value["provenance"].update(kind="live_public_api", retrieved_at=when, timestamp_basis="http_retrieval")
    value.update(changes)
    return value


def financial_data():
    return json.loads((DATA / "financials.json").read_text())


def clock(value="2026-11-24T00:00:00Z"):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def capture(articles=None, snapshots=None, financial=None, store=None, now=None):
    return b.capture(store or b.empty_store(), articles or [article()], snapshots or [snapshot()], financial or financial_data(), now or clock())


class BaselineTests(unittest.TestCase):
    def test_same_kst_day_locks_without_declaring_publication(self):
        store, result = capture()
        self.assertTrue(result["changed"])
        r = store["articles"][0]
        self.assertEqual(r["target_date"], "2026-11-24")
        self.assertEqual(r["market_observed_at"], "2026-11-23T19:15:00Z")
        self.assertEqual(r["publication_status_at_capture"], "scheduled")
        self.assertEqual(r["baseline_kind"], "scheduled_publication_day")
        self.assertNotIn("PRIVATE", json.dumps(store))
        self.assertEqual(set(r["indicators"]), set(b.UNITS))

    def test_future_article_is_pending(self):
        store, result = capture(now=clock("2026-11-20T09:00:00Z"))
        self.assertFalse(result["changed"])
        self.assertFalse(store["articles"])
        self.assertEqual(result["pending"][0]["reason"], "target_date_in_future")

    def test_later_day_is_never_substituted(self):
        store, result = capture(snapshots=[snapshot("2026-11-25T00:00:00Z")], now=clock("2026-11-26T00:00:00Z"))
        self.assertFalse(store["articles"])
        self.assertEqual(result["missing"][0]["reason"], "no_same_day_live_retrieval")

    def test_actual_retrieval_date_beats_a_backdated_source_label(self):
        s = snapshot(); s["provenance"]["retrieved_at"] = "2026-11-25T00:00:00Z"
        store, result = capture(snapshots=[s], now=clock("2026-11-26T00:00:00Z"))
        self.assertFalse(store["articles"])
        self.assertTrue(result["missing"])

    def test_archive_import_is_not_a_fresh_publication_day_observation(self):
        s = snapshot(); s["provenance"].update(kind="archived_public_api", retrieved_at=None)
        store, result = capture(snapshots=[s])
        self.assertFalse(store["articles"])
        self.assertTrue(result["pending"])

    def test_future_retrieval_cannot_be_captured(self):
        store, result = capture(snapshots=[snapshot("2026-11-24T14:00:00Z")], now=clock("2026-11-24T00:00:00Z"))
        self.assertFalse(store["articles"])

    def test_eligible_historical_day_can_be_locked_later_without_changing_its_time(self):
        store, _ = capture(now=clock("2026-11-26T00:00:00Z"))
        r = store["articles"][0]
        self.assertEqual(r["market_retrieved_at"], "2026-11-23T19:15:00Z")
        self.assertTrue(r["recorded_at"].startswith("2026-11-26"))

    def test_same_second_fractional_timestamp_is_preserved(self):
        store, _ = capture(snapshots=[snapshot("2026-11-23T19:15:00.800000Z")], now=clock("2026-11-23T19:15:00.900000Z"))
        self.assertTrue(store["articles"][0]["recorded_at"].endswith("00.900000Z"))

    def test_existing_baseline_is_immutable_after_source_changes(self):
        store, _ = capture()
        original = copy.deepcopy(store)
        changed_snapshot = snapshot("2026-11-24T01:00:00Z")
        changed_snapshot["totals"]["volume_24h_usdc"] = "99999999999"
        result, state = capture(store=store, snapshots=[changed_snapshot], now=clock("2026-11-25T00:00:00Z"))
        self.assertEqual(result, original)
        self.assertFalse(state["changed"])
        self.assertTrue(state["locked"])

    def test_new_article_appends_without_rewriting_earlier_entry(self):
        store, _ = capture()
        old = copy.deepcopy(store["articles"][0])
        next_article = article(id="second-public-article", scheduled_date="2026-11-25")
        updated, state = capture(store=store, articles=[article(), next_article], snapshots=[snapshot("2026-11-24T19:15:00Z")], now=clock("2026-11-25T00:00:00Z"))
        self.assertEqual(len(updated["articles"]), 2)
        self.assertEqual(updated["articles"][0], old)
        self.assertEqual(len(store["articles"]), 1)

    def test_partial_listings_suppress_swap_share_only(self):
        s = snapshot(); s["totals"]["num_markets_reported"] = s["totals"]["num_markets_observed"] + 1
        store, _ = capture(snapshots=[s])
        indicators = store["articles"][0]["indicators"]
        self.assertIsNone(indicators["swap_volume_share_pct"]["value"])
        self.assertFalse(indicators["swap_volume_share_pct"]["coverage"]["complete_listing_coverage"])
        self.assertIsNotNone(indicators["num_markets_observed"]["value"])
        self.assertIsNotNone(indicators["trader_open_interest_usdc"]["value"])

    def test_period_sums_and_lifetime_stock_are_distinct(self):
        store, _ = capture()
        metrics = store["articles"][0]["indicators"]
        self.assertEqual(metrics["reported_net_profit_cumulative"]["value"], "27673543")
        self.assertEqual(metrics["reported_net_profit_cumulative"]["coverage"]["report_count"], 19)
        self.assertEqual(metrics["lifetime_olp_pnl"]["value"], "26492418")
        self.assertTrue(metrics["lifetime_olp_pnl"]["source_observed_at"].startswith("2026-11-13"))

    def test_later_financial_revision_does_not_leak_into_earlier_baseline(self):
        data = financial_data(); rows = copy.deepcopy(data["reports"])
        last = rows[-1]
        text = "Welcome back to the Variational biweekly update!\n" + "\n".join(last["labels"][key] + ": " + raw for key, raw in last["raw_values"].items())
        text = text.replace("2w Net Profit: $2,991,123", "2w Net Profit: $2,991,128")
        src = copy.deepcopy(next(s for s in last["sources"] if s["platform"] == "telegram"))
        src["observed_at"] = "2026-11-25T00:00:00Z"
        updated = f.parse_report(text, src)
        f.merge_report(rows, updated, src["observed_at"])
        later_ledger = f.finalize(rows, src["observed_at"])
        store, _ = capture(financial=later_ledger, now=clock("2026-11-26T00:00:00Z"))
        self.assertEqual(store["articles"][0]["indicators"]["reported_net_profit_cumulative"]["value"], "27673543")

    def test_financial_unknown_at_capture_remains_unavailable(self):
        data = financial_data()
        for report in data["reports"]:
            for source in report["sources"]:
                source["observed_at"] = "2026-11-26T00:00:00Z"
        store, _ = capture(financial=data, now=clock("2026-11-27T00:00:00Z"))
        r = store["articles"][0]
        self.assertIsNone(r["indicators"]["reported_net_profit_cumulative"]["value"])
        self.assertEqual(r["coverage"]["missing_known_versions_count"], 19)

    def test_published_article_uses_actual_publication_day(self):
        a = article(status="published", published_at="2026-11-24T02:00:00Z", canonical_url="https://4pillars.io/research/public-article")
        store, _ = capture(articles=[a], now=clock("2026-11-24T03:00:00Z"))
        self.assertEqual(store["articles"][0]["baseline_kind"], "publication_day")
        self.assertNotIn("body", store["articles"][0])

    def test_future_publication_and_private_canonical_url_are_rejected(self):
        for changes in ({"published_at": "2026-11-25T00:00:00Z", "canonical_url": "https://4pillars.io/public"},
                        {"published_at": "2026-11-24T00:00:00Z", "canonical_url": "https://app.notion.com/private"}):
            with self.assertRaises(b.BaselineError):
                capture(articles=[article(status="published", **changes)])

    def test_unexpected_public_record_fields_and_units_fail_verification(self):
        store, _ = capture()
        store["articles"][0]["draft"] = "CANARY"
        with self.assertRaises(b.BaselineError): b.validate_store(store)
        store, _ = capture()
        store["articles"][0]["indicators"]["volume_24h_usdc"]["unit"] = "USD"
        with self.assertRaises(b.BaselineError): b.validate_store(store)

    def test_latest_summary_must_match_actual_collected_data(self):
        actual = snapshot()
        summary = copy.deepcopy(actual)
        summary["partition_format"] = "latest"
        summary["totals"]["volume_24h_usdc"] = "1"
        with patch.object(b.markets, "read_existing", return_value=[summary]), patch.object(b, "read_json", return_value={"latest": actual}):
            with self.assertRaises(b.BaselineError):
                b.load_snapshots(DATA)

    def test_atomic_write_and_idempotent_bytes(self):
        store, _ = capture()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research-baselines.json"
            b.write_atomic(path, store)
            before = path.read_bytes()
            broken = copy.deepcopy(store); broken["articles"][0]["market_retrieved_at"] = "2026-11-25T00:00:00Z"
            with self.assertRaises(b.BaselineError): b.write_atomic(path, broken)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(len(list(path.parent.iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
