"""Financial integrity and public-source trust-boundary regression tests."""
import copy
from decimal import Decimal
import html
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import financials as f

NOW = "2026-11-20T09:00:00Z"
REPORT = """Welcome back to the Variational biweekly update!
METRICS
Total Volume Traded: $475.77B
Current Dual-Sided OI: $1.54B
TVL (Excludes OLP Hedging Accounts): $269.28M
Total Markets Currently Listed: 589
2w Spreads Paid (Gross Revenue): $4,908,930
2w Net Revenue: $3,208,936
2w Rewards: ($217,813)
Referral Rewards: ($217,813)
2w Net Profit: $2,991,123
2W Protocol Treasury Inflow: $981,786
2W OLP PnL: $2,009,337
Lifetime OLP PnL: $26,492,418
Current Protocol Treasury Holdings: 13,121,923 USDC
"""


def source(post_id="459", platform="telegram", published="2026-11-13T18:51:03Z"):
    prefix = "https://t.me/variational_io/" if platform == "telegram" else "https://x.com/variational_io/status/"
    return {"platform": platform, "id": post_id, "url": prefix + post_id,
            "author": "variational_io", "published_at": published,
            "observed_at": NOW, "retrieval_method": "public source test fixture"}


def record(text=REPORT, **kwargs):
    return f.parse_report(text, source(**kwargs))


def message(text=REPORT, post_id="459", owner="variational_io", forwarded=False, preview="", published="2026-11-13T18:51:03+00:00"):
    forward = '<div class="tgme_widget_message_forwarded_from">Forwarded</div>' if forwarded else ""
    body = html.escape(text).replace("\n", "<br/>")
    return (f'<div class="tgme_widget_message_wrap"><div data-post="variational_io/{post_id}">'
            f'<a class="tgme_widget_message_owner_name" href="https://t.me/{owner}">Variational</a>'
            f'{forward}<div class="tgme_widget_message_text js-message_text">{body}</div>'
            f'<div class="link_preview_description">{html.escape(preview)}</div>'
            f'<time datetime="{published}"></time></div></div>')


def dataset(report=None):
    return f.finalize([report or record()], NOW, {"initialized": True, "last_seen_message_id": "459", "feed_url": f.FEED_URL, "history_gap": None})


class FinancialParsingTests(unittest.TestCase):
    def test_exact_amounts_units_and_missing_costs(self):
        r = record()
        self.assertEqual(r["period_amounts"]["rewards"], "-217813")
        self.assertEqual(r["snapshot"]["cumulative_volume"], "475770000000.00")
        self.assertEqual(r["snapshot"]["treasury_holdings"], "13121923")
        self.assertEqual(r["units"]["treasury_holdings"], "USDC")
        self.assertEqual(r["units"]["net_profit"], "$")
        self.assertIsNone(r["period_amounts"]["market_making_costs"])
        self.assertIsNone(r["period_start"])
        self.assertIsNone(r["period_end"])

    def test_early_heading_and_historical_labels_stay_distinct(self):
        text = REPORT.replace("the Variational", "our").replace("Total Markets Currently Listed", "Total Tokens Currently Listed")
        text = text.replace("Current Protocol Treasury Holdings: 13,121,923 USDC", "Total On-Chain Protocol Treasury Value: $13,121,923")
        r = record(text)
        self.assertIsNone(r["snapshot"]["markets"])
        self.assertEqual(r["snapshot"]["tokens_listed"], "589")
        self.assertIsNone(r["snapshot"]["treasury_holdings"])
        self.assertEqual(r["snapshot"]["onchain_treasury_value"], "13121923")

    def test_negative_olp_is_not_dropped(self):
        r = record(REPORT.replace("2W OLP PnL: $2,009,337", "2W OLP PnL: ($36,821)"))
        self.assertEqual(r["period_amounts"]["olp_pnl"], "-36821")

    def test_source_arithmetic_warning_preserves_reported_profit(self):
        d = dataset(record(REPORT.replace("2w Net Profit: $2,991,123", "2w Net Profit: $2,991,128")))
        r = d["reports"][0]
        self.assertEqual(r["period_amounts"]["net_profit"], "2991128")
        self.assertEqual(r["warnings"][0]["residual"], "-5")

    def test_missing_required_line_rejected(self):
        with self.assertRaises(f.SourceError):
            record(REPORT.replace("2w Net Profit: $2,991,123\n", ""))

    def test_unknown_financial_label_requires_review(self):
        with self.assertRaises(f.SourceError):
            record(REPORT + "2w New Income: $10\n")

    def test_duplicate_field_rejected(self):
        with self.assertRaises(f.SourceError):
            record(REPORT + "2w Net Revenue: $3,208,936\n")

    def test_nonreport_is_ignored(self):
        self.assertIsNone(record("Weekly growth! Net profit is great."))

    def test_unrecognized_report_heading_fails_closed(self):
        with self.assertRaises(f.SourceError):
            record(REPORT.replace("Variational biweekly update", "financial statement"))

    def test_wrong_origin_and_numeric_ids_rejected(self):
        for change in ({"author": "variational_fake"}, {"id": 459}, {"url": "https://example.com/459"}):
            candidate = source(); candidate.update(change)
            with self.assertRaises(f.SourceError):
                f.parse_report(REPORT, candidate)

    def test_no_unit_substitution_or_unsigned_deduction(self):
        for text in (REPORT.replace("13,121,923 USDC", "$13,121,923"), REPORT.replace("($217,813)", "$217,813")):
            with self.assertRaises(f.SourceError):
                record(text)

    def test_untrusted_numeric_markup_rejected(self):
        with self.assertRaises(f.SourceError):
            record(REPORT.replace("$2,991,123", "$2,991,123<script>alert(1)</script>"))


