"""Builders for the files in `site/data/` owned by the market-data package (see 02-contract.md).

Every derived figure is computed here, in integer cents where it is money, and rounded once on
the way out. Extra key beyond the contract: `approx` (one flag per date) on every daily file built
from snapshots, true where the day's boundary snapshot was not taken at 00:00 UTC.
"""
import os
import re
from datetime import timedelta

from . import history, store
from .listings import listed_date
from .common import (OUT, CONFIG, GROUPS, CLASSES, TRADFI_CLASSES, RANGES, to_date, SIZES, PUBLICATION_DATE, CONTINUOUS_START,
                     STALE_AFTER_MINUTES, cents, iso, median, read_json, r2, r4, r6, usd, utcnow)

# The API annualises swap financing as the payment's rate times 360 and perpetual funding on 365
# days. For the carry comparison only, the swap leg is restated to actual/365.
SWAP_TO_365 = 365.0 / 360.0
SAFE_TICKER = re.compile(r"^[A-Za-z0-9_.-]+$")


def _usd_list(xs):
    return [usd(x) for x in xs]


def _top(group):
    """Three-way cut from the four groups, in cents: crypto majors, other crypto, TradFi (perpetuals plus swaps)."""
    tradfi = [None if a is None else a + b for a, b in zip(group["tradfi_perp"], group["tradfi_swap"])]
    return {"crypto_majors": _usd_list(group["crypto_majors"]), "other_crypto": _usd_list(group["other_crypto"]),
            "tradfi": _usd_list(tradfi)}


def turnover(vol_c, long_c, short_c):
    """24h volume over one-sided open interest (long + short); None when open interest is zero."""
    oi = (long_c or 0) + (short_c or 0)
    return r4((vol_c or 0) / float(oi)) if oi > 0 else None


def long_share(long_c, short_c):
    """Long over long plus short; None when both are zero."""
    oi = (long_c or 0) + (short_c or 0)
    return r4((long_c or 0) / float(oi)) if oi > 0 else None


def ls_ratio(long_c, short_c):
    """Long over short; None when either is missing or short is zero."""
    if long_c is None or short_c is None or short_c == 0:
        return None
    return r4(long_c / float(short_c))


def slip(ba):
    """One-way slippage from mid: half the full quoted bid-ask, same nulls, two decimals."""
    return None if ba is None else r2(ba / 2.0)


def tradfi_json(volume, oi, days, listed):
    """tradfi.json: TradFi (perpetuals plus swaps) share of volume and OI, class cuts, listings."""
    vi = {d: i for i, d in enumerate(volume["dates"]) if volume["tradfi_cls"][i] is not None}
    oi_i = {d: i for i, d in enumerate(oi["dates"]) if oi["tradfi_cls"][i] is not None}
    dates = sorted(set(vi) | set(oi_i))
    out = {"dates": dates, "share_volume": [], "share_oi": [],
           "volume_cls": {c: [] for c in TRADFI_CLASSES}, "oi_cls": {c: [] for c in TRADFI_CLASSES},
           "volume_inst": {"perp": [], "swap": []}, "oi_inst": {"perp": [], "swap": []},
           "approx": []}
    newest = None
    for d in dates:
        for src, idx, share, cls, inst in ((volume, vi, "share_volume", "volume_cls", "volume_inst"),
                                           (oi, oi_i, "share_oi", "oi_cls", "oi_inst")):
            i = idx.get(d)
            tf = src["tradfi_cls"][i] if i is not None else None
            total = src["total"][i] if i is not None else None
            out[share].append(r4(sum(tf.values()) / float(total)) if tf is not None and total else None)
            for c in TRADFI_CLASSES:
                out[cls][c].append(usd(tf[c]) if tf is not None else None)
            # the same TradFi total by instrument: perpetuals and swaps
            out[inst]["perp"].append(usd(src["group"]["tradfi_perp"][i]) if tf is not None else None)
            out[inst]["swap"].append(usd(src["group"]["tradfi_swap"][i]) if tf is not None else None)
        out["approx"].append(bool(d in days and days[d]["approx"]))
        if d in days:
            newest = days[d]["boundary"]
    return dict({"as_of": iso(newest) if newest else None}, **out, listed=listed)


