"""DefiLlama open-interest endpoints. Stdlib only, keyless, cached.

  GET /summary/open-interest/variational   Variational daily open interest from 4 Jan 2026
  GET /overview/open-interest              every venue DefiLlama tracks, with a daily breakdown

Reading. DefiLlama's Variational figure is one-sided (traders only): on 21 Nov 2026 it reads
$620.6M against $621.5M from the Omni API (half of the dual-sided field) at 23:47 UTC. The value
stamped day D is the last reading of UTC day D. The stamp of the day the fetch is made is still
moving (the API's own `totalLastFullDay` skips it), so it is dropped here.

Daily volume. DefiLlama's volume endpoints are paywalled, so the volume history is a CSV export of
the Perp Volume chart on DefiLlama's Variational page, made by the owner and kept in the store as a
source cache: <store>/defillama/perp_volume_export.csv (columns Timestamp, Date, Perp Volume; whole
USD per UTC day) with its provenance in perp_volume_export.json (who exported it, when, from where,
SHA-256 of the file). The row dated on or after the export date is the day still in progress and is
dropped. Reading, checked 23 Nov 2026 against the Omni API's trailing 24 hours: the value stamped day
D is DefiLlama's last reading during UTC day D (21 Nov 2026: $1,241.9M; the API read $1,243.5M for
the 24 hours to 21 Nov 23:47 UTC). Replace the export with `store_volume_export` (command line:
`python3 -I pipeline/sources/defillama.py import-volume <csv> --exported YYYY-MM-DD`).

Peer venues. The overview's daily breakdown already leaves out front ends that re-report another
venue's positions (category `Interface`, e.g. trade[XYZ] on Hyperliquid). From what remains this
module keeps DefiLlama category `Derivatives` only, which removes `Prediction Market`, `Options`,
`Interest Rate Derivatives`, `Synthetics` and `Trading App`. Entries that share a DefiLlama parent
(Lighter Perps and Lighter Robinhood Perps; GMX EVM and GMX Solana) are one venue: their
chain-separate figures are added and the venue takes the parent's name.
"""
import csv
import hashlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

API = "https://api.llama.fi"
SLUG = "variational"
UA = "Mozilla/5.0 (compatible; stats-builder/1.0)"
KEEP_CATEGORY = "Derivatives"
BREAKDOWN_FROM = "2025-12-01"
VOLUME_EXPORT = "perp_volume_export.csv"
VOLUME_NOTE = "perp_volume_export.json"
VOLUME_PAGE = "https://defillama.com/protocol/variational"
VOLUME_HEADER = ["Timestamp", "Date", "Perp Volume"]


def _get(url, tries=2, wait=10):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError) as e:
            last = e
            if i + 1 < tries:
                time.sleep(wait)
    raise RuntimeError("defillama: %s" % last)


def _path(store):
    return os.path.join(store, "defillama", "open_interest.json")


def _day(ts):
    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")


def _parse(summary, overview, today):
    variational = {}
    for ts, v in summary.get("totalDataChart", []):
        if ts % 86400 == 0 and _day(ts) < today and v is not None:
            variational[_day(ts)] = v
    venues, dropped = {}, {}
    for p in overview.get("protocols", []):
        name, cat = p.get("name"), p.get("category")
        if cat != KEEP_CATEGORY:
            dropped[name] = cat
            continue
        linked = p.get("linkedProtocols") or []
        venues[name] = {"venue": linked[0] if p.get("parentProtocol") and linked else name,
                        "parent": p.get("parentProtocol")}
    series = {}
    for ts, row in overview.get("totalDataChartBreakdown", []):
        d = _day(ts)
        if ts % 86400 or d >= today or d < BREAKDOWN_FROM:
            continue
        for name, v in row.items():
            if name in venues and v is not None:
                series.setdefault(name, {})[d] = v
    return {"variational": variational, "venues": venues, "series": series,
            "dropped_categories": sorted(set(c for c in dropped.values() if c))}


