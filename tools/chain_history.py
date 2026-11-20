"""Native-USDC wallet history: verified UTC closes and dated safe-block observations.

Routine runs reuse supplement.json without network calls after token verification.
An explicit --start/--end enables a bounded, sequential weekly archive backfill.
No transaction methods, credentials, provider rotation, or automatic retries.
"""

import argparse
import copy
import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path

from sources import get_json, iso, now_utc, parse_time
from supplement import CONTRACT_DOC, RPC as OFFICIAL_RPC, USDC, WALLETS, read_json, save_json


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_RPC = "https://arb-pokt.nodies.app"
ARCHIVE_DOC = "https://docs.nodies.app/rpc-services/pokt-public-rpc-endpoints"
CHAIN_DOC = "https://docs.arbitrum.io/chain-info"
HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")
QUANTITY = re.compile(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)\Z")
UINT = re.compile(r"0x[0-9a-fA-F]{64}\Z")
AMOUNT = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?\Z")
DECIMALS = 6
CHAIN_ID = 42161
MAX_POINTS = 16
MAX_CALLS = 240
NITRO_SEARCH_FLOOR = 22207818


class HistoryError(ValueError):
    pass


class IntegrityError(HistoryError):
    """Identity, canonicality or conflicting evidence: refuse the whole update."""
    pass


def uint(value):
    if not isinstance(value, str) or not UINT.fullmatch(value):
        raise IntegrityError("Invalid uint256 result")
    return int(value, 16)


def quantity(value):
    if not isinstance(value, str) or not QUANTITY.fullmatch(value):
        raise IntegrityError("Invalid block quantity")
    return int(value, 16)


def amount(raw):
    if not isinstance(raw, int) or isinstance(raw, bool) or not 0 <= raw < 2**256:
        raise IntegrityError("Invalid raw USDC balance")
    return f"{raw // 10**DECIMALS}.{raw % 10**DECIMALS:06d}"


def normalize_amount(value):
    if not isinstance(value, str) or not AMOUNT.fullmatch(value):
        raise IntegrityError("Invalid USDC amount")
    with localcontext() as context:
        context.prec = 100
        raw = int(Decimal(value) * 10**DECIMALS)
    return amount(raw)


def header(payload):
    if not isinstance(payload, dict):
        raise IntegrityError("Missing block header")
    number, stamp = quantity(payload.get("number")), quantity(payload.get("timestamp"))
    if not HASH.fullmatch(str(payload.get("hash", ""))) or not HASH.fullmatch(str(payload.get("parentHash", ""))):
        raise IntegrityError("Invalid block hash")
    if number > 2**63 or stamp > 2**40:
        raise IntegrityError("Invalid block metadata")
    return {"number": number, "hash": payload["hash"].lower(),
            "parent_hash": payload["parentHash"].lower(), "timestamp": stamp}


class Budget:
    """One shared serial budget across both public providers."""
    def __init__(self, maximum=MAX_CALLS, interval=0.75, sleeper=time.sleep, clock=time.monotonic):
        if not 1 <= maximum <= MAX_CALLS or interval < 0.5:
            raise HistoryError("RPC budget is outside the allowed bounds")
        self.maximum, self.interval = maximum, interval
        self.sleeper, self.clock = sleeper, clock
        self.calls, self.last = 0, None

    def take(self):
        if self.calls >= self.maximum:
            raise HistoryError("RPC call budget exhausted")
        if self.last is not None:
            self.sleeper(max(0, self.interval - (self.clock() - self.last)))
        self.calls += 1
        self.last = self.clock()
        return self.calls