def spread_majors_json(days):
    """spread_majors.json from our own snapshots: daily median full bid-ask at $100K for the four majors."""
    majors = ["BTC", "ETH", "SOL", "HYPE"]
    out = {"sources": ["omni"], "dates": [], "src": [], "ba100k_bps": {m: [] for m in majors}, "approx": []}
    newest = None
    for ds in sorted(days):
        d = days[ds]
        out["dates"].append(ds)
        out["src"].append(0)
        for m in majors:
            row = d["rows"].get(m + "-PERP") or {}
            out["ba100k_bps"][m].append(r2(row["ba"]["100k"][0]) if "100k" in row.get("ba", {}) else None)
        out["approx"].append(d["approx"])
        newest = d["boundary"]
    return dict({"as_of": iso(newest) if newest else None}, **out)


def add_trailing_shares(tj, vj, window=7):
    """Seven-day TradFi shares for tradfi.json, which smooth the weekend gap left by closed swaps.

    share_volume_7d: TradFi volume summed over the trailing seven calendar days over total volume
    summed over the same days. share_oi_7d: the mean of the daily share_oi over the same days.
    Each is null unless all seven days are present with a figure.
    """
    at = {d: i for i, d in enumerate(tj["dates"])}
    total = dict(zip(vj["dates"], vj["total"]))
    vol7, oi7 = [], []
    for d in tj["dates"]:
        days_ = [(to_date(d) - timedelta(days=k)).isoformat() for k in range(window)]
        idx = [at.get(x) for x in days_]
        tf_c = tot_c = 0
        ok_v = ok_o = all(i is not None for i in idx)
        shares = []
        for x, i in zip(days_, idx):
            if i is None:
                break
            p_, s_ = tj["volume_inst"]["perp"][i], tj["volume_inst"]["swap"][i]
            if p_ is None or s_ is None or total.get(x) is None:
                ok_v = False
            else:
                tf_c += int(round(p_ * 100)) + int(round(s_ * 100))
                tot_c += int(round(total[x] * 100))
            if tj["share_oi"][i] is None:
                ok_o = False
            else:
                shares.append(tj["share_oi"][i])
        vol7.append(r4(tf_c / float(tot_c)) if ok_v and tot_c > 0 else None)
        oi7.append(r4(sum(shares) / window) if ok_o else None)
    tj["share_volume_7d"], tj["share_oi_7d"] = vol7, oi7


def concentration_json(days):
    """concentration.json: the ten largest listings' share of the day's volume."""
    out = {"dates": [], "top10_share": [], "approx": []}
    newest = None
    for ds in sorted(days):
        d = days[ds]
        vols = sorted((r["vol_c"] for r in d["rows"].values()), reverse=True)
        total = sum(vols)
        out["dates"].append(ds)
        out["top10_share"].append(r4(sum(vols[:10]) / float(total)) if total > 0 else None)
        out["approx"].append(d["approx"])
        newest = d["boundary"]
    return dict({"as_of": iso(newest) if newest else None}, **out)


def _split(group):
    """Two-way cut from the four groups, in cents: crypto (majors plus other crypto) and TradFi (perpetuals plus
    swaps). Null on both where the group split is not known."""
    crypto = [None if a is None else a + b for a, b in zip(group["crypto_majors"], group["other_crypto"])]
    tradfi = [None if a is None else a + b for a, b in zip(group["tradfi_perp"], group["tradfi_swap"])]
    return {"crypto": _usd_list(crypto), "tradfi": _usd_list(tradfi)}


def volume_json(v):
    return {"as_of": iso(v["as_of"]), "sources": list(history.VOLUME_SOURCES),
            "dates": v["dates"], "total": _usd_list(v["total"]), "src": v["src"],
            "cumulative": _usd_list(v["cumulative"]),
            "anchors": [{"t": iso(t), "value": usd(c), "src": k} for t, c, k in v["anchors"]],
            "group": {k: _usd_list(v["group"][k]) for k in GROUPS},
            "top": _top(v["group"]),
            "split": _split(v["group"]),
            "cls": {k: _usd_list(v["cls"][k]) for k in CLASSES},
            "approx": v["approx"]}


