"""Re-verify every invariant on the written files. Used by `build_market.py --check`.

Reads only `site/data/` (plus the newest raw snapshot and the store for cross-ties) and returns a
list of failures. Money comparisons are done in integer cents.
"""
import os
import re
from datetime import date, datetime, timedelta

from . import history, store
from sources import defillama
from .common import (OUT, CONFIG, STORE, GROUPS, CLASSES, TOP, TRADFI_CLASSES, TRADFI_GROUPS, RANGES, MAX_SPREAD_BPS,
                     SIZES, SIZE_FIELD, cents, median, read_json, spread_bps)

FILES = ["meta.json", "events.json", "volume_daily.json", "oi_daily.json", "peers_oi.json",
         "markets_latest.json", "markets_listed.json", "market/index.json", "quote_depth.json",
         "swap_pairs.json", "olp_market.json", "tradfi.json", "concentration.json", "spread_majors.json",
         "markets_scope.json"]
TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ROW_KEYS = ["ticker", "name", "type", "group", "cls", "vol24h", "oi_long", "oi_short", "oi", "funding_ann",
            "fin_long_ann", "fin_short_ann", "ba1k_bps", "ba100k_bps", "ba1m_bps", "quote_at",
            "turnover", "long_share", "listed", "slip1k_bps", "slip100k_bps", "slip1m_bps", "ls_ratio"]
INTRADAY_KEYS = ["ts", "vol24h", "oi", "oi_long", "oi_short", "ls_ratio", "funding_ann", "fin_long_ann",
                 "fin_short_ann", "slip1k_bps", "slip100k_bps", "slip1m_bps"]
TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z$")
MARKET_SERIES = ["vol24h", "oi", "oi_long", "oi_short", "funding_ann", "fin_long_ann", "fin_short_ann",
                 "ba1k_bps", "ba100k_bps", "ba1m_bps", "turnover", "long_share",
                 "slip1k_bps", "slip100k_bps", "slip1m_bps", "ls_ratio"]


def slip_ok(slip_, ba_):
    """One-way slippage is half the full bid-ask: same nulls, and equal within the two roundings."""
    return (slip_ is None) == (ba_ is None) and (slip_ is None or abs(slip_ - ba_ / 2.0) <= 0.0101)


def ratio_ok(got, num, den):
    """A written 4-decimal ratio against its parts: null exactly when the denominator is zero."""
    if den <= 0:
        return got is None
    return got is not None and abs(got - num / float(den)) <= 6e-5


def c(x):
    """Cents of a written figure. A null where a figure is required counts as 0 here; the null
    itself is reported by the field's own check."""
    return 0 if x is None else int(round(x * 100))


