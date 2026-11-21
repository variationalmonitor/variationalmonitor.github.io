"""Collect time-matched indicative crypto round trips without accounts or orders."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import re
from pathlib import Path
import subprocess
from urllib.error import HTTPError
from urllib.parse import urlencode

from execution import COINS, HL_API, HL_DOCS, FEE_DOCS, MAX_AGE_SECONDS, MAX_SKEW_SECONDS
from markets import atomic_json
from sources import get_json, now_utc, parse_time, positive

SIZES = {"size_1k": 1000, "size_100k": 100000, "size_1m": 1000000}
LIGHTER = "https://mainnet.zklighter.elliot.ai/api/v1"
BINANCE = "https://fapi.binance.com"
VENUES = {
    "hyperliquid": {"name":"Hyperliquid","currency":"USDC","fee": "4.5","book":HL_DOCS,"fees":FEE_DOCS},
    "lighter": {"name":"Lighter","currency":"USDC","fee": "0","fee_basis":"Public perpetual market taker_fee field, standard account assumption","book":"https://github.com/elliottech/lighter-python/blob/main/lighter/ws_client.py","fees":"https://github.com/elliottech/lighter-python/blob/main/docs/OrderApi.md"},
    "binance": {"name":"Binance","currency":"USDT","fee":"5","fee_basis":"Illustrative undiscounted taker rate from Binance fee documentation","book":"https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data","fees":"https://www.binance.com/en/support/faq/detail/360033544231"},
    "okx": {"name":"OKX","currency":"USDT","fee":"5","book":"https://www.okx.com/docs-v5/en/","fees":"https://www.okx.com/help/how-to-calculate-the-contract-transaction-fee"},
    "bybit": {"name":"Bybit","currency":"USDT","fee":"5.5","book":"https://bybit-exchange.github.io/docs/v5/market/orderbook","fees":"https://www.bybit.com/en/help-center/article/Trading-Fee-Structure"},
}
PUBLISH_VENUES = ("hyperliquid", "lighter", "binance")
FAILURES = {"permission_required", "not_sampled", "stale_source", "insufficient_depth", "source_unavailable", "market_unavailable", "no_omni_quote", "invalid_data"}
def stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")
def epoch(value):
    return datetime.fromtimestamp(int(value)/1000,timezone.utc)
def fetch(root,endpoint,**params):
    return get_json(root+endpoint+("?"+urlencode(params) if params else ""),limit=3_500_000)

def normalize_side(raw, multiplier, bids):
    if not isinstance(raw,list) or not raw or len(raw)>2000:
        raise ValueError("Invalid book depth")
    levels = [(positive(level[0]),positive(level[1])*multiplier) for level in raw]
    if any(a[0]<=b[0] if bids else a[0]>=b[0] for a,b in zip(levels,levels[1:])):
        raise ValueError("Misordered order book")
    return levels

def vwap(levels, quantity):
    remaining=quantity
    cost=Decimal(0)
    for price,available in levels:
        take=min(remaining,available)
        cost+=take*price
        remaining-=take
        if remaining==0:
            return cost/quantity
    return None

def normalized_book(venue,coin):
    symbol=coin+"USDT"
    factor=Decimal(1)
    if venue=="hyperliquid":
        raw=get_json(HL_API,{"type":"l2Book","coin":coin},limit=200_000)
        if raw["coin"]!=coin: raise ValueError("Peer identity mismatch")
        bids,asks=([item["px"],item["sz"]] for item in raw["levels"][0]),([item["px"],item["sz"]] for item in raw["levels"][1])
        return coin,epoch(raw["time"]),list(bids),list(asks),factor
    if venue=="binance":
        raw=fetch(BINANCE,"/fapi/v1/depth",symbol=symbol,limit=1000)
        return symbol,epoch(raw["T"]),raw["bids"],raw["asks"],factor
    raise ValueError("Venue is not enabled")

def lighter_books():
    records=fetch(LIGHTER,"/orderBooks")["order_books"]
    mappings={}
    for market in records:
        coin=market.get("symbol")
        if (coin in COINS and market["market_type"]=="perp" and market["status"]=="active"
            and market["quote_asset_id"]==0 and Decimal(market["taker_fee"])==0):
            if coin in mappings: raise ValueError("Ambiguous Lighter market")
            mappings[coin]=str(int(market["market_id"]))
    if not mappings: return {}
    proc=subprocess.run(["node",str(Path(__file__).with_name("lighter_book.mjs")),*mappings.values()],
                        capture_output=True,check=True,timeout=12)
    snapshots=json.loads(proc.stdout)
    return {coin:(f"{coin} ({key})",epoch(snapshots[key]["time"]),snapshots[key]["bids"],snapshots[key]["asks"],Decimal(1))
            for coin,key in mappings.items() if key in snapshots}

def costs(venue,coin,omni,book,captured):
    symbol,book_time,raw_bids,raw_asks,factor=book
    quote_time=parse_time(omni["updated_at"])
    skew=abs((book_time-quote_time).total_seconds())
    age=[(captured-moment).total_seconds() for moment in (book_time,quote_time)]
    if skew>MAX_SKEW_SECONDS or min(age)<-5 or max(age)>MAX_AGE_SECONDS:
        return [],"stale_source"
    bids=normalize_side(raw_bids,factor,True)
    asks=normalize_side(raw_asks,factor,False)
    if asks[0][0]<=bids[0][0]: raise ValueError("Crossed book")
    with localcontext() as ctx:
        ctx.prec=40
        midpoint=(bids[0][0]+asks[0][0])/2
        provenance=hashlib.sha256(json.dumps([raw_bids,raw_asks,str(factor)],separators=(",",":")).encode()).hexdigest()
        rows=[]
        for key,notional in SIZES.items():
            quote=omni.get(key)
            if not quote or not quote.get("bid") or not quote.get("ask"):
                continue
            bid,ask=positive(quote["bid"]),positive(quote["ask"])
            if bid>ask: raise ValueError("Crossed Omni quote")
            quantity=Decimal(notional)/midpoint
            buy,sell=vwap(asks,quantity),vwap(bids,quantity)
            if buy is None or sell is None:
                continue
            omni_cost=(ask-bid)*20000/(ask+bid)
            book_cost=(buy-sell)/midpoint*10000
            fee_rate=Decimal(VENUES[venue]["fee"])
            fee=(buy+sell)/midpoint*fee_rate
            rows.append({"venue":venue,"market":coin,"symbol":symbol,"currency":VENUES[venue]["currency"],
                "notional_usd":notional,"omni_bps":round(float(omni_cost),6),"book_bps":round(float(book_cost),6),
                "fee_bps":round(float(fee),6),"total_bps":round(float(book_cost+fee),6),
                "quote_time":stamp(quote_time),"book_time":stamp(book_time),"observed_at":stamp(max(quote_time,book_time)),
                "captured_at":stamp(captured),"book_sha256":provenance,
                "pricing_inputs":{"omni_bid":str(bid),"omni_ask":str(ask),"book_mid":str(midpoint),
                    "buy_vwap":str(buy),"sell_vwap":str(sell)}})
    return rows,"ok" if rows else "insufficient_depth"

def collect(snapshot, enabled=PUBLISH_VENUES, evidence_dir=None):
    quotes={row["ticker"][:-5]:row["quotes"] for row in snapshot["markets"]
            if row["ticker"] in {coin+"-PERP" for coin in COINS}}
    requested=[(venue,coin) for venue in VENUES for coin in COINS]
    outcomes={(venue,coin):([], "permission_required" if venue not in PUBLISH_VENUES else "not_sampled")
              for venue,coin in requested if venue not in enabled}
    def run(venue,coin,light=None,binance_active=()):
        try:
            quote=quotes.get(coin,{})
            if not quote.get("updated_at"): return [],"no_omni_quote"
            if venue=="binance" and coin not in binance_active:
                return [],"market_unavailable"
            book=light if venue=="lighter" else normalized_book(venue,coin)
            if book is None: return [],"source_unavailable"
            captured=now_utc()
            rows,status=costs(venue,coin,quote,book,captured)
            if rows and evidence_dir:
                symbol,when,bids,asks,factor=book
                evidence={"venue":venue,"market":coin,"symbol":symbol,"book_time":stamp(when),
                    "bids":bids,"asks":asks,"multiplier":str(factor),"quote":quote,"captured_at":stamp(captured)}
                atomic_json(evidence_dir/f"{venue}-{coin}-{int(captured.timestamp()*1000)}.json",evidence)
            return rows,status
        except (HTTPError,OSError,subprocess.SubprocessError): return [],"source_unavailable"
        except (KeyError,IndexError,TypeError,ValueError,InvalidOperation): return [],"invalid_data"
    active=set()
    if "binance" in enabled:
        try:
            records=fetch(BINANCE,"/fapi/v1/exchangeInfo")["symbols"]
            active={item["baseAsset"] for item in records if item["baseAsset"] in COINS
                and item["symbol"]==item["baseAsset"]+"USDT" and item["quoteAsset"]=="USDT"
                and item["marginAsset"]=="USDT" and item["contractType"]=="PERPETUAL" and item["status"]=="TRADING"}
        except (HTTPError,OSError,KeyError,TypeError,ValueError):
            active=set()
    with ThreadPoolExecutor(max_workers=10) as pool:
        lighter_task=pool.submit(lighter_books) if "lighter" in enabled else None
        pending={(venue,coin):pool.submit(run,venue,coin,binance_active=active) for venue,coin in requested if venue in enabled and venue!="lighter"}
        try: light=lighter_task.result() if lighter_task else {}
        except (OSError,subprocess.SubprocessError,ValueError,KeyError,TypeError,InvalidOperation): light={}
        outcomes.update({key:task.result() for key,task in pending.items()})
        if lighter_task:
            outcomes.update({("lighter",coin):run("lighter",coin,light.get(coin)) for coin in COINS})
    return [item for key in requested for item in outcomes[key][0]], [
        {"venue":venue,"market":coin,"status":outcomes[(venue,coin)][1],"qualifying_sizes":len(outcomes[(venue,coin)][0])}
        for venue,coin in requested]

def validate(dataset):
    if dataset.get("schema_version")!=1 or dataset.get("venues")!=VENUES or len(dataset["rows"])>90*60:
        raise ValueError("Unexpected execution schema or source")
    seen=set()
    for row in dataset["rows"]:
        venue,market=row["venue"],row["market"]
        if venue not in PUBLISH_VENUES or market not in COINS or row["notional_usd"] not in SIZES.values() or row["currency"]!=VENUES[venue]["currency"]:
            raise ValueError("Unknown venue, market, currency or size")
        expected={"hyperliquid":market,"binance":market+"USDT","bybit":market+"USDT","okx":market+"-USDT-SWAP"}.get(venue)
        if (expected and row["symbol"]!=expected) or (venue=="lighter" and not re.fullmatch(market+r" \(\d{1,6}\)", row["symbol"])):
            raise ValueError("Invalid instrument")
        q,b,c=map(parse_time,(row["quote_time"],row["book_time"],row["captured_at"]))
        if stamp(max(q,b))!=row["observed_at"] or abs((q-b).total_seconds())>MAX_SKEW_SECONDS or any(not -5<=(c-t).total_seconds()<=MAX_AGE_SECONDS for t in (q,b)):
            raise ValueError("Unqualified source times")
        values=[Decimal(str(row[key])) for key in ("omni_bps","book_bps","fee_bps","total_bps")]
        if any(not value.is_finite() or not 0<=value<10000 for value in values) or abs(values[1]+values[2]-values[3])>Decimal(".000002"):
            raise ValueError("Invalid execution arithmetic")
        inputs=row["pricing_inputs"]
        bid,ask,mid,buy,sell=(positive(inputs[key]) for key in ("omni_bid","omni_ask","book_mid","buy_vwap","sell_vwap"))
        with localcontext() as ctx:
            ctx.prec=40
            estimate=[(ask-bid)*20000/(ask+bid),(buy-sell)/mid*10000,(buy+sell)/mid*Decimal(VENUES[venue]["fee"])]
        if bid>ask or not sell<=mid<=buy or any(abs(declared-computed)>Decimal(".000001") for declared,computed in zip(values,estimate)):
            raise ValueError("Execution values differ from pricing inputs")
        fingerprint=row["book_sha256"]
        if len(fingerprint)!=64 or any(c not in "0123456789abcdef" for c in fingerprint):
            raise ValueError("Invalid depth provenance")
        key=venue,market,row["notional_usd"],q,b
        if key in seen: raise ValueError("Duplicate sample")
        seen.add(key)
    parse_time(dataset["attempted_at"])
    statuses=dataset["coverage"]
    if {(row["venue"],row["market"]) for row in statuses}!={(venue,coin) for venue in VENUES for coin in COINS} or len(statuses)!=len(VENUES)*len(COINS):
        raise ValueError("Incomplete execution status table")
    if any(row["status"] not in {"ok",*FAILURES} or not 0<=row["qualifying_sizes"]<=3 or (row["status"]=="ok")!=bool(row["qualifying_sizes"]) for row in statuses):
        raise ValueError("Invalid execution status")

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode",choices=("refresh","verify"))
    parser.add_argument("--data",type=Path,default=Path("public/data"))
    parser.add_argument("--venues",nargs="+",choices=PUBLISH_VENUES,default=PUBLISH_VENUES)
    parser.add_argument("--evidence-dir",type=Path)
    args=parser.parse_args()
    if args.evidence_dir:
        public=(Path(__file__).resolve().parents[1]/"public")
        if args.evidence_dir.resolve().is_relative_to(public): parser.error("Raw evidence must stay outside the public directory")
        args.evidence_dir.mkdir(parents=True,exist_ok=True)
    target=args.data/"multi-venue.json"
    existing=json.loads(target.read_text()) if target.exists() else {}
    if existing: validate(existing)
    if args.mode=="verify":
        validate(existing)
        print(f"{len(existing['rows'])} validated execution observations")
        return
    snapshot=json.loads((args.data/"markets.json").read_text())["latest"]
    rows,status=collect(snapshot,args.venues,args.evidence_dir)
    merged={(row["venue"], row["market"], row["notional_usd"],row["quote_time"],row["book_time"]):row for row in [*existing.get("rows",[]),*rows]}
    last = {(row["venue"],row["market"]):row for row in existing.get("coverage",[])}
    status = [last.get((row["venue"],row["market"]),row) if row["status"]=="not_sampled" else row for row in status]
    dataset={"schema_version":1,"venues":VENUES,"attempted_at":stamp(now_utc()),"coverage":status,
        "rows":sorted(merged.values(),key=lambda row:row["observed_at"])[-5400:]}
    validate(dataset)
    atomic_json(target,dataset)
    print(json.dumps({"accepted":len(rows),"history":len(dataset["rows"]),"venues":{v:sum(row["venue"]==v for row in rows) for v in VENUES}}))

if __name__=="__main__": main()
