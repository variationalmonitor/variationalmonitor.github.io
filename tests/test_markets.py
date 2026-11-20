import copy
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("markets", Path(__file__).resolve().parents[1] / "tools" / "markets.py")
markets = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(markets)


def fixture():
    return {"total_volume_24h": "30", "cumulative_total_volume": "300", "tvl": "80", "open_interest": "60", "num_markets": 2,
        "loss_refund": {"pool_size": "0", "refunded_24h": "0"},
        "listings": [
            {"ticker": "BTC-PERP", "contract_type": "perpetual", "name": "Bitcoin", "mark_price": "100", "volume_24h": "20",
             "open_interest": {"long_open_interest": "9", "short_open_interest": "6"},
             "funding": {"last": {"annualized_rate": "0.03", "interval_rate": "0.000027"}, "next": {"annualized_rate": "-0.04", "interval_rate": "-0.000036"}, "interval_seconds": 28800, "next_timestamp": 1795161600},
             "base_spread_bps": "101", "quotes": {"updated_at": "2026-11-20T07:00:00Z", "base": {"bid": "99", "ask": "101"}, "size_1k": {"bid": "99", "ask": "101"}}},
            {"ticker": "XAU-SWAP", "contract_type": "swap", "name": "Swap on Gold", "mark_price": "200", "volume_24h": "10",
             "open_interest": {"long_open_interest": "2", "short_open_interest": "3"},
             "funding": {"last": {"annualized_long_rate": "0.05", "annualized_short_rate": "0.02", "interval_long_rate": "0.001", "interval_short_rate": "0.002"}, "next": None, "interval_seconds": 86400, "next_timestamp": 1795212300}, "quotes": None} ]}


def encoded(data=None):
    return json.dumps(fixture() if data is None else data).encode()


