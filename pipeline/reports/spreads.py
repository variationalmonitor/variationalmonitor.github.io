"""spreads_daily.json and the on-chain view of each report period.

Input: site/data/treasury.json from the on-chain package ({as_of, dates, inflow, …}:
USDC the Protocol Treasury received per UTC day). A report period's days are
start + 1 … end, the convention of the on-chain builder's residual table.

Implied spreads paid for a day = that day's treasury inflow ÷ the treasury share
in force that day. The share is never a constant: it is the `treasury_share`
of the report period whose days contain the date (reported Protocol Treasury
inflow ÷ reported spreads paid, 4 decimals).

How a change of share is dated: the reports state a share only per two-week
period, so a new share applies to every day of the first period that reports
it (from that period's start + 1). Days after the newest report use the newest
period's share until the next report covers them; if that report states a
different share, those days are recomputed with it.

The series is published from 2026-04-17 only: the share is a steady 20% from
the period ending that day, after 10% in March and a mixed 16.9% in the period
ending 3 Apr.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from .util import money, parse_date, read_json_or_none

SERIES_START = "2026-04-17"
TOLERANCE = 0.05            # implied daily sum against reported spreads paid, per period


def load_treasury(site_data: Path):
    """({date: inflow} over complete UTC days, as_of) or (None, reason)."""
    doc = read_json_or_none(site_data / "treasury.json")
    if not isinstance(doc, dict):
        return None, "treasury.json missing"
    if doc.get("fixture") is True:
        return None, "treasury.json is a frontend fixture"
    dates, inflow, as_of = doc.get("dates"), doc.get("inflow"), doc.get("as_of")
    if not isinstance(dates, list) or not isinstance(inflow, list) or len(dates) != len(inflow) or not isinstance(as_of, str):
        return None, "treasury.json has no daily inflow series"
    days = {}
    for d, v in zip(dates, inflow):
        if d == as_of[:10]:                 # the day of as_of is still in progress
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            days[d] = v
    return days, as_of


def period_days(start: str, end: str) -> list[str]:
    """The UTC days of a report period: start + 1 … end."""
    a, b = parse_date(start), parse_date(end)
    return [(a + timedelta(days=i)).isoformat() for i in range(1, (b - a).days + 1)]


def period_inflow(days: dict | None, start: str, end: str):
    """On-chain treasury inflow over the period's days; None unless every day is covered."""
    if days is None:
        return None
    wanted = period_days(start, end)
    if not wanted or any(d not in days for d in wanted):
        return None
    return money(sum(days[d] for d in wanted))


def build(fin: dict, site_data: Path):
    """Returns (spreads_daily.json object, validation rows). Raises ValueError without a treasury series.

    A validation row is (start, end, reported spreads paid, implied sum, difference as a fraction),
    for every report period whose days all lie on or after SERIES_START.
    """
    days, as_of = load_treasury(site_data)
    if days is None:
        raise ValueError(as_of)
    periods = fin["periods"]
    share_of = {}
    for p in periods:
        for d in period_days(p["start"], p["end"]):
            share_of[d] = p["treasury_share"]
    latest_end, latest_share = periods[-1]["end"], periods[-1]["treasury_share"]
    dates, implied, share = [], [], []
    for d in sorted(days):
        if d < SERIES_START:
            continue
        s = share_of.get(d, latest_share if d > latest_end else None)
        if not s:
            raise ValueError("no treasury share for %s" % d)
        dates.append(d)
        implied.append(money(days[d] / s))
        share.append(s)
    if not dates:
        raise ValueError("treasury.json has no day on or after %s" % SERIES_START)
    for a, b in zip(dates, dates[1:]):
        if (parse_date(b) - parse_date(a)).days != 1:
            raise ValueError("treasury.json skips from %s to %s" % (a, b))
    doc = {"as_of": as_of, "dates": dates, "implied": implied, "share": share}
    return doc, validation(doc, fin)


def validation(doc: dict, fin: dict) -> list[tuple]:
    by_day = dict(zip(doc["dates"], doc["implied"]))
    rows = []
    for p in fin["periods"]:
        wanted = period_days(p["start"], p["end"])
        if wanted[0] < SERIES_START or any(d not in by_day for d in wanted):
            continue
        total = money(sum(by_day[d] for d in wanted))
        rows.append((p["start"], p["end"], p["spreads_paid"], total, round(total / p["spreads_paid"] - 1, 4)))
    return rows


def check(doc: dict, fin: dict, site_data: Path) -> list[str]:
    errors = []
    if not isinstance(doc, dict) or list(doc.keys()) != ["as_of", "dates", "implied", "share"]:
        return ["spreads_daily.json missing or malformed"]
    n = len(doc["dates"])
    if not n or len(doc["implied"]) != n or len(doc["share"]) != n:
        return ["spreads_daily: arrays differ in length"]
    if doc["dates"][0] < SERIES_START:
        errors.append("spreads_daily: starts before %s" % SERIES_START)
    if any(v is None or v < 0 for v in doc["implied"]) or any(not s or not 0 < s <= 1 for s in doc["share"]):
        errors.append("spreads_daily: implied must be non-negative and share a fraction")
    try:
        want, rows = build(fin, site_data)
    except ValueError as exc:
        return errors + ["spreads_daily: %s" % exc]
    if want != doc:
        errors.append("spreads_daily.json differs from a rebuild from treasury.json and financials.json")
    rows = validation(doc, fin)
    if not rows:
        errors.append("spreads_daily: no report period is covered, nothing validates the series")
    for start, end, reported, total, diff in rows:
        if abs(diff) > TOLERANCE:
            errors.append("spreads_daily: %s to %s implied %s against reported %s (%+.2f%%, tolerance %.0f%%)"
                          % (start, end, total, reported, diff * 100, TOLERANCE * 100))
    return errors


def table(rows: list[tuple]) -> str:
    lines = ["  %-10s  %-10s  %14s  %16s  %7s" % ("start", "end", "reported", "implied daily sum", "diff")]
    for start, end, reported, total, diff in rows:
        lines.append("  %-10s  %-10s  %14s  %16s  %+6.2f%%" % (start, end, "{:,}".format(reported), "{:,.2f}".format(total), diff * 100))
    if rows:
        rep, imp = sum(r[2] for r in rows), sum(r[3] for r in rows)
        lines.append("  %-10s  %-10s  %14s  %16s  %+6.2f%%" % ("all", "%d periods" % len(rows), "{:,}".format(rep), "{:,.2f}".format(imp), (imp / rep - 1) * 100))
    return "\n".join(lines)