def load(store, max_age_hours=6, refresh=False, offline=False, log=print):
    path = _path(store)
    cache = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            cache = json.load(fh)
    now = datetime.utcnow().replace(microsecond=0)
    if cache:
        age = now - datetime.strptime(cache["fetched_at"], "%Y-%m-%dT%H:%M:%SZ")
        # a cache from an earlier UTC day would lack yesterday's close: refresh across midnight
        same_day = cache["fetched_at"][:10] == now.strftime("%Y-%m-%d")
        if offline or (age < timedelta(hours=max_age_hours) and same_day and not refresh):
            return cache
    elif offline:
        raise RuntimeError("defillama: no cache and offline")
    try:
        summary = _get("%s/summary/open-interest/%s" % (API, SLUG))
        overview = _get("%s/overview/open-interest" % API)
    except RuntimeError as e:
        if cache:
            log("  defillama fetch failed (%s); using cache from %s" % (e, cache["fetched_at"]))
            return cache
        raise
    cache = _parse(summary, overview, now.strftime("%Y-%m-%d"))
    cache["fetched_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, path)
    return cache


def peer_venues(cache, exclude=("Variational",), top=6, window=30):
    """Largest `top` other venues by average open interest over the last `window` complete days.

    Returns (dates, [{"name", "members", "avg", "values": {date: value}}]) with venues ordered by
    that average. A day on which a venue has no figure counts as zero in the average.
    """
    venues = {}
    for name, days in cache["series"].items():
        if name in exclude:
            continue
        v = venues.setdefault(cache["venues"][name]["venue"], {"members": [], "values": {}})
        v["members"].append(name)
        for d, x in days.items():
            v["values"][d] = v["values"].get(d, 0) + x
    all_days = sorted({d for v in venues.values() for d in v["values"]})
    last = all_days[-window:]
    ranked = []
    for name, v in venues.items():
        v["name"] = name
        v["members"].sort()
        v["avg"] = sum(v["values"].get(d, 0) for d in last) / float(len(last))
        ranked.append(v)
    ranked.sort(key=lambda v: (-v["avg"], v["name"]))
    return last, ranked[:top], ranked[top:top + 4]


def as_of(cache):
    days = sorted(cache["variational"])
    if not days:
        return None
    end = datetime.strptime(days[-1], "%Y-%m-%d") + timedelta(days=1)
    return end.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- daily volume: the owner's CSV export ----------

def _parse_volume_csv(raw):
    """[(date, usd int)] from the export's bytes, in date order. Raises ValueError on anything unexpected."""
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    if not rows or [c.strip() for c in rows[0]] != VOLUME_HEADER:
        raise ValueError("volume export: header is %r, expected %r" % (rows[0] if rows else None, VOLUME_HEADER))
    out = []
    for r in rows[1:]:
        if not r:
            continue
        if len(r) != 3:
            raise ValueError("volume export: row %r" % (r,))
        ts, ds, v = r[0].strip(), r[1].strip(), r[2].strip()
        if _day(int(ts) // 1000) != ds or int(ts) % 86400000:
            raise ValueError("volume export: timestamp %s is not 00:00 UTC of %s" % (ts, ds))
        if not v.isdigit():
            raise ValueError("volume export: %s has volume %r" % (ds, v))
        out.append((ds, int(v)))
    dates = [d for d, _ in out]
    if dates != sorted(set(dates)):
        raise ValueError("volume export: dates are not unique and ascending")
    return out


def store_volume_export(store, csv_path, exported_on, exported_by="owner", page=VOLUME_PAGE):
    """Copy a CSV export of DefiLlama's Perp Volume chart into the store with its provenance. Returns the note."""
    with open(csv_path, "rb") as fh:
        raw = fh.read()
    rows = _parse_volume_csv(raw)
    date.fromisoformat(exported_on)
    folder = os.path.join(store, "defillama")
    os.makedirs(folder, exist_ok=True)
    note = {
        "source": "DefiLlama, Variational protocol page, Perp Volume chart, CSV download",
        "page": page,
        "exported_by": exported_by,
        "exported_on": exported_on,
        "file": VOLUME_EXPORT,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "rows": len(rows),
        "first": rows[0][0],
        "last": rows[-1][0],
        "unit": "USD per UTC day, whole dollars",
        "use": "rows dated before exported_on; the row of the export date is the day in progress and is dropped",
    }
    for name, data in ((VOLUME_EXPORT, raw), (VOLUME_NOTE, (json.dumps(note, indent=1) + "\n").encode("utf-8"))):
        tmp = os.path.join(folder, name + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, os.path.join(folder, name))
    return note


def load_volume_export(store):
    """({date: usd int} for the complete UTC days of the stored export, note), or ({}, None) when the store has none.

    The file must be the one the note describes (same SHA-256); rows dated on or after the export date are dropped.
    """
    folder = os.path.join(store, "defillama")
    csv_path, note_path = os.path.join(folder, VOLUME_EXPORT), os.path.join(folder, VOLUME_NOTE)
    if not os.path.exists(csv_path) and not os.path.exists(note_path):
        return {}, None
    with open(note_path, encoding="utf-8") as fh:
        note = json.load(fh)
    with open(csv_path, "rb") as fh:
        raw = fh.read()
    if hashlib.sha256(raw).hexdigest() != note["sha256"]:
        raise ValueError("volume export: %s is not the file %s describes (SHA-256 differs)" % (VOLUME_EXPORT, VOLUME_NOTE))
    return {d: v for d, v in _parse_volume_csv(raw) if d < note["exported_on"]}, note


def volume_as_of(daily):
    """End of the export's last complete UTC day."""
    if not daily:
        return None
    end = datetime.strptime(max(daily), "%Y-%m-%d") + timedelta(days=1)
    return end.strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    # python3 -I pipeline/sources/defillama.py import-volume <csv> --exported YYYY-MM-DD [--by owner]
    args = sys.argv[1:]
    if len(args) < 4 or args[0] != "import-volume" or "--exported" not in args:
        sys.exit("usage: defillama.py import-volume <csv> --exported YYYY-MM-DD [--by NAME]")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    store_dir = os.environ.get("VM_STORE") or os.path.join(root, ".cache", "store")
    by = args[args.index("--by") + 1] if "--by" in args else "owner"
    n = store_volume_export(store_dir, args[1], args[args.index("--exported") + 1], exported_by=by)
    print("stored %s: %d rows %s to %s, exported %s by %s, sha256 %s"
          % (os.path.join(store_dir, "defillama", VOLUME_EXPORT), n["rows"], n["first"], n["last"], n["exported_on"],
             n["exported_by"], n["sha256"][:16]))