class RPCClient:
    def __init__(self, url, budget):
        if url not in {OFFICIAL_RPC, ARCHIVE_RPC}:
            raise HistoryError("Unapproved public RPC endpoint")
        self.url, self.budget, self.cache = url, budget, {}

    def call(self, method, params):
        if method not in {"eth_chainId", "eth_getBlockByNumber", "eth_call"}:
            raise HistoryError("Only read-only RPC methods are allowed")
        if method == "eth_call":
            call, ref = params
            if call.get("to") != USDC or not isinstance(ref, dict) or ref.get("requireCanonical") is not True:
                raise HistoryError("Token reads require the canonical block hash")
            if not HASH.fullmatch(str(ref.get("blockHash", ""))):
                raise HistoryError("Invalid token block reference")
            permitted = {"0x313ce567"} | {"0x70a08231" + address[2:].zfill(64) for _, address in WALLETS.values()}
            if call.get("data") not in permitted:
                raise HistoryError("Unapproved token call")
        request_id = self.budget.take()
        response = get_json(self.url, {"jsonrpc": "2.0", "id": request_id, "method": method,
                                      "params": params}, limit=400_000)
        if not isinstance(response, dict) or response.get("jsonrpc") != "2.0" or response.get("id") != request_id:
            raise IntegrityError("RPC response identity mismatch")
        if "error" in response:
            message = str((response.get("error") or {}).get("message", "RPC unavailable"))[:180]
            if any(term in message.lower() for term in ("not canonical", "non-canonical", "reorg")):
                raise IntegrityError(message)
            raise HistoryError(message)
        if response.get("result") is None:
            raise HistoryError("Requested RPC state is unavailable")
        return response["result"]

    def verify_chain(self):
        if quantity(self.call("eth_chainId", [])) != CHAIN_ID:
            raise IntegrityError("Unexpected chain; Arbitrum One is required")

    def block(self, number, fresh=False):
        if number not in self.cache or fresh:
            result = header(self.call("eth_getBlockByNumber", [hex(number), False]))
            if result["number"] != number:
                raise IntegrityError("RPC returned a different block number")
            self.cache[number] = result
        return self.cache[number]


def token_call(client, block, data):
    return uint(client.call("eth_call", [{"to": USDC, "data": data},
                                        {"blockHash": block["hash"], "requireCanonical": True}]))


def verify_decimals(client, block):
    if token_call(client, block, "0x313ce567") != DECIMALS:
        raise IntegrityError("Unexpected native-USDC decimals")


def same_header(left, right):
    if any(left[key] != right[key] for key in ("number", "hash", "timestamp", "parent_hash")):
        raise IntegrityError("Historical block metadata differs between providers")


