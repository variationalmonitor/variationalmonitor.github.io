"""TradFi listings over time: each listing dated by the earliest primary-source evidence of it.

One rule for every TradFi listing, in this order of preference:
  announcement  its own listing post in Variational's channel ("... perps are now live ...",
                "A swap for ... is now live ...", "The first swap markets (...) are now live")
  report        else the first Variational update that names it among markets "listed"
                (29 May 2026: "... have been listed since the launch of Phase 1";
                 12 Jun 2026: "28 ... markets have been listed ...: Gold, Silver, ...")
  snapshot      else the first stored Omni snapshot day that contains it

A listing first seen once continuous capture had begun (CONTINUOUS_START, a snapshot every half hour) is
dated by the earliest of the three instead, its snapshot evidence being the UTC day of the first raw snapshot
that lists it: it has a date on its first day, an earlier post moves it back and a later one leaves it. Before
continuous capture the store holds only scattered seed and archived snapshots, so a first snapshot there is a
late bound and the order of preference above stands, with the snapshot day being the day that snapshot closes.

Crypto listings are dated the same way from their own announcement and their snapshots (reports name
TradFi markets only). A listing not yet in `config/classification.json` is dated at build time by the same
function, with the entry `--classify` would add for it (see `listing_dates`).

A named market maps to a ticker only when exactly one TradFi listing fits, among listings that
were not announced after the post (a market announced later cannot be the one named). It fits when
the item is the ticker symbol (which settles it: "QQQ" is QQQ, not the fund whose name ends in
QQQ), or the API's `name` starts with the item, or ends with it, on a word boundary
("WTI Crude" -> "WTI Crude Oil", "Korea ETF" -> "iShares MSCI South Korea ETF").
Anything else stays on its first snapshot date ("Semiconductor 3x ETF", "SpaceX").

Before the first snapshot the count is cumulative by evidence date and never subtracts, so it is a
lower bound: a market that opened without a post and was never named is missing until the first
snapshot. From the first snapshot on the count is what the snapshot lists. The evidence of each
listing is stored in `config/classification.json` (`listed`) by `build_market.py --classify`.
"""
import os
import re

from . import store
from .common import CONTINUOUS_START, ROOT, STORE, TRADFI_CLASSES, TRADFI_GROUPS, read_json

_LIVE = re.compile(r"(perps?|swaps?|swap markets?)\b[^\n]*?\b(?:are|is) now live|swap for[^\n]*?is now live", re.I)
_DOLLAR = re.compile(r"\$([A-Za-z0-9_]+)")
_URL = re.compile(r"omni\.variational\.io/(perpetual|swap)/([A-Za-z0-9_]+)")
_ENUM = re.compile(r"\b(?:have been|has been|were|was) listed\b", re.I)
POST_URL = "https://t.me/variational_io/%s"


def load_posts():
    for path in (os.path.join(STORE, "telegram", "posts.json"), os.path.join(ROOT, ".cache", "telegram", "posts.json")):
        if os.path.exists(path):
            d = read_json(path)
            return (d["posts"] if isinstance(d, dict) else d), os.path.relpath(path, ROOT)
    return [], None


def announcements(listings):
    """[{id, date, kind, mapped: [ticker], unmapped: [token]}] for every listing announcement."""
    posts, source = load_posts()
    out = []
    for p in posts:
        body = (p.get("text") or "").split("X (formerly Twitter)")[0]   # drop the mirrored X card
        m = _LIVE.search(body)
        if not m:
            continue
        swap = "swap" in m.group(0).lower()
        suffix = "-SWAP" if swap else "-PERP"
        tokens = _DOLLAR.findall(body[:m.end()])
        cands = [t.upper() for t in tokens]
        for kind, sym in _URL.findall(body):
            if (kind == "swap") != swap:
                continue
            sym = sym.upper()
            cands.append(sym)
            if swap and sym[-1:] in ("S", "P"):       # the app's swap symbols carry a suffix (US500S, USOILP)
                cands.append(sym[:-1])
        mapped = sorted({c + suffix for c in cands if c + suffix in listings})
        bases = {t.rsplit("-", 1)[0] for t in mapped}
        out.append({"id": p.get("id"), "date": str(p.get("time"))[:10], "kind": "swap" if swap else "perp",
                    "mapped": mapped, "unmapped": sorted({t.upper() for t in tokens} - bases)})
    out.sort(key=lambda a: (a["date"], a["id"] or 0))
    return out, source