class FeedAndMergeTests(unittest.TestCase):
    def test_feed_discovers_ids_and_ignores_forwarded_and_previews(self):
        page = message() + message(post_id="460", forwarded=True) + message(text="Ordinary news", post_id="461", preview=REPORT)
        reports, ids = f.parse_feed(page, NOW)
        self.assertEqual(ids, ["459", "460", "461"])
        self.assertEqual(len(reports), 1)

    def test_spoofed_channel_is_rejected(self):
        with self.assertRaises(f.SourceError):
            f.parse_feed(message(owner="unofficial"), NOW)
        with self.assertRaises(f.SourceError):
            f.parse_feed(message().replace('data-post="variational_io/', 'data-post="spoof/'), NOW)

    def test_duplicate_message_ids_and_empty_feed_rejected(self):
        for page in (message() + message(), "<html>temporary error</html>"):
            with self.assertRaises(f.SourceError):
                f.parse_feed(page, NOW)

    def test_exact_duplicate_is_noop(self):
        rows = [record()]
        before = copy.deepcopy(rows)
        self.assertFalse(f.merge_report(rows, record(), "2026-11-21T00:00:00Z"))
        self.assertEqual(rows, before)

    def test_mirror_deduplicates_and_keeps_real_timestamps(self):
        rows = [record(post_id="2121317860740063501", platform="x", published="2026-11-13T19:02:44Z")]
        self.assertTrue(f.merge_report(rows, record(), NOW))
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]["sources"]), 2)
        self.assertEqual(rows[0]["published_at"], "2026-11-13T18:51:03Z")

    def test_identical_same_platform_repost_is_not_double_counted(self):
        rows = [record()]
        self.assertTrue(f.merge_report(rows, record(post_id="460"), NOW))
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]["sources"]), 2)

    def test_cross_source_conflict_does_not_mutate(self):
        rows = [record(post_id="2121317860740063501", platform="x")]
        before = copy.deepcopy(rows)
        with self.assertRaises(f.SourceError):
            f.merge_report(rows, record(REPORT.replace("$2,991,123", "$2,991,128")), NOW)
        self.assertEqual(rows, before)

    def test_known_source_edit_creates_revision_and_warning(self):
        rows = [record()]
        revised = record(REPORT.replace("$2,991,123", "$2,991,128"))
        self.assertTrue(f.merge_report(rows, revised, NOW))
        d = f.finalize(rows, NOW)
        self.assertEqual(len(d["reports"][0]["revisions"]), 1)
        self.assertEqual(d["reports"][0]["revisions"][0]["period_amounts"]["net_profit"], "2991123")
        self.assertEqual(d["totals"]["period_amounts"]["net_profit"], "2991128")
        self.assertTrue(d["reports"][0]["warnings"])

    def test_unchanged_refresh_preserves_all_static_metadata(self):
        original = dataset()
        page = message() + message(text="new ordinary post", post_id="463")
        updated, changed = f.refresh_dataset(original, lambda before: page, "2026-11-21T00:00:00Z")
        self.assertFalse(changed)
        self.assertIs(updated, original)
        self.assertEqual(updated["coverage"]["discovery"]["last_seen_message_id"], "459")

    def test_new_report_refresh_is_atomic_in_memory(self):
        original = dataset()
        next_text = REPORT.replace("$26,492,418", "$28,501,755")
        page = message() + message(text=next_text, post_id="480", published="2026-11-27T18:00:00Z")
        updated, changed = f.refresh_dataset(original, lambda before: page, "2026-11-28T00:00:00Z")
        self.assertTrue(changed)
        self.assertEqual(len(updated["reports"]), 2)
        self.assertEqual(len(original["reports"]), 1)
        self.assertEqual(updated["totals"]["period_amounts"]["net_profit"], "5982246")

    def test_catchup_gap_is_bounded_and_preserves_original(self):
        original = dataset()
        calls = []
        def fetch(before):
            post_id = str(1000 - len(calls)); calls.append(before)
            return message(text="ordinary post", post_id=post_id)
        with self.assertRaises(f.SourceError):
            f.refresh_dataset(original, fetch, NOW)
        self.assertEqual(len(calls), f.MAX_FEED_PAGES - 2)
        self.assertEqual(original["coverage"]["discovery"]["last_seen_message_id"], "459")

    def test_removed_financial_body_keeps_last_good(self):
        original = dataset()
        with self.assertRaises(f.SourceError):
            f.refresh_dataset(original, lambda before: message(text="This report was withdrawn."), NOW)
        self.assertEqual(original["reports"][0]["period_amounts"]["net_profit"], "2991123")

    def test_feed_url_redirect_allowlist(self):
        self.assertTrue(f.allowed_feed_url(f.FEED_URL))
        self.assertTrue(f.allowed_feed_url(f.FEED_URL + "?before=459"))
        for url in ("http://t.me/s/variational_io", "https://evil.test/", f.FEED_URL + "bad", f.FEED_URL + "?url=evil", "https://user@t.me/s/variational_io"):
            self.assertFalse(f.allowed_feed_url(url))


