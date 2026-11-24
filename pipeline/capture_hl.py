#!/usr/bin/env python3
"""Pair an Omni stats snapshot with venue order books taken right now. Python standard library only.

  /usr/bin/python3 pipeline/capture_hl.py <omni_snapshot.json.gz>      live: run right after the snapshot is saved
  /usr/bin/python3 pipeline/capture_hl.py --replay <capture_dir> ...   rebuild samples from an archived pair
                                                                        (variational_v2_stats.json + hl_l2books.json)

The file keeps its first name because the capture loops call it; it now reads every venue in
pipeline/config/venues.json: Hyperliquid and Lighter. (Binance was dropped on 23 Nov 2026: it refuses
GitHub's runners, so it could not be captured; samples written before then keep their binance_* fields,
which nothing reads.)

Never calls the Omni API: Omni quotes are read from the snapshot file only. The venues are read at the
same time, each on its own thread (about a second in all), then one line per market and size is appended to
<store>/exec/YYYY-MM-DD.jsonl (the UTC day in the snapshot's name). A venue that fails, is refused or
overruns its time leaves its own fields empty and never delays or blocks the others; the run is cut off at
VENUE_BUDGET_S seconds.

One sample
  omni_bps      Omni's quoted full bid-ask at that size over the quote mid. None when the size is not quoted,
                a side is zero, or the listing has no quotes (omni_status says which).
  hl_book_bps   Cost of buying and selling the same quantity (size / mid) against Hyperliquid's displayed book,
                over the book mid. None when no aggregation of the book fills the size (hl_status).
  hl_agg        Which book was walked: "finest" (unaggregated) or the finest nSigFigs aggregation that fills.
  hl_round_bps  The most that aggregation can overstate hl_book_bps (0 for "finest"); it never understates.
  omni_at, hl_at, gap_s   Omni's quote `updated_at`, the Hyperliquid book time, and hl_at - omni_at in seconds.
  matched       True only when both costs exist and |gap_s| <= match_window_s (45). Unmatched samples are
                written too, so the match rate can be measured.
  hl_fee_side_bps   Base-tier taker fee per side at capture time: the base rate for validator-operated perps;
                for trade[XYZ] markets the documented formula applied to the market's live deployerFeeScale and
                growthMode. None when those settings could not be read.
  lighter_*     The same reading for the other venue, present only where venues.json maps the market:
                <venue> (instrument), <venue>_book_bps (same cost function; these books are exact price levels,
                so there is no aggregation or rounding), <venue>_mid, <venue>_at, <venue>_gap_s,
                <venue>_matched (same 45-second rule against the same Omni quote), <venue>_fee_side_bps and
                <venue>_status: ok, insufficient_depth, fetch_failed, identity_mismatch or inactive (Lighter's
                market id no longer is the active perpetual named in venues.json). lighter_src says whether
                the book came from the websocket ("ws") or the REST fallback ("rest").

A failed market never stops the others, and a snapshot already in the store is skipped unless --force.
"""
import argparse
import gzip
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sources import hyperliquid as hl, lighter  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "pipeline", "config", "venues.json")
STORE = os.environ.get("VM_STORE") or os.path.join(ROOT, ".cache", "store")
STAMP = re.compile(r"(\d{8}T\d{4}Z)")
VENUE_BUDGET_S = 12.0  # hard stop for reading the venues; a venue still running then is recorded as failed


def load_config(path=CONFIG):
    with open(path) as handle:
        return json.load(handle)


def read_json(path):
    with open(path, "rb") as handle:
        raw = handle.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_time(text):
    """ISO 8601 with up to nanosecond fractions ("2026-11-11T14:59:27.221421594Z") -> epoch seconds."""
    match = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(\.\d+)?(Z|[+-]\d\d:?\d\d)?$", text.strip())
    if not match:
        raise ValueError("bad time " + text)
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    offset = 0
    zone = match.group(8)
    if zone and zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        digits = zone[1:].replace(":", "")
        offset = sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
    base = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc).timestamp()
    return base + float(match.group(7) or 0) - offset


