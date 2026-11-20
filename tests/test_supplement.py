import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from execution import compare
from supplement import CATALOG, chain_snapshot, public_research, read_json, token_record


class SupplementalDataTests(unittest.TestCase):
    def setUp(self):
        self.when = datetime.now(timezone.utc)
        self.catalog = read_json(CATALOG)

    def test_scheduled_content_cannot_enter_public_payload(self):
        entry = self.catalog["research"]["articles"][0]
        scheduled = dict(entry, status="scheduled", published_at=None, canonical_url=None,
                         body="Pending publication")
        with self.assertRaisesRegex(ValueError, "publication status"):
            public_research({"research": {"articles": [scheduled]}})

    def test_published_articles_require_public_publisher_url(self):
        item = dict(self.catalog["research"]["articles"][0], status="published",
                    published_at="2026-11-20T01:00:00Z", canonical_url="https://private.example/work",
                    body="An approved text")
        with self.assertRaisesRegex(ValueError, "public publisher"):
            public_research({"research": {"articles": [item]}})
        item["canonical_url"] = "https://4pillars.io/en/articles/public-article"
        self.assertEqual(public_research({"research": {"articles": [item]}})["articles"][0], item)

    def test_preview_listing_suppresses_market_placeholders(self):
        payload = {"id": "variational", "symbol": "var", "preview_listing": True,
                   "market_data": {"current_price": {"usd": 9}, "circulating_supply": 0}}
        record = token_record(payload, self.catalog, self.when)
        self.assertEqual(record["status"], "preview")
        self.assertIsNone(record["price_usd"])
        self.assertIsNone(record["circulating_supply"])
        self.assertEqual(sum(r["percent"] for r in record["allocations"]), 100)

    def test_original_research_observations_keep_each_window_and_require_publication(self):
        record = self.catalog["research"]["articles"][0]
        public_url = "https://4pillars.io/en/articles/public-article"
        rows = [{"metric": "Observed markets", "period": "First observation window", "value": "10",
                 "source_url": public_url},
                {"metric": "Volume share", "period": "Second observation window", "value": "15%",
                 "source_url": public_url}]
        item = dict(record, status="scheduled", canonical_url=None, published_at=None, original_observations=rows)
        with self.assertRaisesRegex(ValueError, "publication status"):
            public_research({"research": {"articles": [item]}})
        item.update(status="published", published_at="2020-04-03T12:00:00+09:00", canonical_url=public_url)
        self.assertEqual(public_research({"research": {"articles": [item]}})["articles"][0]["original_observations"], rows)

    def test_original_research_requires_a_source_and_its_observation_period(self):
        public_url = "https://4pillars.io/en/articles/public-article"
        item = dict(self.catalog["research"]["articles"][0], status="published", canonical_url=public_url,
                    published_at="2020-04-03T12:00:00+09:00")
        base = {"metric": "Observed markets", "period": "March 2020", "value": "10", "source_url": public_url}
        for overrides in ({"period": ""}, {"source_url": "https://app.notion.com/p/private"},
                          {"source_url": "https://viewer:password@example.com/source"}, {"private_note": "draft"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                public_research({"research": {"articles": [dict(item, original_observations=[base | overrides])]}})

    def test_future_publication_time_cannot_publish_draft(self):
        item = dict(self.catalog["research"]["articles"][0], status="published",
                    canonical_url="https://4pillars.io/en/articles/public-article",
                    published_at=(self.when + timedelta(days=1)).isoformat())
        with self.assertRaisesRegex(ValueError, "Publication time"):
            public_research({"research": {"articles": [item]}})

    def test_changed_chain_is_rejected(self):
        with patch("supplement.rpc", return_value="0x1"):
            with self.assertRaisesRegex(ValueError, "Unexpected chain"):
                chain_snapshot(None)

    def test_paired_spread_requires_recent_quotes_and_sufficient_book(self):
        book = {"coin": "BTC", "time": int(self.when.timestamp() * 1000),
                "levels": [[{"px": "99", "sz": "2000"}], [{"px": "101", "sz": "2000"}]]}
        quote = {"bid": "99.5", "ask": "100.5"}
        row = compare("BTC", quote, self.when.isoformat(), book, self.when)
        self.assertEqual((row["omni_bps"], row["peer_book_bps"], row["peer_total_bps"]), (100, 200, 209))
        old = (self.when - timedelta(seconds=55)).isoformat()
        self.assertIsNone(compare("BTC", quote, old, book, self.when))
        thin = copy.deepcopy(book)
        thin["levels"][0][0]["sz"] = "1"
        self.assertIsNone(compare("BTC", quote, self.when.isoformat(), thin, self.when))


if __name__ == "__main__":
    unittest.main()
