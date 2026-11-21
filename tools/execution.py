"""Compare one $100K indicative Omni quote with book depth at the same time."""

from datetime import datetime, timezone
from decimal import Decimal

from sources import get_json, iso, now_utc, parse_time, positive


HL_API = "https://api.hyperliquid.xyz/info"
OMNI_DOCS = "https://docs.variational.io/technical-documentation/api"
HL_DOCS = "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint"
FEE_DOCS = "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees"
COINS = ("BTC", "ETH", "SOL", "HYPE")
NOTIONAL = Decimal("100000")
BASE_TAKER_FEE_BPS = Decimal("4.5")
MAX_SKEW_SECONDS = 45
MAX_AGE_SECONDS = 120


def depth_price(levels, buying, notional=NOTIONAL):
    remaining = notional
    size = Decimal(0)
    previous = None
    for level in levels:
        price, quantity = positive(level["px"]), positive(level["sz"])
        if previous is not None and (price < previous if buying else price > previous):
            raise ValueError("Book depth is out of order")
        previous = price
        cost = min(remaining, price * quantity)
        size += cost / price
        remaining -= cost
        if remaining == 0:
            return notional / size
    return None


def compare(coin, bid_ask, quoted_at, book, observed, *, notional=NOTIONAL):
    if book.get("coin") != coin:
        raise ValueError("Peer instrument mismatch")
    book_time = datetime.fromtimestamp(book["time"] / 1000, timezone.utc)
    quote_time = parse_time(quoted_at)
    skew = abs((book_time - quote_time).total_seconds())
    ages = [(observed - t).total_seconds() for t in (quote_time, book_time)]
    if skew > MAX_SKEW_SECONDS or min(ages) < -5 or max(ages) > MAX_AGE_SECONDS:
        return None
    bid, ask = positive(bid_ask["bid"]), positive(bid_ask["ask"])
    bids, asks = book["levels"]
    if not bids or not asks:
        return None
    best_bid, best_ask = positive(bids[0]["px"]), positive(asks[0]["px"])
    if ask < bid or best_ask <= best_bid:
        raise ValueError("Invalid bid and ask")
    buy, sell = depth_price(asks, True, notional), depth_price(bids, False, notional)
    if buy is None or sell is None:
        return None
    omni = (ask - bid) / ((ask + bid) / 2) * 10000
    book_cost = (buy - sell) / ((best_ask + best_bid) / 2) * 10000
    fees = 2 * BASE_TAKER_FEE_BPS
    return {
        "market": coin, "omni_ticker": coin + "-PERP", "peer_ticker": coin,
        "omni_bps": round(float(omni), 4), "peer_book_bps": round(float(book_cost), 4),
        "peer_fees_bps": float(fees), "peer_total_bps": round(float(book_cost + fees), 4),
        "samples": 1, "quote_time": quoted_at, "peer_book_time": iso(book_time),
        "skew_seconds": round(skew, 2),
    }


def sample(rows, observed):
    return {
        "observed_date": iso(observed)[:10], "observed_at": iso(observed),
        "notional_usd": int(NOTIONAL), "eligible_markets": len(COINS),
        "method": "Two-sided indicative Omni spread versus depth-weighted Hyperliquid bids and asks plus the base taker fee of 4.5 bps per side. $100,000 each way. Quotes must be within 45 seconds of each other and 120 seconds of collection. Only fully covered books qualify.",
        "source_urls": [OMNI_DOCS, HL_DOCS, FEE_DOCS], "rows": rows,
    }


def current_sample(omni):
    rows = []
    for coin in COINS:
        quote = omni.get(coin + "-PERP")
        if not quote:
            continue
        bid_ask, quoted_at = quote
        if abs((now_utc() - parse_time(quoted_at)).total_seconds()) > MAX_AGE_SECONDS:
            continue
        book = get_json(HL_API, {"type": "l2Book", "coin": coin}, limit=150_000)
        row = compare(coin, bid_ask, quoted_at, book, now_utc())
        if row is not None:
            rows.append(row)
    return sample(rows, now_utc())
