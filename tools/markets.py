#!/usr/bin/env python3
"""Collect public Omni v2 observations; never invent missing daily history.

Default: python3 tools/markets.py
Historical import: --seed PATH UTC_TIMESTAMP TIMESTAMP_BASIS (repeatable).
Only allowlisted public API fields enter generated files; local paths are omitted.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

API_URL = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/v2/stats"
DOC_URL = "https://docs.variational.io/technical-documentation/api"
MAX_BYTES = 4_000_000
SCHEMA_VERSION = 1
HISTORY_SCHEMA_VERSION = 2
FULL_HISTORY_DAYS = 31
COMPACT_HISTORY_DAYS = 365
MAX_PARTITION_BYTES = 8_000_000
MAX_INDEX_BYTES = 10_000_000
MAX_MARKET_DATA_BYTES = 200_000_000
QUOTE_KEYS = ("base", "size_1k", "size_100k", "size_1m")
SOURCE = {
    "name": "Variational Omni public statistics API",
    "url": API_URL,
    "documentation_url": DOC_URL,
    "api_version": "v2",
    "product": "Omni",
    "amount_unit": "USDC",
    "volume_window": "Rolling 24 hours, not a UTC calendar-day total",
    "total_oi_scope": "Traders and OLP",
    "market_oi_scope": "Trader long plus short positions; OLP mirror positions excluded",
    "quote_status": "Indicative; not executed trades or an order book",
    "quote_notional_convention": "Source dollar-notional convention; no currency conversion inferred",
    "quote_sizes": {"base": None, "size_1k": "1000", "size_100k": "100000", "size_1m": "1000000"},
    "full_spread_formula": "(ask - bid) / ((ask + bid) / 2) * 10000; midpoint convention, rounded to 12 decimal places",
    "funding": "Source annualized and interval rates retained separately. Swap long/short financing is not perpetual funding.",
}


class DataError(ValueError):
    """Unsafe or inconsistent source; retain last good data."""


class SourceError(RuntimeError):
    def __init__(self, code: str, retry_after: str | None = None):
        self.code = code
        self.retry_after = retry_after
        super().__init__(code)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    if not isinstance(value, str) or len(value) > 50:
        raise DataError("invalid_timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DataError("invalid_timestamp") from exc
    if result.tzinfo is None:
        raise DataError("timestamp_requires_timezone")
    return result.astimezone(timezone.utc)


def number(value, *, nonnegative: bool = False, positive: bool = False) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise DataError("decimal_string_required")
    if len(str(value)) > 100:
        raise DataError("numeric_value_too_long")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise DataError("invalid_decimal") from exc
    if not result.is_finite() or abs(result) > Decimal("1e30") or result.as_tuple().exponent < -60:
        raise DataError("invalid_decimal_range")
    if (nonnegative and result < 0) or (positive and result <= 0):
        raise DataError("invalid_numeric_sign")
    return format(result, "f")


def integer(value, *, minimum: int = 0, maximum: int = 10_000_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise DataError("invalid_integer")
    return value


def text_field(value, maximum=180) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or re.search(r"[<>\x00-\x1f\x7f]", value):
        raise DataError("invalid_display_text")
    return value


def load_source_bytes(raw: bytes) -> dict:
    if len(raw) > MAX_BYTES:
        raise DataError("response_too_large")
    try:
        def unique_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise DataError("duplicate_json_key")
                result[key] = value
            return result
        data = json.loads(raw, parse_float=Decimal, object_pairs_hook=unique_keys,
            parse_constant=lambda _: (_ for _ in ()).throw(DataError("nonfinite_json")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DataError("invalid_json") from exc
    if not isinstance(data, dict):
        raise DataError("invalid_response_envelope")
    return data


def normalize_funding(value, contract_type: str) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise DataError("invalid_funding")
    rate_keys = ("annualized_rate", "interval_rate") if contract_type == "perpetual" else (
        "annualized_long_rate", "annualized_short_rate", "interval_long_rate", "interval_short_rate"
    )
    out = {}
    for moment in ("last", "next"):
        term = value.get(moment)
        if term is None:
            out[moment] = None
        elif isinstance(term, dict) and set(term).issubset(rate_keys):
            out[moment] = {key: None if term.get(key) is None else number(term[key]) for key in rate_keys}
        else:
            raise DataError("unrecognized_funding_terms")
    out["interval_seconds"] = None if value.get("interval_seconds") is None else integer(value["interval_seconds"], minimum=1, maximum=604800)
    out["next_timestamp"] = None if value.get("next_timestamp") is None else integer(value["next_timestamp"], minimum=1)
    return out


def normalize_quotes(value, observed_at: str) -> dict:
    out = {"updated_at": None, **{key: None for key in QUOTE_KEYS}}
    if value is None:
        return out
    if not isinstance(value, dict):
        raise DataError("invalid_quotes")
    updated = value.get("updated_at")
    if updated is not None:
        if parse_time(updated) > parse_time(observed_at) + timedelta(minutes=5):
            raise DataError("future_quote_timestamp")
        out["updated_at"] = updated
    for key in QUOTE_KEYS:
        pair = value.get(key)
        if pair is None:
            continue
        if not isinstance(pair, dict) or updated is None:
            raise DataError("quote_missing_price_or_timestamp")
        bid = number(pair.get("bid"), positive=True)
        ask = number(pair.get("ask"), positive=True)
        if Decimal(ask) < Decimal(bid):
            raise DataError("crossed_quote")
        with localcontext() as ctx:
            ctx.prec = 80
            spread = (Decimal(ask) - Decimal(bid)) / ((Decimal(ask) + Decimal(bid)) / 2) * 10000
            spread_text = format(spread.quantize(Decimal("0.000000000001")), "f")
        out[key] = {"bid": bid, "ask": ask, "full_spread_bps": spread_text}
    return out


def normalize_snapshot(raw: bytes, observed_at: str, *, kind="live_public_api", timestamp_basis="http_retrieval", http_date=None) -> dict:
    data = load_source_bytes(raw)
    observed = parse_time(observed_at)
    if observed > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise DataError("future_observation")
    if timestamp_basis not in ("http_retrieval", "archive_collection_label", "archived_retrieval_metadata"):
        raise DataError("unknown_timestamp_basis")
    listings = data.get("listings")
    if not isinstance(listings, list) or not listings or len(listings) > 10000:
        raise DataError("missing_or_excessive_listings")
    reported_count = integer(data.get("num_markets"), minimum=1, maximum=10000)
    if len(listings) != reported_count:
        raise DataError("market_count_mismatch")
    cumulative_keys = [key for key in ("cumulative_total_volume", "cumulative_volume") if key in data]
    if not cumulative_keys:
        raise DataError("missing_cumulative_volume")
    cumulative = [number(data[key], nonnegative=True) for key in cumulative_keys]
    if len({Decimal(value) for value in cumulative}) != 1:
        raise DataError("conflicting_cumulative_volume_fields")
    markets, seen = [], set()
    with localcontext() as ctx:
        ctx.prec = 80
        for row in listings:
            if not isinstance(row, dict):
                raise DataError("invalid_listing")
            ticker = text_field(row.get("ticker"), 80)
            contract_type = row.get("contract_type")
            if contract_type not in ("perpetual", "swap"):
                raise DataError("unknown_contract_type")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,79}", ticker) or not ticker.endswith("-PERP" if contract_type == "perpetual" else "-SWAP"):
                raise DataError("invalid_ticker")
            if ticker in seen:
                raise DataError("duplicate_market")
            seen.add(ticker)
            oi = row.get("open_interest")
            if not isinstance(oi, dict):
                raise DataError("missing_market_oi")
            long_oi = number(oi.get("long_open_interest"), nonnegative=True)
            short_oi = number(oi.get("short_open_interest"), nonnegative=True)
            markets.append({
                "ticker": ticker,
                "name": text_field(row.get("name")),
                "contract_type": contract_type,
                "mark_price": number(row.get("mark_price"), positive=True),
                "volume_24h_usdc": number(row.get("volume_24h"), nonnegative=True),
                "long_open_interest_usdc": long_oi,
                "short_open_interest_usdc": short_oi,
                "trader_open_interest_usdc": format(Decimal(long_oi) + Decimal(short_oi), "f"),
                "funding": normalize_funding(row.get("funding"), contract_type),
                "base_spread_bps": None if row.get("base_spread_bps") is None else number(row["base_spread_bps"], nonnegative=True),
                "quotes": normalize_quotes(row.get("quotes"), observed_at),
            })
        markets.sort(key=lambda row: row["ticker"])
        total_volume = number(data.get("total_volume_24h"), nonnegative=True)
        volume_sum = sum((Decimal(row["volume_24h_usdc"]) for row in markets), Decimal(0))
        residual = Decimal(total_volume) - volume_sum
        if abs(residual) > Decimal("0.000001"):
            raise DataError("volume_total_mismatch")
        trader_oi = sum((Decimal(row["trader_open_interest_usdc"]) for row in markets), Decimal(0))
        total_oi = number(data.get("open_interest"), nonnegative=True)
        if Decimal(total_oi) + Decimal("0.000001") < trader_oi:
            raise DataError("reported_oi_below_trader_oi")
        products = []
        for contract_type in ("perpetual", "swap"):
            selected = [row for row in markets if row["contract_type"] == contract_type]
            products.append({"contract_type": contract_type, "markets": len(selected),
                "volume_24h_usdc": format(sum((Decimal(row["volume_24h_usdc"]) for row in selected), Decimal(0)), "f"),
                "trader_open_interest_usdc": format(sum((Decimal(row["trader_open_interest_usdc"]) for row in selected), Decimal(0)), "f")})
        refund = data.get("loss_refund") or {}
        if not isinstance(refund, dict):
            raise DataError("invalid_loss_refund")
        totals = {"volume_24h_usdc": total_volume, "cumulative_total_volume_usdc": cumulative[0],
            "tvl_usdc": number(data.get("tvl"), nonnegative=True), "open_interest_total_usdc": total_oi,
            "trader_open_interest_usdc": format(trader_oi, "f"), "num_markets_reported": reported_count,
            "num_markets_observed": len(markets),
            "loss_refund_pool_usdc": None if refund.get("pool_size") is None else number(refund["pool_size"], nonnegative=True),
            "loss_refunded_24h_usdc": None if refund.get("refunded_24h") is None else number(refund["refunded_24h"], nonnegative=True)}
        ratio = None if trader_oi == 0 else format((Decimal(total_oi) / trader_oi).quantize(Decimal("0.000000000001")), "f")
    return {"date": observed.date().isoformat(), "observed_at": observed_at,
        "provenance": {"kind": kind, "source_url": API_URL, "timestamp_basis": timestamp_basis,
            "timestamp_precision": "minute" if timestamp_basis == "archive_collection_label" else "source_recorded",
            "retrieved_at": observed_at if kind == "live_public_api" else None,
            "http_date": http_date, "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "cumulative_volume_source_field": cumulative_keys[0]},
        "totals": totals, "by_contract_type": products, "markets": markets,
        "validation": {"volume_24h_residual_usdc": format(residual, "f"),
            "reported_oi_to_trader_oi_ratio": ratio,
            "oi_note": "Ratio is an observed diagnostic, not a hardcoded universal 2x assumption."}}


def ensure_nonregression(incoming: dict, previous: dict | None) -> None:
    if previous is None:
        return
    if parse_time(incoming["observed_at"]) < parse_time(previous["observed_at"]):
        raise DataError("observation_time_regression")
    if Decimal(incoming["totals"]["cumulative_total_volume_usdc"]) < Decimal(previous["totals"]["cumulative_total_volume_usdc"]):
        raise DataError("cumulative_volume_regression")


def merge_history(existing: list[dict], additions: list[dict]) -> list[dict]:
    days = {}
    for snapshot in existing + additions:
        date = snapshot["date"]
        if date != parse_time(snapshot["observed_at"]).date().isoformat():
            raise DataError("observation_date_mismatch")
        previous = days.get(date)
        if previous is None or parse_time(snapshot["observed_at"]) > parse_time(previous["observed_at"]):
            if previous is not None:
                ensure_nonregression(snapshot, previous)
            days[date] = snapshot
    merged = [days[date] for date in sorted(days)]
    for previous, incoming in zip(merged, merged[1:]):
        ensure_nonregression(incoming, previous)
    return merged


def coverage(snapshots: list[dict]) -> dict:
    dates = [snapshot["date"] for snapshot in snapshots]
    available = set(dates)
    start, end = parse_time(dates[0] + "T00:00:00Z"), parse_time(dates[-1] + "T00:00:00Z")
    missing = [(start + timedelta(days=n)).date().isoformat() for n in range((end - start).days + 1)
        if (start + timedelta(days=n)).date().isoformat() not in available]
    return {"start_date": dates[0], "end_date": dates[-1], "available_dates": dates,
        "missing_dates": missing, "snapshot_count": len(dates), "daily_selection": "Latest available observation per UTC date",
        "history_limit": "Dated observations only; gaps are not interpolated. These are not daily closes or calendar-day volume totals."}


def fetch_public() -> tuple[bytes, str | None]:
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, message, headers, newurl):
            raise SourceError("unexpected_source_redirect")
    request = Request(API_URL, headers={"Accept": "application/json", "User-Agent": "VariationalCommunityDashboard/0.1"})
    try:
        with build_opener(NoRedirect).open(request, timeout=25) as response:
            if response.geturl() != API_URL:
                raise SourceError("unexpected_source_redirect")
            if "application/json" not in response.headers.get("Content-Type", ""):
                raise SourceError("unexpected_content_type")
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise SourceError("response_too_large")
            return raw, response.headers.get("Date")
    except HTTPError as exc:
        # No immediate retry: the next authorized run must honor Retry-After.
        retry_after = exc.headers.get("Retry-After")
        if retry_after and (len(retry_after) > 100 or re.search(r"[\r\n<>]", retry_after)):
            retry_after = None
        raise SourceError(f"http_{exc.code}", retry_after) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise SourceError("network_failure") from exc


def retry_deadline(attempt_at: str, error: SourceError) -> str | None:
    if error.code not in ("http_429", "http_503"):
        return None
    attempted = parse_time(attempt_at)
    deadline = attempted + timedelta(minutes=5)
    if error.retry_after:
        try:
            if error.retry_after.isdigit():
                requested = attempted + timedelta(seconds=min(int(error.retry_after), 315360000))
            else:
                requested = parsedate_to_datetime(error.retry_after).astimezone(timezone.utc)
            deadline = max(deadline, requested)
        except (TypeError, ValueError, OverflowError):
            pass
    return deadline.isoformat(timespec="seconds").replace("+00:00", "Z")


def json_bytes(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json_bytes(data)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".markets-", delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(encoded)
            file.flush()
            os.fsync(file.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def partition_url(date: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or parse_time(date + "T00:00:00Z").date().isoformat() != date:
        raise DataError("unsafe_partition_date")
    return f"data/market-history/{date}.json"


def partition_path(output_dir: Path, date: str) -> Path:
    partition_url(date)
    return output_dir / "market-history" / f"{date}.json"


def validate_stored_rows(snapshot: dict) -> None:
    rows, totals = snapshot.get("markets", []), snapshot.get("totals", {})
    if not rows or len(rows) != totals.get("num_markets_observed") or len({row["ticker"] for row in rows}) != len(rows):
        raise DataError("invalid_historical_markets")
    with localcontext() as ctx:
        ctx.prec = 80
        volume = sum((Decimal(number(row["volume_24h_usdc"], nonnegative=True)) for row in rows), Decimal(0))
        oi = sum((Decimal(number(row["trader_open_interest_usdc"], nonnegative=True)) for row in rows), Decimal(0))
        if abs(volume - Decimal(number(totals["volume_24h_usdc"], nonnegative=True))) > Decimal("0.000001"):
            raise DataError("invalid_historical_volume")
        if oi != Decimal(number(totals["trader_open_interest_usdc"], nonnegative=True)):
            raise DataError("invalid_historical_oi")


def load_partition(output_dir: Path, entry: dict) -> dict:
    path = partition_path(output_dir, entry["date"])
    if entry.get("partition_url") != partition_url(entry["date"]):
        raise DataError("unsafe_partition_url")
    raw = path.read_bytes()
    if len(raw) > MAX_PARTITION_BYTES or hashlib.sha256(raw).hexdigest() != entry.get("partition_sha256"):
        raise DataError("partition_integrity_failure")
    envelope = json.loads(raw)
    snapshot = envelope.get("snapshot", {})
    if envelope.get("schema_version") != HISTORY_SCHEMA_VERSION or snapshot.get("date") != entry["date"] or snapshot.get("observed_at") != entry["observed_at"]:
        raise DataError("partition_metadata_mismatch")
    validate_stored_rows(snapshot)
    return snapshot


def read_existing(output_dir: Path) -> list[dict]:
    history_path = output_dir / "market-history.json"
    if not history_path.exists():
        return []
    existing = json.loads(history_path.read_text())
    if existing.get("schema_version") not in (1, HISTORY_SCHEMA_VERSION) or existing.get("source", {}).get("url") != API_URL:
        raise DataError("unsupported_existing_history")
    snapshots = existing.get("snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        raise DataError("invalid_existing_history")
    for snapshot in snapshots:
        if snapshot.get("provenance", {}).get("source_url") != API_URL:
            raise DataError("invalid_historical_source")
        partition_url(snapshot["date"])
        if "markets" in snapshot:  # One-time migration from the original monolith.
            validate_stored_rows(snapshot)
        elif snapshot.get("partition_format") == "latest":
            if snapshot.get("partition_url") != "data/markets.json":
                raise DataError("unsafe_latest_url")
            current = json.loads((output_dir / "markets.json").read_text())["latest"]
            if current["date"] != snapshot["date"] or current["observed_at"] != snapshot["observed_at"]:
                raise DataError("latest_index_mismatch")
            validate_stored_rows(current)
            snapshot["markets"] = current["markets"]
        elif snapshot.get("partition_format") in ("full", "compact"):
            if snapshot.get("partition_url") != partition_url(snapshot["date"]) or not partition_path(output_dir, snapshot["date"]).is_file():
                raise DataError("missing_or_unsafe_partition")
        elif snapshot.get("partition_format") != "summary" or snapshot.get("partition_url") is not None:
            raise DataError("unknown_partition_format")
    return merge_history(snapshots, [])


def compact_snapshot(snapshot: dict) -> dict:
    result = {key: value for key, value in snapshot.items() if key != "markets" and not key.startswith("partition_")}
    keys = ("ticker", "contract_type", "mark_price", "volume_24h_usdc", "long_open_interest_usdc", "short_open_interest_usdc", "trader_open_interest_usdc", "funding")
    rows = []
    for original in snapshot["markets"]:
        row = {key: original[key] for key in keys}
        quotes = original["quotes"]
        row["quotes"] = {"updated_at": quotes["updated_at"], **{
            key: None if quotes[key] is None else {"full_spread_bps": quotes[key]["full_spread_bps"]} for key in QUOTE_KEYS}}
        rows.append(row)
    result["markets"] = rows
    return result


def prepare_publication(output_dir: Path, snapshots: list[dict], latest: dict) -> tuple[dict, dict[Path, dict], list[Path]]:
    """Plan every mutation and byte limit before writing any file."""
    anchor = parse_time(snapshots[-1]["observed_at"]).date()
    entries, writes, removals = [], {}, []
    summary_keys = ("date", "observed_at", "provenance", "totals", "by_contract_type", "validation")
    for snapshot in snapshots:
        entry = {key: snapshot[key] for key in summary_keys}
        age = (anchor - parse_time(snapshot["observed_at"]).date()).days
        path = partition_path(output_dir, snapshot["date"])
        if snapshot["date"] == snapshots[-1]["date"]:
            entry.update(partition_url="data/markets.json", partition_format="latest", partition_sha256=None)
            if path.exists():
                removals.append(path)
        elif age >= COMPACT_HISTORY_DAYS:
            entry.update(partition_url=None, partition_format="summary", partition_sha256=None)
            if path.exists():
                removals.append(path)
        else:
            desired = "full" if age < FULL_HISTORY_DAYS else "compact"
            if snapshot.get("partition_format") == desired and "markets" not in snapshot:
                entry.update({key: snapshot[key] for key in ("partition_url", "partition_format", "partition_sha256")})
            else:
                full = snapshot if "markets" in snapshot else load_partition(output_dir, snapshot)
                body = {key: full[key] for key in (*summary_keys, "markets")}
                if desired == "compact":
                    body = compact_snapshot(body)
                envelope = {"schema_version": HISTORY_SCHEMA_VERSION, "format": desired, "snapshot": body}
                encoded = json_bytes(envelope)
                if len(encoded) > MAX_PARTITION_BYTES:
                    raise DataError("partition_size_budget_exceeded")
                entry.update(partition_url=partition_url(snapshot["date"]), partition_format=desired,
                    partition_sha256=hashlib.sha256(encoded).hexdigest())
                writes[path] = envelope
        entries.append(entry)
    history = {"schema_version": HISTORY_SCHEMA_VERSION, "source": SOURCE, "coverage": coverage(snapshots),
        "retention": {"full_market_days": FULL_HISTORY_DAYS, "compact_market_days": COMPACT_HISTORY_DAYS,
            "age_anchor": "Latest successful observation date; failed checks do not age or rewrite history",
            "full_fields": "Prices, volume, trader OI, full funding terms and indicative bid/ask quotes",
            "compact_fields": "Ticker/type, mark, volume, trader long/short OI, full funding terms, quote timestamps and derived full spreads; original bid/ask and name omitted",
            "older_history": "Aggregate totals and product summaries only; no per-market series beyond the retained window",
            "per_file_max_bytes": MAX_PARTITION_BYTES, "market_data_budget_bytes": MAX_MARKET_DATA_BYTES},
        "snapshots": entries}
    if len(json_bytes(history)) > MAX_INDEX_BYTES:
        raise DataError("history_index_budget_exceeded")
    if len(json_bytes(latest)) > MAX_PARTITION_BYTES:
        raise DataError("latest_size_budget_exceeded")
    sizes = {path: path.stat().st_size for path in (output_dir / "market-history").glob("*.json") if path not in removals}
    sizes.update({path: len(json_bytes(value)) for path, value in writes.items()})
    if sum(sizes.values()) + len(json_bytes(history)) + len(json_bytes(latest)) > MAX_MARKET_DATA_BYTES:
        raise DataError("market_history_storage_budget_exceeded")
    return history, writes, removals


def publish_files(output_dir: Path, latest: dict, history: dict, writes: dict[Path, dict], removals: list[Path]) -> None:
    # The enclosing repo commit/site deployment is the multi-file transaction.
    for path, body in writes.items():
        if not path.exists() or path.read_bytes() != json_bytes(body):
            atomic_json(path, body)
    history_path = output_dir / "market-history.json"
    if not history_path.exists() or json.loads(history_path.read_text()) != history:
        atomic_json(history_path, history)
    latest_path = output_dir / "markets.json"
    if not latest_path.exists() or latest_path.read_bytes() != json_bytes(latest):
        atomic_json(latest_path, latest)
    for path in removals:
        path.unlink(missing_ok=True)


def repack(output_dir: Path) -> dict:
    """Migrate storage only; preserve source observations, status and timestamps."""
    snapshots = read_existing(output_dir)
    latest = json.loads((output_dir / "markets.json").read_text())
    history, writes, removals = prepare_publication(output_dir, snapshots, latest)
    publish_files(output_dir, latest, history, writes, removals)
    return latest


def update(output_dir: Path, seeds=(), *, collect=True, fetcher=fetch_public, now=None) -> dict:
    attempt_at = now or utc_now()
    snapshots = read_existing(output_dir)
    seeded = [normalize_snapshot(Path(path).read_bytes(), observed_at, kind="archived_public_api", timestamp_basis=basis)
        for path, observed_at, basis in seeds]
    snapshots = merge_history(snapshots, seeded)
    previous = snapshots[-1] if snapshots else None
    status = {"state": "last_known_good", "last_attempt_at": attempt_at if collect else None,
        "last_successful_observation_at": previous["observed_at"] if previous else None,
        "error": None, "retry_after": None, "retry_not_before": None}
    if collect:
        try:
            latest_path = output_dir / "markets.json"
            prior_status = json.loads(latest_path.read_text()).get("status", {}) if latest_path.exists() else {}
            backoff = prior_status.get("retry_not_before")
            if not backoff and prior_status.get("error") in ("http_429", "http_503") and prior_status.get("last_attempt_at"):
                backoff = retry_deadline(prior_status["last_attempt_at"], SourceError(prior_status["error"], prior_status.get("retry_after")))
            if backoff and parse_time(attempt_at) < parse_time(backoff):
                status["retry_not_before"] = backoff
                raise SourceError("rate_limit_backoff", prior_status.get("retry_after"))
            raw, http_date = fetcher()
            current = normalize_snapshot(raw, now or utc_now(), http_date=http_date)
            ensure_nonregression(current, previous)
            snapshots = merge_history(snapshots, [current])
            status.update(state="ok", last_successful_observation_at=current["observed_at"])
        except (DataError, SourceError) as exc:
            status["error"] = exc.code if isinstance(exc, SourceError) else str(exc)
            status["retry_after"] = getattr(exc, "retry_after", None)
            if isinstance(exc, SourceError) and status["retry_not_before"] is None:
                status["retry_not_before"] = retry_deadline(attempt_at, exc)
            if not snapshots:
                raise
    elif snapshots:
        status["error"] = "archived_observations_only"
    if not snapshots:
        raise DataError("no_valid_observation")
    newest = {key: value for key, value in snapshots[-1].items() if not key.startswith("partition_")}
    if "markets" not in newest:
        raise DataError("latest_requires_full_market_rows")
    latest = {"schema_version": SCHEMA_VERSION, "generated_at": attempt_at, "source": SOURCE, "status": status,
        "latest": newest, "history_url": "data/market-history.json", "coverage": coverage(snapshots)}
    history, writes, removals = prepare_publication(output_dir, snapshots, latest)
    publish_files(output_dir, latest, history, writes, removals)
    return latest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parents[1] / "public" / "data")
    parser.add_argument("--seed", nargs=3, action="append", default=[], metavar=("PATH", "UTC_TIMESTAMP", "TIMESTAMP_BASIS"))
    parser.add_argument("--seed-only", action="store_true", help="Import public historical observations without making a network request")
    parser.add_argument("--repack", action="store_true", help="Migrate existing storage without a source request or timestamp/status change")
    args = parser.parse_args(argv)
    if args.repack and (args.seed or args.seed_only):
        parser.error("--repack cannot be combined with import arguments")
    try:
        result = repack(args.output_dir) if args.repack else update(args.output_dir, args.seed, collect=not args.seed_only)
    except (DataError, SourceError, OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"Market update stopped; existing files retained: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps({"state": result["status"]["state"], "observation": result["latest"]["observed_at"],
        "markets": len(result["latest"]["markets"]), "history_dates": result["coverage"]["available_dates"],
        "error": result["status"]["error"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
