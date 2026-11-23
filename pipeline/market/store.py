"""The Omni store: raw snapshots in, `platform.jsonl` and sealed `days/YYYY-MM-DD.json` out.

Nothing here calls the Omni API. Snapshots are read from the capture folders; the two old derived
files for 20 and 21 Nov 2026 are read through an adapter. Everything is idempotent: a snapshot
already in `platform.jsonl` is not read again and a sealed day file is never rewritten.

Day assignment. A snapshot belongs to the UTC day whose end (00:00 of the next day) is nearest to
its capture time; that is the day with the larger overlap with its trailing-24h window. The
snapshot nearest that midnight is the day's boundary snapshot. A day is sealed once a snapshot at
or after its end exists. The day is `approx` when the boundary snapshot is more than ten minutes
from midnight.
"""
import glob
import gzip
import json
import os
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from . import financing
from .common import (STORE, SNAPSHOT_DIRS, LEGACY_FILES, SIZES, SIZE_FIELD, MIDNIGHT_TOLERANCE,
                     day_start, iso, parse_iso, parse_stamp, median, spread_bps, read_json,
                     write_json, utcnow)

PLATFORM_PATH = os.path.join(STORE, "platform.jsonl")
SEEN_PATH = os.path.join(STORE, "listings_seen.json")
DAYS_DIR = os.path.join(STORE, "days")
MARKS = {"floor": Decimal("0.1095"), "zero": Decimal("0"), "preipo": Decimal("0.05475")}
# Day-file schema. 2: two-sided quote rule (bid >= ask / 2) and `quoted_1m` counted over every
# snapshot of the day. A day sealed under an older schema is sealed again while its raw snapshots
# are still on disk; once they are gone the file stays as it is.
# 3: `pairs` holds each leg's bid-ask (perpetual and swap) over the snapshots where both are quoted.
SCHEMA = 3


# ---------- snapshot readers ----------

def _norm_listing(l, legacy=False):
    oi = l.get("open_interest") or {}
    quotes = l.get("quotes") or None
    return {
        "ticker": l["ticker"],
        "name": l.get("name"),
        "type": "swap" if l["contract_type"] == "swap" else "perp",
        "vol24h": l["volume_24h_usdc"] if legacy else l["volume_24h"],
        "oi_long": l["long_open_interest_usdc"] if legacy else oi.get("long_open_interest"),
        "oi_short": l["short_open_interest_usdc"] if legacy else oi.get("short_open_interest"),
        "funding": l.get("funding"),
        "quotes": quotes,
    }


