#!/usr/bin/env python3
"""Validate a small manually reviewed public-specification identity registry.

No network collector: expanding this registry requires source review. Current
taxonomy is not historical listing evidence. Names are exact identities.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys

CLASSES = {"equity", "etf", "commodity", "index", "currency", "pre_ipo", "crypto", "unclassified"}
SOURCES = {
    "https://docs.variational.io/omni/trading/swaps",
    "https://docs.variational.io/omni/trading/tradfi-perpetuals",
    "https://docs.variational.io/technical-documentation/api",
}


def validate(data):
    if data.get("schema_version") != 1 or data.get("source", {}).get("url") not in SOURCES:
        raise ValueError("Invalid classification schema/source")
    observed = datetime.fromisoformat(data["observed_at"].replace("Z", "+00:00"))
    if observed.tzinfo is None:
        raise ValueError("Classification review needs timezone")
    rows = data.get("mappings")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 2000:
        raise ValueError("Invalid registry population")
    seen = set()
    for row in rows:
        if not isinstance(row.get("ticker"), str) or not re.fullmatch(r"[A-Za-z0-9._:/+\-]{1,100}-(?:PERP|SWAP)", row["ticker"]) or row["ticker"] in seen:
            raise ValueError("Invalid/duplicate registry ticker")
        seen.add(row["ticker"])
        if not isinstance(row.get("name"), str) or not 1 <= len(row["name"]) <= 180 or any(ord(c)<32 for c in row["name"]):
            raise ValueError("Invalid registry name")
        if row.get("contract_type") not in {"perpetual", "swap"} or not row["ticker"].endswith("-PERP" if row["contract_type"] == "perpetual" else "-SWAP"):
            raise ValueError("Registry contract mismatch")
        if row.get("scope") not in {"tradfi", "crypto", "unclassified"} or row.get("asset_class") not in CLASSES:
            raise ValueError("Invalid curated category")
        if row["scope"] == "crypto" and row["asset_class"] != "crypto" or row["scope"] == "tradfi" and row["asset_class"] in {"crypto","unclassified"}:
            raise ValueError("Inconsistent category scope")
        if not row.get("source_urls") or any(url not in SOURCES for url in row["source_urls"]):
            raise ValueError("Registry needs allowed primary sources")
        if row.get("listed_at") is not None:
            listed=datetime.fromisoformat(row["listed_at"].replace("Z", "+00:00"))
            if listed.tzinfo is None or listed>observed:
                raise ValueError("Invalid documented listing date")
    coverage=data["coverage"]
    if coverage["classified_count"] != len(rows) or coverage["market_count"]-len(rows) != coverage["unclassified_count"] or coverage["unclassified_count"] < 0:
        raise ValueError("Invalid curated coverage")


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["verify"])
    parser.add_argument("--output", type=Path, default=Path("public/data/market-classifications.json"))
    args=parser.parse_args(argv)
    try:
        data=json.loads(args.output.read_text()); validate(data)
        print(json.dumps({"valid":True, **data["coverage"]})); return 0
    except (ValueError,OSError,KeyError,TypeError) as exc:
        print(json.dumps({"valid":False,"error":str(exc)}),file=sys.stderr); return 1


if __name__=="__main__": raise SystemExit(main())