class Checker:
    def __init__(self):
        self.fail = []
        self.n = 0

    def ok(self, cond, msg):
        self.n += 1
        if not cond:
            self.fail.append(msg)
        return cond

    def dates(self, name, dates, contiguous=False):
        self.ok(all(DATE.match(d) for d in dates), "%s: bad date format" % name)
        self.ok(all(a < b for a, b in zip(dates, dates[1:])), "%s: dates not strictly increasing" % name)
        if contiguous and dates:
            n = (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days + 1
            self.ok(n == len(dates), "%s: dates not contiguous (%d of %d)" % (name, len(dates), n))

    def same_len(self, name, n, arrays):
        for k, a in arrays.items():
            self.ok(isinstance(a, list) and len(a) == n, "%s: %s has length %s, dates %d"
                    % (name, k, len(a) if isinstance(a, list) else "n/a", n))

    def as_of(self, name, d):
        self.ok(isinstance(d.get("as_of"), str) and TIME.match(d["as_of"]), "%s: as_of missing or malformed" % name)
        self.ok("fixture" not in d, "%s: fixture file still in place" % name)

    def split(self, name, d, keysets):
        """Each split is all-null or complete on a date, and when present sums exactly to total."""
        for label, keys in keysets:
            part = d[label]
            self.ok(sorted(part) == sorted(keys), "%s: %s keys %s" % (name, label, sorted(part)))
            for i, ds in enumerate(d["dates"]):
                vals = [part[k][i] for k in keys]
                nn = [v for v in vals if v is not None]
                if not nn:
                    continue
                if not self.ok(len(nn) == len(keys), "%s %s: %s partly null" % (name, ds, label)):
                    continue
                self.ok(d["total"][i] is not None and sum(c(v) for v in vals) == c(d["total"][i]),
                        "%s %s: %s sums to %.2f, total %s" % (name, ds, label, sum(vals), d["total"][i]))


def run(k=None):
    """Run every check. Failures accumulate on the Checker; an unexpected shape is a failure too."""
    k = k or Checker()
    try:
        _run(k)
    except Exception as e:   # a file so malformed that a check cannot even be evaluated
        k.ok(False, "check aborted: %s: %s" % (type(e).__name__, e))
    return k


def _run(k):
    data = {}
    for f in FILES:
        p = os.path.join(OUT, f)
        if not k.ok(os.path.exists(p), "%s: missing" % f):
            continue
        try:
            data[f] = read_json(p)
        except ValueError as e:
            k.ok(False, "%s: invalid JSON (%s)" % (f, e))
    if k.fail:
        return k

    # ---- volume_daily
    v = data["volume_daily.json"]
    k.as_of("volume_daily", v)
    k.ok(v["sources"] == ["omni", "defillama", "report_avg"], "volume_daily: sources")
    n = len(v["dates"])
    k.dates("volume_daily", v["dates"], contiguous=True)
    k.same_len("volume_daily", n, dict({"total": v["total"], "src": v["src"], "approx": v["approx"]},
               **{"group." + g: v["group"][g] for g in v["group"]}, **{"cls." + x: v["cls"][x] for x in v["cls"]}))
    k.ok(all(isinstance(x, (int, float)) and x > 0 for x in v["total"]), "volume_daily: total has null or non-positive values")
    k.ok(all(s in range(len(v["sources"])) for s in v["src"]), "volume_daily: src index out of range")
    k.ok(all(isinstance(a, bool) for a in v["approx"]), "volume_daily: approx not boolean")
    k.split("volume_daily", v, [("group", GROUPS), ("cls", CLASSES)] + ([("top", TOP)] if "top" in v else []))
    if k.ok(isinstance(v.get("top"), dict) and sorted(v["top"]) == sorted(TOP), "volume_daily: top missing or wrong keys"):
        k.same_len("volume_daily", len(v["dates"]), {"top." + x: v["top"][x] for x in TOP})
        for i, ds in enumerate(v["dates"]):
            g_ = [v["group"][x][i] for x in GROUPS]
            t_ = [v["top"][x][i] for x in TOP]
            if g_[0] is None:        # same null rule as `group`
                k.ok(all(x is None for x in t_), "volume_daily %s: top present without a group split" % ds)
            else:
                k.ok(all(x is not None for x in t_) and c(t_[0]) == c(g_[0]) and c(t_[1]) == c(g_[1])
                     and c(t_[2]) == c(g_[2]) + c(g_[3]), "volume_daily %s: top does not match the groups" % ds)
    # split: crypto (majors + other crypto) and TradFi (perpetuals + swaps), summing to total, null where the groups are
    sp = v.get("split")
    if k.ok(isinstance(sp, dict) and list(sp) == ["crypto", "tradfi"], "volume_daily: split missing or keys not [crypto, tradfi]"):
        k.same_len("volume_daily", n, {"split.crypto": sp["crypto"], "split.tradfi": sp["tradfi"]})
        for i, ds in enumerate(v["dates"]):
            cr, tf_ = sp["crypto"][i], sp["tradfi"][i]
            if v["group"]["crypto_majors"][i] is None:
                k.ok(cr is None and tf_ is None, "volume_daily %s: split present without a group split" % ds)
                continue
            k.ok(cr is not None and tf_ is not None and c(cr) + c(tf_) == c(v["total"][i]),
                 "volume_daily %s: split crypto %s + tradfi %s != total %s" % (ds, cr, tf_, v["total"][i]))
            k.ok(cr is not None and c(cr) == c(v["group"]["crypto_majors"][i]) + c(v["group"]["other_crypto"][i])
                 and tf_ is not None and c(tf_) == c(v["group"]["tradfi_perp"][i]) + c(v["group"]["tradfi_swap"][i]),
                 "volume_daily %s: split does not match the groups" % ds)
    # DefiLlama days are the stored export's figure to the cent, on every export day that is not an Omni day
    dl_vol = defillama.load_volume_export(STORE)[0]
    for i, ds in enumerate(v["dates"]):
        if v["src"][i] == 1:
            k.ok(ds in dl_vol and c(v["total"][i]) == cents(dl_vol[ds]),
                 "volume_daily %s: defillama total %s, the stored export has %s" % (ds, v["total"][i], dl_vol.get(ds)))
        elif v["src"][i] == 2:
            k.ok(ds not in dl_vol, "volume_daily %s: report average on a day the DefiLlama export covers" % ds)
    for i, ds in enumerate(v["dates"]):
        has = v["group"]["crypto_majors"][i] is not None
        k.ok(has == (v["cls"]["crypto"][i] is not None), "volume_daily %s: group and cls split differ in presence" % ds)
        k.ok(not v["approx"][i] or has, "volume_daily %s: approx without a split" % ds)
        k.ok(not has or all(v["group"][g][i] >= 0 for g in GROUPS), "volume_daily %s: negative split value" % ds)

    # ---- volume_daily.cumulative: non-decreasing, and fixed by the counter anchors
    cumv = v.get("cumulative")
    anch = v.get("anchors")
    if k.ok(isinstance(cumv, list) and len(cumv) == n and isinstance(anch, list) and anch,
            "volume_daily: cumulative or anchors missing"):
        k.ok(all(x is not None for x in cumv), "volume_daily: null in cumulative")
        k.ok(all(a <= b for a, b in zip(cumv, cumv[1:])), "volume_daily: cumulative decreases")
        A = [(datetime.strptime(a["t"], "%Y-%m-%dT%H:%M:%SZ"), c(a["value"]), a["src"]) for a in anch]
        k.ok(all(a[0] < b[0] and a[1] <= b[1] for a, b in zip(A, A[1:])), "volume_daily: anchors out of order or falling")
        k.ok(all(a[2] in ("report", "omni") for a in A), "volume_daily: anchor source")
        # every anchor is a reading that exists in the store, to the cent
        plat = {t_: cents(r_["cumulative_volume"]) for t_, r_ in store.load_platform().items()}
        reps = {r_["t"].strftime("%Y-%m-%dT%H:%M:%SZ"): cents(r_["cum"]) for r_ in history.load_reports()[0] if r_["cum"] is not None}
        for a, (t_, val, src_) in zip(anch, A):
            k.ok((plat if src_ == "omni" else reps).get(a["t"]) == val, "volume_daily: anchor %s %s is not a stored reading" % (src_, a["t"]))
        D = [date.fromisoformat(x) for x in v["dates"]]
        T = dict(zip(D, [c(x) for x in v["total"]]))

        def weight(a, b):          # seconds inside [a, b) x the day's total; outside the series, the nearest date's
            w, d_ = 0, a.date()
            while d_ <= b.date():
                lo = max(a, datetime(d_.year, d_.month, d_.day))
                hi = min(b, datetime(d_.year, d_.month, d_.day) + timedelta(days=1))
                if hi > lo:
                    w += int((hi - lo).total_seconds()) * T[min(max(d_, D[0]), D[-1])]
                d_ += timedelta(days=1)
            return w

        for i, d_ in enumerate(D):
            m_ = datetime(d_.year, d_.month, d_.day) + timedelta(days=1)
            left = [a for a in A if a[0] <= m_]
            right = [a for a in A if a[0] > m_]
            if not k.ok(left, "volume_daily %s: no anchor before the date" % d_):
                continue
            lf = left[-1]
            if lf[0] == m_:
                want = lf[1]                       # an anchor read exactly at midnight is the value
            elif not right:
                want = lf[1] + weight(lf[0], m_) / 86400.0
            else:
                rt = right[0]
                want = lf[1] + (rt[1] - lf[1]) * weight(lf[0], m_) / float(weight(lf[0], rt[0]))
            k.ok(abs(c(cumv[i]) - want) <= 100, "volume_daily %s: cumulative %.2f, anchors give %.2f" % (d_, cumv[i], want / 100.0))
        # an intraday anchor must sit between the day-end values either side of it
        at = dict(zip(D, [c(x) for x in cumv]))
        for t_, val, src_ in A:
            d_ = t_.date()
            if t_ == datetime(d_.year, d_.month, d_.day):
                d_ -= timedelta(days=1)
                if d_ in at:
                    k.ok(abs(at[d_] - val) <= 100, "volume_daily: midnight anchor %s is not the day-end value" % t_)
                continue
            lo, hi = at.get(d_ - timedelta(days=1)), at.get(d_)
            k.ok((lo is None or lo <= val + 100) and (hi is None or val <= hi + 100),
                 "volume_daily: anchor %s (%s) lies outside the day-end values around it" % (t_, src_))

    # ---- oi_daily
    o = data["oi_daily.json"]
    k.as_of("oi_daily", o)
    k.ok(o["sources"] == ["omni", "defillama", "report_half"], "oi_daily: sources")
    n = len(o["dates"])
    k.dates("oi_daily", o["dates"], contiguous=True)
    k.same_len("oi_daily", n, dict({"total": o["total"], "src": o["src"], "long": o["long"], "short": o["short"],
               "approx": o["approx"]}, **{"group." + g: o["group"][g] for g in o["group"]},
               **{"cls." + x: o["cls"][x] for x in o["cls"]}))
    k.split("oi_daily", o, [("group", GROUPS), ("cls", CLASSES)] + ([("top", TOP)] if "top" in o else []))
    if k.ok(isinstance(o.get("top"), dict) and sorted(o["top"]) == sorted(TOP), "oi_daily: top missing or wrong keys"):
        k.same_len("oi_daily", len(o["dates"]), {"top." + x: o["top"][x] for x in TOP})
        for i, ds in enumerate(o["dates"]):
            g_ = [o["group"][x][i] for x in GROUPS]
            t_ = [o["top"][x][i] for x in TOP]
            if g_[0] is None:        # same null rule as `group`
                k.ok(all(x is None for x in t_), "oi_daily %s: top present without a group split" % ds)
            else:
                k.ok(all(x is not None for x in t_) and c(t_[0]) == c(g_[0]) and c(t_[1]) == c(g_[1])
                     and c(t_[2]) == c(g_[2]) + c(g_[3]), "oi_daily %s: top does not match the groups" % ds)
    for i, ds in enumerate(o["dates"]):
        t, s = o["total"][i], o["src"][i]
        k.ok((t is None) == (s is None), "oi_daily %s: total and src disagree on null" % ds)
        k.ok(t is None or t > 0, "oi_daily %s: non-positive total" % ds)
        k.ok(s is None or s in range(len(o["sources"])), "oi_daily %s: src index out of range" % ds)
        lg, sh = o["long"][i], o["short"][i]
        k.ok((lg is None) == (sh is None), "oi_daily %s: long and short disagree on null" % ds)
        if lg is not None:
            k.ok(t is not None and c(lg) + c(sh) == c(t), "oi_daily %s: long + short != total" % ds)
            k.ok(s == 0, "oi_daily %s: long/short on a day that is not an Omni day" % ds)
            k.ok(lg >= 0 and sh >= 0, "oi_daily %s: negative long or short" % ds)
        k.ok((s == 0) == (o["group"]["crypto_majors"][i] is not None), "oi_daily %s: split presence != Omni day" % ds)
    k.ok(o["total"][-1] is not None, "oi_daily: last date is null")
    if k.ok(isinstance(o.get("dual"), list) and len(o["dual"]) == n, "oi_daily: dual missing or wrong length"):
        for ds, t, s_, dl_ in zip(o["dates"], o["total"], o["src"], o["dual"]):
            if t is None:
                k.ok(dl_ is None, "oi_daily %s: dual without a total" % ds)
            elif s_ == 2:        # reported dual-sided figure; the one-sided total is half of it, rounded to a cent
                k.ok(dl_ is not None and abs(c(dl_) - 2 * c(t)) <= 1, "oi_daily %s: dual %s is not the reported figure" % (ds, dl_))
            else:
                k.ok(dl_ is not None and c(dl_) == 2 * c(t), "oi_daily %s: dual %s is not twice the total" % (ds, dl_))

    # ---- peers_oi
    p = data["peers_oi.json"]
    k.as_of("peers_oi", p)
    n = len(p["dates"])
    k.dates("peers_oi", p["dates"], contiguous=True)
    k.ok(len(p["venues"]) == 6, "peers_oi: %d venues, expected 6" % len(p["venues"]))
    k.same_len("peers_oi", n, dict({"variational": p["variational"]}, **{v_["name"]: v_["values"] for v_ in p["venues"]}))
    k.ok(len({v_["name"] for v_ in p["venues"]}) == len(p["venues"]), "peers_oi: duplicate venue names")
    k.ok(not any("ariational" in v_["name"] for v_ in p["venues"]), "peers_oi: Variational among the peers")
    oi_at = dict(zip(o["dates"], o["total"]))
    for ds, x in zip(p["dates"], p["variational"]):
        k.ok(x is not None and oi_at.get(ds) is not None and c(x) == c(oi_at[ds]),
             "peers_oi %s: variational %s does not tie to oi_daily %s" % (ds, x, oi_at.get(ds)))
    for v_ in p["venues"]:
        k.ok(all(x is None or x >= 0 for x in v_["values"]), "peers_oi: negative value for %s" % v_["name"])
        k.ok(v_["values"][-1] is not None, "peers_oi: %s has no latest value" % v_["name"])

    # ---- markets_latest
    olp_dates = data["quote_depth.json"]["dates"]      # our own snapshot days
    m = data["markets_latest.json"]
    k.as_of("markets_latest", m)
    rows = m["rows"]
    k.ok(len(rows) > 0, "markets_latest: no rows")
    cfg_all = read_json(os.path.join(CONFIG, "classification.json"))["listings"]
    first_day = min(olp_dates) if olp_dates else None
    listed_cfg = {}
    for t_, e_ in cfg_all.items():
        l_ = e_.get("listed")
        known = l_ and not (l_["evidence"] == "snapshot" and (first_day is None or l_["date"] <= first_day))
        listed_cfg[t_] = l_["date"] if known else None
    k.ok(all(e_["cls"] in CLASSES for e_ in cfg_all.values()), "classification: a class outside %s" % CLASSES)
    k.ok(all((e_["cls"] in TRADFI_CLASSES) == (e_["group"] in TRADFI_GROUPS) for e_ in cfg_all.values()),
         "classification: TradFi group without a TradFi class, or the reverse")
    k.ok(len({r["ticker"] for r in rows}) == len(rows), "markets_latest: duplicate tickers")
    for r in rows:
        t = r["ticker"]
        k.ok(list(r) == ROW_KEYS, "markets_latest %s: keys %s" % (t, list(r)))
        k.ok(r["type"] in ("perp", "swap") and t.endswith("-PERP" if r["type"] == "perp" else "-SWAP"),
             "markets_latest %s: type %s" % (t, r["type"]))
        k.ok(r["group"] in GROUPS + ["unclassified"] and r["cls"] in CLASSES, "markets_latest %s: group or cls" % t)
        k.ok((r["group"] == "tradfi_swap") == (r["type"] == "swap"), "markets_latest %s: swap not in tradfi_swap" % t)
        k.ok((r["cls"] == "crypto") == (r["group"] in ("crypto_majors", "other_crypto", "unclassified")),
             "markets_latest %s: class %s in group %s" % (t, r["cls"], r["group"]))
        k.ok(r["listed"] is None or DATE.match(r["listed"]), "markets_latest %s: listed %s" % (t, r["listed"]))
        k.ok(r["listed"] == listed_cfg.get(t), "markets_latest %s: listed %s, classification gives %s" % (t, r["listed"], listed_cfg.get(t)))
        k.ok(all(r[x] is not None and r[x] >= 0 for x in ("vol24h", "oi_long", "oi_short")),
             "markets_latest %s: volume or OI null or negative" % t)
        if r["type"] == "perp":
            k.ok(r["fin_long_ann"] is None and r["fin_short_ann"] is None, "markets_latest %s: financing on a perpetual" % t)
        else:
            k.ok(r["funding_ann"] is None, "markets_latest %s: funding on a swap" % t)
            k.ok((r["fin_long_ann"] is None) == (r["fin_short_ann"] is None), "markets_latest %s: one-sided financing" % t)
        for x in ("ba1k_bps", "ba100k_bps", "ba1m_bps"):
            k.ok(r[x] is None or 0 <= r[x] <= MAX_SPREAD_BPS + 0.01, "markets_latest %s: %s outside the two-sided range" % (t, x))
        k.ok(r["quote_at"] is None or TIME.match(r["quote_at"]), "markets_latest %s: quote_at" % t)
        oi_c = c(r["oi_long"]) + c(r["oi_short"])
        k.ok(r["oi"] is not None and c(r["oi"]) == oi_c, "markets_latest %s: oi is not long plus short" % t)
        k.ok(ratio_ok(r["turnover"], c(r["vol24h"]), oi_c), "markets_latest %s: turnover %s" % (t, r["turnover"]))
        k.ok(ratio_ok(r["long_share"], c(r["oi_long"]), oi_c) and (r["long_share"] is None or 0 <= r["long_share"] <= 1),
             "markets_latest %s: long_share %s" % (t, r["long_share"]))
        k.ok(all(r[x] is None for x in ("ba1k_bps", "ba100k_bps", "ba1m_bps")) or r["quote_at"] is not None,
             "markets_latest %s: bid-ask without quote time" % t)
        k.ok(all(slip_ok(r["slip%s_bps" % x], r["ba%s_bps" % x]) for x in SIZES), "markets_latest %s: slip is not half the bid-ask" % t)
        k.ok(ratio_ok(r["ls_ratio"], c(r["oi_long"]), c(r["oi_short"])), "markets_latest %s: ls_ratio %s" % (t, r["ls_ratio"]))
    # tie to the raw snapshot the table was built from
    refs = {r.key: r for r in store.list_refs()}
    if k.ok(m["as_of"] in refs, "markets_latest: snapshot %s no longer on disk" % m["as_of"]):
        snap = refs[m["as_of"]].load()
        plat = snap["platform"]
        k.ok(len(rows) == len(snap["listings"]), "markets_latest: %d rows, snapshot has %d listings" % (len(rows), len(snap["listings"])))
        k.ok(abs(sum(c(r["vol24h"]) for r in rows) - cents(plat["volume_24h"])) <= len(rows),
             "markets_latest: listing volumes do not sum to the API's total_volume_24h")
        k.ok(abs(2 * sum(c(r["oi_long"]) + c(r["oi_short"]) for r in rows) - cents(plat["dual_sided_oi"])) <= 2 * len(rows),
             "markets_latest: twice the listings' OI does not equal the API's dual-sided open interest")
        raw = {l["ticker"]: l for l in snap["listings"]}
        for r in rows:
            quotes = raw[r["ticker"]].get("quotes") or {}
            for size in SIZES:
                want = spread_bps(quotes.get(SIZE_FIELD[size]))
                want = None if want is None else round(want, 2)
                k.ok(r["ba%s_bps" % size] == want, "markets_latest %s: %s bid-ask %s, snapshot gives %s"
                     % (r["ticker"], size, r["ba%s_bps" % size], want))
                half = spread_bps(quotes.get(SIZE_FIELD[size]))
                half = None if half is None else round(half / 2.0, 2)
                k.ok(r["slip%s_bps" % size] == half, "markets_latest %s: %s slip %s, snapshot gives %s"
                     % (r["ticker"], size, r["slip%s_bps" % size], half))
            f = raw[r["ticker"]].get("funding")
            if r["type"] == "swap" and r["fin_long_ann"] is not None:
                ratio = float(f["last"]["annualized_long_rate"]) / r["fin_long_ann"] if r["fin_long_ann"] else None
                k.ok(ratio is None or abs(ratio - round(ratio)) < 1e-3 and 1 <= round(ratio) <= 5,
                     "markets_latest %s: financing multiple %s is not a whole number" % (r["ticker"], ratio))

    # ---- markets_listed
    ml = data["markets_listed.json"]
    k.as_of("markets_listed", ml)
    k.ok(ml["sources"] == ["report", "omni"], "markets_listed: sources")
    n = len(ml["dates"])
    k.dates("markets_listed", ml["dates"])
    k.same_len("markets_listed", n, {x: ml[x] for x in ("total", "src", "perp", "swap")})
    for i, ds in enumerate(ml["dates"]):
        k.ok(isinstance(ml["total"][i], int) and ml["total"][i] > 0, "markets_listed %s: total" % ds)
        k.ok((ml["perp"][i] is None) == (ml["swap"][i] is None), "markets_listed %s: perp and swap disagree on null" % ds)
        if ml["perp"][i] is not None:
            k.ok(ml["perp"][i] + ml["swap"][i] == ml["total"][i], "markets_listed %s: perp + swap != total" % ds)
    k.ok(all(a <= b for a, b in zip(ml["src"], ml["src"][1:])), "markets_listed: report dates after Omni dates")

    # ---- market files
    idx = data["market/index.json"]
    k.ok("fixture" not in idx, "market/index: fixture file still in place")
    tick = idx["tickers"]
    k.ok(len(set(tick)) == len(tick), "market/index: duplicate tickers")
    on_disk = sorted(f[:-5] for f in os.listdir(os.path.join(OUT, "market")) if f.endswith(".json") and f != "index.json")
    k.ok(on_disk == sorted(tick), "market/: files on disk differ from index (%d vs %d)" % (len(on_disk), len(tick)))
    per_day, q1m, vols, tf_listed = {}, {}, {}, {}
    # Tie rule. A finished day's volume, open interest and rates are its boundary snapshot (the one
    # nearest the day's end, normally stamped 00:00 of the next day): they must equal the intraday
    # point at that time. Its slippage is the median over the day's snapshots.
    recent = {}
    for ds in store.sealed_days()[-9:]:
        raw_ = store.load_day(ds)
        end_ = datetime.strptime(ds, "%Y-%m-%d") + timedelta(days=1)
        recent[ds] = {"bts": datetime.strptime(raw_["boundary"], "%Y-%m-%dT%H:%M:%SZ").strftime("%Y-%m-%dT%H:%MZ"),
                      "samples": raw_["samples"], "lo": end_ - timedelta(days=1) + timedelta(minutes=10),
                      "hi": end_ + timedelta(minutes=10)}
    intra_ts, intra_group, intra_at = [], {}, {}
    latest_row = {r["ticker"]: r for r in rows}
    latest_stamp = m["as_of"][:16] + "Z"
    for t in tick:
        p_ = os.path.join(OUT, "market", t + ".json")
        if not os.path.exists(p_):
            continue
        f = read_json(p_)
        k.ok("fixture" not in f and f["ticker"] == t, "market/%s: ticker or fixture" % t)
        k.ok(f["type"] in ("perp", "swap") and f["group"] in GROUPS + ["unclassified"] and f["cls"] in CLASSES,
             "market/%s: type, group or cls" % t)
        n = len(f["dates"])
        k.ok(n > 0, "market/%s: empty" % t)
        k.dates("market/" + t, f["dates"])
        k.same_len("market/" + t, n, dict({s: f[s] for s in MARKET_SERIES}, approx=f["approx"]))
        for i, ds in enumerate(f["dates"]):
            k.ok(all(f[x][i] is not None and f[x][i] >= 0 for x in ("vol24h", "oi_long", "oi_short")),
                 "market/%s %s: volume or OI null or negative" % (t, ds))
            if f["type"] == "perp":
                k.ok(f["fin_long_ann"][i] is None and f["fin_short_ann"][i] is None, "market/%s %s: financing on a perpetual" % (t, ds))
            else:
                k.ok(f["funding_ann"][i] is None, "market/%s %s: funding on a swap" % (t, ds))
            k.ok(all(f[x][i] is None or 0 <= f[x][i] <= MAX_SPREAD_BPS + 0.01 for x in ("ba1k_bps", "ba100k_bps", "ba1m_bps")),
                 "market/%s %s: bid-ask outside the two-sided range" % (t, ds))
            q1m.setdefault(ds, {"perp": 0, "swap": 0})[f["type"]] += f["ba1m_bps"][i] is not None
            k.ok(f["oi"][i] is not None and c(f["oi"][i]) == c(f["oi_long"][i]) + c(f["oi_short"][i]),
                 "market/%s %s: oi is not long plus short" % (t, ds))
            oi_c = c(f["oi_long"][i]) + c(f["oi_short"][i])
            k.ok(ratio_ok(f["turnover"][i], c(f["vol24h"][i]), oi_c), "market/%s %s: turnover %s" % (t, ds, f["turnover"][i]))
            k.ok(ratio_ok(f["long_share"][i], c(f["oi_long"][i]), oi_c)
                 and (f["long_share"][i] is None or 0 <= f["long_share"][i] <= 1),
                 "market/%s %s: long_share %s" % (t, ds, f["long_share"][i]))
            k.ok(all(slip_ok(f["slip%s_bps" % x][i], f["ba%s_bps" % x][i]) for x in SIZES),
                 "market/%s %s: slip is not half the bid-ask" % (t, ds))
            k.ok(ratio_ok(f["ls_ratio"][i], c(f["oi_long"][i]), c(f["oi_short"][i])), "market/%s %s: ls_ratio %s" % (t, ds, f["ls_ratio"][i]))
            vols.setdefault(ds, []).append(c(f["vol24h"][i]))
            if f["group"] in TRADFI_GROUPS:
                tf_listed.setdefault(ds, {x: 0 for x in TRADFI_CLASSES})[f["cls"]] += 1
            a = per_day.setdefault(ds, [0, 0, 0])
            a[0] += c(f["oi_long"][i])
            a[1] += c(f["oi_short"][i])
            a[2] += 1
        # intraday: one point per snapshot over the trailing seven days
        it = f.get("intraday")
        if not k.ok(isinstance(it, dict) and list(it) == INTRADAY_KEYS, "market/%s: intraday keys" % t):
            continue
        ts_ = it["ts"]
        k.same_len("market/%s intraday" % t, len(ts_), {x: it[x] for x in INTRADAY_KEYS})
        k.ok(len(ts_) <= 336 and all(TS.match(x) for x in ts_) and all(a < b for a, b in zip(ts_, ts_[1:])),
             "market/%s: intraday ts malformed, unordered or more than 336" % t)
        if len(ts_) > len(intra_ts):
            intra_ts = ts_
        pos = {x: j for j, x in enumerate(ts_)}
        intra_at[t] = (pos, it)
        gkey = "other_crypto" if f["group"] == "unclassified" else f["group"]
        for j, x in enumerate(ts_):
            lg_, sh_ = it["oi_long"][j], it["oi_short"][j]
            if not k.ok(lg_ is not None and sh_ is not None and it["oi"][j] is not None and c(it["oi"][j]) == c(lg_) + c(sh_),
                        "market/%s %s: intraday oi is not long plus short" % (t, x)):
                continue
            k.ok(ratio_ok(it["ls_ratio"][j], c(lg_), c(sh_)), "market/%s %s: intraday ls_ratio" % (t, x))
            k.ok(all(it[y][j] is None or 0 <= it[y][j] <= MAX_SPREAD_BPS / 2 + 0.01 for y in ("slip1k_bps", "slip100k_bps", "slip1m_bps")),
                 "market/%s %s: intraday slip outside the two-sided range" % (t, x))
            k.ok(it["fin_long_ann"][j] is None and it["fin_short_ann"][j] is None if f["type"] == "perp" else it["funding_ann"][j] is None,
                 "market/%s %s: intraday rate of the wrong instrument" % (t, x))
            a_ = intra_group.setdefault(x, {}).setdefault(gkey, [0, 0])
            a_[0] += c(lg_)
            a_[1] += c(sh_)
        # daily figure = the intraday point at the day's boundary snapshot
        for i, ds in enumerate(f["dates"]):
            rc = recent.get(ds)
            if not rc or rc["bts"] not in pos:
                continue
            j = pos[rc["bts"]]
            for y in ("vol24h", "oi", "oi_long", "oi_short", "ls_ratio", "funding_ann", "fin_long_ann", "fin_short_ann"):
                k.ok(f[y][i] == it[y][j], "market/%s %s: daily %s %s differs from the intraday point at %s (%s)"
                     % (t, ds, y, f[y][i], rc["bts"], it[y][j]))
            sample = [jj for jj, x in enumerate(ts_) if x == rc["bts"] or rc["lo"] < datetime.strptime(x, "%Y-%m-%dT%H:%MZ") <= rc["hi"]]
            if len(sample) == rc["samples"]:        # every snapshot of the day is still in the window
                for y in ("slip1k_bps", "slip100k_bps", "slip1m_bps"):
                    med = median([it[y][jj] for jj in sample if it[y][jj] is not None])
                    k.ok((f[y][i] is None) == (med is None) and (med is None or abs(f[y][i] - med) <= 0.0101),
                         "market/%s %s: daily %s %s, median of the day's intraday points %s" % (t, ds, y, f[y][i], med))
        # the latest table is the newest intraday point
        lr = latest_row.get(t)
        if lr and ts_ and ts_[-1] == latest_stamp:
            for y in INTRADAY_KEYS[1:]:
                k.ok(lr[y] == it[y][-1], "markets_latest %s: %s %s differs from the newest intraday point %s" % (t, y, lr[y], it[y][-1]))
    # cross-ties: per-market files against oi_daily, olp_market and markets_listed on Omni days
    olp = data["olp_market.json"]
    net_at = dict(zip(olp["dates"], olp["net_long"]["total"]))
    listed_at = {d: t for d, t, s in zip(ml["dates"], ml["total"], ml["src"]) if s == 1}
    for i, ds in enumerate(o["dates"]):
        if o["src"][i] != 0:
            continue
        a = per_day.get(ds)
        if not k.ok(a is not None, "oi_daily %s: Omni day with no per-market files" % ds):
            continue
        k.ok(a[0] == c(o["long"][i]) and a[1] == c(o["short"][i]), "oi_daily %s: long/short do not tie to market files" % ds)
        k.ok(ds in net_at and c(net_at[ds]) == a[0] - a[1], "olp_market %s: net long does not tie to oi_daily" % ds)
        k.ok(listed_at.get(ds) == a[2], "markets_listed %s: %s listed, %d market files" % (ds, listed_at.get(ds), a[2]))

    # ---- quote_depth
    q = data["quote_depth.json"]
    k.as_of("quote_depth", q)
    n = len(q["dates"])
    k.dates("quote_depth", q["dates"])
    k.same_len("quote_depth", n, dict({"q." + x: q["quoted_1m"][x] for x in ("total", "perp", "swap")},
               **{"m." + g: q["median_ba100k"][g] for g in GROUPS}, approx=q["approx"]))
    k.ok(sorted(q["median_ba100k"]) == sorted(GROUPS), "quote_depth: median_ba100k keys")
    for i, ds in enumerate(q["dates"]):
        k.ok(q["quoted_1m"]["perp"][i] + q["quoted_1m"]["swap"][i] == q["quoted_1m"]["total"][i],
             "quote_depth %s: perp + swap != total" % ds)
        k.ok(all(q["median_ba100k"][g][i] is None or 0 < q["median_ba100k"][g][i] <= MAX_SPREAD_BPS + 0.01 for g in GROUPS),
             "quote_depth %s: median outside the two-sided range" % ds)
        # quoted on any snapshot of the day: the per-market daily median at $1M is non-null for exactly those listings
        got = q1m.get(ds, {"perp": 0, "swap": 0})
        k.ok(q["quoted_1m"]["perp"][i] >= got["perp"] and q["quoted_1m"]["swap"][i] >= got["swap"]
             and q["quoted_1m"]["total"][i] - got["perp"] - got["swap"] <= 2,
             "quote_depth %s: quoted_1m %s does not match the market files %s" % (ds, q["quoted_1m"]["total"][i], got))

    # ---- swap_pairs
    e = data["swap_pairs.json"]
    k.as_of("swap_pairs", e)
    keys = [x["key"] for x in e["pairs"]]
    k.ok(len(keys) == len(set(keys)) and len(keys) > 0, "swap_pairs: pair keys")
    cfg_pairs = read_json(os.path.join(CONFIG, "pairs.json"))["pairs"]
    k.ok([(x["key"], x["perp"], x["swap"]) for x in e["pairs"]] == [(x["key"], x["perp"], x["swap"]) for x in cfg_pairs],
         "swap_pairs: pairs differ from config/pairs.json")
    for x in e["pairs"]:
        pc, sc = cfg_all.get(x["perp"], {}), cfg_all.get(x["swap"], {})
        k.ok(pc.get("group") == "tradfi_perp" and sc.get("group") == "tradfi_swap",
             "swap_pairs %s: legs are not a TradFi perpetual and a swap" % x["key"])
    n = len(e["dates"])
    k.dates("swap_pairs", e["dates"])
    for leg in ("perp", "swap"):
        k.ok(sorted(e[leg]) == ["ba100k", "carry"] and all(sorted(e[leg][f_]) == sorted(keys) for f_ in e[leg]),
             "swap_pairs: %s series keys" % leg)
        k.same_len("swap_pairs." + leg, n, dict({"ba." + x: e[leg]["ba100k"][x] for x in keys},
                   **{"carry." + x: e[leg]["carry"][x] for x in keys}))
    k.same_len("swap_pairs", n, {"approx": e["approx"]})
    for x in e["pairs"]:
        key = x["key"]
        try:
            pf = read_json(os.path.join(OUT, "market", x["perp"] + ".json"))
            sf = read_json(os.path.join(OUT, "market", x["swap"] + ".json"))
        except (OSError, ValueError):
            k.ok(False, "swap_pairs %s: leg file missing" % key)
            continue
        fa = dict(zip(pf["dates"], pf["funding_ann"]))
        sa = dict(zip(sf["dates"], sf["fin_long_ann"]))
        for i, ds in enumerate(e["dates"]):
            pb, sb = e["perp"]["ba100k"][key][i], e["swap"]["ba100k"][key][i]
            k.ok((pb is None) == (sb is None), "swap_pairs %s %s: one bid-ask leg without the other" % (key, ds))
            k.ok(all(y is None or 0 <= y <= MAX_SPREAD_BPS + 0.01 for y in (pb, sb)), "swap_pairs %s %s: bid-ask range" % (key, ds))
            # carry on one actual/365 basis: the perpetual as given, the swap's API rate (x 360) restated
            want_p = fa.get(ds)
            want_s = None if sa.get(ds) is None else sa[ds] * 365.0 / 360.0
            for got, want, leg in ((e["perp"]["carry"][key][i], want_p, "perpetual"), (e["swap"]["carry"][key][i], want_s, "swap")):
                k.ok((got is None) == (want is None) and (got is None or abs(got - want) <= 2e-6),
                     "swap_pairs %s %s: %s carry %s, market file gives %s" % (key, ds, leg, got, want))
    labels = [r_[0] for r_ in RANGES]
    if k.ok(list(e.get("range", {})) == labels, "swap_pairs: range keys %s" % list(e.get("range", {}))) and e["dates"]:
        last = date.fromisoformat(e["dates"][-1])
        for label, span_ in RANGES:
            idx = [i for i, ds in enumerate(e["dates"]) if span_ is None or (last - date.fromisoformat(ds)).days < span_]
            for key in keys:
                got = e["range"][label].get(key) or {}
                ba = [(e["perp"]["ba100k"][key][i], e["swap"]["ba100k"][key][i]) for i in idx if e["perp"]["ba100k"][key][i] is not None]
                ca = [(e["perp"]["carry"][key][i], e["swap"]["carry"][key][i]) for i in idx
                      if e["perp"]["carry"][key][i] is not None and e["swap"]["carry"][key][i] is not None]
                want = {"perp_ba100k": median([y[0] for y in ba]), "swap_ba100k": median([y[1] for y in ba]),
                        "perp_carry": median([y[0] for y in ca]), "swap_carry": median([y[1] for y in ca])}
                k.ok(got.get("n") == len(ba), "swap_pairs range %s %s: n %s, dates give %d" % (label, key, got.get("n"), len(ba)))
                for f_, w_ in want.items():
                    g_ = got.get(f_)
                    k.ok((g_ is None) == (w_ is None) and (g_ is None or abs(g_ - w_) <= 0.0051),
                         "swap_pairs range %s %s: %s %s, dates give %s" % (label, key, f_, g_, w_))
    k.ok(not os.path.exists(os.path.join(OUT, "execution_pairs.json")), "execution_pairs.json still present (replaced by swap_pairs.json)")

    # ---- olp_market
    k.as_of("olp_market", olp)
    n = len(olp["dates"])
    k.dates("olp_market", olp["dates"])
    k.same_len("olp_market", n, dict({"total": olp["net_long"]["total"], "matched_share": olp["matched_share"],
               "approx": olp["approx"]}, **{g: olp["net_long"]["group"][g] for g in olp["net_long"]["group"]}))
    k.ok(sorted(olp["net_long"]["group"]) == sorted(GROUPS), "olp_market: group keys")
    k.ok("sources" not in olp, "olp_market: sources present")
    own_dates = []
    for i, ds in enumerate(olp["dates"]):
        own_dates.append(ds)
        k.ok(sum(c(olp["net_long"]["group"][g][i]) for g in GROUPS) == c(olp["net_long"]["total"][i]),
             "olp_market %s: groups do not sum to total" % ds)
        ms = olp["matched_share"][i]
        k.ok(ms is not None and 0 <= ms <= 1, "olp_market %s: matched_share %s" % (ds, ms))
    # the snapshot-daily files share their dates
    k.ok(own_dates == q["dates"] and q["dates"] == e["dates"], "olp_market, quote_depth and swap_pairs dates differ")

    # ---- tradfi
    tf = data["tradfi.json"]
    k.as_of("tradfi", tf)
    n = len(tf["dates"])
    k.dates("tradfi", tf["dates"])
    k.ok(sorted(tf["volume_cls"]) == sorted(TRADFI_CLASSES) and sorted(tf["oi_cls"]) == sorted(TRADFI_CLASSES), "tradfi: class keys")
    k.same_len("tradfi", n, dict({"share_volume": tf["share_volume"], "share_oi": tf["share_oi"], "approx": tf["approx"]},
               **{"v." + x: tf["volume_cls"][x] for x in tf["volume_cls"]}, **{"o." + x: tf["oi_cls"][x] for x in tf["oi_cls"]}))
    for inst_key in ("volume_inst", "oi_inst"):
        if k.ok(sorted(tf.get(inst_key, {})) == ["perp", "swap"], "tradfi: %s keys" % inst_key):
            k.same_len("tradfi." + inst_key, n, tf[inst_key])
    k.ok("sources" not in tf, "tradfi: sources present")
    for label, cls_key, share_key, src in (("volume", "volume_cls", "share_volume", v), ("oi", "oi_cls", "share_oi", o)):
        at = {ds: i for i, ds in enumerate(src["dates"])}
        split_dates = {ds for ds, g in zip(src["dates"], src["group"]["tradfi_perp"]) if g is not None}
        k.ok(split_dates <= set(tf["dates"]), "tradfi: %s split dates missing from the file" % label)
        for i, ds in enumerate(tf["dates"]):
            vals = [tf[cls_key][x][i] for x in TRADFI_CLASSES]
            share = tf[share_key][i]
            if ds not in split_dates:
                inst = tf.get("oi_inst" if label == "oi" else "volume_inst") or {}
                k.ok(share is None and all(x is None for x in vals)
                     and inst.get("perp", [None] * n)[i] is None and inst.get("swap", [None] * n)[i] is None,
                     "tradfi %s: %s present without a split" % (ds, label))
                continue
            j = at[ds]
            part = c(src["group"]["tradfi_perp"][j]) + c(src["group"]["tradfi_swap"][j])
            if not k.ok(all(x is not None and x >= 0 for x in vals), "tradfi %s: %s class null or negative" % (ds, label)):
                continue
            k.ok(sum(c(x) for x in vals) == part, "tradfi %s: %s classes sum to %.2f, TradFi groups are %.2f"
                 % (ds, label, sum(vals), part / 100.0))
            inst = tf.get(label + "_inst") or tf.get("oi_inst" if label == "oi" else "volume_inst") or {}
            ip, isw = inst.get("perp", [None] * n)[i], inst.get("swap", [None] * n)[i]
            k.ok(ip is not None and isw is not None and c(ip) == c(src["group"]["tradfi_perp"][j])
                 and c(isw) == c(src["group"]["tradfi_swap"][j]) and c(ip) + c(isw) == part,
                 "tradfi %s: %s by instrument does not sum to the TradFi total" % (ds, label))
            k.ok(share is not None and 0 <= share <= 1 and ratio_ok(share, part, c(src["total"][j])),
                 "tradfi %s: share of %s %s" % (ds, label, share))
    # seven-day shares: recomputed from the daily figures, null unless all seven calendar days are present
    if k.ok(isinstance(tf.get("share_volume_7d"), list) and isinstance(tf.get("share_oi_7d"), list)
            and len(tf["share_volume_7d"]) == n == len(tf["share_oi_7d"]), "tradfi: seven-day shares missing or wrong length"):
        tf_at = {ds: i for i, ds in enumerate(tf["dates"])}
        v_total = dict(zip(v["dates"], v["total"]))
        for i, ds in enumerate(tf["dates"]):
            d0 = date.fromisoformat(ds)
            win = [(d0 - timedelta(days=x)).isoformat() for x in range(7)]
            js = [tf_at.get(x) for x in win]
            full = all(j is not None for j in js)
            num = den = 0
            okv = oko = full
            sh = []
            if full:
                for x, j in zip(win, js):
                    if tf["volume_inst"]["perp"][j] is None or v_total.get(x) is None:
                        okv = False
                    else:
                        num += c(tf["volume_inst"]["perp"][j]) + c(tf["volume_inst"]["swap"][j])
                        den += c(v_total[x])
                    if tf["share_oi"][j] is None:
                        oko = False
                    else:
                        sh.append(tf["share_oi"][j])
            got_v, got_o = tf["share_volume_7d"][i], tf["share_oi_7d"][i]
            k.ok((got_v is None) == (not okv) and (got_v is None or (0 <= got_v <= 1 and ratio_ok(got_v, num, den))),
                 "tradfi %s: share_volume_7d %s" % (ds, got_v))
            k.ok((got_o is None) == (not oko) and (got_o is None or (0 <= got_o <= 1 and abs(got_o - sum(sh) / 7.0) <= 6e-5)),
                 "tradfi %s: share_oi_7d %s" % (ds, got_o))
    ls = tf["listed"]
    m_ = len(ls["dates"])
    k.dates("tradfi.listed", ls["dates"])
    k.same_len("tradfi.listed", m_, dict({"src": ls["src"]}, **{x: ls[x] for x in TRADFI_CLASSES}))
    k.ok(all(x in ("announcement", "omni") for x in ls["src"]), "tradfi.listed: src values")
    first_omni = ls["src"].index("omni") if "omni" in ls["src"] else m_
    k.ok(all(x == "announcement" for x in ls["src"][:first_omni]) and all(x == "omni" for x in ls["src"][first_omni:]),
         "tradfi.listed: announcement dates after the first snapshot date")
    for x in TRADFI_CLASSES:
        col = ls[x]
        k.ok(all(isinstance(y, int) and y >= 0 for y in col), "tradfi.listed: %s not a count" % x)
        k.ok(all(a <= b for a, b in zip(col[:first_omni], col[1:first_omni])), "tradfi.listed: %s falls in the announcement-built part" % x)
    evd = ls.get("evidence") or {}
    if k.ok(sorted(evd) == ["announcement", "report", "snapshot"], "tradfi.listed: evidence keys"):
        k.same_len("tradfi.listed.evidence", m_, evd)
        for i in range(m_):
            k.ok(sum(evd[x][i] for x in evd) == sum(ls[x][i] for x in TRADFI_CLASSES),
                 "tradfi.listed %s: evidence mix does not sum to the class counts" % ls["dates"][i])
            k.ok(i >= first_omni or evd["snapshot"][i] == 0, "tradfi.listed %s: snapshot evidence before the first snapshot" % ls["dates"][i])
    cfg_l = read_json(os.path.join(CONFIG, "classification.json"))["listings"]
    for t_, e_ in cfg_l.items():
        if e_["group"] in TRADFI_GROUPS:
            l_ = e_.get("listed") or {}
            k.ok(l_.get("evidence") in ("announcement", "report", "snapshot") and DATE.match(str(l_.get("date", "")))
                 and l_.get("source"), "classification %s: listing evidence missing" % t_)
    for i in range(first_omni, m_):
        want = tf_listed.get(ls["dates"][i], {x: 0 for x in TRADFI_CLASSES})
        k.ok(all(ls[x][i] == want[x] for x in TRADFI_CLASSES), "tradfi.listed %s: counts do not match the market files" % ls["dates"][i])

    # ---- concentration
    cc = data["concentration.json"]
    k.as_of("concentration", cc)
    k.dates("concentration", cc["dates"])
    k.same_len("concentration", len(cc["dates"]), {"top10_share": cc["top10_share"], "approx": cc["approx"]})
    for ds, share in zip(cc["dates"], cc["top10_share"]):
        vs = sorted(vols.get(ds, []), reverse=True)
        k.ok(ratio_ok(share, sum(vs[:10]), sum(vs)) and (share is None or 0 < share <= 1),
             "concentration %s: top10_share %s, market files give %s" % (ds, share, sum(vs[:10]) / float(sum(vs)) if sum(vs) else None))
    k.ok(cc["dates"] == q["dates"], "concentration: dates differ from the other snapshot-daily files")

    # ---- spread_majors
    sm = data["spread_majors.json"]
    k.as_of("spread_majors", sm)
    k.ok(sm["sources"] == ["omni"], "spread_majors: sources")
    k.ok(sorted(sm["ba100k_bps"]) == ["BTC", "ETH", "HYPE", "SOL"], "spread_majors: tickers")
    k.dates("spread_majors", sm["dates"])
    k.same_len("spread_majors", len(sm["dates"]), dict({"src": sm["src"], "approx": sm["approx"]},
               **{x: sm["ba100k_bps"][x] for x in sm["ba100k_bps"]}))
    for x in sm["ba100k_bps"]:
        try:
            mf = read_json(os.path.join(OUT, "market", x + "-PERP.json"))
            own = dict(zip(mf["dates"], mf["ba100k_bps"]))
        except (OSError, ValueError):
            own = {}
        for i, ds in enumerate(sm["dates"]):
            val = sm["ba100k_bps"][x][i]
            k.ok(val is None or 0 < val <= MAX_SPREAD_BPS + 0.01, "spread_majors %s %s: %s outside the two-sided range" % (x, ds, val))
            # our own dates repeat the market file's daily median
            k.ok(sm["src"][i] == 0 and val == own.get(ds), "spread_majors %s %s: %s, market file gives %s" % (x, ds, val, own.get(ds)))
    k.ok(sm["dates"] == q["dates"], "spread_majors: own dates differ from the snapshot days")

    # ---- markets_scope
    ms_ = data["markets_scope.json"]
    k.as_of("markets_scope", ms_)
    k.ok(ms_["sources"] == ["own"], "markets_scope: sources %s" % ms_["sources"])
    want_scopes = ["all.all", "all.perp", "all.swap", "crypto.all", "crypto.perp", "tradfi.all", "tradfi.perp", "tradfi.swap"]
    sc = ms_["scopes"]
    if k.ok(list(sc) == want_scopes, "markets_scope: scopes %s" % list(sc)):
        k.ok(sc["crypto.all"] == sc["crypto.perp"], "markets_scope: crypto.all and crypto.perp differ")
        k.ok(sc["all.swap"] == sc["tradfi.swap"], "markets_scope: all.swap and tradfi.swap differ")
        group_bands = {"all.all": ["crypto_majors", "other_crypto", "tradfi_perp", "swap"],
                       "all.perp": ["crypto_majors", "other_crypto", "tradfi_perp"],
                       "crypto.all": ["crypto_majors", "other_crypto"], "crypto.perp": ["crypto_majors", "other_crypto"],
                       "tradfi.all": ["tradfi_perp", "swap"]}
        members = {"all.all": GROUPS, "all.perp": GROUPS[:3], "all.swap": ["tradfi_swap"], "crypto.all": GROUPS[:2],
                   "crypto.perp": GROUPS[:2], "tradfi.all": GROUPS[2:], "tradfi.perp": ["tradfi_perp"], "tradfi.swap": ["tradfi_swap"]}
        mfiles = {}
        for t_ in tick:
            try:
                mfiles[t_] = read_json(os.path.join(OUT, "market", t_ + ".json"))
            except (OSError, ValueError):
                pass
        at = {}
        for name in want_scopes:
            s_ = sc[name]
            nd = len(s_["dates"])
            k.dates("markets_scope." + name, s_["dates"])
            bands = group_bands.get(name) or [x for x in TRADFI_CLASSES if any(
                e_["group"] in members[name] and e_["cls"] == x for e_ in cfg_all.values())]
            k.ok(list(s_["volume"]["bands"]) == bands and list(s_["oi"]["bands"]) == bands, "markets_scope %s: band keys" % name)
            series = dict({"src": s_["src"], "approx": s_["approx"], "v.total": s_["volume"]["total"], "o.total": s_["oi"]["total"],
                           "long": s_["long"], "short": s_["short"], "listed": s_["listed"], "top10": s_["top10_share"],
                           "ls_ratio": s_.get("ls_ratio")},
                          **{"v." + b_: s_["volume"]["bands"][b_] for b_ in s_["volume"]["bands"]},
                          **{"o." + b_: s_["oi"]["bands"][b_] for b_ in s_["oi"]["bands"]})
            fund = s_["funding"]
            swap_scope = members[name] == ["tradfi_swap"]
            k.ok(sorted(fund) == (["long", "short", "tickers"] if swap_scope else ["series", "tickers"]), "markets_scope %s: funding keys" % name)
            tk_ = fund["tickers"]
            k.ok(tk_ == s_["slippage"]["tickers"] and 0 < len(tk_) <= 5 and len(set(tk_)) == len(tk_), "markets_scope %s: tickers %s" % (name, tk_))
            k.ok(all(cfg_all.get(x, {}).get("group") in members[name] for x in tk_), "markets_scope %s: a ticker outside the scope" % name)
            fsets = [fund["long"], fund["short"]] if swap_scope else [fund["series"]]
            for f_ in fsets + [s_["slippage"]["series"]]:
                k.ok(list(f_) == tk_, "markets_scope %s: series keys differ from tickers" % name)
                series.update({"t.%d.%s" % (id(f_) % 997, x): f_[x] for x in f_})
            k.same_len("markets_scope." + name, nd, series)
            k.ok(all(x in range(len(ms_["sources"])) for x in s_["src"]), "markets_scope %s: src index" % name)
            for i, ds in enumerate(s_["dates"]):
                for part in ("volume", "oi"):
                    tot = s_[part]["total"][i]
                    vals = [s_[part]["bands"][b_][i] for b_ in bands]
                    k.ok(tot is None or tot >= 0, "markets_scope %s %s: negative %s" % (name, ds, part))
                    if any(x is not None for x in vals):
                        k.ok(tot is not None and all(x is not None and x >= 0 for x in vals) and sum(c(x) for x in vals) == c(tot),
                             "markets_scope %s %s: %s bands do not sum to the total" % (name, ds, part))
                lg, sh, ot = s_["long"][i], s_["short"][i], s_["oi"]["total"][i]
                k.ok((lg is None) == (sh is None) and (lg is None or (ot is not None and c(lg) + c(sh) == c(ot))),
                     "markets_scope %s %s: long + short != open interest" % (name, ds))
                lsr = s_["ls_ratio"][i]
                k.ok(lsr is None if (lg is None or sh is None or c(sh) == 0) else ratio_ok(lsr, c(lg), c(sh)),
                     "markets_scope %s %s: ls_ratio %s is not long over short" % (name, ds, lsr))
                t10 = s_["top10_share"][i]
                k.ok(t10 is None or 0 < t10 <= 1, "markets_scope %s %s: top10_share %s" % (name, ds, t10))
                # zero is a true count: a snapshot from before a scope's first listing (TradFi on 22 Jan 2026) lists none
                k.ok(s_["listed"][i] is None or (isinstance(s_["listed"][i], int) and s_["listed"][i] >= 0), "markets_scope %s %s: listed" % (name, ds))
                k.ok(s_["volume"]["total"][i] is not None or ot is not None or s_["listed"][i] is not None,
                     "markets_scope %s %s: empty date" % (name, ds))
            at[name] = {ds: i for i, ds in enumerate(s_["dates"])}
            si_ = s_.get("intraday")
            want_keys = ["ts", "oi", "long", "short", "ls_ratio", "funding", "slippage"]
            if k.ok(isinstance(si_, dict) and list(si_) == want_keys, "markets_scope %s: intraday keys" % name):
                nts = len(si_["ts"])
                k.ok(si_["ts"] == intra_ts, "markets_scope %s: intraday ts differ from the market files" % name)
                ifund = si_["funding"]
                k.ok(sorted(ifund) == (["long", "short", "tickers"] if swap_scope else ["series", "tickers"])
                     and ifund["tickers"] == tk_ and si_["slippage"]["tickers"] == tk_, "markets_scope %s: intraday tickers" % name)
                isets = ([("fin_long_ann", ifund["long"]), ("fin_short_ann", ifund["short"])] if swap_scope else [(None, ifund["series"])])
                arrays = {"oi": si_["oi"], "long": si_["long"], "short": si_["short"], "ls_ratio": si_["ls_ratio"]}
                for field, f_ in isets + [("slip100k_bps", si_["slippage"]["series"])]:
                    k.ok(list(f_) == tk_, "markets_scope %s: intraday series keys" % name)
                    arrays.update({"i.%s.%s" % (field, x): f_[x] for x in f_})
                k.same_len("markets_scope.%s intraday" % name, nts, arrays)
                for j, x in enumerate(si_["ts"]):
                    lg_, sh_ = si_["long"][j], si_["short"][j]
                    want_ = [sum(intra_group.get(x, {}).get(g_, [0, 0])[z] for g_ in members[name]) for z in (0, 1)]
                    k.ok(lg_ is not None and sh_ is not None and [c(lg_), c(sh_)] == want_,
                         "markets_scope %s %s: intraday long/short differ from the market files' sum" % (name, x))
                    k.ok(si_["oi"][j] is not None and c(si_["oi"][j]) == c(lg_) + c(sh_), "markets_scope %s %s: intraday oi is not long plus short" % (name, x))
                    k.ok(ratio_ok(si_["ls_ratio"][j], c(lg_), c(sh_)), "markets_scope %s %s: intraday ls_ratio" % (name, x))
                    for field, f_ in isets + [("slip100k_bps", si_["slippage"]["series"])]:
                        for tkr in tk_:
                            pos_, it_ = intra_at.get(tkr, ({}, None))
                            fld = field or ("fin_long_ann" if tkr.endswith("-SWAP") else "funding_ann")
                            want_v = it_[fld][pos_[x]] if x in pos_ else None
                            k.ok(f_[tkr][j] == want_v, "markets_scope %s %s: intraday %s of %s differs from its market file" % (name, x, fld, tkr))
                # daily figure = the intraday point at the day's boundary snapshot, on our own days
                ipos = {x: j for j, x in enumerate(si_["ts"])}
                for ds, rc in recent.items():
                    i = at[name].get(ds)
                    if i is None or s_["src"][i] != 0 or rc["bts"] not in ipos or s_["long"][i] is None:
                        continue
                    j = ipos[rc["bts"]]
                    k.ok(s_["oi"]["total"][i] == si_["oi"][j] and s_["long"][i] == si_["long"][j] and s_["short"][i] == si_["short"][j]
                         and s_["ls_ratio"][i] == si_["ls_ratio"][j],
                         "markets_scope %s %s: daily open interest, long, short or ratio differs from the intraday point at %s" % (name, ds, rc["bts"]))
            # our snapshot days: listings, concentration, funding and slippage repeat the market files
            for ds in q["dates"]:
                i = at[name].get(ds)
                if not k.ok(i is not None, "markets_scope %s: snapshot day %s missing" % (name, ds)):
                    continue
                inscope = [f_ for f_ in mfiles.values() if f_["group"] in members[name] and ds in f_["dates"]]
                k.ok(s_["listed"][i] == len(inscope), "markets_scope %s %s: listed %s, market files %d" % (name, ds, s_["listed"][i], len(inscope)))
                vs = sorted((c(f_["vol24h"][f_["dates"].index(ds)]) for f_ in inscope), reverse=True)
                k.ok(ratio_ok(s_["top10_share"][i], sum(vs[:10]), sum(vs)), "markets_scope %s %s: top10_share %s" % (name, ds, s_["top10_share"][i]))
                for x in tk_:
                    f_ = mfiles.get(x)
                    j = f_["dates"].index(ds) if f_ and ds in f_["dates"] else None
                    if swap_scope:
                        pairs_ = ((fund["long"][x][i], "fin_long_ann"), (fund["short"][x][i], "fin_short_ann"))
                    else:
                        pairs_ = ((fund["series"][x][i], "fin_long_ann" if x.endswith("-SWAP") else "funding_ann"),)
                    for got, field in pairs_:
                        k.ok(got == (f_[field][j] if j is not None else None), "markets_scope %s %s: %s funding %s" % (name, ds, x, got))
                    got = s_["slippage"]["series"][x][i]
                    full = f_["ba100k_bps"][j] if j is not None else None
                    k.ok((got is None) == (full is None) and (got is None or abs(got - full / 2.0) <= 0.0101),
                         "markets_scope %s %s: %s slippage %s, half of %s" % (name, ds, x, got, full))
            # a TradFi scope's listings before the first snapshot never fall
            pre = [x for ds, x in zip(s_["dates"], s_["listed"]) if q["dates"] and ds < q["dates"][0] and x is not None]
            k.ok(all(a <= b for a, b in zip(pre, pre[1:])), "markets_scope %s: listed falls before the first snapshot" % name)
            k.ok(name.startswith("tradfi") or name == "all.swap" or not pre, "markets_scope %s: listed before the first snapshot" % name)
            k.ok(all(x == 0 for x in s_["src"]), "markets_scope %s: a date not from this site's own sources" % name)

        def val(name, part, ds):
            i = at[name].get(ds)
            return None if i is None else c(sc[name][part]["total"][i]) if sc[name][part]["total"][i] is not None else None

        aa = sc["all.all"]
        for part, src in (("volume", v), ("oi", o)):
            ref = {ds: x for ds, x in zip(src["dates"], src["total"]) if x is not None}
            k.ok(set(ref) <= set(aa["dates"]), "markets_scope all.all: %s dates missing" % part)
            for ds, x in ref.items():
                k.ok(val("all.all", part, ds) == c(x), "markets_scope all.all %s: %s total differs from %s_daily" % (ds, part, part))
            for ds in aa["dates"]:
                a_, pp, ss = val("all.all", part, ds), val("all.perp", part, ds), val("all.swap", part, ds)
                cr, tfa, tp_, ts_ = val("crypto.all", part, ds), val("tradfi.all", part, ds), val("tradfi.perp", part, ds), val("tradfi.swap", part, ds)
                k.ok(pp is None or a_ is None or pp + (ss or 0) == a_, "markets_scope %s: all.perp + all.swap != all.all (%s)" % (ds, part))
                k.ok(cr is None or tfa is None or a_ is None or cr + tfa == a_, "markets_scope %s: crypto + tradfi != all (%s)" % (ds, part))
                k.ok(tfa is None or tp_ is None or tp_ + (ts_ or 0) == tfa, "markets_scope %s: tradfi.perp + tradfi.swap != tradfi.all (%s)" % (ds, part))
                k.ok((tfa is None) == (tp_ is None), "markets_scope %s: tradfi.all without tradfi.perp (%s)" % (ds, part))
        for ds, lg, sh in zip(o["dates"], o["long"], o["short"]):
            i = at["all.all"].get(ds)
            k.ok(i is None or (aa["long"][i] == lg and aa["short"][i] == sh), "markets_scope all.all %s: long/short differ from oi_daily" % ds)
        tfj = {ds: i for i, ds in enumerate(tf["dates"])}
        for ds, i in tfj.items():
            for part, inst in (("volume", "volume_inst"), ("oi", "oi_inst")):
                if tf[inst]["perp"][i] is not None:
                    k.ok(val("tradfi.perp", part, ds) == c(tf[inst]["perp"][i]) and (val("tradfi.swap", part, ds) or 0) == c(tf[inst]["swap"][i]),
                         "markets_scope %s: TradFi %s by instrument differs from tradfi.json" % (ds, part))
        cc_at = dict(zip(data["concentration.json"]["dates"], data["concentration.json"]["top10_share"]))
        for ds, x in cc_at.items():
            k.ok(aa["top10_share"][at["all.all"][ds]] == x, "markets_scope all.all %s: top10_share differs from concentration.json" % ds)

    # ---- events
    ev = data["events.json"]
    k.ok(isinstance(ev, list) and 0 < len(ev) <= 5, "events: must be a list of at most five")
    cited = {(e_["date"], e_["label"]): e_ for e_ in read_json(os.path.join(CONFIG, "events.json"))["events"]}
    if isinstance(ev, list):
        for x in ev:
            k.ok(sorted(x) == ["date", "label"] and DATE.match(x["date"]) and len(x["label"].split()) <= 3,
                 "events: bad entry %s" % x)
            src_ = cited.get((x.get("date"), x.get("label")), {})
            k.ok(src_.get("verified") == "direct" and str(src_.get("source", "")).startswith("https://"),
                 "events: %s has no directly dated source in config/events.json" % x.get("label"))

    # ---- meta
    me = data["meta.json"]
    k.ok("fixture" not in me, "meta: fixture file still in place")
    base_keys = ["built_at", "data_as_of", "publication_date", "article_url", "split_start",
                 "continuous_start", "stale_after_minutes", "sources"]
    k.ok(list(me) == base_keys, "meta: keys %s" % list(me))
    for name, d_ in (("olp_market", olp), ("tradfi", tf)):
        k.ok("sources" not in d_ and "src" not in d_, "%s: source tags present" % name)
    k.ok(sorted(me["sources"]) == sorted(["omni", "onchain", "onchain_block", "reports",
                                           "defillama", "polymarket", "hyperliquid", "telegram"]), "meta: source keys")
    for x in ("omni", "defillama"):
        k.ok(isinstance(me["sources"][x], str) and TIME.match(me["sources"][x]), "meta: sources.%s" % x)
    k.ok(me["data_as_of"] == me["sources"]["omni"] == m["as_of"], "meta: data_as_of does not match markets_latest")
    first_split = next((d for d, g in zip(v["dates"], v["group"]["crypto_majors"]) if g is not None), None)
    k.ok(me["split_start"] == first_split, "meta: split_start %s, first split %s" % (me["split_start"], first_split))
    k.ok(me["publication_date"] == "2026-11-24" and me["stale_after_minutes"] == 120, "meta: constants")

    # ---- daily files hold finished UTC days only
    today = me["built_at"][:10]
    for name, d in (("volume_daily", v), ("oi_daily", o), ("quote_depth", q), ("markets_listed", ml)):
        k.ok(d["dates"][-1] < today, "%s: last date %s is not a finished UTC day" % (name, d["dates"][-1]))
    return k
