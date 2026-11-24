"""markets_scope.json: every Markets-page series precomputed for the app's two filter rows.

Asset (all, crypto, tradfi) x instrument (all, perp, swap). Every swap is TradFi, so there is no
`crypto.swap`; `crypto.all` equals `crypto.perp` and `all.swap` equals `tradfi.swap`, and both of
each pair are written so the page needs no special case.

The file is assembled from the files already built (volume_daily, oi_daily, tradfi, spread_majors)
and from the sealed snapshot days for whatever only our own snapshots can give: class bands inside an instrument, long and short below
the whole platform, listings, concentration, funding and per-listing slippage.

Rules
  * A scope's total is written wherever it is known; its bands only where the split is known too
    (a day with a total but no split has none at all).
  * `all.perp` before swaps existed is the platform total: nothing else was listed.
  * long and short below `all.all` come from our own snapshot days; `all.perp`
    also takes the platform's long and short on dates where the swap side is zero.
  * `listed`: listings in scope on each snapshot day. For TradFi scopes before the first snapshot
    it is the number of listings whose earliest evidence (see listings.py) is on or before the
    date, a lower bound that never falls.
  * funding and slippage tickers: the five largest listings in scope by volume summed over our
    snapshot days, BTC, ETH, SOL and HYPE first where the scope contains them.
  * A swap's rates are the API's own annualised figures with the weekend multiple divided out, as
    in its market file: `series` shows the rate longs pay in a mixed scope; swap scopes carry
    `long` and `short`.
  * slippage is half the full quoted bid-ask at $100K (one way from mid), daily median.
"""
from .common import TRADFI_CLASSES, TRADFI_GROUPS, allocate, iso, r2, r4, r6, usd
from .listings import listing_dates

SCOPES = ["all.all", "all.perp", "all.swap", "crypto.all", "crypto.perp", "tradfi.all", "tradfi.perp", "tradfi.swap"]
MAJORS = ["BTC-PERP", "ETH-PERP", "SOL-PERP", "HYPE-PERP"]
MEMBERS = {
    "all.all": ("crypto_majors", "other_crypto", "tradfi_perp", "tradfi_swap"),
    "all.perp": ("crypto_majors", "other_crypto", "tradfi_perp"),
    "all.swap": ("tradfi_swap",),
    "crypto.all": ("crypto_majors", "other_crypto"),
    "crypto.perp": ("crypto_majors", "other_crypto"),
    "tradfi.all": ("tradfi_perp", "tradfi_swap"),
    "tradfi.perp": ("tradfi_perp",),
    "tradfi.swap": ("tradfi_swap",),
}
GROUP_BANDS = {
    "all.all": ["crypto_majors", "other_crypto", "tradfi_perp", "swap"],
    "all.perp": ["crypto_majors", "other_crypto", "tradfi_perp"],
    "crypto.all": ["crypto_majors", "other_crypto"],
    "crypto.perp": ["crypto_majors", "other_crypto"],
    "tradfi.all": ["tradfi_perp", "swap"],
}
NOTES = [
    "crypto.all and crypto.perp are identical: every crypto listing is a perpetual.",
    "all.swap and tradfi.swap are identical: every swap is a TradFi listing.",
    "Bands are null where the scope total is known but its split is not.",
    "src: own = this site's sources as tagged per date in volume_daily.json and oi_daily.json.",
    "A swap's funding figures are the API's annualised rates (interval x 360) with the weekend multiple divided out.",
]


def _c(x):
    return None if x is None else int(round(x * 100))


def _four(doc, tf, key_inst, i, j):
    """(majors, other, tradfi perp, swap) in cents for one date, or None when the four-way cut is unknown."""
    g = doc["group"]
    if g["crypto_majors"][i] is not None:
        return tuple(_c(g[k][i]) for k in ("crypto_majors", "other_crypto", "tradfi_perp", "tradfi_swap"))
    top = doc["top"]
    if top["crypto_majors"][i] is not None and j is not None and tf[key_inst]["perp"][j] is not None:
        return (_c(top["crypto_majors"][i]), _c(top["other_crypto"][i]), _c(tf[key_inst]["perp"][j]), _c(tf[key_inst]["swap"][j]))
    return None


def _scope_money(scope, total, four, pre_swaps):
    """(scope total in cents, group bands or None) from the platform total and the four-way cut."""
    if scope == "all.all":
        return total, (dict(zip(GROUP_BANDS[scope], four)) if four else None)
    if four is None:
        return (total if scope == "all.perp" and pre_swaps else None), None
    maj, oth, tp, sw = four
    if scope == "all.perp":
        return maj + oth + tp, {"crypto_majors": maj, "other_crypto": oth, "tradfi_perp": tp}
    if scope in ("crypto.all", "crypto.perp"):
        return maj + oth, {"crypto_majors": maj, "other_crypto": oth}
    if scope == "tradfi.all":
        return tp + sw, {"tradfi_perp": tp, "swap": sw}
    return (tp if scope == "tradfi.perp" else sw), None


