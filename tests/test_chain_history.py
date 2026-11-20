import copy
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import chain_history as ch


BASE = datetime(2026, 11, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 11, 20, 9, 15, tzinfo=timezone.utc)
SAFE_TIME = datetime(2026, 11, 20, 9, tzinfo=timezone.utc)
FLOOR = ch.NITRO_SEARCH_FLOOR
SAFE = FLOOR + int((SAFE_TIME - BASE).total_seconds()) * 4


def block(number):
    return {"number": number, "hash": "0x" + format(number, "064x"),
            "parent_hash": "0x" + format(number - 1, "064x"),
            "timestamp": int(BASE.timestamp()) + (number - FLOOR) // 4}


def supplement():
    return {"chain": {"chain_id": ch.CHAIN_ID, "usdc_contract": ch.USDC,
                      "block_number": SAFE, "block_hash": block(SAFE)["hash"],
                      "block_timestamp": ch.iso(SAFE_TIME), "observed_at": ch.iso(NOW),
                      "accounts": [{"id": key, "address": address, "balance_usdc": "123.456789"}
                                   for key, (_, address) in ch.WALLETS.items()]}}


class FakeClient:
    fail_call = False
    mismatch = False
    mismatch_following = False
    wrong_chain = False
    decimals = 6

    def __init__(self, url, budget):
        self.url, self.budget = url, budget

    def verify_chain(self):
        if self.wrong_chain:
            raise ch.IntegrityError("Unexpected chain; Arbitrum One is required")
        return None

    def block(self, number, fresh=False):
        result = block(number)
        if self.url == ch.OFFICIAL_RPC and number != SAFE and (self.mismatch or
                (self.mismatch_following and (number - FLOOR) % (86400 * 4) == 0)):
            result["hash"] = "0x" + "f" * 64
        return result

    def call(self, method, params):
        assert method == "eth_call"
        if params[0]["data"] == "0x313ce567":
            return "0x" + format(self.decimals, "064x")
        if self.fail_call:
            raise ch.HistoryError("historical state is not available")
        assert params[1]["requireCanonical"] is True
        return "0x" + format(123456789, "064x")


class ChainHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        (self.data / "supplement.json").write_text(json.dumps(supplement()))
        FakeClient.fail_call = False
        FakeClient.mismatch = False
        FakeClient.mismatch_following = False
        FakeClient.wrong_chain = False
        FakeClient.decimals = 6

    def tearDown(self):
        self.tmp.cleanup()

    def refresh(self, **kwargs):
        with patch("chain_history.now_utc", return_value=NOW):
            return ch.refresh(self.data, observed=NOW, client_factory=FakeClient, **kwargs)

    def document(self):
        return json.loads((self.data / "chain-history.json").read_text())

    def test_exact_closing_block_including_duplicate_second_timestamps(self):
        rpc = FakeClient(ch.ARCHIVE_RPC, None)
        result, following = ch.find_close(rpc, date(2026, 11, 3), block(FLOOR), block(SAFE))
        self.assertEqual(result["timestamp"], int(datetime(2026, 11, 3, 23, 59, 59, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(following["number"], result["number"] + 1)
        self.assertEqual(following["timestamp"], result["timestamp"] + 1)
        self.assertEqual(following["parent_hash"], result["hash"])

    def test_weekly_backfill_plus_safe_snapshot_retains_different_kinds(self):
        result = self.refresh(start=date(2026, 11, 3), end=date(2026, 11, 17))
        self.assertEqual((result["points"], result["closes"]), (4, 3))
        data = self.document()
        self.assertEqual([p["date"] for p in data["points"]], ["2026-11-03", "2026-11-10", "2026-11-17", "2026-11-20"])
        self.assertEqual(data["points"][-1]["kind"], "safe_block_observation")
        self.assertTrue(all(p["kind"] == "utc_day_close" for p in data["points"][:-1]))
        self.assertEqual(data["asset"]["decimals_verification"]["value"], 6)
        self.assertEqual(data["archive"]["missing_requested_dates"], [])

    def test_daily_reuse_needs_no_rpc_after_verification_and_no_rewrite(self):
        self.refresh()
        previous = (self.data / "chain-history.json").read_bytes()
        def forbidden(*args):
            self.fail("Routine daily reuse must not construct an RPC client")
        result = ch.refresh(self.data, observed=NOW, client_factory=forbidden)
        self.assertFalse(result["changed"])
        self.assertEqual(result["rpc_calls"], 0)
        self.assertEqual((self.data / "chain-history.json").read_bytes(), previous)

    def test_new_safe_day_appends_without_turning_it_into_a_close(self):
        self.refresh()
        document = supplement()
        tomorrow = NOW + timedelta(days=1)
        document["chain"].update(block_number=SAFE + 4 * 86400,
                                  block_hash=block(SAFE + 4 * 86400)["hash"],
                                  block_timestamp=ch.iso(SAFE_TIME + timedelta(days=1)), observed_at=ch.iso(tomorrow))
        (self.data / "supplement.json").write_text(json.dumps(document))
        outcome = ch.refresh(self.data, observed=tomorrow, client_factory=lambda *args: self.fail("RPC called"))
        self.assertEqual(outcome["points"], 2)
        self.assertEqual(self.document()["coverage"]["closing_point_count"], 0)

    def test_archive_failure_preserves_prior_points_and_marks_missing(self):
        self.refresh(start=date(2026, 11, 3), end=date(2026, 11, 3))
        previous = self.document()["points"]
        FakeClient.fail_call = True
        result = self.refresh(start=date(2026, 11, 10), end=date(2026, 11, 10))
        self.assertIn("historical state", result["failure"])
        self.assertEqual(self.document()["points"], previous)
        self.assertEqual(self.document()["archive"]["missing_requested_dates"], ["2026-11-10"])

    def test_two_provider_disagreement_preserves_entire_existing_file(self):
        self.refresh()
        previous = (self.data / "chain-history.json").read_bytes()
        FakeClient.mismatch = True
        with self.assertRaisesRegex(ch.IntegrityError, "differs between providers"):
            self.refresh(start=date(2026, 11, 3), end=date(2026, 11, 3))
        self.assertEqual((self.data / "chain-history.json").read_bytes(), previous)

    def test_following_block_disagreement_refuses_close(self):
        FakeClient.mismatch_following = True
        with self.assertRaisesRegex(ch.IntegrityError, "differs between providers"):
            self.refresh(start=date(2026, 11, 3), end=date(2026, 11, 3))
        self.assertFalse((self.data / "chain-history.json").exists())

    def test_wrong_archive_chain_preserves_entire_existing_file(self):
        self.refresh()
        previous = (self.data / "chain-history.json").read_bytes()
        FakeClient.wrong_chain = True
        with self.assertRaisesRegex(ch.IntegrityError, "Unexpected chain"):
            self.refresh(start=date(2026, 11, 3), end=date(2026, 11, 3))
        self.assertEqual((self.data / "chain-history.json").read_bytes(), previous)

    def test_bad_decimals_refuse_first_file(self):
        FakeClient.decimals = 18
        with self.assertRaisesRegex(ch.IntegrityError, "decimals"):
            self.refresh()
        self.assertFalse((self.data / "chain-history.json").exists())

    def test_bad_chain_identity_refuses_write(self):
        document = supplement()
        document["chain"]["chain_id"] = 1
        (self.data / "supplement.json").write_text(json.dumps(document))
        with self.assertRaisesRegex(ch.HistoryError, "identity"):
            self.refresh()

    def test_wrong_address_or_duplicate_account_refuses_write(self):
        for variant in ("address", "duplicate"):
            with self.subTest(variant=variant):
                document = supplement()
                if variant == "address":
                    document["chain"]["accounts"][0]["address"] = "0x" + "0" * 40
                else:
                    document["chain"]["accounts"].append(copy.deepcopy(document["chain"]["accounts"][0]))
                (self.data / "supplement.json").write_text(json.dumps(document))
                with self.assertRaises(ch.HistoryError):
                    self.refresh()

    def test_stale_safe_snapshot_refuses_write(self):
        with self.assertRaisesRegex(ch.HistoryError, "stale"):
            ch.refresh(self.data, observed=NOW + timedelta(days=2), client_factory=FakeClient)

    def test_invalid_existing_file_is_not_overwritten(self):
        path = self.data / "chain-history.json"
        path.write_text('{"schema_version":77}')
        with self.assertRaises(ch.HistoryError):
            self.refresh()
        self.assertEqual(path.read_text(), '{"schema_version":77}')

    def test_missing_and_negative_values_never_become_zero(self):
        for value in (None, "-1", "NaN", "Infinity", "0.1234567", "1e6"):
            with self.subTest(value=value), self.assertRaises(ch.HistoryError):
                ch.normalize_amount(value)
        self.assertEqual(ch.normalize_amount("0"), "0.000000")

    def test_uint256_amount_conversion_is_exact(self):
        raw = 2**256 - 1
        rendered = ch.amount(raw)
        self.assertEqual(rendered.replace(".", ""), str(raw))
        self.assertEqual(ch.normalize_amount(rendered), rendered)

    def test_budget_is_serial_bounded_and_refuses_excess(self):
        sleeps = []
        budget = ch.Budget(2, .75, sleeper=sleeps.append, clock=lambda: 0)
        self.assertEqual(budget.take(), 1)
        self.assertEqual(budget.take(), 2)
        self.assertEqual(sleeps, [.75])
        with self.assertRaisesRegex(ch.HistoryError, "budget exhausted"):
            budget.take()

    def test_read_only_rpc_allowlist_and_canonical_hash_are_enforced(self):
        client = ch.RPCClient(ch.ARCHIVE_RPC, ch.Budget(5))
        with self.assertRaisesRegex(ch.HistoryError, "read-only"):
            client.call("eth_sendRawTransaction", ["0x"])
        with self.assertRaisesRegex(ch.HistoryError, "canonical block"):
            client.call("eth_call", [{"to": ch.USDC, "data": "0x313ce567"}, "latest"])
        with self.assertRaisesRegex(ch.HistoryError, "Unapproved public"):
            ch.RPCClient("https://localhost/rpc", ch.Budget())

    def test_rpc_error_stops_without_automatic_retry(self):
        client = ch.RPCClient(ch.ARCHIVE_RPC, ch.Budget(5))
        with patch("chain_history.get_json", return_value={"jsonrpc": "2.0", "id": 1, "error": {"message": "rate limited"}}) as get:
            with self.assertRaisesRegex(ch.HistoryError, "rate limited"):
                client.verify_chain()
            self.assertEqual(get.call_count, 1)

    def test_limits_and_future_days_are_rejected(self):
        with self.assertRaises(ch.HistoryError):
            ch.requested_dates(date(2026, 1, 1), date(2026, 11, 20), 1)
        with self.assertRaises(ch.HistoryError):
            self.refresh(start=date(2026, 11, 20), end=date(2026, 11, 20))

    def test_explicit_dated_backfill_is_bounded_and_preserves_omitted_days(self):
        self.refresh(dates=[date(2026, 11, 2), date(2026, 11, 12)])
        self.assertEqual([p["date"] for p in self.document()["points"]], ["2026-11-02", "2026-11-12", "2026-11-20"])
        with self.assertRaises(ch.HistoryError):
            self.refresh(dates=[date(2026, 11, 2)] * 2)
        with self.assertRaises(ch.HistoryError):
            self.refresh(dates=[date(2026, 11, 2)], start=date(2026, 11, 2), end=date(2026, 11, 2))

    def test_closed_points_are_not_overwritten_by_intraday_snapshots(self):
        point = ch.supplement_point(supplement()["chain"], NOW)
        frozen = dict(point, kind="utc_day_close")
        points = {point["date"]: frozen}
        ch.merge(points, point)
        self.assertIs(points[point["date"]], frozen)

    def test_same_block_changed_balance_is_rejected(self):
        point = ch.supplement_point(supplement()["chain"], NOW)
        previous = dict(point, treasury_usdc="124.000000")
        with self.assertRaisesRegex(ch.HistoryError, "state changed"):
            ch.merge({point["date"]: previous}, point)


if __name__ == "__main__":
    unittest.main()
