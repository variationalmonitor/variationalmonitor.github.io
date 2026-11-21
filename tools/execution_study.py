"""Replay saved public Omni quotes and Hyperliquid order books for the size study."""

import argparse
import json
import hashlib
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from execution import COINS, FEE_DOCS, HL_DOCS, OMNI_DOCS, MAX_SKEW_SECONDS, compare
from sources import iso, parse_time


SIZES = {"size_1k": 1000, "size_100k": 100000, "size_1m": 1000000}


def replay(source):
    rows = {}
    captures = {}
    for capture in sorted(source.iterdir()):
        stats, book_path = capture / "variational_v2_stats.json", capture / "hl_l2books.json"
        if not stats.is_file() or not book_path.is_file():
            continue
        session = datetime.strptime(capture.name, "%Y%m%dT%H%MZ").replace(tzinfo=timezone.utc)
        stats_raw, book_raw = stats.read_bytes(), book_path.read_bytes()
        stats_data = json.loads(stats_raw)
        books = json.loads(book_raw)
        books = books.get("books", books)
        markets = {item["ticker"]: item for item in stats_data["listings"]}
        for coin in COINS:
            quotes = markets.get(coin + "-PERP", {}).get("quotes") or {}
            book = books.get(coin, {}).get("finest")
            if not book or not quotes.get("updated_at"):
                continue
            quote_time = parse_time(quotes["updated_at"])
            book_time = datetime.fromtimestamp(book["time"] / 1000, timezone.utc)
            if any(abs(moment - session) > timedelta(minutes=5) for moment in (quote_time, book_time)):
                continue
            observed = max(quote_time, book_time)
            for key, size in SIZES.items():
                pair = quotes.get(key)
                if not pair or not Decimal(pair["bid"]) or not Decimal(pair["ask"]):
                    continue
                item = compare(coin, pair, quotes["updated_at"], book, observed, notional=Decimal(size))
                if item:
                    captures[capture.name] = {
                        "omni_sha256": hashlib.sha256(stats_raw).hexdigest(),
                        "book_sha256": hashlib.sha256(book_raw).hexdigest(),
                    }
                    item.update(notional_usd=size, observed_at=iso(observed), capture=capture.name,
                                peer_book_time=book_time.isoformat(timespec="milliseconds").replace("+00:00", "Z"))
                    rows[(coin, size, quote_time, book_time)] = item
    return {"schema_version": 1, "source_urls": [OMNI_DOCS, HL_DOCS, FEE_DOCS],
            "timestamp_basis": "later_source_timestamp", "captures": captures,
            "rows": sorted(rows.values(), key=lambda item: (item["observed_at"], item["market"], item["notional_usd"]))}


def validate(dataset):
    if (dataset.get("schema_version") != 1 or dataset.get("timestamp_basis") != "later_source_timestamp"
            or dataset.get("source_urls") != [OMNI_DOCS, HL_DOCS, FEE_DOCS]):
        raise ValueError("Unexpected execution study source or schema")
    unique = set()
    for item in dataset["rows"]:
        coin, size = item["market"], item["notional_usd"]
        if coin not in COINS or size not in SIZES.values() or item["omni_ticker"] != coin+"-PERP" or item["peer_ticker"] != coin:
            raise ValueError("Market or size is outside the execution study")
        quote, book = parse_time(item["quote_time"]), parse_time(item["peer_book_time"])
        skew = abs((quote - book).total_seconds())
        if skew > MAX_SKEW_SECONDS or iso(max(quote, book)) != item["observed_at"] or abs(skew-item["skew_seconds"]) > .02:
            raise ValueError("Execution pair timestamps do not qualify")
        evidence = dataset["captures"][item["capture"]]
        if any(len(evidence[key]) != 64 or any(c not in "0123456789abcdef" for c in evidence[key]) for key in ("omni_sha256", "book_sha256")):
            raise ValueError("Missing raw capture fingerprints")
        session = datetime.strptime(item["capture"], "%Y%m%dT%H%MZ").replace(tzinfo=timezone.utc)
        if any(abs(moment-session) > timedelta(minutes=5) for moment in (quote, book)):
            raise ValueError("Quote pair is outside its archived capture")
        if any(not math.isfinite(item[key]) or item[key] < 0 for key in ("omni_bps", "peer_book_bps", "peer_total_bps")) or item["peer_fees_bps"] != 9 or item["samples"] != 1:
            raise ValueError("Invalid cost, fee or sample count")
        if abs(item["peer_total_bps"] - item["peer_book_bps"] - item["peer_fees_bps"]) > .00011:
            raise ValueError("Peer fee calculation failed")
        key = (coin, size, quote, book)
        if key in unique:
            raise ValueError("Duplicate matched quote")
        unique.add(key)
    if not unique:
        raise ValueError("Empty execution study")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("captures", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    dataset = replay(args.captures)
    validate(dataset)
    args.output.write_text(json.dumps(dataset, indent=2) + "\n")
    print(f"Verified {len(dataset['rows'])} date-, market- and size-specific matched quotes")