def _items(sentence):
    """Market names in a sentence that says they were listed."""
    s = sentence.strip().lstrip("•◦-* ").rstrip(".")
    m = _ENUM.search(s)
    part = s.split(":", 1)[1] if ":" in s and m and m.start() < s.index(":") else s[:m.start()]
    part = re.split(r"\.\s", part)[0]          # the list ends with its sentence
    out = []
    for raw in re.split(r",|\band\b", part):
        item = raw.strip()
        sym = re.search(r"\(([A-Za-z0-9_]+)\)", item)
        item = re.sub(r"\s*\([^)]*\)", "", item).strip()
        item = re.sub(r"\s+swaps?$|\s+perps?$", "", item, flags=re.I)
        if item:
            out.append((item, sym.group(1).upper() if sym else None))
    return out


def _fits(item, sym, ticker, name):
    """2 when the item is the ticker symbol itself, 1 when the API name starts or ends with it, else 0."""
    base = ticker.rsplit("-", 1)[0]
    if item.upper() == base or (sym and sym == base):
        return 2
    n = (name or "").replace("Swap on ", "").lower()
    it = re.sub(r"\bco$", "company", item.lower())
    if n.startswith(it) and (len(n) == len(it) or not n[len(it)].isalnum()):
        return 1
    if n.endswith(it) and (len(n) == len(it) or not n[-len(it) - 1].isalnum()):
        return 1
    return 0


def report_mentions(listings, announced):
    """[{id, date, mapped: {item: ticker}, unmapped: [item]}] for updates that enumerate listed markets."""
    posts, _ = load_posts()
    out = []
    for p in posts:
        body = (p.get("text") or "").split("X (formerly Twitter)")[0]
        date = str(p.get("time"))[:10]
        rec = {"id": p.get("id"), "date": date, "mapped": {}, "unmapped": []}
        for line in body.split("\n"):
            if not _ENUM.search(line) or _LIVE.search(line):
                continue
            for item, sym in _items(line):
                score = {tk: _fits(item, sym, tk, e.get("name")) for tk, e in listings.items()
                         if e["group"] in TRADFI_GROUPS and announced.get(tk, "0000") <= date}
                exact = [tk for tk, v in score.items() if v == 2]
                # the ticker symbol itself settles it; otherwise exactly one name may fit
                fits = exact if len(exact) == 1 else [tk for tk, v in score.items() if v]
                if len(fits) == 1:
                    rec["mapped"][item] = fits[0]
                else:
                    rec["unmapped"].append(item)
        if rec["mapped"] or rec["unmapped"]:
            out.append(rec)
    out.sort(key=lambda r: (r["date"], r["id"] or 0))
    return out


def evidence(listings, days, seen):
    """{ticker: {date, evidence, source}} for every listing, by the rule in the module docstring.

    Crypto listings get the same treatment (their own announcement, else the first snapshot day),
    which is what the markets table shows as the listing date; only TradFi listings are counted
    in `tradfi.json`. `seen` is the store's listing register (listings_seen.json): `first` is the
    first stored raw snapshot that listed each ticker.
    """
    ann, _ = announcements(listings)
    by_ann = {}
    for a in ann:
        for tk in a["mapped"]:
            by_ann.setdefault(tk, a)
    announced = {tk: a["date"] for tk, a in by_ann.items()}
    reports = report_mentions(listings, announced)
    by_rep = {}
    for r in reports:
        for tk in r["mapped"].values():
            by_rep.setdefault(tk, r)
    first_day = {}
    for d in sorted(days):
        for tk in days[d]["rows"]:
            first_day.setdefault(tk, d)
    platform = None
    out = {}
    for tk, e in listings.items():
        posted = [{"date": p["date"], "evidence": k, "source": POST_URL % p["id"]}
                  for k, p in (("announcement", by_ann.get(tk)), ("report", by_rep.get(tk))) if p]
        first = (seen.get(tk) or {}).get("first")
        if first and first >= CONTINUOUS_START:
            # first seen by the half-hourly capture: the earliest evidence, a tie going to the post
            if platform is None:
                platform = store.load_platform()
            raw = {"date": first[:10], "evidence": "snapshot",
                   "source": (platform.get(first) or {}).get("src") or "snapshot " + first}
            out[tk] = min(posted + [raw], key=lambda x: x["date"])
        elif posted:
            out[tk] = posted[0]
        elif tk in first_day:
            d = first_day[tk]
            out[tk] = {"date": d, "evidence": "snapshot", "source": days[d]["raw"]["boundary_src"]}
        elif tk in seen:
            out[tk] = {"date": seen[tk]["first"][:10], "evidence": "snapshot", "source": "snapshot " + seen[tk]["first"]}
    return out, ann, reports