class MarketTests(unittest.TestCase):
    def normalize(self, data=None, when="2026-11-20T07:01:00Z"):
        return markets.normalize_snapshot(encoded(data), when)

    def test_sidedness_funding_and_full_spread_remain_distinct(self):
        snapshot = self.normalize()
        self.assertEqual(snapshot["totals"]["trader_open_interest_usdc"], "20")
        # A different legitimate aggregate/trader ratio is not forced to 2x.
        self.assertEqual(snapshot["validation"]["reported_oi_to_trader_oi_ratio"], "3.000000000000")
        perp, swap = snapshot["markets"]
        self.assertEqual(perp["quotes"]["base"]["full_spread_bps"], "200.000000000000")
        self.assertEqual(perp["base_spread_bps"], "101")
        self.assertEqual(perp["funding"]["next"]["annualized_rate"], "-0.04")
        self.assertEqual(swap["funding"]["last"]["interval_long_rate"], "0.001")
        self.assertIsNone(swap["funding"]["next"])
        self.assertIsNone(perp["quotes"]["size_1m"])
        self.assertIsNone(swap["quotes"]["updated_at"])

    def test_verified_cumulative_key_aliases_and_conflicts(self):
        old = fixture()
        old["cumulative_volume"] = old.pop("cumulative_total_volume")
        self.assertEqual(self.normalize(old)["provenance"]["cumulative_volume_source_field"], "cumulative_volume")
        old["cumulative_total_volume"] = "301"
        with self.assertRaisesRegex(markets.DataError, "conflicting_cumulative"):
            self.normalize(old)

    def test_partial_duplicate_or_invalid_financial_payload_rejected(self):
        mutations = [
            lambda d: d.update(num_markets=3),
            lambda d: d["listings"][1].update(ticker="BTC-PERP", contract_type="perpetual"),
            lambda d: d.update(total_volume_24h="40"),
            lambda d: d["listings"][0].update(volume_24h="-1"),
            lambda d: d["listings"][0].update(mark_price="NaN"),
            lambda d: d["listings"][0].update(name="<script>alert(1)</script>"),
            lambda d: d["listings"][0]["quotes"].update(updated_at="2026-11-21T00:00:00Z"),
            lambda d: d["listings"][0]["quotes"]["base"].update(bid="102"),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                data = fixture()
                mutate(data)
                with self.assertRaises(markets.DataError):
                    self.normalize(data)

    def test_history_same_day_replacement_and_visible_gaps(self):
        first = self.normalize(when="2026-11-20T07:01:00Z")
        later = self.normalize(when="2026-11-20T08:00:00Z")
        earlier = copy.deepcopy(first)
        earlier.update(date="2026-11-18", observed_at="2026-11-18T12:00:00Z")
        merged = markets.merge_history([first, earlier], [later])
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[-1]["observed_at"], "2026-11-20T08:00:00Z")
        self.assertEqual(markets.coverage(merged)["missing_dates"], ["2026-11-19"])

    def test_same_day_import_cannot_regress_cumulative_counter(self):
        first = self.normalize(when="2026-11-20T07:01:00Z")
        later = self.normalize(when="2026-11-20T08:00:00Z")
        later["totals"]["cumulative_total_volume_usdc"] = "299"
        with self.assertRaisesRegex(markets.DataError, "cumulative_volume_regression"):
            markets.merge_history([first], [later])

    def test_rate_limit_keeps_exact_last_good_and_history(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            good = markets.update(output, fetcher=lambda: (encoded(), None), now="2026-11-20T07:01:00Z")
            previous_history = (output / "market-history.json").read_bytes()
            def rate_limited():
                raise markets.SourceError("http_429", "120")
            fallback = markets.update(output, fetcher=rate_limited, now="2026-11-20T08:00:00Z")
            self.assertEqual(fallback["latest"], good["latest"])
            self.assertEqual(fallback["status"]["state"], "last_known_good")
            self.assertEqual(fallback["status"]["retry_after"], "120")
            self.assertEqual((output / "market-history.json").read_bytes(), previous_history)

    def test_persisted_retry_after_prevents_premature_request(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            markets.update(output, fetcher=lambda: (encoded(), None), now="2026-11-20T07:01:00Z")
            def limited():
                raise markets.SourceError("http_429", "900")
            markets.update(output, fetcher=limited, now="2026-11-20T07:02:00Z")
            def unexpected_request():
                self.fail("Source must not be requested inside persisted Retry-After")
            result = markets.update(output, fetcher=unexpected_request, now="2026-11-20T07:05:00Z")
            self.assertEqual(result["status"]["error"], "rate_limit_backoff")
            self.assertEqual(result["status"]["retry_not_before"], "2026-11-20T07:17:00Z")

    def test_ambiguous_json_is_rejected(self):
        with self.assertRaisesRegex(markets.DataError, "duplicate_json_key"):
            markets.load_source_bytes(b'{"tvl":"1","tvl":"2"}')
        with self.assertRaisesRegex(markets.DataError, "nonfinite_json"):
            markets.load_source_bytes(b'{"tvl":NaN}')

    def test_cumulative_regression_does_not_replace_history(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            good = markets.update(output, fetcher=lambda: (encoded(), None), now="2026-11-20T07:01:00Z")
            lower = fixture()
            lower["cumulative_total_volume"] = "299"
            fallback = markets.update(output, fetcher=lambda: (encoded(lower), None), now="2026-11-20T08:00:00Z")
            self.assertEqual(fallback["latest"], good["latest"])
            self.assertEqual(fallback["status"]["error"], "cumulative_volume_regression")

    def test_first_failure_creates_no_empty_dataset(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            with self.assertRaises(markets.DataError):
                markets.update(output, fetcher=lambda: (b"{}", None), now="2026-11-20T08:00:00Z")
            self.assertFalse((output / "markets.json").exists())

    def test_no_private_fields_or_paths_are_copied(self):
        data = fixture()
        data["private_note"] = "/Users/private/secret"
        data["listings"][0]["website"] = "javascript:alert(1)"
        output = json.dumps(self.normalize(data))
        self.assertNotIn("private_note", output)
        self.assertNotIn("javascript", output)
        self.assertNotIn("/Users/", output)

    def test_partition_retention_keeps_slim_index_and_reuses_latest(self):
        snapshots = []
        for date in ("2025-11-19", "2026-10-20", "2026-11-01", "2026-11-20"):
            snapshot = self.normalize()
            snapshot.update(date=date, observed_at=date + "T07:01:00Z")
            snapshots.append(snapshot)
        latest = {"schema_version": 1, "source": markets.SOURCE, "latest": snapshots[-1], "status": {}}
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            history, writes, removals = markets.prepare_publication(output, snapshots, latest)
            markets.publish_files(output, latest, history, writes, removals)
            self.assertEqual(history["schema_version"], 2)
            self.assertEqual([s["partition_format"] for s in history["snapshots"]], ["summary", "compact", "full", "latest"])
            self.assertTrue(all("markets" not in entry for entry in history["snapshots"]))
            self.assertIsNone(history["snapshots"][0]["partition_url"])
            self.assertEqual(history["snapshots"][-1]["partition_url"], "data/markets.json")
            self.assertFalse(markets.partition_path(output, "2026-11-20").exists())
            compact = markets.load_partition(output, history["snapshots"][1])
            self.assertEqual(compact["markets"][0]["quotes"]["base"], {"full_spread_bps": "200.000000000000"})
            self.assertNotIn("name", compact["markets"][0])
            self.assertEqual(compact["markets"][0]["funding"], snapshots[1]["markets"][0]["funding"])

    def test_unchanged_failure_does_not_rewrite_dated_partitions(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            yesterday = fixture()
            yesterday["listings"][0]["quotes"]["updated_at"] = "2026-11-19T07:00:00Z"
            markets.update(output, fetcher=lambda: (encoded(yesterday), None), now="2026-11-19T07:01:00Z")
            markets.update(output, fetcher=lambda: (encoded(), None), now="2026-11-20T07:01:00Z")
            partition = markets.partition_path(output, "2026-11-19")
            before = (partition.read_bytes(), partition.stat().st_mtime_ns, (output / "market-history.json").read_bytes())
            def limited():
                raise markets.SourceError("http_429")
            markets.update(output, fetcher=limited, now="2026-11-20T08:01:00Z")
            after = (partition.read_bytes(), partition.stat().st_mtime_ns, (output / "market-history.json").read_bytes())
            self.assertEqual(before, after)

    def test_partition_integrity_and_paths_are_enforced(self):
        with self.assertRaises(markets.DataError):
            markets.partition_path(Path("/tmp"), "../../private")
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            yesterday = fixture()
            yesterday["listings"][0]["quotes"]["updated_at"] = "2026-11-19T07:00:00Z"
            markets.update(output, fetcher=lambda: (encoded(yesterday), None), now="2026-11-19T07:01:00Z")
            markets.update(output, fetcher=lambda: (encoded(), None), now="2026-11-20T07:01:00Z")
            index = json.loads((output / "market-history.json").read_text())
            entry = index["snapshots"][0]
            markets.partition_path(output, entry["date"]).write_text("{}")
            with self.assertRaisesRegex(markets.DataError, "partition_integrity"):
                markets.load_partition(output, entry)
            entry["partition_url"] = "../private.json"
            with self.assertRaisesRegex(markets.DataError, "unsafe_partition"):
                markets.load_partition(output, entry)

    def test_storage_budget_failure_retains_previous_files(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            markets.update(output, fetcher=lambda: (encoded(), None), now="2026-11-20T07:01:00Z")
            before = {p.name: p.read_bytes() for p in output.glob("*.json")}
            with patch.object(markets, "MAX_MARKET_DATA_BYTES", 1):
                with self.assertRaisesRegex(markets.DataError, "storage_budget"):
                    markets.update(output, fetcher=lambda: (encoded(), None), now="2026-11-20T08:01:00Z")
            self.assertEqual(before, {p.name: p.read_bytes() for p in output.glob("*.json")})


if __name__ == "__main__":
    unittest.main()
