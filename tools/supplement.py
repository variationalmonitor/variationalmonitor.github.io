"""Refresh verified Arbitrum balances, listing status and public comparison data."""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit

from execution import current_sample
from sources import get_bytes, get_json, iso, now_utc, parse_time


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "public" / "data"
CATALOG = ROOT / "config" / "catalog.json"
RPC = "https://arb1.arbitrum.io/rpc"
CONTRACT_DOC = "https://docs.variational.io/technical-documentation/mainnet-contracts"
USDC = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
WALLETS = {
    "treasury": ("Protocol Treasury", "0x5e91b40467fb8902c46a7b6cb90482363188d645"),
    "olp": ("Core OLP Vault", "0x74bbbb0e7f0bad6938509dd4b556a39a4db1f2cd"),
}
CG_API = "https://api.coingecko.com/api/v3/coins/variational?localization=false&tickers=false&market_data=true&community_data=false&developer_data=false&sparkline=false"
CG_PAGE = "https://www.coingecko.com/en/coins/variational"
PRO_DOC = "https://docs.variational.io/pro/about-pro"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     suffix=".tmp", delete=False) as output:
        name = output.name
        try:
            json.dump(document, output, indent=2, ensure_ascii=False, allow_nan=False)
            output.write("\n")
        except BaseException:
            os.unlink(name)
            raise
    os.chmod(name, 0o644)
    os.replace(name, path)


