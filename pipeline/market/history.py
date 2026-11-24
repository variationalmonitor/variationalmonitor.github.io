"""Long histories that mix sources: daily volume, one-sided open interest, markets listed, peers.

Source order, best first, per UTC day:
  volume  omni (exact day from the Omni API) > defillama (the owner's export of DefiLlama's daily
          Perp Volume, from 29 Dec 2025) > report_avg (the reported two-week total averaged per day)
  OI      omni (sealed day) > defillama (daily from 4 Jan 2026) > report_half (report dates only)
  listed  report (until the first Omni day) > omni

Reconciling DefiLlama with the counter. A report period runs from one report's publication time to
the next (the counter in a report is read at publication: 13 Nov 2026 18:51 UTC reads $475.77B and
the API counter was $471.31B at 19:00 UTC two days earlier, on a run rate of about $2.2B a day).
DefiLlama's days are used as published: `total` is never rescaled. They are summed over each period,
weighting the two boundary days by the hours inside it, and compared with the change in the reported
counter; the residual is logged per period (the days after the last report are compared with the
Omni API counter the same way). The cumulative series does not add up the daily totals: between two
consecutive counter readings (reports and Omni snapshots) the daily totals give only the shape, and
one scale factor per interval makes the line meet both readings exactly (`build_cumulative`).
"""
import os
from datetime import timedelta
from decimal import Decimal

from . import store
from .classify import sum_group
from .common import (ROOT, STORE, GROUPS, CLASSES, TRADFI_CLASSES, TRADFI_GROUPS, MIDNIGHT_TOLERANCE, allocate, cents, day_start, daterange,
                     parse_iso, read_json, to_date)

SPLIT_TOLERANCE = 0.01            # exact days: listing 24h volumes must sum to within 1% of the day
COVER_TOLERANCE = timedelta(minutes=30)
# "Total Tokens Currently Listed" (reports to 29 May 2026) and "Total Markets Currently Listed"
# (from 12 Jun 2026) are read as one counter: the count rose 456 -> 465 across the first RWA
# listings while still labelled tokens. Set False to start the series on 12 Jun 2026.
INCLUDE_TOKENS_LISTED = True


# ---------- reports ----------

def load_reports():
    """[{t, cum, oi, markets, tokens, url}] sorted by publication time, and the file used."""
    path = os.path.join(STORE, "reports.json")
    out = []
    if os.path.exists(path):
        for r in read_json(path):
            out.append({"t": parse_iso(r["published_at"]), "cum": r.get("cumulative_volume"),
                        "oi": r.get("dual_sided_oi"), "markets": r.get("markets"),
                        "tokens": r.get("tokens_listed"), "url": r.get("source_url")})
        used = "store/reports.json"
    else:
        path = os.path.join(ROOT, "public", "data", "official-activity.json")
        for r in read_json(path)["reports"]:
            m = r["metrics"]
            g = lambda k: (m[k]["value"] if m.get(k) else None)
            out.append({"t": parse_iso(r["published_at"]), "cum": g("cumulative_volume"),
                        "oi": g("dual_sided_oi"), "markets": g("markets"),
                        "tokens": g("tokens_listed"), "url": (r.get("source_urls") or [None])[0]})
        used = "public/data/official-activity.json"
    for r in out:
        for k in ("cum", "oi"):
            r[k] = Decimal(str(r[k])) if r[k] is not None else None
        for k in ("markets", "tokens"):
            r[k] = int(Decimal(str(r[k]))) if r[k] is not None else None
    out.sort(key=lambda r: r["t"])
    return out, used


# ---------- report periods and DefiLlama residuals ----------

def _overlap_days(a, b):
    """[(date, fraction of that UTC day inside [a, b))]."""
    out = []
    for d in daterange(a.date(), b.date()):
        lo, hi = max(a, day_start(d)), min(b, day_start(d) + timedelta(days=1))
        if hi > lo:
            out.append((d, (hi - lo).total_seconds() / 86400.0))
    return out


