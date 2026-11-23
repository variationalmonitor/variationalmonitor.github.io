"""Binance USDⓈ-M futures public depth. Stdlib only.

GET https://fapi.binance.com/fapi/v1/depth?symbol=<SYMBOL>&limit=1000 returns up to 1,000 exact price levels
per side with `E` (message output time, ms), used as the book time. Books are returned in the shape
`sources/hyperliquid.py` walks ({"time": ms, "levels": [bids, asks]} with "px"/"sz" strings, best first).
Weight 20 per call against a 2,400-a-minute limit.

Refusal
  Binance does not serve some locations (it answers from Korea; cloud runners hosted in the United States are
  expected to be refused). A refusal is an HTTP 451 or 403. `depth_books` asks for one symbol first; if that
  answer is a refusal it stops there, sends nothing else, and reports every symbol as REFUSED. Nothing is
  retried and no other host, mirror or proxy is ever tried: the venue is simply unavailable for that tick.
"""
import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "https://fapi.binance.com"
REFUSED = "venue_unavailable"
FAILED = "fetch_failed"
REFUSAL_CODES = (451, 403)


def depth(symbol, timeout=2.5):
    """(book, None) on success, (None, REFUSED | FAILED) otherwise. One attempt, never raises."""
    url = "%s/fapi/v1/depth?symbol=%s&limit=1000" % (API, symbol)
    try:
        request = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": "variational-monitor"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.load(response)
        book = {"time": int(body["E"]), "src": "rest",
                "levels": [[{"px": price, "sz": size} for price, size in body["bids"]],
                           [{"px": price, "sz": size} for price, size in body["asks"]]]}
        if not book["levels"][0] or not book["levels"][1]:
            return None, FAILED
        return book, None
    except urllib.error.HTTPError as error:
        return None, REFUSED if error.code in REFUSAL_CODES else FAILED
    except Exception:
        return None, FAILED


def depth_books(symbols, timeout=2.5):
    """{symbol: (book or None, status or None)}. The first symbol is asked alone; a refusal ends the tick."""
    symbols = list(dict.fromkeys(symbols))
    if not symbols:
        return {}
    first = depth(symbols[0], timeout)
    if first[1] == REFUSED:
        return {symbol: (None, REFUSED) for symbol in symbols}
    results = {symbols[0]: first}
    rest = symbols[1:]
    if rest:
        with ThreadPoolExecutor(max_workers=len(rest)) as pool:
            results.update(zip(rest, pool.map(lambda symbol: depth(symbol, timeout), rest)))
    if any(status == REFUSED for _, status in results.values()):  # refused part-way: the whole venue is out
        return {symbol: (None, REFUSED) for symbol in symbols}
    return results