def _intraday(intra, members, tickers, swap_scope):
    """A scope's intraday block: summed long and short per snapshot, and the scope's tickers' series."""
    ts = (intra or {}).get("ts") or []
    lg = [sum(g[m][0] for m in members) for g in (intra or {}).get("groups") or []]
    sh = [sum(g[m][1] for m in members) for g in (intra or {}).get("groups") or []]

    def series(field):
        out = {}
        for tk in tickers:
            m = ((intra or {}).get("markets") or {}).get(tk) or {"ts": []}
            at = {t: i for i, t in enumerate(m["ts"])}
            out[tk] = [m[field][at[t]] if t in at else None for t in ts]
        return out

    funding = ({"tickers": tickers, "long": series("fin_long_ann"), "short": series("fin_short_ann")} if swap_scope else
               {"tickers": tickers, "series": {tk: (series("fin_long_ann") if tk.endswith("-SWAP") else series("funding_ann"))[tk]
                                               for tk in tickers}})
    return {"ts": ts, "oi": [usd(a + b) for a, b in zip(lg, sh)], "long": [usd(a) for a in lg], "short": [usd(b) for b in sh],
            "ls_ratio": [r4(a / float(b)) if b else None for a, b in zip(lg, sh)],
            "funding": funding, "slippage": {"tickers": tickers, "series": series("slip100k_bps")}}


