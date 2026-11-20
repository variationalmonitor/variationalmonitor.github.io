import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

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


class FakeClock:
    def __init__(self, start="2026-11-20T08:00:00Z"):
        self.started = markets.parse_time(start)
        self.elapsed = 0
        self.sleeps = []

    def now(self):
        return (self.started + timedelta(seconds=self.elapsed)).isoformat().replace("+00:00", "Z")

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.elapsed += seconds


class MarketTests(unittest.TestCase):
    def normalize(self, data=None, when="2026-11-20T07:01:00Z"):
        return markets.normalize_snapshot(encoded(data), when)

    def test_sidedness_funding_and_full_spread_remain_distinct(self):
        snapshot = self.normalize()
        self.assertEqual(snapshot["totals"]["trader_open_interest_usdc"], "30")
        self.assertEqual(snapshot["totals"]["observed_trader_open_interest_usdc"], "20")
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

    def test_zero_indicative_bid_quarantines_only_that_quote_size(self):
        data = fixture()
        row = data["listings"][0]
        row["ticker"] = "TOWNS-PERP"
        row["quotes"]["size_100k"] = {"bid": "0", "ask": "0.001931"}
        snapshot = self.normalize(data)
        market = next(m for m in snapshot["markets"] if m["ticker"] == "TOWNS-PERP")
        self.assertIsNone(market["quotes"]["size_100k"])
        self.assertEqual(market["quotes"]["base"]["bid"], "99")
        self.assertEqual(market["volume_24h_usdc"], "20")
        self.assertEqual(market["trader_open_interest_usdc"], "15")
        self.assertEqual(snapshot["totals"]["num_markets_observed"], 2)
        self.assertTrue(snapshot["totals"]["market_coverage_complete"])
        self.assertEqual(snapshot["validation"]["volume_24h_residual_usdc"], "0")
        warning = snapshot["validation"]["warnings"][0]
        self.assertEqual(warning["ticker"], "TOWNS-PERP")
        self.assertEqual(warning["source_fields"], ["quotes.size_100k.bid"])
        self.assertEqual(warning["source_values"], {"bid": "0", "ask": "0.001931"})
        self.assertEqual(warning["action"], "quote_size_unavailable")
        self.assertEqual(snapshot["validation"]["quote_size_coverage"]["size_100k"], {"available": 0, "unavailable": 2, "quarantined": 1})
        self.assertEqual(markets.compact_snapshot(snapshot)["markets"][0]["source_warnings"], market["source_warnings"])
        markets.validate_stored_rows(snapshot)

    def test_quote_quarantine_does_not_relax_marks_negative_prices_or_accounting(self):
        for mutation in (
            lambda d: d["listings"][0].update(mark_price="0"),
            lambda d: d["listings"][0]["quotes"].update(size_100k={"bid": "-1", "ask": "1"}),
            lambda d: d["listings"][0].update(volume_24h="-1"),
        ):
            data = fixture();mutation(data)
            with self.assertRaises(markets.DataError):
                self.normalize(data)

    def test_recoverable_quote_warning_allows_fresh_validated_observation(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            previous = markets.update(output, fetcher=lambda: (encoded(), None), now="2026-11-20T07:01:00Z")
            data = fixture()
            data["listings"][0]["quotes"]["size_100k"] = {"bid": "0", "ask": "1"}
            fresh = markets.update(output, fetcher=lambda: (encoded(data), None), now="2026-11-20T08:00:00Z")
            self.assertEqual(fresh["status"]["state"], "ok")
            self.assertEqual(fresh["latest"]["observed_at"], "2026-11-20T08:00:00Z")
            self.assertEqual(fresh["latest"]["totals"], previous["latest"]["totals"])
            self.assertEqual(fresh["latest"]["validation"]["quarantined_quote_sizes"], 1)

    def test_partial_duplicate_or_invalid_financial_payload_rejected(self):
        mutations = [
            lambda d: d.update(num_markets=1),
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

    def test_reported_population_can_exceed_observed_listings(self):
        data = fixture()
        data.update(num_markets=3, total_volume_24h="50")
        snapshot = self.normalize(data)
        totals = snapshot["totals"]
        self.assertEqual((totals["num_markets_reported"], totals["num_markets_observed"]), (3, 2))
        self.assertFalse(totals["market_coverage_complete"])
        self.assertEqual(totals["volume_24h_usdc"], "50")
        self.assertEqual(totals["trader_open_interest_usdc"], "30")
        self.assertEqual(totals["observed_trader_open_interest_usdc"], "20")
        self.assertEqual(snapshot["validation"]["volume_24h_residual_usdc"], "20")
        self.assertEqual(sum(Decimal(p["volume_24h_usdc"]) for p in snapshot["by_contract_type"]), Decimal("30"))
        markets.validate_stored_rows(snapshot)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            markets.update(output, fetcher=lambda: (encoded(data), None), now="2026-11-20T07:01:00Z")
            repeated = markets.update(output, fetcher=lambda: (encoded(data), None), now="2026-11-20T07:02:00Z")
            self.assertEqual(repeated["status"]["state"], "ok")
            self.assertFalse(repeated["latest"]["totals"]["market_coverage_complete"])
        del data["total_volume_24h"]
        with self.assertRaises(markets.DataError):
            self.normalize(data)  # Partial row sums cannot stand in for a missing platform aggregate.

    def test_429_body_is_bounded_and_only_numeric_hint_survives(self):
        body = b'{"retry_after_seconds":2.5,"message":"private-secret-must-not-persist"}'
        stream = io.BytesIO(body)
        stream.read = Mock(wraps=stream.read)
        error = HTTPError(markets.API_URL, 429, "rate limited", {"Retry-After": "4"}, stream)
        opener = Mock()
        opener.open.side_effect = error
        with patch.object(markets, "build_opener", return_value=opener):
            with self.assertRaises(markets.SourceError) as result:
                markets.fetch_public()
        stream.read.assert_called_once_with(markets.MAX_RATE_LIMIT_BODY_BYTES + 1)
        self.assertEqual(result.exception.retry_after, "4")
        self.assertEqual(result.exception.retry_after_seconds, "2.5")
        self.assertNotIn("private-secret", str(result.exception.__dict__))
        self.assertTrue(stream.closed)

    def test_invalid_retry_hints_are_ignored(self):
        invalid = [b'{}', b'[]', b'{"retry_after_seconds":true}', b'{"retry_after_seconds":-1}',
            b'{"retry_after_seconds":NaN}', b'{"retry_after_seconds":"Infinity"}',
            b'{"retry_after_seconds":1e-100000000}', b'{"retry_after_seconds":1e100000000}',
            b'{"retry_after_seconds":315360001}', b'{"retry_after_seconds":"Bearer secret"}',
            b'{"retry_after_seconds":1,"retry_after_seconds":2}',
            b'{"retry_after_seconds":1,"padding":"' + b'x' * markets.MAX_RATE_LIMIT_BODY_BYTES + b'"}']
        for raw in invalid:
            with self.subTest(raw=raw[:90]):
                self.assertIsNone(markets.rate_limit_body_hint(raw))
        for header in ("Bearer secret", "-5", "1.5", "5\r\nX-Secret: token", "999999999999999999999"):
            self.assertIsNone(markets.SourceError("http_429", header).retry_after)

    def test_longest_valid_retry_instruction_wins(self):
        start = "2026-11-20T08:00:00Z"
        for header, body, delay in (("15", "4", 15), ("4", "15", 15),
            ("Fri, 20 Nov 2026 08:00:20 GMT", "5", 20), (None, "2.5", 3)):
            with self.subTest(header=header, body=body):
                deadline = markets.instructed_retry_time(start, markets.SourceError("http_429", header, body))
                self.assertEqual((deadline - markets.parse_time(start)).total_seconds(), delay)
        deadline = markets.retry_deadline(start, markets.SourceError("http_429", "900", "5"))
        self.assertEqual(deadline, "2026-11-20T08:15:00Z")

    def test_short_retry_success_records_fresh_receipt_time(self):
        clock = FakeClock()
        calls = []
        def fetcher():
            calls.append(clock.now())
            clock.elapsed += 2
            if len(calls) == 1:
                raise markets.SourceError("http_429", "3", "5")
            return encoded(), "Fri, 20 Nov 2026 08:00:09 GMT"
        with tempfile.TemporaryDirectory() as temp:
            result = markets.update(Path(temp), fetcher=fetcher, clock=clock.now, sleeper=clock.sleep, monotonic=clock.monotonic)
        self.assertEqual(clock.sleeps, [5])
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"]["state"], "ok")
        self.assertEqual(result["status"]["fetch_attempts"], 2)
        self.assertEqual(result["status"]["last_attempt_at"], "2026-11-20T08:00:07Z")
        self.assertEqual(result["latest"]["observed_at"], "2026-11-20T08:00:09Z")
        self.assertEqual(result["latest"]["provenance"]["retrieved_at"], "2026-11-20T08:00:09Z")
        self.assertEqual(result["generated_at"], "2026-11-20T08:00:09Z")

    def test_second_429_stops_and_retains_financial_history(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            good = markets.update(output, fetcher=lambda: (encoded(), None), now="2026-11-20T07:01:00Z")
            before = (output / "market-history.json").read_bytes()
            clock = FakeClock()
            calls = []
            def limited():
                calls.append(clock.now())
                raise markets.SourceError("http_429", retry_after_seconds="5")
            result = markets.update(output, fetcher=limited, clock=clock.now, sleeper=clock.sleep, monotonic=clock.monotonic)
            self.assertEqual(len(calls), 2)
            self.assertEqual(clock.sleeps, [5])
            self.assertEqual(result["latest"], good["latest"])
            self.assertEqual(result["status"]["retry_not_before"], "2026-11-20T08:05:05Z")
            self.assertEqual(result["status"]["retry_after_seconds"], "5")
            self.assertEqual((output / "market-history.json").read_bytes(), before)

    def test_long_unknown_or_budget_exhausted_delay_never_retries(self):
        cases = [("90", "5", 0), (None, None, 0), ("invalid", "-1", 0), (None, "5", 40)]
        for header, body, elapsed in cases:
            with self.subTest(header=header, body=body, elapsed=elapsed):
                clock = FakeClock()
                calls = []
                def limited():
                    calls.append(1)
                    clock.elapsed += elapsed
                    raise markets.SourceError("http_429", header, body)
                with self.assertRaises(markets.SourceError):
                    markets.fetch_with_short_retry(limited, clock=clock.now, sleeper=clock.sleep, monotonic=clock.monotonic)
                self.assertEqual(len(calls), 1)
                self.assertEqual(clock.sleeps, [])

    def test_interrupted_wait_never_retries_early(self):
        clock = FakeClock()
        fetcher = Mock(side_effect=markets.SourceError("http_429", retry_after_seconds="5"))
        sleeper = Mock()  # Does not advance the mock clock: interrupted wait.
        with self.assertRaises(markets.SourceError):
            markets.fetch_with_short_retry(fetcher, clock=clock.now, sleeper=sleeper, monotonic=clock.monotonic)
        self.assertEqual(fetcher.call_count, 1)
        sleeper.assert_called_once_with(5)

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