class DatasetTests(unittest.TestCase):
    def test_historical_golden_totals_and_stock_exclusion(self):
        path = Path(__file__).resolve().parents[1] / "public/data/financials.json"
        d = json.loads(path.read_text())
        f.validate_dataset(d)
        reports = [r for r in d["reports"] if r["published_at"] < "2026-11-14"]
        self.assertEqual(len(reports), 19)
        covered = f.finalize(reports, NOW)
        self.assertEqual(covered["totals"]["period_amounts"], {
            "gross_revenue": "64653135", "market_making_costs": "-15488037",
            "net_revenue": "30819751", "rewards": "-3456245", "net_profit": "27673543",
            "treasury_inflow": "12530285", "olp_pnl": "15143258"})
        self.assertEqual(covered["totals"]["field_coverage"]["market_making_costs"]["reported_count"], 12)
        self.assertEqual(covered["coverage"]["unresolved_warning_count"], 3)
        self.assertEqual(reports[-1]["snapshot"]["lifetime_olp_pnl"], "26492418")
        self.assertNotIn("lifetime_olp_pnl", covered["totals"]["period_amounts"])
        self.assertEqual(sum(Decimal(r["period_amounts"]["olp_pnl"]) < 0 for r in reports), 1)
        self.assertEqual(reports[0]["snapshot"]["tokens_listed"], "470")
        self.assertIsNone(reports[0]["snapshot"]["markets"])

    def test_source_transcription_tamper_is_rejected(self):
        d = dataset()
        d["reports"][0]["period_amounts"]["net_profit"] = "1"
        d["totals"]["period_amounts"]["net_profit"] = "1"
        with self.assertRaises(f.SourceError):
            f.validate_dataset(d)

    def test_warning_and_partial_coverage_tamper_rejected(self):
        d = dataset(record(REPORT.replace("2w Net Profit: $2,991,123", "2w Net Profit: $2,991,128")))
        d["reports"][0]["warnings"] = []
        with self.assertRaises(f.SourceError):
            f.validate_dataset(d)
        d = dataset(); d["totals"]["field_coverage"]["market_making_costs"]["complete"] = True
        with self.assertRaises(f.SourceError):
            f.validate_dataset(d)

    def test_atomic_write_rejects_bad_data_without_touching_last_good(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "financials.json"
            f.atomic_write(path, dataset())
            before = path.read_bytes()
            broken = dataset(); broken["totals"]["period_amounts"]["net_profit"] = "0"
            with self.assertRaises(f.SourceError):
                f.atomic_write(path, broken)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(len(list(path.parent.iterdir())), 1)

    def test_unchanged_cli_does_not_write_or_update_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "financials.json"
            f.atomic_write(path, dataset())
            before = path.read_bytes(); mtime = path.stat().st_mtime_ns
            with patch.object(f, "PublicFetcher", return_value=lambda before: message()):
                self.assertEqual(f.main(["refresh", "--output", str(path)]), 0)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(path.stat().st_mtime_ns, mtime)

    def test_failure_cli_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "financials.json"
            f.atomic_write(path, dataset())
            before = path.read_bytes()
            with patch.object(f, "PublicFetcher", return_value=lambda before: "blocked"):
                self.assertEqual(f.main(["refresh", "--output", str(path)]), 1)
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
