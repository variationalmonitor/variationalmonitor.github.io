"""Intraday series: one point per saved snapshot over the trailing seven days.

Read straight from the raw snapshot files (the capture folders, or `VM_SNAPSHOTS` in CI); nothing
is added to the store. The window is rolling: every snapshot later than seven days before the
newest one, at most 336 points (48 a day), a repeated API response excluded. Each point is derived
exactly as the daily fields are (`store.listing_row`): same two-sided quote rule, same swap
financing normalisation, same rounding.

How the daily series relate to it: a finished UTC day's volume, open interest, long, short and
rates are the day's boundary snapshot, which is the snapshot nearest the day's end (normally the
one stamped 00:00 of the next day), so they equal the intraday point at that time. The daily
bid-ask and slippage are medians over the day's snapshots, not a single point.
"""
from datetime import timedelta

from . import store
from .classify import sum_group
from .common import GROUPS, SIZES, cents, r2, r4, r6, usd

WINDOW = timedelta(days=7)
MAX_POINTS = 336
MARKET_KEYS = ["ts", "vol24h", "oi", "oi_long", "oi_short", "ls_ratio", "funding_ann", "fin_long_ann",
               "fin_short_ann", "slip1k_bps", "slip100k_bps", "slip1m_bps"]


def stamp(t):
    """UTC to the minute, as written in the files: 2026-11-22T07:30Z."""
    return t.strftime("%Y-%m-%dT%H:%MZ")


def empty_market():
    return {k: [] for k in MARKET_KEYS}


def collect(refs, platform_rows, classifier):
    """{"ts": [...], "markets": {ticker: series}, "groups": [{group: (long, short) in cents}]}."""
    live = [r for r in refs if not platform_rows.get(r.key, {}).get("dup")]
    out = {"ts": [], "markets": {}, "groups": []}
    if not live:
        return out
    window = [r for r in live if r.t > live[-1].t - WINDOW][-MAX_POINTS:]
    fin_refs = {}
    for ref in window:
        day = (ref.t - timedelta(hours=12)).date()      # the day this snapshot would close, as in sealing
        if day not in fin_refs:
            fin_refs[day] = store.fin_ref_for(ref.t)
        ts = stamp(ref.t)
        if out["ts"] and out["ts"][-1] == ts:       # two captures in one minute: keep the first
            continue
        snap = ref.load()
        groups = {g: [0, 0] for g in GROUPS}
        for l in snap["listings"]:
            r = store.listing_row(l, ref.t, classifier, classifier.fin_cfg, fin_refs[day])
            lg, sh = cents(r["oi_long"]) or 0, cents(r["oi_short"]) or 0
            g = groups[sum_group(r["group"])]
            g[0] += lg
            g[1] += sh
            m = out["markets"].setdefault(l["ticker"], empty_market())
            m["ts"].append(ts)
            m["vol24h"].append(usd(cents(r["vol24h"])))
            m["oi"].append(usd(lg + sh))
            m["oi_long"].append(usd(lg))
            m["oi_short"].append(usd(sh))
            m["ls_ratio"].append(r4(lg / float(sh)) if sh else None)
            m["funding_ann"].append(r6(r["funding_ann"]))
            m["fin_long_ann"].append(r6(r["fin_long_ann"]))
            m["fin_short_ann"].append(r6(r["fin_short_ann"]))
            for s in SIZES:
                v = r["ba_last"][s]
                m["slip%s_bps" % s].append(None if v is None else r2(v / 2.0))
        ref.drop()
        out["ts"].append(ts)
        out["groups"].append({g: tuple(v) for g, v in groups.items()})
    return out