def find_close(client, day, lower, upper):
    """Find the final block strictly before the following UTC midnight."""
    cutoff = int(datetime.combine(day + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp())
    if not lower["timestamp"] < cutoff <= upper["timestamp"]:
        raise HistoryError("Requested close is outside the verified block bracket")
    for attempt in range(36):
        if upper["number"] == lower["number"] + 1:
            if upper["parent_hash"] != lower["hash"]:
                raise IntegrityError("Closing block boundary is not linked")
            if datetime.fromtimestamp(lower["timestamp"], timezone.utc).date() != day:
                raise HistoryError("No observed block within the requested UTC date")
            return lower, upper
        span = upper["number"] - lower["number"]
        if attempt % 5 == 4 or upper["timestamp"] == lower["timestamp"]:
            candidate = lower["number"] + span // 2
        else:
            candidate = lower["number"] + ((cutoff - lower["timestamp"]) * span // (upper["timestamp"] - lower["timestamp"]))
        candidate = max(lower["number"] + 1, min(upper["number"] - 1, candidate))
        block = client.block(candidate)
        if not lower["timestamp"] <= block["timestamp"] <= upper["timestamp"]:
            raise IntegrityError("Block timestamps are not monotonic")
        if block["timestamp"] < cutoff:
            lower = block
        else:
            upper = block
    raise HistoryError("Closing block search exceeded its bound")


def capture_close(archive, official, day, block, following, observed):
    same_header(block, official.block(block["number"], fresh=True))
    same_header(following, official.block(following["number"], fresh=True))
    verify_decimals(archive, block)
    values = {key + "_usdc": amount(token_call(archive, block, "0x70a08231" + address[2:].zfill(64)))
              for key, (_, address) in WALLETS.items()}
    same_header(block, archive.block(block["number"], fresh=True))
    return {"date": day.isoformat(), "kind": "utc_day_close", "observed_at": iso(observed),
            "block_number": block["number"], "block_hash": block["hash"],
            "block_timestamp": iso(datetime.fromtimestamp(block["timestamp"], timezone.utc)),
            "usdc_decimals": DECIMALS, **values, "rpc_url": ARCHIVE_RPC,
            "source_url": f"https://arbiscan.io/block/{block['number']}",
            "closing_boundary": {"next_block_number": following["number"], "next_block_hash": following["hash"],
                                 "next_block_timestamp": iso(datetime.fromtimestamp(following["timestamp"], timezone.utc))},
            "metadata_cross_check": {"rpc_url": OFFICIAL_RPC, "block_hash": block["hash"],
                                     "next_block_hash": following["hash"], "verified_at": iso(observed)}}


def supplement_point(chain, observed):
    if chain.get("chain_id") != CHAIN_ID or chain.get("usdc_contract") != USDC:
        raise HistoryError("Supplement chain or native-USDC identity mismatch")
    stamp, retrieved = parse_time(chain["block_timestamp"]), parse_time(chain["observed_at"])
    if not -120 <= (observed - stamp).total_seconds() <= 86400 or stamp > retrieved + timedelta(seconds=120):
        raise HistoryError("Supplement safe block is stale or future-dated")
    if retrieved > observed + timedelta(seconds=120):
        raise HistoryError("Supplement retrieval time is future-dated")
    accounts = {account["id"]: account for account in chain["accounts"]}
    if len(accounts) != len(chain["accounts"]) or set(accounts) != set(WALLETS):
        raise HistoryError("Unexpected supplement wallet set")
    values = {}
    for key, (_, address) in WALLETS.items():
        if accounts[key]["address"] != address:
            raise HistoryError("Supplement wallet address mismatch")
        values[key + "_usdc"] = normalize_amount(accounts[key]["balance_usdc"])
    point = {"date": iso(stamp)[:10], "kind": "safe_block_observation", "observed_at": iso(retrieved),
             "block_number": chain["block_number"], "block_hash": chain["block_hash"],
             "block_timestamp": iso(stamp), "usdc_decimals": DECIMALS, **values,
             "rpc_url": OFFICIAL_RPC, "source_url": f"https://arbiscan.io/block/{chain['block_number']}",
             "upstream_source": "data/supplement.json"}
    validate_point(point)
    return point


def validate_point(point):
    if point.get("kind") not in {"utc_day_close", "safe_block_observation"}:
        raise HistoryError("Unknown point kind")
    day = date.fromisoformat(point["date"])
    block = point.get("block_number")
    if isinstance(block, bool) or not isinstance(block, int) or not 0 < block < 2**63:
        raise HistoryError("Invalid point block number")
    if not HASH.fullmatch(str(point.get("block_hash", ""))) or point.get("usdc_decimals") != DECIMALS:
        raise HistoryError("Invalid point hash or decimals")
    stamp = parse_time(point["block_timestamp"])
    if stamp.astimezone(timezone.utc).date() != day or parse_time(point["observed_at"]) + timedelta(seconds=120) < stamp:
        raise HistoryError("Point date does not match its block")
    for key in WALLETS:
        normalize_amount(point[key + "_usdc"])
    if point.get("rpc_url") not in {OFFICIAL_RPC, ARCHIVE_RPC} or point.get("source_url") != f"https://arbiscan.io/block/{block}":
        raise HistoryError("Unexpected point source")
    if point["kind"] == "utc_day_close":
        boundary = point.get("closing_boundary", {})
        if (boundary.get("next_block_number") != block + 1 or not HASH.fullmatch(str(boundary.get("next_block_hash", "")))
                or parse_time(boundary["next_block_timestamp"]).astimezone(timezone.utc).date() <= day):
            raise HistoryError("Unverified UTC closing boundary")
        cross_check = point.get("metadata_cross_check", {})
        if (cross_check.get("rpc_url") != OFFICIAL_RPC or cross_check.get("block_hash") != point["block_hash"]
                or cross_check.get("next_block_hash") != boundary["next_block_hash"]):
            raise HistoryError("Missing independent block metadata check")


def validate(document):
    if document.get("schema_version") != 1 or document.get("chain_id") != CHAIN_ID:
        raise HistoryError("Unsupported history identity")
    asset = document.get("asset", {})
    if asset.get("contract_address") != USDC or asset.get("decimals") != DECIMALS or asset.get("symbol") != "USDC":
        raise HistoryError("Unexpected history asset")
    accounts = document.get("accounts", [])
    if len(accounts) != len(WALLETS) or {a["id"]: a["address"] for a in accounts} != {k: v[1] for k, v in WALLETS.items()}:
        raise HistoryError("Unexpected history wallets")
    proof = asset.get("decimals_verification", {})
    if (proof.get("value") != DECIMALS or proof.get("rpc_url") not in {OFFICIAL_RPC, ARCHIVE_RPC}
            or not isinstance(proof.get("block_number"), int) or isinstance(proof.get("block_number"), bool)
            or proof["block_number"] <= 0 or not HASH.fullmatch(str(proof.get("block_hash", "")))):
        raise HistoryError("Missing native-USDC decimals verification")
    parse_time(proof["block_timestamp"])
    parse_time(proof["observed_at"])
    seen, previous = set(), None
    for point in document["points"]:
        validate_point(point)
        if point["date"] in seen or (previous and point["date"] <= previous):
            raise HistoryError("History dates must be unique and chronological")
        seen.add(point["date"])
        previous = point["date"]


def merge(points, point):
    previous = points.get(point["date"])
    if previous and previous["kind"] == "utc_day_close":
        return
    if previous and point["block_number"] < previous["block_number"]:
        raise IntegrityError("Wallet observation regressed")
    if previous and point["block_number"] == previous["block_number"]:
        for key in ("block_hash", "treasury_usdc", "olp_usdc"):
            if previous[key] != point[key]:
                raise IntegrityError("Previously observed wallet state changed")
    points[point["date"]] = point


def requested_dates(start, end, step):
    if step not in {1, 7} or start > end or (end - start).days > 366:
        raise HistoryError("Backfill range must be daily or weekly, at most one year")
    days, current = [], start
    while current <= end:
        days.append(current)
        current += timedelta(days=step)
    if len(days) > MAX_POINTS:
        raise HistoryError("At most 16 dates may be requested per explicit run")
    return days


def refresh(data_dir, *, start=None, end=None, dates=None, step=7, maximum=MAX_CALLS, interval=0.75,
            observed=None, client_factory=RPCClient):
    observed = observed or now_utc()
    path = data_dir / "chain-history.json"
    previous = read_json(path) if path.exists() else None
    if previous:
        validate(previous)
    chain = read_json(data_dir / "supplement.json")["chain"]
    snapshot = supplement_point(chain, observed)
    result = copy.deepcopy(previous) if previous else {
        "schema_version": 1, "chain_id": CHAIN_ID,
        "asset": {"symbol": "USDC", "contract_address": USDC, "decimals": DECIMALS},
        "accounts": [{"id": key, "name": label, "address": address, "source_url": CONTRACT_DOC}
                     for key, (label, address) in WALLETS.items()],
        "methodology": {
            "scope": "Native-USDC balances of the documented Protocol Treasury and Core OLP Vault wallets only. Not protocol TVL, revenue, net flows, or consolidated OLP NAV.",
            "timezone": "UTC", "closing_definition": "Final canonical block before the next UTC midnight; the following block proves the boundary.",
            "observation_definition": "The actual safe-block snapshot retained by supplement.json. An intraday observation is not a daily close.",
            "source_url": CONTRACT_DOC, "rpc_documentation": [CHAIN_DOC, ARCHIVE_DOC],
            "block_pinning": "Historical eth_call uses EIP-1898 blockHash with requireCanonical=true; each block's metadata is cross-checked against the official RPC.",
            "missing_data": "No interpolation or estimates. Explicit dated backfills and future daily observations retain their actual dates and point kinds."},
        "points": []}
    points = {p["date"]: p for p in result["points"]}
    merge(points, snapshot)
    if dates is not None and (start or end):
        raise HistoryError("Use an explicit date list or a range, not both")
    days = sorted(dates) if dates is not None else requested_dates(start, end, step) if start and end else []
    if len(days) != len(set(days)) or len(days) > MAX_POINTS or (days and (days[-1] - days[0]).days > 366):
        raise HistoryError("Explicit dates must be unique, at most 16 dates within one year")
    if bool(start) != bool(end) or any(day >= observed.astimezone(timezone.utc).date() for day in days):
        raise HistoryError("Historical closes require explicit completed UTC dates")
    budget = Budget(maximum, interval)
    failure = None
    if days or not result["asset"].get("decimals_verification"):
        archive, official = client_factory(ARCHIVE_RPC, budget), client_factory(OFFICIAL_RPC, budget)
        try:
            archive.verify_chain()
            official.verify_chain()
            upper = archive.block(snapshot["block_number"])
            if upper["hash"] != snapshot["block_hash"].lower() or upper["timestamp"] != int(parse_time(snapshot["block_timestamp"]).timestamp()):
                raise IntegrityError("Archive provider differs from the verified safe block")
            same_header(upper, official.block(upper["number"]))
            verify_decimals(archive, upper)
            result["asset"]["decimals_verification"] = {"block_number": upper["number"], "block_hash": upper["hash"],
                                                       "block_timestamp": snapshot["block_timestamp"], "rpc_url": ARCHIVE_RPC,
                                                       "observed_at": iso(observed), "value": DECIMALS}
            lower = archive.block(NITRO_SEARCH_FLOOR) if days else None
            completed = []
            for day in days:
                existing = points.get(day.isoformat())
                if existing and existing["kind"] == "utc_day_close":
                    check = official.block(existing["block_number"])
                    if check["hash"] != existing["block_hash"] or check["timestamp"] != int(parse_time(existing["block_timestamp"]).timestamp()):
                        raise IntegrityError("Previously recorded closing block changed")
                    lower = archive.block(existing["block_number"])
                    following = archive.block(existing["closing_boundary"]["next_block_number"])
                    same_header(lower, check)
                    same_header(following, official.block(following["number"]))
                    if following["parent_hash"] != lower["hash"] or following["hash"] != existing["closing_boundary"]["next_block_hash"]:
                        raise IntegrityError("Previously recorded closing boundary changed")
                else:
                    block, following = find_close(archive, day, lower, upper)
                    point = capture_close(archive, official, day, block, following, now_utc())
                    validate_point(point)
                    merge(points, point)
                    lower = block
                completed.append(day.isoformat())
            if days:
                result["archive"] = {"state": "available", "rpc_url": ARCHIVE_RPC, "last_attempt_at": iso(observed),
                                     "requested_dates": [d.isoformat() for d in days], "verified_dates": completed,
                                     "missing_requested_dates": [], "calls_used": budget.calls}
        except IntegrityError:
            raise
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
            failure = f"{type(error).__name__}: {str(error)[:180]}"
            verified = [d.isoformat() for d in days if points.get(d.isoformat(), {}).get("kind") == "utc_day_close"]
            result["archive"] = {"state": "partial" if verified else "unavailable", "rpc_url": ARCHIVE_RPC,
                                 "last_attempt_at": iso(observed), "reason": failure, "calls_used": budget.calls,
                                 "requested_dates": [d.isoformat() for d in days], "verified_dates": verified,
                                 "missing_requested_dates": [d.isoformat() for d in days if d.isoformat() not in verified]}
    if not result["asset"].get("decimals_verification"):
        raise HistoryError("No verified native-USDC decimals; previous history retained")
    result["points"] = [points[key] for key in sorted(points)]
    closes = sum(point["kind"] == "utc_day_close" for point in result["points"])
    result["coverage"] = {"first_date": result["points"][0]["date"], "last_date": result["points"][-1]["date"],
                          "point_count": len(points), "closing_point_count": closes,
                          "safe_observation_count": len(points) - closes,
                          "cadence": "Explicit dated closes plus daily safe-block observations; not a continuous daily closing series"}
    validate(result)
    comparison = copy.deepcopy(previous) if previous else None
    result.pop("generated_at", None)
    if comparison:
        comparison.pop("generated_at", None)
    changed = result != comparison
    if changed:
        result["generated_at"] = iso(max([observed] + [parse_time(point["observed_at"]) for point in result["points"]]))
        save_json(path, result)
    return {"changed": changed, "points": len(points), "closes": closes, "rpc_calls": budget.calls, "failure": failure}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "public" / "data")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--dates", type=date.fromisoformat, nargs="+")
    parser.add_argument("--step-days", type=int, choices=(1, 7), default=7)
    parser.add_argument("--max-calls", type=int, default=MAX_CALLS)
    parser.add_argument("--interval", type=float, default=0.75)
    args = parser.parse_args()
    try:
        outcome = refresh(args.data, start=args.start, end=args.end, dates=args.dates, step=args.step_days,
                          maximum=args.max_calls, interval=args.interval)
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
        print(f"Chain history retained: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(outcome))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
