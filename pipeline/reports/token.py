"""token.json: Polymarket-implied FDV and launch date, weekly points, allocation."""
from __future__ import annotations

import math
import re
from datetime import datetime

from sources import docs, polymarket

from . import parse
from .util import iso_z, money, parse_time, utc_now

POINTS = re.compile(
    r"For week (?P<week>\d+) of the Omni points program, (?P<points>[\d,]+) points "
    r"were distributed across (?P<accounts>[\d,]+) accounts")


# ------------------------------------------------------------------- median

def implied_median(strikes: list[tuple[float, float]]):
    """Valuation at a 50% probability, log-interpolated between two strikes.

    strikes: (strike, probability FDV exceeds it). Looks, from the lowest strike
    up, for the first adjacent pair whose probabilities bracket 0.5 and
    interpolates the probability linearly in log(strike). None when 0.5 is not
    bracketed by two listed strikes.
    """
    pts = sorted((k, p) for k, p in strikes if p is not None)
    for (k_lo, p_lo), (k_hi, p_hi) in zip(pts, pts[1:]):
        if p_lo >= 0.5 >= p_hi:
            if p_lo == p_hi:                      # both exactly 0.5
                return k_lo
            w = (p_lo - 0.5) / (p_lo - p_hi)
            return math.exp(math.log(k_lo) + w * (math.log(k_hi) - math.log(k_lo)))
    return None


def _strike(title: str) -> float:
    number, _ = parse.parse_amount(title)
    if number is None or number <= 0:
        raise ValueError("cannot read FDV strike from %r" % title)
    return float(number)


def _launch_date(title: str):
    return datetime.strptime(title.strip(), "%B %d, %Y").date()


def _series(daily: dict, market: dict, today: str, fetched_day: str):
    """Daily closes of one market plus today's provisional value."""
    entry = daily["markets"].get(market["id"], {"dates": [], "p": []})
    out = dict(zip(entry["dates"], entry["p"]))
    if market["p"] is not None and fetched_day not in out:
        out[fetched_day] = round(market["p"], 4)   # the day of the latest fetch, still open
    return out


# -------------------------------------------------------------------- build

def build(store, posts: list[dict], today: str | None = None, use_clob: bool = False) -> dict:
    now = utc_now()
    today = today or now.strftime("%Y-%m-%d")
    fdv_event = polymarket.load_event(store, polymarket.EVENT_FDV)
    launch_event = polymarket.load_event(store, polymarket.EVENT_LAUNCH)
    if fdv_event is None or launch_event is None:
        raise ValueError("Polymarket events are not in the store; run without --offline once")

    fdv_markets = [m for m in fdv_event["markets"] if not m["closed"] and m["p"] is not None]
    launch_markets = [m for m in launch_event["markets"] if not m["closed"] and m["p"] is not None]
    daily = polymarket.update_daily(store, fdv_markets + launch_markets, today, use_clob=use_clob)

    # ---- FDV strikes, now
    strikes = sorted(({"label": m["title"], "strike": money(_strike(m["title"])), "p": round(m["p"], 4), "_m": m}
                      for m in fdv_markets), key=lambda s: s["strike"])
    median = implied_median([(s["strike"], s["p"]) for s in strikes])

    # ---- FDV median, daily
    fdv_day = fdv_event["fetched_at"][:10]
    per_strike = [(s["strike"], _series(daily, s["_m"], today, fdv_day)) for s in strikes]
    dates = sorted({d for _, series in per_strike for d in series})
    medians = []
    for d in dates:
        value = implied_median([(k, series[d]) for k, series in per_strike if d in series])
        medians.append(money(value) if value is not None else None)

    # ---- launch markets still open
    launch = []
    launch_day = launch_event["fetched_at"][:10]
    for m in sorted(launch_markets, key=lambda m: _launch_date(m["title"])):
        when = _launch_date(m["title"])
        series = _series(daily, m, today, launch_day)
        ds = sorted(series)
        launch.append({
            "label": "%d %s %d" % (when.day, when.strftime("%b"), when.year),
            "date": when.isoformat(),
            "p": round(m["p"], 4),
            "dates": ds,
            "history": [series[d] for d in ds],
        })

    # ---- weekly points
    points = []
    for post in posts:
        m = POINTS.search(post["text"])
        if not m:
            continue
        points.append({"date": post["time"][:10], "week": int(m.group("week")),
                       "accounts": int(m.group("accounts").replace(",", "")),
                       "points": int(m.group("points").replace(",", ""))})
    points.sort(key=lambda r: r["week"])
    weeks = [r["week"] for r in points]
    if len(set(weeks)) != len(weeks):
        raise ValueError("a points week is announced twice: %s" % weeks)

    # ---- allocation
    text, meta = docs.load(store)
    if text is None:
        raise ValueError("docs are not in the store; run without --offline once")
    allocation = docs.allocation(text)

    newest = max([parse_time(fdv_event["fetched_at"]), parse_time(launch_event["fetched_at"])]
                 + ([parse_time(max(p["time"] for p in posts))] if posts else []))
    return {
        "as_of": iso_z(newest),
        "fdv": {
            "strikes": [{"label": s["label"], "strike": s["strike"], "p": s["p"]} for s in strikes],
            "median": money(median) if median is not None else None,
            "history": {"dates": dates, "median": medians},
        },
        "launch": launch,
        "points": points,
        "allocation": allocation,
    }