def omni_quote(listing, size_key):
    """(bps, mid, updated_at seconds, status) for one listing and size ("100k" / "1m")."""
    if listing is None:
        return None, None, None, "not_listed"
    quotes = listing.get("quotes")
    if not quotes:
        return None, None, None, "closed"
    try:
        at = parse_time(quotes["updated_at"])
    except Exception:
        at = None
    quote = quotes.get("size_" + size_key)
    if not quote:
        return None, None, at, "not_quoted"
    try:
        bid, ask = float(quote["bid"]), float(quote["ask"])
    except Exception:
        return None, None, at, "bad_quote"
    if bid <= 0 or ask <= 0:
        return None, None, at, "zero_side"
    if ask < bid:
        return None, None, at, "crossed"
    if at is None:
        return None, None, None, "no_time"
    middle = (ask + bid) / 2
    return (ask - bid) / middle * 1e4, middle, at, "ok"


def venue_fields(prefix, instrument, book, failure, fee, usd, omni_bps, omni_at, window):
    """The <prefix>_* fields of one sample: the shared cost function on one venue's exact-level book."""
    cost = mid = at = None
    status = failure
    if status is None:
        try:
            if not hl.valid_book(book):
                status = "fetch_failed"
            else:
                at, mid = int(book["time"]) / 1000, hl.mid(book)
                cost = hl.round_trip(book, usd)
                status = "ok" if cost is not None else "insufficient_depth"
        except Exception as error:
            cost, status = None, "error:" + type(error).__name__
    gap = at - omni_at if (at is not None and omni_at is not None) else None
    return {
        prefix: instrument,
        prefix + "_at": iso(at) if at is not None else None,
        prefix + "_gap_s": round(gap, 3) if gap is not None else None,
        prefix + "_matched": bool(omni_bps is not None and cost is not None and gap is not None and abs(gap) <= window),
        prefix + "_book_bps": round(cost, 4) if cost is not None else None,
        prefix + "_mid": mid,
        prefix + "_status": status,
        prefix + "_fee_side_bps": fee,
    }


def lighter_reading(spec, extra):
    """(book, failure status or None, fee per side or None) for one market's Lighter mapping."""
    data = extra.get("lighter")
    if not data:
        return None, "fetch_failed", None
    listed = (data.get("markets") or {}).get(spec["id"]) if data.get("markets") is not None else None
    fee = spec.get("taker_fee_bps")
    if data.get("markets") is not None:
        if not listed or listed.get("symbol") != spec["symbol"]:
            return None, "identity_mismatch", None
        if listed.get("status") != "active" or listed.get("market_type") != "perp":
            return None, "inactive", None
        try:
            if float(listed.get("taker_fee")) != 0:
                fee = None  # the venue reports a fee this file has not verified
        except (TypeError, ValueError):
            fee = None
    book = (data.get("books") or {}).get(spec["id"])
    return book, (None if book else "fetch_failed"), fee


def build_samples(config, listings, books, universes, snap, src, run_at=None, extra=None):
    """One sample per market and size. `books` is {coin: {agg: book}}; `universes` is {dex: {coin: entry} | None}.

    `extra` holds the other venues' readings ({"lighter": {...} | None}); when it is
    None (a replay of an archive that has only Hyperliquid books) their fields are left out of the sample.
    """
    window = config["match_window_s"]
    base_fee = config["hyperliquid"]["base_taker_bps"]
    samples = []
    for market in config["markets"]:
        coin = market["hl"]
        coin_books = books.get(coin) or {}
        if market.get("hl_dex"):
            universe = universes.get(market["hl_dex"])
            fee = hl.universe_fee(universe.get(coin), base_fee) if universe else None
        else:
            fee = base_fee
        for size_key, usd in config["sizes"].items():
            row = {"snap": snap, "src": src, "key": market["key"], "size": size_key,
                   "omni": market["omni"], "hl": coin}
            try:
                bps, omni_mid, omni_at, status = omni_quote(listings.get(market["omni"]), size_key)
            except Exception as error:
                bps, omni_mid, omni_at, status = None, None, None, "error:" + type(error).__name__
            try:
                cost = hl.book_cost(coin_books, usd)
                hl_status = "ok" if cost["bps"] is not None else (
                    "insufficient_depth" if any(coin_books.values()) else "fetch_failed")
            except Exception as error:
                cost = {"bps": None, "agg": None, "round_bps": None, "time_ms": None, "mid": None}
                hl_status = "error:" + type(error).__name__
            hl_at = cost["time_ms"] / 1000 if cost["time_ms"] else None
            if hl_at is None:  # no usable book for this size: still record when the venue was read
                times = [int(book["time"]) for book in coin_books.values() if book]
                hl_at = min(times) / 1000 if times else None
            gap = hl_at - omni_at if (hl_at is not None and omni_at is not None) else None
            row.update({
                "omni_at": iso(omni_at) if omni_at is not None else None,
                "hl_at": iso(hl_at) if hl_at is not None else None,
                "gap_s": round(gap, 3) if gap is not None else None,
                "matched": bool(bps is not None and cost["bps"] is not None and gap is not None and abs(gap) <= window),
                "omni_bps": round(bps, 4) if bps is not None else None,
                "omni_mid": omni_mid,
                "omni_status": status,
                "hl_book_bps": round(cost["bps"], 4) if cost["bps"] is not None else None,
                "hl_mid": cost["mid"],
                "hl_agg": cost["agg"],
                "hl_round_bps": round(cost["round_bps"], 4) if cost["round_bps"] is not None else None,
                "hl_status": hl_status,
                "hl_fee_side_bps": round(fee, 4) if fee is not None else None,
            })
            if extra is not None:
                for prefix, reading in (("lighter", lighter_reading),):
                    spec = market.get(prefix)
                    if not spec:
                        continue
                    try:
                        book, failure, venue_fee = reading(spec, extra)
                    except Exception as error:
                        book, failure, venue_fee = None, "error:" + type(error).__name__, None
                    row.update(venue_fields(prefix, spec["symbol"], book, failure, venue_fee, usd, bps, omni_at, window))
                    if prefix == "lighter" and book:
                        row["lighter_src"] = book.get("src")
            if run_at is not None:
                row["run_at"] = iso(run_at)
            samples.append(row)
    return samples