def listing_dates(classifier, days, seen):
    """Every listing seen and the evidence that dates it: one function for the build and for --classify.

    Returns (listings, dated, computed, announcements, reports). `listings` is classification.json's
    entries plus, for a seen listing not in the file yet, the entry --classify would add for it;
    `computed` is `evidence` over all of them; `dated` is the stored `listed` where the file has one,
    else the computed evidence, which is what --classify stores.
    """
    listings = classifier.every_listing()
    computed, ann, reports = evidence(listings, days, seen)
    dated = {tk: e.get("listed") or computed.get(tk) for tk, e in listings.items()}
    return listings, {tk: v for tk, v in dated.items() if v}, computed, ann, reports


def build_listed(days, classifier, seen):
    """`listed` for tradfi.json, the figures the build report prints and the listing evidence of every listing."""
    listings, every_ev, computed, ann, reports = listing_dates(classifier, days, seen)
    ev = {tk: every_ev[tk] for tk in computed if listings[tk]["group"] in TRADFI_GROUPS}
    stale = sorted(tk for tk in ev if listings[tk].get("listed") not in (None, computed[tk]))
    not_in_config = sorted(tk for tk in ev if tk in classifier.listings and not listings[tk].get("listed"))
    first_omni = min(days) if days else "9999-12-31"
    kinds = ["announcement", "report", "snapshot"]
    out = dict({"dates": [], "src": []}, **{c: [] for c in TRADFI_CLASSES},
               evidence={k: [] for k in kinds})

    def row(date, src, live):
        out["dates"].append(date)
        out["src"].append(src)
        for c in TRADFI_CLASSES:
            out[c].append(sum(1 for tk in live if listings[tk]["cls"] == c))
        for k in kinds:
            out["evidence"][k].append(sum(1 for tk in live if ev[tk]["evidence"] == k))

    dated = sorted((e["date"], tk) for tk, e in ev.items() if e["evidence"] != "snapshot" and e["date"] < first_omni)
    live = set()
    for i, (d, tk) in enumerate(dated):
        live.add(tk)
        if i + 1 == len(dated) or dated[i + 1][0] != d:
            row(d, "announcement", live)
    before = set(live)
    omni_first = None
    for d in sorted(days):
        now = {tk for tk, r in days[d]["rows"].items() if r["sgroup"] in TRADFI_GROUPS and tk in ev}
        if omni_first is None:
            omni_first = now
        row(d, "omni", now)
    all_mapped = {tk for a in ann for tk in a["mapped"]}
    info = {
        "source": load_posts()[1], "posts": len(ann), "first_date": ann[0]["date"] if ann else None,
        "tickers_mapped": len(all_mapped),
        "unmapped_tokens": sorted({(a["date"], t) for a in ann for t in a["unmapped"]}),
        "posts_without_ticker": [(a["date"], a["id"]) for a in ann if not a["mapped"]],
        "reports": reports,
        "by_evidence": {k: sum(1 for e in ev.values() if e["evidence"] == k) for k in kinds},
        "before_first_snapshot": len(before),
        "before_by_evidence": {k: sum(1 for tk in before if ev[tk]["evidence"] == k) for k in kinds},
        "first_snapshot_date": first_omni if days else None,
        "first_snapshot_count": len(omni_first) if omni_first is not None else None,
        "step": sorted(omni_first - before) if omni_first is not None else [],
        "dated_but_not_in_first_snapshot": sorted(before - omni_first) if omni_first is not None else [],
        "config_stale": stale, "not_in_config": not_in_config,
    }
    return out, info, every_ev


def listed_date(listed, first_day):
    """Date a listing first appeared, for the markets table, from its evidence: None when it is not known.

    A listing whose only evidence is the first stored snapshot day was already live when capture
    began, so its date is unknown.
    """
    if not listed or (listed["evidence"] == "snapshot" and (first_day is None or listed["date"] <= first_day)):
        return None
    return listed["date"]