def check(token: dict) -> list[str]:
    errors = []
    if sorted(token.keys()) != ["allocation", "as_of", "fdv", "launch", "points"]:
        return ["token: top-level keys %s" % sorted(token.keys())]
    fdv = token["fdv"]
    strikes = fdv["strikes"]
    if not strikes:
        errors.append("token: no FDV strikes")
    for s in strikes:
        if sorted(s.keys()) != ["label", "p", "strike"] or not 0 <= s["p"] <= 1:
            errors.append("token: bad strike %s" % s)
    if [s["strike"] for s in strikes] != sorted(s["strike"] for s in strikes):
        errors.append("token: strikes not ascending")
    want = implied_median([(s["strike"], s["p"]) for s in strikes])
    want = money(want) if want is not None else None
    if fdv["median"] != want:
        errors.append("token: median %s does not follow from the strikes (%s)" % (fdv["median"], want))
    hist = fdv["history"]
    if len(hist["dates"]) != len(hist["median"]) or hist["dates"] != sorted(set(hist["dates"])):
        errors.append("token: FDV history arrays malformed")
    if hist["dates"] and hist["dates"][-1] == token["as_of"][:10] and hist["median"][-1] != fdv["median"]:
        errors.append("token: last history point differs from the headline median")
    for m in token["launch"]:
        if not {"label", "p", "dates", "history"} <= set(m) or len(m["dates"]) != len(m["history"]):
            errors.append("token: launch market malformed %s" % m.get("label"))
        elif any(v is not None and not 0 <= v <= 1 for v in m["history"]) or not 0 <= m["p"] <= 1:
            errors.append("token: launch probability out of range %s" % m["label"])
        elif m["dates"] and m["dates"][-1] == token["as_of"][:10] and m["history"][-1] != m["p"]:
            errors.append("token: launch history end differs from p (%s)" % m["label"])
    weeks = [r["week"] for r in token["points"]]
    if not weeks or weeks != list(range(weeks[0], weeks[0] + len(weeks))):
        errors.append("token: points weeks are not consecutive")
    for r in token["points"]:
        if sorted(r.keys()) != ["accounts", "date", "points", "week"] or r["accounts"] <= 0 or r["points"] <= 0:
            errors.append("token: bad points row %s" % r)
    if abs(sum(a["pct"] for a in token["allocation"]) - 1) > 1e-9:
        errors.append("token: allocation does not sum to 1")
    return errors
