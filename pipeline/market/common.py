"""Shared paths, time and number helpers for the market-data package.

Python 3 standard library only. All datetimes are naive UTC.
Money is carried as integer cents so that splits sum to totals exactly.
"""
import json
import os
from datetime import datetime, timedelta, date
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_FLOOR

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STORE = os.environ.get("VM_STORE") or os.path.join(ROOT, ".cache", "store")
OUT = os.environ.get("VM_SITE_DATA") or os.path.join(ROOT, "site", "data")
CONFIG = os.path.join(ROOT, "pipeline", "config")
# VM_SNAPSHOTS: one or more folders separated by os.pathsep (CI: the data branch's snapshots_seed and snapshots).
SNAPSHOT_DIRS = [p for p in os.environ.get("VM_SNAPSHOTS", "").split(os.pathsep) if p] or [
    os.path.join(ROOT, ".cache", "snapshots_seed"),
    os.path.join(ROOT, ".cache", "snapshots"),
    os.path.join(STORE, "snapshots"),
]
# Old derived per-market files (20 and 21 Nov 2026). Read once into sealed day files; the
# folder is deleted at cutover and nothing depends on it afterwards.
LEGACY_FILES = [
    os.path.join(ROOT, "public", "data", "market-history", "2026-11-20.json"),
    os.path.join(ROOT, "public", "data", "markets.json"),
]

GROUPS = ["crypto_majors", "other_crypto", "tradfi_perp", "tradfi_swap"]
# Asset classes in Variational's own terms. The six after `crypto` are the TradFi classes.
CLASSES = ["crypto", "equity", "etf", "index", "commodity", "fx", "pre_ipo"]
TRADFI_CLASSES = CLASSES[1:]
TRADFI_GROUPS = ("tradfi_perp", "tradfi_swap")
TOP = ["crypto_majors", "other_crypto", "tradfi"]     # the three-way cut: tradfi = TradFi perpetuals plus swaps
RANGES = [("7D", 7), ("30D", 30), ("90D", 90), ("180D", 180), ("1Y", 365), ("All", None)]
SIZES = ["1k", "100k", "1m"]
SIZE_FIELD = {"1k": "size_1k", "100k": "size_100k", "1m": "size_1m"}

MIDNIGHT_TOLERANCE = timedelta(minutes=10)   # a snapshot this close to 00:00 UTC counts as taken at 00:00
PUBLICATION_DATE = "2026-11-24"
CONTINUOUS_START = "2026-11-22"
STALE_AFTER_MINUTES = 120


# ---------- time ----------

def utcnow():
    return datetime.utcnow().replace(microsecond=0)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    """'2026-11-22T08:30:00Z', with or without fractional seconds or offset, to naive UTC."""
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1]
    if "+" in s[10:]:
        s = s[: 10 + s[10:].index("+")]
    if "." in s:
        s = s[: s.index(".")]
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")


def parse_stamp(name):
    """'20261122T0730Z...' file stamp to naive UTC."""
    return datetime.strptime(name[:14], "%Y%m%dT%H%MZ")


def day_start(d):
    return datetime(d.year, d.month, d.day)


def daterange(a, b):
    """Dates a..b inclusive."""
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def to_date(s):
    return date.fromisoformat(s[:10])


# ---------- numbers ----------

def cents(x):
    """API decimal string (or number) to integer cents, half-even."""
    if x is None:
        return None
    return int((Decimal(str(x)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def usd(c):
    """Integer cents to a JSON number rounded to cents."""
    if c is None:
        return None
    return round(c / 100.0, 2)


def allocate(total_c, weights_c):
    """Split integer `total_c` across keys in proportion to integer weights (largest remainder).

    Returns {key: int} summing to total_c exactly. Deterministic: ties go to the smaller key.
    """
    s = sum(weights_c.values())
    if s <= 0:
        raise ValueError("allocate: weights sum to zero")
    out, rem = {}, []
    for k in sorted(weights_c):
        exact = Decimal(total_c) * Decimal(weights_c[k]) / Decimal(s)
        fl = int(exact.to_integral_value(rounding=ROUND_FLOOR))
        out[k] = fl
        rem.append((exact - fl, k))
    left = total_c - sum(out.values())
    rem.sort(key=lambda r: (-r[0], r[1]))
    for i in range(left):
        out[rem[i][1]] += 1
    return out


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return None
    m = n // 2
    return xs[m] if n % 2 else (xs[m - 1] + xs[m]) / 2.0


def spread_bps(quote):
    """Full quoted bid-ask over mid, in bps, for a two-sided quote; otherwise None.

    Two-sided means bid > 0, ask > 0 and bid >= ask / 2 (so the spread is at most 6,666.67 bps).
    A placeholder bid such as 0.0000013 against an ask of 0.0062 is not a quote. A crossed quote
    (ask below bid) is not counted either.
    """
    if not quote:
        return None
    try:
        bid = float(quote.get("bid"))
        ask = float(quote.get("ask"))
    except (TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0 or bid < ask / 2.0 or ask < bid:
        return None
    return (ask - bid) / ((ask + bid) / 2.0) * 1e4


MAX_SPREAD_BPS = 2.0 / 3.0 * 1e4   # the widest spread the two-sided rule lets through


def r2(x):
    return None if x is None else round(x, 2)


def r4(x):
    return None if x is None else round(x, 4)


def r6(x):
    return None if x is None else round(x, 6)


# ---------- files ----------

def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, obj):
    """Compact UTF-8 JSON, written atomically. NaN and Infinity are refused."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)
    return len(text)


def write_if_changed(path, obj):
    text = json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            if fh.read() == text:
                return False
    write_json(path, obj)
    return True
