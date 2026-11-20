#!/usr/bin/env python3
"""Append dated public-data baselines; never publish articles or invent dates.

Run after collectors: python3 tools/research_baselines.py --data-dir public/data
Validate only:       python3 tools/research_baselines.py verify --data-dir public/data
No network calls. A caller must serialize publication jobs, as for other collectors.
"""
from __future__ import annotations

import argparse
import copy
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

if __package__:
    from . import financials, markets
else:
    import financials
    import markets

DEFAULT_TIMEZONE = "Asia/Seoul"
UNITS = {
    "volume_24h_usdc": "USDC", "trader_open_interest_usdc": "USDC",
    "num_markets_observed": "count", "swap_volume_share_pct": "%",
    "reported_net_profit_cumulative": "$", "lifetime_olp_pnl": "$",
}
MAX_BYTES = 5_000_000
MAX_RECORDS = 500
ENTRY_KEYS = {
    "article_id", "target_date", "timezone", "baseline_kind",
    "publication_status_at_capture", "recorded_at", "market_observed_at",
    "market_retrieved_at", "market_source_sha256", "financial_report_published_at",
    "coverage", "indicators",
}


class BaselineError(ValueError):
    pass


def timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise BaselineError("Missing timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BaselineError("Invalid timestamp") from exc
    if result.tzinfo is None:
        raise BaselineError("Timestamp requires timezone")
    return result.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def day(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise BaselineError("Invalid target date")
    return date.fromisoformat(value)


def number(value, *, nonnegative=False) -> str:
    if isinstance(value, bool) or value is None or not re.fullmatch(r"-?\d+(?:\.\d+)?", str(value)):
        raise BaselineError("Invalid decimal value")
    result = Decimal(str(value))
    if not result.is_finite() or abs(result) > Decimal("1e20") or (nonnegative and result < 0):
        raise BaselineError("Decimal outside supported bounds")
    return format(result, "f") if result else "0"


def read_json(path: Path, maximum=MAX_BYTES) -> dict:
    raw = path.read_bytes()
    if len(raw) > maximum:
        raise BaselineError("Input exceeds byte budget")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise BaselineError("Expected a JSON object")
    return result


def empty_store() -> dict:
    return {"schema_version": 1, "baseline_timezone": DEFAULT_TIMEZONE, "articles": []}


def public_source(url) -> bool:
    if not isinstance(url, str):
        return False
    if url in {markets.API_URL, markets.DOC_URL}:
        return True
    return re.fullmatch(r"https://(?:x\.com/variational_io/status|t\.me/variational_io)/\d+", url) is not None


def validate_store(store: dict) -> None:
    if set(store) != {"schema_version", "baseline_timezone", "articles"} or store["schema_version"] != 1:
        raise BaselineError("Unsupported baseline schema")
    if store["baseline_timezone"] != DEFAULT_TIMEZONE or not isinstance(store["articles"], list) or len(store["articles"]) > MAX_RECORDS:
        raise BaselineError("Invalid baseline collection")
    seen = set()
    for record in store["articles"]:
        if set(record) != ENTRY_KEYS or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", record["article_id"]):
            raise BaselineError("Baseline has unexpected or private fields")
        target = day(record["target_date"])
        tz = ZoneInfo(record["timezone"])
        key = (record["article_id"], record["target_date"], record["timezone"])
        if key in seen:
            raise BaselineError("Duplicate article-day baseline")
        seen.add(key)
        recorded = timestamp(record["recorded_at"])
        observed, retrieved = timestamp(record["market_observed_at"]), timestamp(record["market_retrieved_at"])
        if observed > retrieved or retrieved > recorded or any(t.astimezone(tz).date() != target for t in (observed, retrieved)):
            raise BaselineError("Baseline dates do not match real source retrieval")
        if not re.fullmatch(r"[0-9a-f]{64}", record["market_source_sha256"]):
            raise BaselineError("Missing source checksum")
        status = record["publication_status_at_capture"]
        if status not in {"scheduled", "published"}:
            raise BaselineError("Invalid publication status")
        expected_kind = "scheduled_publication_day" if status == "scheduled" else "publication_day"
        if record["baseline_kind"] != expected_kind:
            raise BaselineError("Baseline kind does not match captured status")
        if set(record["indicators"]) != set(UNITS):
            raise BaselineError("Indicator IDs changed")
        for key, indicator in record["indicators"].items():
            if set(indicator) != {"value", "unit", "source_observed_at", "source_url", "coverage"} or indicator["unit"] != UNITS[key]:
                raise BaselineError("Unexpected indicator fields or units")
            value = indicator["value"]
            if value is not None:
                number(value)
                if not public_source(indicator["source_url"]):
                    raise BaselineError("Untrusted indicator source URL")
                if timestamp(indicator["source_observed_at"]) > retrieved:
                    raise BaselineError("Indicator is newer than baseline observation")
            elif indicator["source_observed_at"] is not None or indicator["source_url"] is not None:
                raise BaselineError("Unavailable indicator has fabricated source metadata")
        if record["financial_report_published_at"] is not None and timestamp(record["financial_report_published_at"]) > retrieved:
            raise BaselineError("Future financial report in baseline")


def article_target(article: dict, now: datetime) -> tuple[date, ZoneInfo, str] | None:
    article_id = article.get("id", "")
    if not isinstance(article_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", article_id):
        raise BaselineError("Invalid public article ID")
    tz = ZoneInfo(article.get("scheduled_timezone", DEFAULT_TIMEZONE))
    status = article.get("status")
    if status == "scheduled":
        return day(article.get("scheduled_date")), tz, "scheduled_publication_day"
    if status == "published":
        published = timestamp(article.get("published_at"))
        url = urlparse(article.get("canonical_url") or "")
        if url.scheme != "https" or not url.hostname or url.username or url.hostname.endswith(("notion.so", "notion.com")):
            raise BaselineError("Published article needs an existing public canonical source")
        if published > now:
            raise BaselineError("Publication status cannot precede publication timestamp")
        return published.astimezone(tz).date(), tz, "publication_day"
    return None


def eligible_snapshot(snapshot: dict, target: date, tz: ZoneInfo, now: datetime) -> bool:
    provenance = snapshot.get("provenance", {})
    if provenance.get("kind") != "live_public_api" or provenance.get("source_url") != markets.API_URL or not provenance.get("retrieved_at"):
        return False
    observed = timestamp(snapshot["observed_at"])
    retrieved = timestamp(provenance["retrieved_at"])
    if observed > retrieved or retrieved > now:
        return False
    return observed.astimezone(tz).date() == target and retrieved.astimezone(tz).date() == target


def known_financials(data: dict, cutoff: datetime) -> tuple[list[dict], list[str]]:
    """Use only report versions already observed at the market baseline time."""
    financials.validate_dataset(data)
    selected, missing = [], []
    for report in data["reports"]:
        if timestamp(report["published_at"]) > cutoff:
            continue
        available = []
        for version in report.get("revisions", []) + [report]:
            valid_sources = [s for s in version["sources"] if s.get("matches_current", True)
                             and timestamp(s["published_at"]) <= cutoff
                             and timestamp(s["observed_at"]) <= cutoff]
            if valid_sources:
                known_at = min(timestamp(s["observed_at"]) for s in valid_sources)
                available.append((known_at, version, valid_sources))
        if not available:
            missing.append(report["id"])
            continue
        _, version, valid_sources = max(available, key=lambda item: item[0])
        chosen = copy.deepcopy(version)
        chosen["id"] = report["id"]
        chosen["published_at"] = report["published_at"]
        chosen["known_sources"] = valid_sources
        selected.append(chosen)
    selected.sort(key=lambda r: (r["published_at"], r["id"]))
    return selected, missing


def indicator(value, unit, source_time, source_url, scope, **coverage):
    return {"value": None if value is None else number(value), "unit": unit,
            "source_observed_at": source_time if value is not None else None,
            "source_url": source_url if value is not None else None,
            "coverage": {"scope": scope, **coverage}}


def build_record(article: dict, target: date, tz: ZoneInfo, kind: str,
                 snapshot: dict, financial_data: dict, now: datetime) -> dict:
    observed, retrieved = snapshot["observed_at"], snapshot["provenance"]["retrieved_at"]
    totals = snapshot["totals"]
    volume = number(totals["volume_24h_usdc"], nonnegative=True)
    oi = number(totals["trader_open_interest_usdc"], nonnegative=True)
    count = totals["num_markets_observed"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise BaselineError("Invalid observed market count")
    reported = totals["num_markets_reported"]
    if isinstance(reported, bool) or not isinstance(reported, int) or reported < count:
        raise BaselineError("Invalid reported market count")
    listing_coverage = {"markets_observed": count, "markets_reported": reported,
                        "complete_listing_coverage": count == reported}
    swaps = [p for p in snapshot["by_contract_type"] if p["contract_type"] == "swap"]
    if len(swaps) != 1:
        raise BaselineError("Missing or duplicate swap totals")
    swap_volume = Decimal(number(swaps[0]["volume_24h_usdc"], nonnegative=True))
    if swap_volume > Decimal(volume):
        raise BaselineError("Swap volume exceeds total volume")
    with localcontext() as ctx:
        ctx.prec = 50
        share = None if not Decimal(volume) or count != reported else format((swap_volume / Decimal(volume) * 100).quantize(Decimal("0.000000000001")), "f")
    indicators = {
        "volume_24h_usdc": indicator(volume, "USDC", observed, markets.API_URL, "rolling_24h"),
        "trader_open_interest_usdc": indicator(oi, "USDC", observed, markets.API_URL, "trader_positions_only"),
        "num_markets_observed": indicator(str(count), "count", observed, markets.API_URL, "returned_listings", **listing_coverage),
        "swap_volume_share_pct": indicator(share, "%", observed, markets.API_URL, "swap_rolling_24h_volume_divided_by_total", **listing_coverage),
    }
    reports, missing = known_financials(financial_data, timestamp(retrieved))
    latest = reports[-1] if reports else None
    source_url = None
    report_time = latest["published_at"] if latest else None
    if latest:
        sources = sorted(latest["known_sources"], key=lambda s: (s["platform"] != "telegram", s["published_at"]))
        source_url = sources[0]["url"]
    cumulative = sum((Decimal(r["period_amounts"]["net_profit"]) for r in reports), Decimal(0)) if reports else None
    financial_coverage = {"report_count": len(reports),
                          "first_report_published_at": reports[0]["published_at"] if reports else None,
                          "last_report_published_at": report_time,
                          "missing_known_versions_count": len(missing)}
    indicators["reported_net_profit_cumulative"] = indicator(cumulative, "$", report_time, source_url, "sum_of_covered_report_periods", **financial_coverage)
    indicators["lifetime_olp_pnl"] = indicator(latest["snapshot"]["lifetime_olp_pnl"] if latest else None, "$", report_time, source_url, "latest_reported_lifetime_stock", report_count=1 if latest else 0)
    return {
        "article_id": article["id"], "target_date": target.isoformat(), "timezone": tz.key,
        "baseline_kind": kind, "publication_status_at_capture": article["status"],
        "recorded_at": iso(now), "market_observed_at": observed, "market_retrieved_at": retrieved,
        "market_source_sha256": snapshot["provenance"]["raw_sha256"],
        "financial_report_published_at": report_time,
        "coverage": {"market_source_kind": "live_public_api", **listing_coverage, **financial_coverage,
                     "financial_report_ids": [r["id"] for r in reports],
                     "missing_financial_version_ids": missing,
                     "reporting_period_boundaries_known": False,
                     "note": "Same-day observation, not a daily close or proof of article publication. Covered report sums are not protocol-lifetime earnings; source discrepancies are preserved."},
        "indicators": indicators,
    }


def capture(store: dict, articles: list[dict], snapshots: list[dict], financial_data: dict,
            now: datetime) -> tuple[dict, dict]:
    if now.tzinfo is None:
        raise BaselineError("Recorder clock requires timezone")
    validate_store(store)
    output = copy.deepcopy(store)
    result = {"changed": False, "captured": [], "locked": [], "pending": [], "missing": []}
    seen_articles = set()
    for article in articles:
        if article.get("id") in seen_articles:
            raise BaselineError("Duplicate article ID")
        seen_articles.add(article.get("id"))
        target = article_target(article, now)
        if target is None:
            continue
        target_date, tz, kind = target
        key = {"article_id": article["id"], "target_date": target_date.isoformat()}
        if any(r["article_id"] == article["id"] and r["target_date"] == target_date.isoformat() and r["timezone"] == tz.key for r in output["articles"]):
            result["locked"].append(key)
            continue
        if target_date > now.astimezone(tz).date():
            result["pending"].append({**key, "reason": "target_date_in_future"})
            continue
        eligible = [s for s in snapshots if eligible_snapshot(s, target_date, tz, now)]
        if not eligible:
            bucket = "pending" if target_date == now.astimezone(tz).date() else "missing"
            result[bucket].append({**key, "reason": "no_same_day_live_retrieval"})
            continue
        # First available same-day observation locks once; later runs cannot rewrite it.
        selected = min(eligible, key=lambda s: timestamp(s["provenance"]["retrieved_at"]))
        output["articles"].append(build_record(article, target_date, tz, kind, selected, financial_data, now))
        result["captured"].append(key)
        result["changed"] = True
    validate_store(output)
    return output, result


def load_snapshots(data_dir: Path) -> list[dict]:
    entries = markets.read_existing(data_dir)
    for entry in entries:
        if entry.get("partition_format") == "latest":
            current = read_json(data_dir / "markets.json")["latest"]
            if any(current[key] != entry[key] for key in ("totals", "by_contract_type", "provenance")):
                raise BaselineError("Latest history summary differs from actual collected data")
        if entry.get("partition_format") in {"full", "compact"}:
            loaded = markets.load_partition(data_dir, entry)
            if loaded["totals"] != entry["totals"] or loaded["by_contract_type"] != entry["by_contract_type"] or loaded["provenance"] != entry["provenance"]:
                raise BaselineError("Market history summary does not match its verified partition")
    return entries


def write_atomic(path: Path, data: dict) -> None:
    validate_store(data)
    raw = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
    if len(raw) > MAX_BYTES:
        raise BaselineError("Baseline store exceeds byte budget")
    fd, temporary = tempfile.mkstemp(prefix=".research-baselines.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("record", "verify"), nargs="?", default="record")
    parser.add_argument("--data-dir", type=Path, default=Path("public/data"))
    args = parser.parse_args(argv)
    target = args.data_dir / "research-baselines.json"
    try:
        existing = read_json(target) if target.exists() else empty_store()
        validate_store(existing)
        if args.command == "verify":
            print(json.dumps({"valid": True, "baseline_count": len(existing["articles"])}))
            return 0
        public_articles = read_json(args.data_dir / "supplement.json")["research"]["articles"]
        financial_data = read_json(args.data_dir / "financials.json")
        snapshots = load_snapshots(args.data_dir)
        updated, result = capture(existing, public_articles, snapshots, financial_data, datetime.now(timezone.utc))
        if result["changed"] or not target.exists():
            write_atomic(target, updated)
            result["changed"] = True
        print(json.dumps(result))
        return 0
    except (ValueError, KeyError, TypeError, OSError, InvalidOperation) as exc:
        print(json.dumps({"changed": False, "error": str(exc), "previous_baselines_retained": True}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