def build(vj, oj, tj, sj, days, classifier, seen, swaps_launch, intra=None):
    _vsrc = dict(zip(vj["dates"], vj["src"]))
    _avg_src = vj["sources"].index("report_avg") if "report_avg" in vj["sources"] else None
    listings = classifier.listings
    sources = ["own"]
    own_days = sorted(days)      # our snapshot days
    snap_days = sorted(days)
    vi = {d: i for i, d in enumerate(vj["dates"])}
    oi = {d: i for i, d in enumerate(oj["dates"])}
    ti = {d: i for i, d in enumerate(tj["dates"])}
    first_snap = snap_days[0] if snap_days else "9999-12-31"
    every, ev = listing_dates(classifier, days, seen)[:2]

    # per own day: each listing's volume as allocated in volume_daily (same totals, same weights)
    alloc = {}
    for d in own_days:
        i = vi.get(d)
        if i is None or vj["group"]["crypto_majors"][i] is None:
            continue
        weights = {tk: r["vol_c"] for tk, r in days[d]["rows"].items() if r["vol_c"] > 0}
        alloc[d] = allocate(_c(vj["total"][i]), weights)

    out = {}
    for scope in SCOPES:
        members = MEMBERS[scope]
        class_scope = scope not in GROUP_BANDS
        in_scope = lambda r: r["sgroup"] in members
        classes = [c for c in TRADFI_CLASSES
                   if any(e["group"] in members and e["cls"] == c for e in listings.values())] if class_scope else []
        band_keys = classes if class_scope else GROUP_BANDS[scope]

        # tickers: largest by volume over our snapshot days, the majors first where present
        vol = {}
        for d in snap_days:
            for tk, r in days[d]["rows"].items():
                if in_scope(r):
                    vol[tk] = vol.get(tk, 0) + r["vol_c"]
        ranked = sorted(vol, key=lambda tk: (-vol[tk], tk))
        lead = [tk for tk in MAJORS if tk in vol]
        tickers = (lead + [tk for tk in ranked if tk not in lead])[:5]
        swap_scope = members == ("tradfi_swap",)

        rows = {}
        for d in sorted(set(vj["dates"]) | set(oj["dates"]) | set(snap_days)):
            rec = {}
            pre_swaps = bool(swaps_launch) and d < swaps_launch
            # volume
            i = vi.get(d)
            if i is not None:
                tot, bands = _scope_money(scope, _c(vj["total"][i]), _four(vj, tj, "volume_inst", i, ti.get(d)), pre_swaps)
                if tot is not None and class_scope and d in alloc:
                    bands = {c: 0 for c in classes}
                    for tk, v in alloc[d].items():
                        r = days[d]["rows"][tk]
                        if in_scope(r):
                            bands[r["cls"]] += v
                    assert sum(bands.values()) == tot, (scope, d, sum(bands.values()), tot)
                rec["vol"], rec["vol_bands"] = tot, bands
            # open interest
            j = oi.get(d)
            if j is not None and oj["total"][j] is not None:
                tot, bands = _scope_money(scope, _c(oj["total"][j]), _four(oj, tj, "oi_inst", j, ti.get(d)), pre_swaps)
                lg = sh = None
                if scope == "all.all":
                    lg, sh = _c(oj["long"][j]), _c(oj["short"][j])
                elif d in own_days and tot is not None:
                    sel = [r for r in days[d]["rows"].values() if in_scope(r)]
                    lg, sh = sum(r["long_c"] for r in sel), sum(r["short_c"] for r in sel)
                    assert lg + sh == tot, (scope, d, lg + sh, tot)
                    if class_scope:
                        bands = {c: 0 for c in classes}
                        for r in sel:
                            bands[r["cls"]] += r["long_c"] + r["short_c"]
                elif scope == "all.perp" and tot is not None and tot == _c(oj["total"][j]) and oj["long"][j] is not None:
                    lg, sh = _c(oj["long"][j]), _c(oj["short"][j])        # no swap side: the platform's long and short
                rec["oi"], rec["oi_bands"], rec["long"], rec["short"] = tot, bands, lg, sh
            # listings in scope
            if d in days:
                rec["listed"] = sum(1 for r in days[d]["rows"].values() if in_scope(r))
            elif members[0] in TRADFI_GROUPS and d < first_snap:
                n = sum(1 for tk, e in ev.items() if every[tk]["group"] in members
                        and e["evidence"] != "snapshot" and e["date"] <= d)
                rec["listed"] = n if n else None
            if rec.get("vol") is None and rec.get("oi") is None and rec.get("listed") is None:
                continue
            # only our own snapshots give these
            if d in days:
                sel = sorted((r["vol_c"] for r in days[d]["rows"].values() if in_scope(r)), reverse=True)
                rec["top10"] = r4(sum(sel[:10]) / float(sum(sel))) if sum(sel) > 0 else None
            rows[d] = rec

        dates = sorted(rows)
        if swap_scope:       # before the first swap traded the scope has nothing to show: start where it starts
            while dates and not rows[dates[0]].get("vol") and not rows[dates[0]].get("oi") and rows[dates[0]].get("listed") is None:
                dates.pop(0)
        col = lambda k: [rows[d].get(k) for d in dates]
        bands_of = lambda k: {b: [usd((rows[d].get(k) or {}).get(b)) if rows[d].get(k) else None for d in dates] for b in band_keys}

        def per_ticker(fn):
            return {tk: [fn(days[d]["rows"].get(tk)) if d in days and days[d]["rows"].get(tk) else None for d in dates]
                    for tk in tickers}

        if swap_scope:
            funding = {"tickers": tickers, "long": per_ticker(lambda r: r6(r.get("fin_long_ann"))),
                       "short": per_ticker(lambda r: r6(r.get("fin_short_ann")))}
        else:
            funding = {"tickers": tickers, "series": per_ticker(
                lambda r: r6(r.get("fin_long_ann") if r["type"] == "swap" else r.get("funding_ann")))}
        slip = per_ticker(lambda r: r2(r["ba"]["100k"][0] / 2.0) if "100k" in r.get("ba", {}) else None)
        out[scope] = {
            "dates": dates,
            "src": [0] * len(dates),             # every date is from this site's own sources
            "approx": [bool(d in own_days and days[d]["approx"]) for d in dates],
            # 1 where the day's volume is a reported two-week total averaged per day (volume_daily's `report_avg`),
            # so the page can draw those bars lighter, as it does on the all-products chart
            "avg": [1 if (x is not None and _avg_src is not None and _vsrc.get(d) == _avg_src) else 0 for d, x in zip(dates, col("vol"))],
            "volume": {"total": [usd(x) for x in col("vol")], "bands": bands_of("vol_bands")},
            "oi": {"total": [usd(x) for x in col("oi")], "bands": bands_of("oi_bands")},
            "long": [usd(x) for x in col("long")], "short": [usd(x) for x in col("short")],
            # long over short; null where either is unknown or short is zero
            "ls_ratio": [r4(a / float(b)) if a is not None and b else None for a, b in zip(col("long"), col("short"))],
            "listed": col("listed"),
            "top10_share": col("top10"),
            "funding": funding,
            "slippage": {"tickers": tickers, "series": slip},
            "intraday": _intraday(intra, members, tickers, swap_scope),
        }
    newest = max((days[d]["boundary"] for d in days), default=None)
    notes = NOTES
    return {"as_of": iso(newest) if newest else vj["as_of"], "sources": sources, "notes": notes, "scopes": out}