def fetch_live(config):
    """Books for every mapped coin, finest first, coarser aggregations only where the finest cannot fill.

    Typically under a second. Bounded when the API hangs: each request gives up after 2.5s and one retry, the
    market settings are read alongside the first batch, and the second batch is skipped if the first was slow.
    """
    coins = [market["hl"] for market in config["markets"]]
    dexes = sorted({market["hl_dex"] for market in config["markets"] if market.get("hl_dex")})
    largest = max(config["sizes"].values())
    books = {coin: {} for coin in coins}
    started = time.time()
    with ThreadPoolExecutor(max_workers=1) as side:
        settings = side.submit(lambda: {dex: hl.fetch_universe(dex, timeout=2.5) for dex in dexes})
        for (coin, agg), book in hl.fetch_books([(coin, "finest") for coin in coins], timeout=2.5).items():
            books[coin][agg] = book
        thin = [coin for coin in coins
                if books[coin].get("finest") and hl.round_trip(books[coin]["finest"], largest) is None]
        if thin and time.time() - started < 4.0:
            jobs = [(coin, agg) for coin in thin for agg in hl.AGG_ORDER[1:]]
            for (coin, agg), book in hl.fetch_books(jobs, timeout=2.5).items():
                books[coin][agg] = book
        universes = settings.result()
    return books, universes


def fetch_lighter(config):
    """Every mapped Lighter book from one websocket connection; REST for any market the socket did not deliver."""
    ids = [market["lighter"]["id"] for market in config["markets"] if market.get("lighter")]
    with ThreadPoolExecutor(max_workers=1) as side:
        listing = side.submit(lighter.markets, 2.5)
        books = lighter.ws_books(ids, timeout=4.0)
        missing = [market for market in ids if market not in books]
        if missing:
            with ThreadPoolExecutor(max_workers=len(missing)) as pool:
                for market, book in zip(missing, pool.map(lambda m: lighter.rest_book(m, 2.5), missing)):
                    if book:
                        books[market] = book
        return {"books": books, "markets": listing.result()}


def fetch_all(config, budget=VENUE_BUDGET_S):
    """Read the venues at once. Returns (hl books, hl universes, extra); a venue that raised or was
    still running at the budget is None in `extra` (empty for Hyperliquid) and its samples say fetch_failed."""
    results = {}

    def run(name, call):
        try:
            results[name] = call(config)
        except Exception:
            results[name] = None

    threads = [threading.Thread(target=run, args=(name, call), daemon=True)
               for name, call in (("hl", fetch_live), ("lighter", fetch_lighter))]
    deadline = time.time() + budget
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(max(0.0, deadline - time.time()))
    done = dict(results)  # a venue finishing after this line is ignored
    books, universes = done.get("hl") or ({}, {})
    return books, universes, {"lighter": done.get("lighter")}