def _weighted(daily, a, b):
    s = 0.0
    for d, w in _overlap_days(a, b):
        v = daily.get(d.isoformat())
        if v is None:
            return None
        s += w * v
    return s


def report_periods(reports, daily, omni_close):
    """One row per report period (plus the open period closed by the Omni counter), with DefiLlama's
    sum over it and the residual against the reported change in the counter (None where DefiLlama does
    not cover every day the period touches)."""
    counters = [(r["t"], r["cum"], "report") for r in reports if r["cum"] is not None]
    if omni_close and counters and omni_close[0] > counters[-1][0]:
        counters.append((omni_close[0], omni_close[1], "omni"))
    daily_end = day_start(to_date(max(daily))) + timedelta(days=1) if daily else None
    periods = []
    for (ta, ca, _), (tb, cb, kind) in zip(counters, counters[1:]):
        reported = float(cb - ca)
        # an Omni snapshot stamped a few minutes after DefiLlama's last midnight still closes the period
        tw = _weighted(daily, ta, min(tb, daily_end) if kind == "omni" and daily_end else tb)
        whole = [daily.get(d.isoformat()) for d in daterange(ta.date(), tb.date() - timedelta(days=1))]
        whole = sum(whole) if whole and all(v is not None for v in whole) else None
        periods.append({
            "start": ta, "end": tb, "closed_by": kind, "reported": reported,
            "days": (tb - ta).total_seconds() / 86400.0,
            "defillama": tw,
            "residual": (tw / reported - 1) if tw is not None and reported > 0 else None,
            "residual_whole_days": (whole / reported - 1) if whole is not None and reported > 0 else None,
        })
    return periods


def omni_counter_for(daily, platform_rows):
    """The Omni cumulative-volume reading that closes the open period: the newest one DefiLlama's days
    reach, or the newest of all when there is no DefiLlama series."""
    limit = (day_start(to_date(max(daily))) + timedelta(days=1) + MIDNIGHT_TOLERANCE) if daily else None
    best = None
    for k, r in platform_rows.items():
        t = parse_iso(k)
        if not r.get("dup") and (limit is None or t <= limit) and (best is None or t > best[0]):
            best = (t, Decimal(r["cumulative_volume"]))
    return best


def _avg_day(d, periods):
    """Average-rate volume for a UTC day, or None if the periods do not cover it."""
    lo, hi = day_start(d), day_start(d) + timedelta(days=1)
    covered, v = timedelta(0), 0.0
    for p in periods:
        a, b = max(lo, p["start"]), min(hi, p["end"])
        if b > a:
            covered += b - a
            v += (b - a).total_seconds() / 86400.0 * p["reported"] / p["days"]
    return v if (hi - lo) - covered <= COVER_TOLERANCE else None


# ---------- store days ----------

def load_days(classifier):
    """Sealed days with integer-cent figures and the current classification applied."""
    days = {}
    for ds in store.sealed_days():
        raw = store.load_day(ds)
        rows = {}
        for tk, r in raw["listings"].items():
            group, cls = classifier.get(tk, r["type"], r["name"])[:2]
            lc, sc = cents(r["oi_long"]) or 0, cents(r["oi_short"]) or 0
            rows[tk] = dict(r, group=group, cls=cls, sgroup=sum_group(group),
                            vol_c=cents(r["vol24h"]) or 0, long_c=lc, short_c=sc)
        days[ds] = {"raw": raw, "rows": rows, "approx": raw["approx"],
                    "boundary": parse_iso(raw["boundary"])}
    return days


def _by(rows, key, field, keys):
    out = {k: 0 for k in keys}
    for r in rows.values():
        out[r[key]] += r[field]
    return out


# ---------- volume ----------

VOLUME_SOURCES = ["omni", "defillama", "report_avg"]