def oi_json(o):
    return {"as_of": iso(o["as_of"]), "sources": ["omni", "defillama", "report_half"],
            "dates": o["dates"], "total": _usd_list(o["total"]), "src": o["src"],
            "group": {k: _usd_list(o["group"][k]) for k in GROUPS},
            "top": _top(o["group"]),
            "cls": {k: _usd_list(o["cls"][k]) for k in CLASSES},
            "long": _usd_list(o["long"]), "short": _usd_list(o["short"]),
            "dual": _usd_list(o["dual"]), "approx": o["approx"]}


def listed_json(m):
    return {"as_of": iso(m["as_of"]), "sources": ["report", "omni"], "dates": m["dates"],
            "total": m["total"], "src": m["src"], "perp": m["perp"], "swap": m["swap"]}


def peers_json(dl, peers, oi, dl_as_of):
    first = min(dl["variational"])
    last = max(dl["variational"])
    idx = {d: i for i, d in enumerate(oi["dates"])}
    dates = [d for d in oi["dates"] if first <= d <= last]
    return {"as_of": dl_as_of, "dates": dates,
            "variational": [usd(oi["total"][idx[d]]) for d in dates],
            "venues": [{"name": v["name"],
                        "values": [usd(cents(v["values"][d])) if d in v["values"] else None for d in dates]}
                       for v in peers]}


def latest_json(ref, classifier, first_day=None):
    """markets_latest.json from the newest snapshot."""
    snap = ref.load()
    fin_ref = store.fin_ref_for(ref.t)
    rows, unresolved_fin = [], []
    for l in snap["listings"]:
        r = store.listing_row(l, ref.t, classifier, classifier.fin_cfg, fin_ref)
        if r["type"] == "swap" and r["fin_long_ann"] is None:
            unresolved_fin.append((l["ticker"], r.get("fin_basis")))
        ba = r["ba_last"]
        rows.append({
            "ticker": l["ticker"], "name": r["name"] or l["ticker"].rsplit("-", 1)[0],
            "type": r["type"], "group": r["group"], "cls": r["cls"],
            "vol24h": usd(cents(r["vol24h"])),
            "oi_long": usd(cents(r["oi_long"])), "oi_short": usd(cents(r["oi_short"])),
            "oi": usd((cents(r["oi_long"]) or 0) + (cents(r["oi_short"]) or 0)),
            "funding_ann": r6(r["funding_ann"]),
            "fin_long_ann": r6(r["fin_long_ann"]), "fin_short_ann": r6(r["fin_short_ann"]),
            "ba1k_bps": r2(ba["1k"]), "ba100k_bps": r2(ba["100k"]), "ba1m_bps": r2(ba["1m"]),
            "quote_at": r["quote_at"],
            "turnover": turnover(cents(r["vol24h"]), cents(r["oi_long"]), cents(r["oi_short"])),
            "long_share": long_share(cents(r["oi_long"]), cents(r["oi_short"])),
            "listed": listed_date(classifier.listings.get(l["ticker"]), first_day),
            "slip1k_bps": slip(ba["1k"]), "slip100k_bps": slip(ba["100k"]), "slip1m_bps": slip(ba["1m"]),
            "ls_ratio": ls_ratio(cents(r["oi_long"]), cents(r["oi_short"])),
        })
    rows.sort(key=lambda r: (-(r["vol24h"] or 0), r["ticker"]))
    platform = snap["platform"]
    ref.drop()
    return {"as_of": ref.key, "rows": rows}, platform, unresolved_fin


def _blank_market(tk):
    return {"ticker": tk, "name": None, "type": None, "group": None,
            "cls": None, "dates": [], "vol24h": [], "oi": [], "oi_long": [],
            "oi_short": [], "funding_ann": [], "fin_long_ann": [],
            "fin_short_ann": [], "ba1k_bps": [], "ba100k_bps": [],
            "ba1m_bps": [], "turnover": [], "long_share": [],
            "slip1k_bps": [], "slip100k_bps": [], "slip1m_bps": [], "ls_ratio": [],
            "approx": []}


def _describe(f, tk, r):
    f.update(name=r["name"] or tk.rsplit("-", 1)[0], type=r["type"], group=r["group"], cls=r["cls"])