def rpc(method, params):
    result = get_json(RPC, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if result.get("id") != 1 or result.get("result") is None or "error" in result:
        raise ValueError("Arbitrum RPC could not confirm the requested state")
    return result["result"]


def chain_snapshot(previous):
    if int(rpc("eth_chainId", []), 16) != 42161:
        raise ValueError("Unexpected chain")
    block = rpc("eth_getBlockByNumber", ["safe", False])
    block_id = int(block["number"], 16)
    block_hash = block["hash"]
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", block_hash):
        raise ValueError("Invalid block hash")
    stamp = datetime.fromtimestamp(int(block["timestamp"], 16), timezone.utc)
    if not -120 <= (now_utc() - stamp).total_seconds() <= 24 * 3600:
        raise ValueError("Arbitrum safe block is outside the freshness window")
    if previous and block_id < previous["block_number"]:
        raise ValueError("Arbitrum safe block regressed")
    if previous and previous.get("block_hash"):
        prior = rpc("eth_getBlockByNumber", [hex(previous["block_number"]), False])
        if prior["hash"] != previous["block_hash"]:
            raise ValueError("Previously observed block changed; reconcile history")
    accounts = []
    for key, (label, address) in WALLETS.items():
        call = {"to": USDC, "data": "0x70a08231" + address[2:].zfill(64)}
        raw = rpc("eth_call", [call, block["number"]])
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", raw):
            raise ValueError("Invalid USDC balance response")
        balance = format(Decimal(int(raw, 16)).scaleb(-6), "f")
        accounts.append({"id": key, "name": label, "address": address, "balance_usdc": balance,
                         "source_url": "https://arbiscan.io/address/" + address})
    if rpc("eth_getBlockByNumber", [block["number"], False])["hash"] != block_hash:
        raise ValueError("Block changed while reading balances")
    observed = iso(now_utc())
    date = iso(stamp)[:10]
    point = {"date": date, "observed_at": observed, "block_number": block_id,
             "block_timestamp": iso(stamp), "block_hash": block_hash}
    point.update({a["id"] + "_usdc": a["balance_usdc"] for a in accounts})
    history = {p["date"]: p for p in (previous or {}).get("history", [])}
    history[date] = point
    return {"chain_id": 42161, "observed_at": observed, "block_number": block_id,
            "block_timestamp": iso(stamp), "block_hash": block_hash, "usdc_contract": USDC,
            "source_url": CONTRACT_DOC, "block_url": f"https://arbiscan.io/block/{block_id}",
            "accounts": accounts, "history": [history[k] for k in sorted(history)]}


def valid_amount(value):
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return format(amount, "f") if amount.is_finite() and amount > 0 else None


def token_record(payload, catalog, observed):
    if payload.get("id") != "variational" or str(payload.get("symbol")).upper() != "VAR":
        raise ValueError("CoinGecko listing identity mismatch")
    record = {"checked_at": iso(observed), "symbol": "VAR", "source_url": CG_PAGE,
              "contract_address": None, "price_usd": None, "market_cap_usd": None,
              "circulating_supply": None, "fdv_usd": None, **catalog["token_policy"]}
    record["status"] = "preview" if payload.get("preview_listing") is True else "identity_pending"
    registry = catalog.get("verified_var_contract")
    if not registry or record["status"] == "preview":
        return record
    platform = payload.get("detail_platforms", {}).get(registry["platform_id"], {})
    if (not re.fullmatch(r"0x[0-9a-fA-F]{40}", registry["address"])
            or platform.get("contract_address", "").lower() != registry["address"].lower()
            or platform.get("decimal_place") != registry["decimals"]
            or not registry.get("source_url", "").startswith("https://docs.variational.io/")):
        return record
    record.update(status="verified", contract_address=registry["address"],
                  identity_source=registry["source_url"])
    quote = payload.get("market_data") or {}
    as_of = quote.get("last_updated") or payload.get("last_updated")
    if not as_of or not -60 <= (observed - parse_time(as_of)).total_seconds() <= 36 * 3600:
        return record
    for output, source in (("price_usd", "current_price"), ("market_cap_usd", "market_cap"),
                           ("fdv_usd", "fully_diluted_valuation")):
        record[output] = valid_amount((quote.get(source) or {}).get("usd"))
    record.update(circulating_supply=valid_amount(quote.get("circulating_supply")), market_as_of=as_of)
    return record


def token_snapshot(catalog):
    key = os.environ.get("COINGECKO_DEMO_API_KEY")
    headers = {"x-cg-demo-api-key": key} if key else {}
    return token_record(get_json(CG_API, headers=headers), catalog, now_utc())


def pro_snapshot():
    page = get_bytes(PRO_DOC + ".md", limit=180_000).decode("utf-8")
    if "About Pro" not in page or "Variational Pro" not in page:
        raise ValueError("Unrecognized Variational Pro documentation")
    return {"checked_at": iso(now_utc()), "source_url": PRO_DOC,
            "status": "prelaunch" if "Variational Pro is currently not live" in page else "review"}


def execution_snapshot(market_file, previous):
    markets = read_json(market_file)["latest"]["markets"]
    quotes = {row["ticker"]: (row["quotes"]["size_100k"], row["quotes"]["updated_at"])
              for row in markets if row.get("quotes") and row["quotes"].get("size_100k")}
    result = current_sample(quotes)
    if not result["rows"] and previous and previous.get("rows"):
        return {**previous, "last_attempt_at": result["observed_at"], "matched_on_last_attempt": 0}
    return {**result, "last_attempt_at": result["observed_at"], "matched_on_last_attempt": len(result["rows"])}


def public_research(catalog):
    """A scheduled entry admits public metadata only, even if a private key is added by mistake."""
    common = {"id", "title", "author", "publisher", "language", "status", "scheduled_date",
              "published_at", "canonical_url", "topics", "related"}
    result = []
    seen = set()
    for entry in catalog["research"]["articles"]:
        state = entry["status"]
        if state not in {"scheduled", "published"} or not re.fullmatch(r"[a-z0-9-]{1,100}", entry["id"]):
            raise ValueError("Invalid article status or identifier")
        if entry["id"] in seen:
            raise ValueError("Duplicate article identifier")
        seen.add(entry["id"])
        allowed = common | ({"summary", "body", "data_as_of", "revision"} if state == "published" else set())
        if set(entry) - allowed:
            raise ValueError("Unexpected research field for its publication status")
        if state == "scheduled":
            if entry.get("published_at") or entry.get("canonical_url"):
                raise ValueError("Scheduled entry contains publication fields")
        else:
            url = urlsplit(entry.get("canonical_url") or "")
            if (url.scheme != "https" or url.netloc not in {"4pillars.io", "www.4pillars.io"}
                    or url.path in {"", "/"} or not entry.get("published_at")):
                raise ValueError("Published research needs a dated public publisher article")
        result.append({k: v for k, v in entry.items() if k in allowed})
    return {"articles": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DATA_DIR)
    args = parser.parse_args()
    path = args.data / "supplement.json"
    current = read_json(path) if path.exists() else {}
    catalog = read_json(CATALOG)
    result = {**current, "schema_version": 1, "research": public_research(catalog)}
    loaders = {
        "chain": lambda: chain_snapshot(current.get("chain")),
        "token": lambda: token_snapshot(catalog),
        "pro": pro_snapshot,
        "execution": lambda: execution_snapshot(args.data / "markets.json", current.get("execution")),
    }
    issues = {}
    for name, load in loaders.items():
        try:
            result[name] = load()
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
            reason = f"HTTP {error.code}" if isinstance(error, HTTPError) else type(error).__name__
            issues[name] = {"status": "delayed", "last_attempt_at": iso(now_utc()), "reason": reason}
            print(f"{name}: retaining the previous observation ({reason})", file=sys.stderr)
            if name not in current:
                raise
    result["source_issues"] = issues
    save_json(path, result)
    print("Supplement updated. Source delays:", ", ".join(issues) if issues else "none")


if __name__ == "__main__":
    main()