def build_volume(reports, dl, days, platform_rows, log=print):
    """Daily volume in integer cents. `dl` is DefiLlama's daily volume {date: usd} (the stored export)."""
    last_store = max(days) if days else None
    periods = report_periods(reports, dl, omni_counter_for(dl, platform_rows))

    first = (reports[0]["t"] + timedelta(days=1)).date()
    candidates = [first]
    if last_store:
        candidates.append(to_date(last_store))
    if dl:
        candidates.append(to_date(max(dl)))
    last = max(candidates)

    out = {"dates": [], "total": [], "src": [], "approx": [], "tradfi_cls": [],
           "group": {g: [] for g in GROUPS}, "cls": {c: [] for c in CLASSES}}
    notes = {"split_days": [], "split_dropped": [], "gaps": [], "provisional": []}
    newest = None
    for d in daterange(first, last):
        ds = d.isoformat()
        sd = days.get(ds)
        total_c, src, tf = None, None, None
        method = sd["raw"]["platform"]["vol_method"] if sd else None
        if sd and method in ("cumulative_diff", "trailing24h_at_close"):
            total_c, src = cents(sd["raw"]["platform"]["volume_utc_day"]), 0
            stamp = sd["boundary"]
        elif ds in dl:
            total_c, src = cents(dl[ds]), 1
            stamp = day_start(d) + timedelta(days=1)
        else:
            v = _avg_day(d, periods)
            if v is not None:
                total_c, src = cents(repr(v)), 2
                stamp = day_start(d) + timedelta(days=1)
        if total_c is None and sd:
            # a snapshot day that no daily series reaches yet: the snapshot's own trailing 24 hours
            total_c, src = sum(r["vol_c"] for r in sd["rows"].values()), 0
            stamp = sd["boundary"]
            notes["provisional"].append(ds)
        if total_c is None:
            # no source at all: the series stops here until the day can be filled
            notes["gaps"] = [x.isoformat() for x in daterange(d, last)]
            break
        newest = stamp if newest is None or stamp > newest else newest
        split = None
        if sd:
            weights = {tk: r["vol_c"] for tk, r in sd["rows"].items() if r["vol_c"] > 0}
            wsum = sum(weights.values())
            if wsum > 0:
                k = total_c / float(wsum)
                if src == 0 and abs(k - 1) > SPLIT_TOLERANCE:
                    notes["split_dropped"].append((ds, k))
                else:
                    alloc = allocate(total_c, weights)
                    g = {x: 0 for x in GROUPS}
                    c = {x: 0 for x in CLASSES}
                    tf = {x: 0 for x in TRADFI_CLASSES}
                    for tk, v in alloc.items():
                        g[sd["rows"][tk]["sgroup"]] += v
                        c[sd["rows"][tk]["cls"]] += v
                        if sd["rows"][tk]["sgroup"] in TRADFI_GROUPS:
                            tf[sd["rows"][tk]["cls"]] += v
                    split = (g, c)
                    notes["split_days"].append({"date": ds, "boundary": sd["raw"]["boundary"],
                                                "approx": sd["approx"], "snapshot_24h": wsum / 100.0,
                                                "total": total_c / 100.0, "factor": k,
                                                "src": VOLUME_SOURCES[src]})
        out["dates"].append(ds)
        out["total"].append(total_c)
        out["src"].append(src)
        out["approx"].append(bool(sd and sd["approx"] and split))
        out["tradfi_cls"].append(tf if split else None)
        for x in GROUPS:
            out["group"][x].append(split[0][x] if split else None)
        for x in CLASSES:
            out["cls"][x].append(split[1][x] if split else None)
    out["as_of"] = newest
    out["cumulative"], out["anchors"], notes["cumulative"] = build_cumulative(
        out["dates"], out["total"], reports, platform_rows)
    return out, periods, notes


# ---------- cumulative volume ----------

REPORT_SUPERSEDED_WITHIN = timedelta(hours=6)