def market_files(days, order_hint, intra=None):
    """({ticker: file}, index). One file per listing seen in any sealed day or in the intraday window.

    A listing seen in the intraday window only (its first UTC day, which has not sealed yet) gets a file with
    every daily array empty; its name, type, group and class come from the newest snapshot in the window that
    lists it, derived as the daily rows derive them."""
    files = {}
    for ds in sorted(days):
        d = days[ds]
        for tk, r in d["rows"].items():
            if not SAFE_TICKER.match(tk):
                continue
            f = files.setdefault(tk, _blank_market(tk))
            _describe(f, tk, r)
            f["dates"].append(ds)
            f["vol24h"].append(usd(r["vol_c"]))
            f["oi"].append(usd(r["long_c"] + r["short_c"]))      # one-sided total: long plus short
            f["oi_long"].append(usd(r["long_c"]))
            f["oi_short"].append(usd(r["short_c"]))
            f["funding_ann"].append(r6(r.get("funding_ann")))
            f["fin_long_ann"].append(r6(r.get("fin_long_ann")))
            f["fin_short_ann"].append(r6(r.get("fin_short_ann")))
            for s in SIZES:
                f["ba%s_bps" % s].append(r2(r["ba"][s][0]) if s in r.get("ba", {}) else None)
                f["slip%s_bps" % s].append(slip(r["ba"][s][0]) if s in r.get("ba", {}) else None)
            f["turnover"].append(turnover(r["vol_c"], r["long_c"], r["short_c"]))
            f["long_share"].append(long_share(r["long_c"], r["short_c"]))
            f["ls_ratio"].append(ls_ratio(r["long_c"], r["short_c"]))
            f["approx"].append(d["approx"])
    from .intraday import empty_market
    series = (intra or {}).get("markets") or {}
    for tk, r in sorted(((intra or {}).get("listings") or {}).items()):
        if tk not in files and SAFE_TICKER.match(tk) and (series.get(tk) or {}).get("ts"):
            _describe(files.setdefault(tk, _blank_market(tk)), tk, r)
    for tk, f in files.items():
        f["intraday"] = series.get(tk) or empty_market()
    rank = {tk: i for i, tk in enumerate(order_hint)}
    tickers = sorted(files, key=lambda tk: (rank.get(tk, len(rank)), tk))
    return files, {"tickers": tickers}


def quote_depth_json(days):
    out = {"dates": [], "quoted_1m": {"total": [], "perp": [], "swap": []},
           "median_ba100k": {g: [] for g in GROUPS}, "approx": []}
    newest = None
    for ds in sorted(days):
        d = days[ds]
        n = d["raw"]["quoted_1m"]
        per_group = {g: [] for g in GROUPS}
        for r in d["rows"].values():
            if "100k" in r.get("ba", {}):
                per_group[r["sgroup"]].append(r["ba"]["100k"][0])
        out["dates"].append(ds)
        out["quoted_1m"]["perp"].append(n["perp"])
        out["quoted_1m"]["swap"].append(n["swap"])
        out["quoted_1m"]["total"].append(n["perp"] + n["swap"])
        for g in GROUPS:
            out["median_ba100k"][g].append(r2(median(per_group[g])))
        out["approx"].append(d["approx"])
        newest = d["boundary"]
    return dict({"as_of": iso(newest) if newest else None}, **out)


