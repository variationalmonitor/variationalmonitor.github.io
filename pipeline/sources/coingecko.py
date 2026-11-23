"""CoinGecko public API: Variational Omni daily volume. Stdlib only, keyless, cached.

Two calls per refresh (at most), never more often than `max_age_hours`:
  GET /exchanges/variational-omni/volume_chart?days=365   daily exchange volume in BTC
  GET /coins/bitcoin/market_chart?vs_currency=usd&days=365&interval=daily   BTC price at 00:00 UTC

Reading. The point stamped 00:00 UTC of day D is the trailing 24 hours to that instant, which is
UTC day D-1; it is converted with the BTC price of the same stamp. Checked on 22 Nov 2026: the
point stamped 22 Nov gives $1,241.3M against $1,243.5M reported by the Omni API for the 24 hours
to 21 Nov 23:47 UTC.

The cache is append-only for history: a stamp older than three days keeps the value first seen,
and a later disagreement of more than 0.5% is recorded under `revisions` rather than applied.
This module does not decide which days are usable; that is the acceptance test in
`market/history.py`.
"""
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

API = "https://api.coingecko.com/api/v3"
EXCHANGE = "variational-omni"
UA = "Mozilla/5.0 (compatible; stats-builder/1.0)"
FREEZE_AFTER_DAYS = 3


def _get(url, tries=2, wait=20):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError) as e:
            last = e
            if i + 1 < tries:
                time.sleep(wait)
    raise RuntimeError("coingecko: %s" % last)


def _path(store):
    return os.path.join(store, "coingecko", "variational_omni.json")


def _midnight_points(pairs):
    out = {}
    for ms, v in pairs:
        t = datetime.utcfromtimestamp(ms / 1000.0)
        if (t.hour, t.minute, t.second) == (0, 0, 0):
            out[t.strftime("%Y-%m-%d")] = v
    return out


def _merge(old, new, cutoff, revisions, label):
    for stamp, v in new.items():
        if stamp in old and stamp < cutoff:
            a, b = float(old[stamp]), float(v)
            if a and abs(b / a - 1) > 0.005:
                rec = {"series": label, "stamp": stamp, "kept": old[stamp], "seen": v}
                if rec not in revisions:
                    revisions.append(rec)
            continue
        old[stamp] = v


def load(store, max_age_hours=6, refresh=False, offline=False, log=print):
    """Return the cache, refreshing it when stale. Falls back to the cache if the fetch fails."""
    path = _path(store)
    cache = {"fetched_at": None, "volume_btc": {}, "btc_usd": {}, "revisions": []}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            cache = json.load(fh)
    now = datetime.utcnow().replace(microsecond=0)
    fresh = cache["fetched_at"] and \
        now - datetime.strptime(cache["fetched_at"], "%Y-%m-%dT%H:%M:%SZ") < timedelta(hours=max_age_hours)
    if offline or (fresh and not refresh):
        return cache
    try:
        vol = _get("%s/exchanges/%s/volume_chart?days=365" % (API, EXCHANGE))
        time.sleep(3)
        btc = _get("%s/coins/bitcoin/market_chart?vs_currency=usd&days=365&interval=daily" % API)
    except RuntimeError as e:
        log("  coingecko fetch failed (%s); using cache from %s" % (e, cache["fetched_at"]))
        return cache
    cutoff = (now - timedelta(days=FREEZE_AFTER_DAYS)).strftime("%Y-%m-%d")
    _merge(cache["volume_btc"], _midnight_points(vol), cutoff, cache["revisions"], "volume_btc")
    _merge(cache["btc_usd"], _midnight_points(btc["prices"]), cutoff, cache["revisions"], "btc_usd")
    cache["fetched_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, path)
    return cache


def daily_usd(cache):
    """{UTC day 'YYYY-MM-DD': USD volume}: the stamp of day D+1 times the BTC price of the same stamp."""
    out = {}
    for stamp, btc in cache["volume_btc"].items():
        px = cache["btc_usd"].get(stamp)
        if px is None:
            continue
        day = (datetime.strptime(stamp, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        out[day] = float(btc) * float(px)
    return out


def as_of(cache):
    """End of the newest UTC day covered, as an ISO time."""
    stamps = [s for s in cache["volume_btc"] if s in cache["btc_usd"]]
    return max(stamps) + "T00:00:00Z" if stamps else None