def _load_raw(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        d = json.load(fh)
    return {
        "platform": {
            "cumulative_volume": d["cumulative_volume"],
            "volume_24h": d["total_volume_24h"],
            "dual_sided_oi": d["open_interest"],
            "tvl": d["tvl"],
            "markets": d["num_markets"],
        },
        "listings": [_norm_listing(l) for l in d["listings"]],
    }


def _legacy_snapshot(path):
    d = read_json(path)
    return d["snapshot"] if "snapshot" in d else d["latest"]


def _load_legacy(path):
    s = _legacy_snapshot(path)
    t = s["totals"]
    return {
        "platform": {
            "cumulative_volume": t["cumulative_total_volume_usdc"],
            "volume_24h": t["volume_24h_usdc"],
            "dual_sided_oi": t["open_interest_total_usdc"],
            "tvl": t["tvl_usdc"],
            "markets": t["num_markets_reported"],
        },
        "listings": [_norm_listing(l, legacy=True) for l in s["markets"]],
    }


class Ref:
    """A snapshot on disk: capture time, where it is, how to read it."""

    def __init__(self, t, path, legacy=False):
        self.t, self.path, self.legacy = t, path, legacy
        self.key = iso(t)
        base = os.path.basename(path)
        self.src = ("legacy:" if legacy else os.path.basename(os.path.dirname(path)) + "/") + base
        self._snap = None

    def load(self):
        if self._snap is None:
            self._snap = _load_legacy(self.path) if self.legacy else _load_raw(self.path)
            self._snap["t"] = self.t
        return self._snap

    def drop(self):
        self._snap = None


def list_refs():
    refs = {}
    for folder in SNAPSHOT_DIRS:
        for path in sorted(glob.glob(os.path.join(folder, "*.json.gz"))):
            try:
                t = parse_stamp(os.path.basename(path))
            except ValueError:
                continue
            refs.setdefault(iso(t), Ref(t, path))
    for path in LEGACY_FILES:
        if os.path.exists(path):
            try:
                t = parse_iso(_legacy_snapshot(path)["observed_at"])
            except (KeyError, ValueError):
                continue
            refs.setdefault(iso(t), Ref(t, path, legacy=True))
    return [refs[k] for k in sorted(refs)]


# ---------- platform.jsonl and the listing register ----------

def load_platform():
    rows = {}
    if os.path.exists(PLATFORM_PATH):
        with open(PLATFORM_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    rows[r["t"]] = r
    return rows


def _write_platform(rows):
    os.makedirs(STORE, exist_ok=True)
    tmp = PLATFORM_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for k in sorted(rows):
            fh.write(json.dumps(rows[k], separators=(",", ":")) + "\n")
    os.replace(tmp, PLATFORM_PATH)


def load_seen():
    return read_json(SEEN_PATH) if os.path.exists(SEEN_PATH) else {}


def _marks(listing):
    f = listing.get("funding")
    if listing["type"] != "perp" or not f or not f.get("last"):
        return []
    try:
        rate = Decimal(str(f["last"]["annualized_rate"]))
    except Exception:
        return []
    return [k for k, v in MARKS.items() if rate == v]


def ingest(refs):
    """Add every snapshot not yet in platform.jsonl; update the listing register. Returns counts."""
    rows, seen = load_platform(), load_seen()
    added = 0
    for ref in refs:
        if ref.key in rows:
            continue
        snap = ref.load()
        p = snap["platform"]
        updated = [l["quotes"].get("updated_at") for l in snap["listings"]
                   if l.get("quotes") and l["quotes"].get("updated_at")]
        rows[ref.key] = {
            "t": ref.key,
            "cumulative_volume": p["cumulative_volume"],
            "volume_24h": p["volume_24h"],
            "dual_sided_oi": p["dual_sided_oi"],
            "tvl": p["tvl"],
            "markets": p["markets"],
            "listings": len(snap["listings"]),
            "quote_max_updated_at": iso(parse_iso(max(updated))) if updated else None,
            "src": ref.src,
        }
        for l in snap["listings"]:
            s = seen.setdefault(l["ticker"], {"name": l["name"], "type": l["type"],
                                              "first": ref.key, "last": ref.key, "marks": []})
            s["first"], s["last"] = min(s["first"], ref.key), max(s["last"], ref.key)
            if s["last"] == ref.key and l["name"]:
                s["name"] = l["name"]
            s["marks"] = sorted(set(s["marks"]) | set(_marks(l)))
        added += 1
        ref.drop()
    # a repeated response (same counters as the snapshot before it) is flagged and never sampled
    prev = None
    for k in sorted(rows):
        r = rows[k]
        sig = (r["cumulative_volume"], r["volume_24h"], r["dual_sided_oi"])
        r["dup"] = bool(prev is not None and sig == prev)
        prev = sig
    if added or not os.path.exists(PLATFORM_PATH):
        _write_platform(rows)
        write_json(SEEN_PATH, {k: seen[k] for k in sorted(seen)})
    return {"snapshots": len(rows), "added": added, "listings_seen": len(seen),
            "duplicates": sum(1 for r in rows.values() if r["dup"])}


# ---------- sealing ----------

def day_path(d):
    return os.path.join(DAYS_DIR, d.isoformat() + ".json")


def sealed_days():
    out = []
    for p in sorted(glob.glob(os.path.join(DAYS_DIR, "*.json"))):
        out.append(os.path.basename(p)[:10])
    return out


def load_day(d):
    return read_json(os.path.join(DAYS_DIR, (d if isinstance(d, str) else d.isoformat()) + ".json"))


def financing_refs(before_date):
    """Latest schedule-established normalised long rate per swap from sealed days before a date."""
    from .common import to_date
    refs = {}
    for ds in sealed_days():
        if to_date(ds) >= before_date:
            continue
        for tk, row in load_day(ds)["listings"].items():
            if row["type"] == "swap" and row.get("fin_basis") == "schedule" and row.get("fin_long_ann"):
                refs[tk] = (row["fin_long_ann"], to_date(row["fin_pay"]))
    return refs


def fin_ref_for(snap_t):
    """Financing references for a snapshot: sealed days before the day the snapshot would close."""
    return financing_refs((snap_t - timedelta(hours=12)).date())


def listing_row(l, snap_t, classifier, fin_cfg, fin_ref):
    """Everything derived from one listing in one snapshot (shared by day files and the latest table)."""
    group, cls = classifier.get(l["ticker"], l["type"], l["name"])[:2]
    row = {"name": l["name"], "type": l["type"], "group": group, "cls": cls,
           "vol24h": l["vol24h"], "oi_long": l["oi_long"], "oi_short": l["oi_short"],
           "funding_ann": None, "fin_long_ann": None, "fin_short_ann": None}
    f = l.get("funding")
    if l["type"] == "perp":
        if f and f.get("last") and f["last"].get("annualized_rate") is not None:
            row["funding_ann"] = float(f["last"]["annualized_rate"])
    else:
        n = financing.normalise(fin_cfg.get(l["ticker"]), snap_t, (f or {}).get("last"),
                                fin_ref.get(l["ticker"]))
        row.update({"fin_long_ann": n["long"], "fin_short_ann": n["short"], "fin_mult": n["mult"],
                    "fin_basis": n["basis"], "fin_pay": n["pay"],
                    "fin_raw_long": n["raw_long"], "fin_raw_short": n["raw_short"]})
    q = l.get("quotes") or {}
    row["quote_at"] = iso(parse_iso(q["updated_at"])) if q.get("updated_at") else None
    row["ba_last"] = {s: spread_bps(q.get(SIZE_FIELD[s])) for s in SIZES}
    return row


def seal(refs, classifier, fin_cfg, pairs, log=print):
    """Seal every finished, unsealed UTC day that has a snapshot assigned to it."""
    rows = load_platform()
    live = [r for r in refs if not rows.get(r.key, {}).get("dup")]
    if not live:
        return []
    last_t = max(r.t for r in live)
    by_day = defaultdict(list)
    for r in live:
        by_day[(r.t - timedelta(hours=12)).date()].append(r)
    os.makedirs(DAYS_DIR, exist_ok=True)
    done = []
    for d in sorted(by_day):
        end = day_start(d) + timedelta(days=1)
        if last_t < end:
            continue
        resealed = False
        if os.path.exists(day_path(d)):
            if load_day(d).get("schema", 1) >= SCHEMA:
                continue
            resealed = True
        boundary = min(by_day[d], key=lambda r: (abs((r.t - end).total_seconds()), -r.t.timestamp()))
        offset = (boundary.t - end).total_seconds() / 60.0
        approx = abs(boundary.t - end) > MIDNIGHT_TOLERANCE
        start = day_start(d)
        samples = [r for r in live if start + MIDNIGHT_TOLERANCE < r.t <= end + MIDNIGHT_TOLERANCE]
        if boundary not in samples:
            samples.append(boundary)
        samples.sort(key=lambda r: r.t)

        # bid-ask samples per listing and size; pair differences from the same snapshot
        ba = defaultdict(lambda: {s: [] for s in SIZES})
        q1m_any = {}
        pair_legs = {p["key"]: ([], []) for p in pairs}
        for r in samples:
            snap = r.load()
            at100k = {}
            for l in snap["listings"]:
                q = l.get("quotes") or {}
                for s in SIZES:
                    v = spread_bps(q.get(SIZE_FIELD[s]))
                    if v is not None:
                        ba[l["ticker"]][s].append(v)
                        if s == "1m":
                            q1m_any[l["ticker"]] = l["type"]
                        if s == "100k":
                            at100k[l["ticker"]] = v
            for p in pairs:
                if p["perp"] in at100k and p["swap"] in at100k:
                    pair_legs[p["key"]][0].append(at100k[p["perp"]])
                    pair_legs[p["key"]][1].append(at100k[p["swap"]])
            if r is not boundary:
                r.drop()

        snap = boundary.load()
        fin_ref = financing_refs(d)
        listings = {}
        for l in snap["listings"]:
            row = listing_row(l, boundary.t, classifier, fin_cfg, fin_ref)
            last = row.pop("ba_last")
            row["q1m_last"] = last["1m"] is not None
            row["q1m_any"] = l["ticker"] in q1m_any
            row["ba"] = {s: [round(median(v), 6), round(min(v), 6), round(max(v), 6), len(v)]
                         for s, v in ba[l["ticker"]].items() if v}
            listings[l["ticker"]] = row
        boundary.drop()

        p = snap["platform"]
        open_row = None
        for k, r in rows.items():
            if not r["dup"] and abs(parse_iso(k) - start) <= MIDNIGHT_TOLERANCE:
                open_row = r
        vol_method, vol_day = None, None
        if not approx and open_row is not None:
            vol_method = "cumulative_diff"
            vol_day = str(Decimal(p["cumulative_volume"]) - Decimal(open_row["cumulative_volume"]))
        elif not approx:
            vol_method = "trailing24h_at_close"
            vol_day = p["volume_24h"]
        day = {
            "schema": SCHEMA,
            "date": d.isoformat(),
            "sealed_at": iso(utcnow()),
            "boundary": boundary.key,
            "boundary_src": boundary.src,
            "offset_minutes": round(offset, 1),
            "approx": approx,
            "samples": len(samples),
            "sample_first": samples[0].key,
            "sample_last": samples[-1].key,
            "platform": {
                "cumulative_volume_close": p["cumulative_volume"],
                "cumulative_volume_open": open_row["cumulative_volume"] if open_row else None,
                "volume_utc_day": vol_day,
                "vol_method": vol_method,
                "volume_24h_close": p["volume_24h"],
                "dual_sided_oi": p["dual_sided_oi"],
                "tvl": p["tvl"],
                "markets": p["markets"],
                "perp": sum(1 for r in listings.values() if r["type"] == "perp"),
                "swap": sum(1 for r in listings.values() if r["type"] == "swap"),
            },
            # distinct listings with a two-sided $1M quote in at least one of the day's snapshots
            "quoted_1m": {t: sum(1 for v in q1m_any.values() if v == t) for t in ("perp", "swap")},
            # per pair: each leg's $100K bid-ask, median over the snapshots where both legs are quoted
            "pairs": {k: {"perp_ba100k_med": round(median(a), 6), "swap_ba100k_med": round(median(b), 6), "n": len(a)}
                      for k, (a, b) in pair_legs.items() if a},
            "listings": listings,
        }
        write_json(day_path(d), day)
        done.append(d.isoformat())
        log("  %s %s  boundary %s (%+.0f min)%s  samples %d  listings %d  volume %s"
            % ("re-sealed (schema %d)" % SCHEMA if resealed else "sealed", d, boundary.key, offset,
               "  approx" if approx else "", len(samples), len(listings),
               vol_method or "from daily series"))
    return done