def counter_anchors(reports, platform_rows):
    """Known readings of the cumulative-volume counter, in time order: (time, cents, source).

    Every stored Omni snapshot is an anchor (a repeated response is not). A report is an anchor at
    its publication time unless an Omni snapshot lies within six hours of it: the report quotes the
    same counter rounded to $10M and read at an unstated minute, so next to an exact reading it
    would only bend the line. An anchor that would make the counter fall is dropped (the report of
    the pair, or the later of two Omni readings). Returns (anchors, superseded, dropped).
    """
    omni = sorted((parse_iso(k), cents(r["cumulative_volume"]), "omni")
                  for k, r in platform_rows.items() if not r.get("dup"))
    times = [t for t, _, _ in omni]
    out, superseded, dropped = list(omni), [], []
    for r in reports:
        if r["cum"] is None:
            continue
        a = (r["t"], cents(r["cum"]), "report")
        if any(abs(r["t"] - t) <= REPORT_SUPERSEDED_WITHIN for t in times):
            superseded.append(a)
        else:
            out.append(a)
    out.sort()
    clean = []
    for a in out:
        while clean and a[1] < clean[-1][1] and clean[-1][2] == "report" and a[2] == "omni":
            dropped.append(clean.pop())
        if clean and (a[1] < clean[-1][1] or a[0] == clean[-1][0]):
            dropped.append(a)
            continue
        clean.append(a)
    return clean, superseded, dropped


def _profile_weight(a, b, totals, first, last):
    """Sum over UTC days of (seconds of the day inside [a, b)) x (that day's total, in cents).

    A day outside the series takes the total of the nearest date in it, which makes the stretch
    before the first date and after the last one linear in time. Integers throughout.
    """
    w = 0
    for d in daterange(a.date(), b.date()):
        lo, hi = max(a, day_start(d)), min(b, day_start(d) + timedelta(days=1))
        if hi > lo:
            key = min(max(d, first), last)
            w += int((hi - lo).total_seconds()) * totals[key]
    return w


def cumulative_at(m, anchors, totals, first, last):
    """Counter value in cents at instant m, and the anchors that fix it.

    Within a UTC day volume is taken to accrue evenly. Between two consecutive anchors the daily
    totals give the shape and one scale factor for that interval makes the line meet both anchors.
    Past the last anchor the totals are added unscaled.
    """
    left = right = None
    for a in anchors:            # anchors are few around any one midnight; a scan keeps this plain
        if a[0] <= m:
            left = a
        else:
            right = a
            break
    if left is None:
        return None, []
    if left[0] == m:
        return left[1], [left]
    w1 = _profile_weight(left[0], m, totals, first, last)
    if right is None:
        return left[1] + (2 * w1 + 86400) // (2 * 86400), [left]
    w2 = _profile_weight(left[0], right[0], totals, first, last)
    if w2 <= 0:
        return left[1], [left, right]
    gain = right[1] - left[1]
    return left[1] + (2 * gain * w1 + w2) // (2 * w2), [left, right]


def build_cumulative(dates, total_c, reports, platform_rows):
    anchors, superseded, dropped = counter_anchors(reports, platform_rows)
    days = [to_date(d) for d in dates]
    totals = dict(zip(days, total_c))
    first, last = days[0], days[-1]
    cum, used, unanchored = [], {}, []
    # bisect-free but linear overall: walk the anchors once alongside the dates
    i = 0
    for d in days:
        m = day_start(d) + timedelta(days=1)
        while i + 1 < len(anchors) and anchors[i + 1][0] <= m:
            i += 1
        window = anchors[max(i - 1, 0): i + 2]
        value, fixed = cumulative_at(m, window, totals, first, last)
        if len(fixed) == 1 and fixed[0][0] != m:
            unanchored.append(d.isoformat())
        for a in fixed:
            used[a[0]] = a
        cum.append(value)
    notes = {"anchors_total": len(anchors), "anchors_omni": sum(1 for a in anchors if a[2] == "omni"),
             "superseded": superseded, "dropped": dropped, "unanchored": unanchored}
    return cum, [used[k] for k in sorted(used)], notes


# ---------- open interest ----------