def swap_pairs_json(days, pairs):
    """swap_pairs.json: both instruments on each shared underlying, level by level.

    Bid-ask at $100K is each leg's daily median over the snapshots in which both legs are quoted,
    so a day the swap is closed has neither. Carry is the day's last reading: annualised funding
    for the perpetual, and for the swap the rate longs pay with the weekend multiple divided out,
    restated from the API's 360-day year to 365 days. `range` is the median over the dates in each
    range that have both legs (`n` counts the dates with both bid-asks).
    """
    keys = [p["key"] for p in pairs]
    out = {"pairs": [{k: p[k] for k in ("key", "label", "perp", "swap")} for p in pairs], "dates": [],
           "perp": {"ba100k": {k: [] for k in keys}, "carry": {k: [] for k in keys}},
           "swap": {"ba100k": {k: [] for k in keys}, "carry": {k: [] for k in keys}},
           "range": {}, "approx": []}
    newest = None
    for ds in sorted(days):
        d = days[ds]
        out["dates"].append(ds)
        for p in pairs:
            legs = d["raw"].get("pairs", {}).get(p["key"])
            out["perp"]["ba100k"][p["key"]].append(r2(legs["perp_ba100k_med"]) if legs else None)
            out["swap"]["ba100k"][p["key"]].append(r2(legs["swap_ba100k_med"]) if legs else None)
            perp, swap = d["rows"].get(p["perp"]), d["rows"].get(p["swap"])
            fa = perp.get("funding_ann") if perp else None
            sa = swap.get("fin_long_ann") if swap else None
            out["perp"]["carry"][p["key"]].append(r6(fa))
            out["swap"]["carry"][p["key"]].append(r6(sa * SWAP_TO_365) if sa is not None else None)
        out["approx"].append(d["approx"])
        newest = d["boundary"]
    if out["dates"]:
        last = to_date(out["dates"][-1])
        for label, span in RANGES:
            idx = [i for i, ds in enumerate(out["dates"]) if span is None or (last - to_date(ds)).days < span]
            out["range"][label] = {}
            for k in keys:
                ba = [(out["perp"]["ba100k"][k][i], out["swap"]["ba100k"][k][i]) for i in idx
                      if out["perp"]["ba100k"][k][i] is not None and out["swap"]["ba100k"][k][i] is not None]
                ca = [(out["perp"]["carry"][k][i], out["swap"]["carry"][k][i]) for i in idx
                      if out["perp"]["carry"][k][i] is not None and out["swap"]["carry"][k][i] is not None]
                out["range"][label][k] = {
                    "perp_ba100k": r2(median([x[0] for x in ba])), "swap_ba100k": r2(median([x[1] for x in ba])),
                    "perp_carry": r6(median([x[0] for x in ca])), "swap_carry": r6(median([x[1] for x in ca])),
                    "n": len(ba)}
    return dict({"as_of": iso(newest) if newest else None}, **out)


def olp_json(days):
    out = {"dates": [], "net_long": {"total": [], "group": {g: [] for g in GROUPS}},
           "matched_share": [], "approx": []}
    newest = None
    for ds in sorted(days):
        d = days[ds]
        net = {g: 0 for g in GROUPS}
        lo = hi = 0
        for r in d["rows"].values():
            net[r["sgroup"]] += r["long_c"] - r["short_c"]
            lo += min(r["long_c"], r["short_c"])
            hi += max(r["long_c"], r["short_c"])
        out["dates"].append(ds)
        out["net_long"]["total"].append(usd(sum(net.values())))
        for g in GROUPS:
            out["net_long"]["group"][g].append(usd(net[g]))
        out["matched_share"].append(r4(lo / float(hi)) if hi else None)
        out["approx"].append(d["approx"])
        newest = d["boundary"]
    return dict({"as_of": iso(newest) if newest else None}, **out)


def events_list():
    cfg = read_json(os.path.join(CONFIG, "events.json"))
    ev = sorted(({"date": e["date"], "label": e["label"]} for e in cfg["events"]), key=lambda e: e["date"])
    assert len(ev) <= 5 and all(len(e["label"].split()) <= 3 for e in ev)
    return ev


def swaps_launch_date():
    for e in read_json(os.path.join(CONFIG, "events.json"))["events"]:
        if e.get("key") == "swaps_launch":
            return e["date"]
    return None


def meta_json(omni_as_of, dl_as_of, split_start):
    """meta.json. Keys owned by other packages stay null unless a previous non-fixture file set them.
    `defillama` is the as-of of DefiLlama's open-interest API (the daily volume export ends where our own
    counter days begin, 22 Nov 2026)."""
    sources = {k: None for k in ("omni", "onchain", "onchain_block", "reports",
                                 "defillama", "polymarket", "hyperliquid", "telegram")}
    article_url = None
    path = os.path.join(OUT, "meta.json")
    if os.path.exists(path):
        try:
            old = read_json(path)
            if not old.get("fixture"):
                sources.update({k: v for k, v in (old.get("sources") or {}).items() if k in sources})
                article_url = old.get("article_url")
        except ValueError:
            pass
    sources.update({"omni": omni_as_of, "defillama": dl_as_of})
    return {"built_at": iso(utcnow()), "data_as_of": omni_as_of, "publication_date": PUBLICATION_DATE,
            "article_url": article_url, "split_start": split_start,
            "continuous_start": CONTINUOUS_START, "stale_after_minutes": STALE_AFTER_MINUTES,
            "sources": sources}