def load_replay(directory):
    """An archived pair written by the research script: returns (listings, books, universes)."""
    stats = read_json(os.path.join(directory, "variational_v2_stats.json"))
    raw = read_json(os.path.join(directory, "hl_l2books.json"))
    if "books" in raw:
        books, universe = raw["books"], raw.get("xyz_meta")
    else:  # first capture of 11 Nov 2026: books only, no market settings
        books, universe = raw, None
    books = {coin: {agg: (book if hl.valid_book(book) else None) for agg, book in by_agg.items()}
             for coin, by_agg in books.items()}
    return {item["ticker"]: item for item in stats["listings"]}, books, {"xyz": universe}


def day_path(store, snap):
    return os.path.join(store, "exec", "%s-%s-%s.jsonl" % (snap[:4], snap[4:6], snap[6:8]))


def already_stored(path, snap):
    if not os.path.exists(path):
        return False
    needle = '"snap":"%s"' % snap
    with open(path) as handle:
        return any(needle in line for line in handle)


def append(store, snap, samples):
    path = day_path(store, snap)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as handle:
        handle.write("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in samples))
    return path


def summary(snap, samples, elapsed):
    quoted = [row for row in samples if row["omni_bps"] is not None]
    matched = [row for row in samples if row["matched"]]
    no_book = [row for row in samples if row["hl_book_bps"] is None]
    gaps = [abs(row["gap_s"]) for row in quoted if row["gap_s"] is not None]
    aggregated = sorted({"%s/%s:%s" % (row["key"], row["size"], row["hl_agg"]) for row in samples
                         if row["hl_agg"] not in (None, "finest")})
    others = []
    for prefix in ("lighter",):
        rows = [row for row in samples if prefix + "_status" in row]
        if not rows:
            continue
        mapped = [row for row in rows if row["omni_bps"] is not None]
        bad = sorted({row[prefix + "_status"] for row in rows if row[prefix + "_status"] != "ok"})
        others.append("%s=%d/%d%s" % (prefix, sum(row[prefix + "_matched"] for row in rows), len(mapped),
                                     "(" + ",".join(bad) + ")" if bad else ""))
    return "%s samples=%d omni_quoted=%d matched=%d hl_no_cost=%d max_gap=%s %saggregated=[%s] %.1fs" % (
        snap, len(samples), len(quoted), len(matched), len(no_book),
        ("%.0fs" % max(gaps)) if gaps else "n/a", "".join(text + " " for text in others), " ".join(aggregated), elapsed)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("snapshot", nargs="?", help="Omni stats snapshot (.json or .json.gz) saved a moment ago")
    parser.add_argument("--replay", nargs="+", metavar="DIR", help="archived capture directories to rebuild from")
    parser.add_argument("--store", default=STORE)
    parser.add_argument("--force", action="store_true", help="append even if the snapshot is already stored")
    parser.add_argument("--dry-run", action="store_true", help="print the samples, write nothing")
    parser.add_argument("--src", choices=("live", "refresh"), default="live",
                        help="label of live samples: refresh = a second reading of the same tick (ci/capture.py)")
    args = parser.parse_args()
    if bool(args.snapshot) == bool(args.replay):
        parser.error("give one snapshot file, or --replay with capture directories")
    config = load_config()
    jobs = [("replay", path) for path in (args.replay or [])] or [("live", args.snapshot)]
    failed = 0
    for mode, path in jobs:
        started = time.time()
        try:
            found = STAMP.search(os.path.basename(os.path.normpath(path)))
            if mode == "live":
                snap = found.group(1) if found else time.strftime("%Y%m%dT%H%MZ", time.gmtime(os.path.getmtime(path)))
                if not args.force and not args.dry_run and already_stored(day_path(args.store, snap), snap):
                    print("%s already stored, skipped" % snap)
                    continue
                books, universes, extra = fetch_all(config)  # venues first: every second here widens the gap
                listings = {item["ticker"]: item for item in read_json(path)["listings"]}
                samples = build_samples(config, listings, books, universes, snap, args.src, run_at=started, extra=extra)
            else:
                if not found:
                    raise ValueError("no UTC stamp in directory name")
                snap = found.group(1)
                if not args.force and not args.dry_run and already_stored(day_path(args.store, snap), snap):
                    print("%s already stored, skipped" % snap)
                    continue
                listings, books, universes = load_replay(path)
                samples = build_samples(config, listings, books, universes, snap, "backfill")
            if args.dry_run:
                for row in samples:
                    print(json.dumps(row, separators=(",", ":")))
            else:
                append(args.store, snap, samples)
            print(summary(snap, samples, time.time() - started))
        except Exception as error:  # one bad input must not take the capture loop down
            failed += 1
            print("capture failed for %s: %s: %s" % (path, type(error).__name__, error), file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