def build_oi(reports, dl_var, days):
    rep = {}
    for r in reports:
        if r["oi"] is not None:
            rep[r["t"].date().isoformat()] = cents(r["oi"] / 2)
    dl_first = min(dl_var) if dl_var else "9999-12-31"
    # a report dates the series only before DefiLlama's first day, so only those report days may stretch the range:
    # a report posted today would otherwise add today, an unfinished day with no figure, as the last row
    keys = sorted({ds for ds in rep if ds < dl_first} | set(dl_var) | set(days))
    rep_dual = {r["t"].date().isoformat(): cents(r["oi"]) for r in reports if r["oi"] is not None}
    out = {"dates": [], "total": [], "src": [], "long": [], "short": [], "approx": [], "tradfi_cls": [], "dual": [],
           "group": {g: [] for g in GROUPS}, "cls": {c: [] for c in CLASSES}}
    checks = []
    if not keys:
        return out, checks
    newest = None
    for d in daterange(to_date(keys[0]), to_date(keys[-1])):
        ds = d.isoformat()
        sd = days.get(ds)
        g = c = tf = None
        lg = sh = None
        if sd:
            lg = sum(r["long_c"] for r in sd["rows"].values())
            sh = sum(r["short_c"] for r in sd["rows"].values())
            total, src = lg + sh, 0
            g = {x: 0 for x in GROUPS}
            c = {x: 0 for x in CLASSES}
            tf = {x: 0 for x in TRADFI_CLASSES}
            for r in sd["rows"].values():
                g[r["sgroup"]] += r["long_c"] + r["short_c"]
                c[r["cls"]] += r["long_c"] + r["short_c"]
                if r["sgroup"] in TRADFI_GROUPS:
                    tf[r["cls"]] += r["long_c"] + r["short_c"]
            stamp = sd["boundary"]
            checks.append({"date": ds, "omni": total / 100.0,
                           "api_dual_half": float(Decimal(sd["raw"]["platform"]["dual_sided_oi"]) / 2),
                           "defillama": dl_var.get(ds), "approx": sd["approx"],
                           "offset_min": sd["raw"]["offset_minutes"]})
        elif ds in dl_var:
            total, src = cents(dl_var[ds]), 1
            stamp = day_start(d) + timedelta(days=1)
        elif ds in rep and ds < dl_first:
            total, src = rep[ds], 2
            stamp = [r["t"] for r in reports if r["t"].date() == d][-1]
        else:
            total, src, stamp = None, None, None
        if stamp is not None and (newest is None or stamp > newest):
            newest = stamp
        out["dates"].append(ds)
        out["total"].append(total)
        out["src"].append(src)
        # dual-sided: the figure as reported where the source is a report, else twice the one-sided total
        out["dual"].append(None if total is None else rep_dual[ds] if src == 2 else 2 * total)
        out["long"].append(lg)
        out["short"].append(sh)
        out["approx"].append(bool(sd and sd["approx"]))
        out["tradfi_cls"].append(tf)
        for x in GROUPS:
            out["group"][x].append(g[x] if g else None)
        for x in CLASSES:
            out["cls"][x].append(c[x] if c else None)
    out["as_of"] = newest
    return out, checks


# ---------- markets listed ----------

def build_listed(reports, days, swaps_launch):
    first_omni = min(days) if days else "9999-12-31"
    out = {"dates": [], "total": [], "src": [], "perp": [], "swap": []}
    newest = None
    for r in reports:
        ds = r["t"].date().isoformat()
        n = r["markets"] if r["markets"] is not None else (r["tokens"] if INCLUDE_TOKENS_LISTED else None)
        if n is None or ds >= first_omni:
            continue
        before_swaps = bool(swaps_launch) and ds < swaps_launch
        out["dates"].append(ds)
        out["total"].append(n)
        out["src"].append(0)
        out["perp"].append(n if before_swaps else None)
        out["swap"].append(0 if before_swaps else None)
        newest = r["t"]
    for ds in sorted(days):
        p = days[ds]["raw"]["platform"]
        tie = p["perp"] + p["swap"] == p["markets"]
        out["dates"].append(ds)
        out["total"].append(p["markets"])
        out["src"].append(1)
        out["perp"].append(p["perp"] if tie else None)
        out["swap"].append(p["swap"] if tie else None)
        newest = days[ds]["boundary"]
    out["as_of"] = newest
    return out
