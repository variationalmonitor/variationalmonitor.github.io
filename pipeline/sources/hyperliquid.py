"""Hyperliquid info API: order books, book-walk cost and the documented fee formula. Stdlib only.

Everything the venue comparison takes from Hyperliquid goes through this module, live or replayed.

Book cost
  `round_trip(book, usd)` buys and sells the same quantity (usd / mid) against the displayed book and returns
  (ask VWAP - bid VWAP) / mid in bps, where mid is the book's own best bid and ask. It returns None when
  either side of the 20 returned levels cannot fill the quantity.

Aggregation
  `l2Book` returns at most 20 levels per side. Where they do not reach the size, the same call with
  `nSigFigs` (and `mantissa`) returns 20 coarser levels. Checked against the unaggregated book at the same
  book time on 22 Nov 2026 (BTC, SOL, HYPE, xyz:GOLD, xyz:SILVER; every complete bucket matched in price and
  size): bids are floored and asks are ceiled to a bucket of
      10 ** (floor(log10(price)) - nSigFigs + 1) * mantissa
  so every aggregated level is priced against the taker by less than one bucket. A cost read from an
  aggregated book therefore overstates the true cost by at most 2 * bucket / mid (one bucket per side) and
  never understates it. `book_cost` uses the finest aggregation that fills the size and reports that bound.

Fees
  `taker_fee_bps` is the taker branch of "Fee formula for developers" on
  https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees for a perpetual quoted in a non-aligned asset.
"""
import json
import math
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "https://api.hyperliquid.xyz/info"
# Finest first. `nsf5` equals the unaggregated book wherever the tick is already five significant figures.
AGGS = [
    ("finest", {}),
    ("nsf5", {"nSigFigs": 5}),
    ("nsf5m2", {"nSigFigs": 5, "mantissa": 2}),
    ("nsf5m5", {"nSigFigs": 5, "mantissa": 5}),
    ("nsf4", {"nSigFigs": 4}),
]
AGG_PARAMS = dict(AGGS)
AGG_ORDER = [name for name, _ in AGGS]


def post(body, timeout=4.0, tries=2):
    """One info request. Returns parsed JSON, or None after `tries` failures (never raises)."""
    data = json.dumps(body).encode()
    for attempt in range(tries):
        try:
            request = urllib.request.Request(API, data=data, headers={"content-type": "application/json"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except Exception:
            if attempt + 1 < tries:
                time.sleep(0.3)
    return None


def fetch_books(jobs, timeout=4.0, workers=16):
    """jobs: [(coin, agg_name)]. Returns {(coin, agg_name): book or None}, fetched in parallel.

    Requests answered from the same node state carry the same `time`, so one batch is one moment.
    """
    def one(job):
        coin, agg = job
        book = post(dict({"type": "l2Book", "coin": coin}, **AGG_PARAMS[agg]), timeout=timeout)
        if not valid_book(book, coin):
            return job, None
        return job, book

    if not jobs:
        return {}
    with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
        return dict(pool.map(one, jobs))


def fetch_universe(dex, timeout=4.0):
    """{coin: universe entry} for a builder-deployed dex ("xyz"), or None when the call fails."""
    meta = post({"type": "meta", "dex": dex}, timeout=timeout)
    try:
        return {entry["name"]: entry for entry in meta["universe"]}
    except Exception:
        return None


def valid_book(book, coin=None):
    try:
        bids, asks = book["levels"]
        if coin is not None and book.get("coin") not in (None, coin):
            return False
        return bool(bids) and bool(asks) and int(book["time"]) > 0 and float(bids[0]["px"]) < float(asks[0]["px"])
    except Exception:
        return False


def mid(book):
    bids, asks = book["levels"]
    return (float(bids[0]["px"]) + float(asks[0]["px"])) / 2


def vwap(levels, quantity):
    """Average price of taking `quantity` (base units) from best to worst; None if the levels run out."""
    left, cost = quantity, 0.0
    for level in levels:
        price, size = float(level["px"]), float(level["sz"])
        take = size if size < left else left
        cost += take * price
        left -= take
        if left <= quantity * 1e-12:
            return cost / quantity
    return None


def round_trip(book, usd):
    """Cost in bps of the book's mid of buying and selling usd / mid at once; None if a side cannot fill."""
    bids, asks = book["levels"]
    middle = mid(book)
    quantity = usd / middle
    sell, buy = vwap(bids, quantity), vwap(asks, quantity)
    if sell is None or buy is None:
        return None
    return (buy - sell) / middle * 1e4


def bucket(price, agg):
    """Price width of one aggregated level; 0.0 for the unaggregated book."""
    params = AGG_PARAMS[agg]
    if not params:
        return 0.0
    return 10 ** (math.floor(math.log10(price)) - params["nSigFigs"] + 1) * params.get("mantissa", 1)


def book_cost(books, usd):
    """books: {agg_name: book or None} for one coin. Uses the finest aggregation whose levels fill `usd`.

    Returns {"bps", "agg", "round_bps", "time_ms", "mid"}; "bps" is None when no available book fills the size.
    "round_bps" is the most the aggregation can overstate "bps" (0 when the unaggregated book was used).
    """
    for agg in AGG_ORDER:
        book = books.get(agg)
        if not book:
            continue
        cost = round_trip(book, usd)
        if cost is None:
            continue
        middle = mid(book)
        width = bucket(middle, agg)
        # An aggregation no coarser than the tick returns the unaggregated book: no rounding then.
        finest = books.get("finest")
        if width and finest and identical(book, finest):
            width = 0.0
        return {"bps": cost, "agg": agg, "round_bps": 2 * width / middle * 1e4,
                "time_ms": int(book["time"]), "mid": middle}
    return {"bps": None, "agg": None, "round_bps": None, "time_ms": None, "mid": None}


def identical(a, b):
    """True when two books taken at the same book time list the same levels (aggregation was a no-op)."""
    if a.get("time") != b.get("time"):
        return False
    strip = lambda book: [[(lv["px"], lv["sz"]) for lv in side] for side in book["levels"]]
    return strip(a) == strip(b)


def taker_fee_bps(base_taker_bps, deployer_fee_scale=None, growth_mode=False):
    """Taker fee per side in bps at the given base rate.

    Validator-operated perpetual: the base rate. Builder-deployed (HIP-3) perpetual: base * scaleIfHip3 *
    growthModeScale, with scaleIfHip3 = scale + 1 below 1 and scale * 2 from 1, growthModeScale = 0.1 in
    growth mode. Referral, staking and volume-tier discounts are not applied (base tier).
    """
    if deployer_fee_scale is None:
        return base_taker_bps
    scale = float(deployer_fee_scale)
    hip3 = scale + 1 if scale < 1 else scale * 2
    return base_taker_bps * hip3 * (0.1 if growth_mode else 1.0)


def universe_fee(entry, base_taker_bps):
    """Fee per side for a builder-dex universe entry (fields `deployerFeeScale`, `growthMode`)."""
    if not entry or entry.get("deployerFeeScale") is None:
        return None
    return taker_fee_bps(base_taker_bps, entry["deployerFeeScale"], entry.get("growthMode") == "enabled")
