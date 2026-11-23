"""financials.json: the two-week statement, from the full biweekly updates.

Lines taken as printed:  spreads paid, rewards, Protocol Treasury inflow, OLP PnL.
Lines derived so the contract identities hold exactly:
    net_profit  = treasury_inflow + olp_pnl
    net_revenue = net_profit + rewards
    mm_costs    = spreads_paid - net_revenue
Each derived line is compared with the printed one; any difference, a line the
post does not print, a disagreement between the X and Telegram posts, and any
gap between the change in lifetime OLP PnL and the period's OLP PnL goes into
the period's `notes`. Nothing is rounded away and signs are kept: OLP PnL is
negative when the post prints it in parentheses.

`volume` is the change in reported cumulative volume from the previous report
(of any format). When the previous report is not 13 to 15 days earlier the
period has no reported starting figure: volume is null and the window is the
14 days the post itself states ("2w").

`olp_bps` is the period's OLP PnL per dollar of that volume (negative in a loss
period, null without volume). `olp_summary` restates the latest period's OLP
PnL, the lifetime figure of the latest report, and how many reported periods
had OLP PnL above zero.

Each period also carries its ratios to spreads paid (`net_revenue_margin`,
`net_profit_margin`, `rewards_share`, `treasury_share`; fractions, 4 decimals),
`net_profit_bps`, the lifetime OLP figure stated in that update, the update's
`source_url`, and `treasury_inflow_onchain`: USDC the Protocol Treasury received
on-chain over the period's UTC days (start + 1 … end), null where the on-chain
series does not cover every one of those days. `cumulative` holds running
totals from the first period; `annualised` is the latest period times 26.

`notes` is one string per period (sentences joined by a space), or null.
"""
from __future__ import annotations

import re
from datetime import timedelta

from .util import money, parse_date, usd

MIN_DAYS, MAX_DAYS = 13, 15
STATED_WINDOW_DAYS = 14          # every period line is labelled "2w"
FINE_PRINT = 10_000_000          # cumulative volume is normally printed to $0.01B
_SCALE = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def _print_step(printed: str) -> float:
    """Size of the last printed digit: '$229.8B' -> 1e8, '$475.77B' -> 1e7, '$8,856,803' -> 1."""
    m = re.match(r"^\(?\$?([\d,]+)(?:\.(\d+))?\s*([KMBT])?", printed.strip())
    if not m:
        raise ValueError("cannot read print precision of %r" % printed)
    return _SCALE.get(m.group(3), 1.0) / 10 ** len(m.group(2) or "")


def _fmt_date(d) -> str:
    return "%d %s %d" % (d.day, d.strftime("%b"), d.year)


DOLLAR_LINES = ("spreads_paid", "net_revenue", "net_profit", "treasury_inflow", "olp_pnl")
PERIODS_PER_YEAR = 26


def build(reports: list[dict], treasury_days: dict | None = None) -> dict:
    """reports: the records of reports.json, oldest first.
    treasury_days: {date: on-chain treasury inflow} over complete UTC days, or None."""
    from .spreads import period_inflow
    periods = []
    prev_any = None       # previous report of any format
    prev_full = None      # previous full financial update
    for rec in reports:
        if not rec["full"]:
            prev_any = rec
            continue
        notes = []
        end = parse_date(rec["published_at"])

        # ---- window and volume
        volume = None
        tied = False              # True when the previous report marks the period start
        gap = (end - parse_date(prev_any["published_at"])).days if prev_any is not None else None
        if gap is not None and MIN_DAYS <= gap <= MAX_DAYS:
            tied = True
            start = parse_date(prev_any["published_at"])
            days = gap
            volume = rec["cumulative_volume"] - prev_any["cumulative_volume"]
            steps = (_print_step(rec["raw"]["cumulative_volume"]), _print_step(prev_any["raw"]["cumulative_volume"]))
            if max(steps) > FINE_PRINT:
                notes.append("Cumulative volume is printed to the nearest $%gB at %s; period volume carries up to $%gM of print rounding."
                             % (max(steps) / 1e9, "both ends" if min(steps) > FINE_PRINT else "one end", sum(steps) / 2 / 1e6))
        else:
            start = end - timedelta(days=STATED_WINDOW_DAYS)
            days = STATED_WINDOW_DAYS
            notes.append(
                "No report marks the start of this period (previous report %s); the window is the two weeks "
                "the post states and period volume is not derivable."
                % (_fmt_date(parse_date(prev_any["published_at"])) if prev_any is not None else "none"))

        # ---- printed lines
        spreads = rec["spreads_paid"]
        treasury = rec["treasury_inflow"]
        olp = rec["olp_pnl"]
        printed_rewards = rec["rewards"]
        if printed_rewards > 0:
            notes.append("Rewards printed without parentheses (%s); treated as a cost." % usd(printed_rewards))
        rewards = abs(printed_rewards)

        # ---- identities
        net_profit = treasury + olp
        net_revenue = net_profit + rewards
        mm_costs = spreads - net_revenue

        if rec["net_profit"] != net_profit:
            notes.append("Net profit printed as %s; Protocol Treasury plus OLP PnL is %s (difference %s)."
                         % (usd(rec["net_profit"]), usd(net_profit), usd(rec["net_profit"] - net_profit)))
        if rec["net_revenue"] != net_revenue:
            notes.append("Net revenue printed as %s; net profit plus rewards is %s (difference %s)."
                         % (usd(rec["net_revenue"]), usd(net_revenue), usd(rec["net_revenue"] - net_revenue)))
        printed_mm = rec.get("market_making_costs")
        if printed_mm is None:
            notes.append("Market-making costs not printed; derived as spreads paid minus net revenue.")
        else:
            if printed_mm > 0:
                notes.append("Market-making costs printed without parentheses (%s); treated as a cost." % usd(printed_mm))
            if abs(printed_mm) != mm_costs:
                notes.append("Market-making costs printed as %s; spreads paid minus net revenue is %s (difference %s)."
                             % (usd(abs(printed_mm)), usd(mm_costs), usd(abs(printed_mm) - mm_costs)))

        # ---- reward sub-lines
        subs = [rec[k] for k in ("loss_refunds", "referral_rewards") if rec.get(k) is not None]
        if subs and sum(subs) != printed_rewards:
            notes.append("Reward sub-lines sum to %s against rewards of %s."
                         % (usd(abs(sum(subs))), usd(rewards)))

        # ---- X against Telegram
        for key, pair in rec.get("x_differences", {}).items():
            notes.append("%s: the X post prints %s, the Telegram post prints %s; the Telegram figure is used."
                         % (key.replace("_", " ").capitalize(), usd(abs(pair["x"])), usd(abs(pair["telegram"]))))

        # ---- lifetime OLP PnL against the period line
        if prev_full is not None and tied:
            delta = rec["lifetime_olp_pnl"] - prev_full["lifetime_olp_pnl"]
            if delta != olp:
                notes.append("Lifetime OLP PnL changed by %s against period OLP PnL of %s (difference %s)."
                             % (usd(delta), usd(olp), usd(delta - olp)))

        periods.append({
            "end": end.isoformat(),
            "start": start.isoformat(),
            "days": days,
            "volume": money(volume),
            "spreads_paid": money(spreads),
            "mm_costs": money(mm_costs),
            "rewards": money(rewards),
            "treasury_inflow": money(treasury),
            "olp_pnl": money(olp),
            "net_revenue": money(net_revenue),
            "net_profit": money(net_profit),
            "spreads_bps": round(spreads / volume * 1e4, 2) if volume else None,
            "net_revenue_bps": round(net_revenue / volume * 1e4, 2) if volume else None,
            "net_profit_bps": round(net_profit / volume * 1e4, 2) if volume else None,
            "olp_bps": round(olp / volume * 1e4, 2) if volume else None,
            "cost_share": round(mm_costs / spreads, 4) if spreads else None,
            "net_revenue_margin": round(net_revenue / spreads, 4),
            "net_profit_margin": round(net_profit / spreads, 4),
            "rewards_share": round(rewards / spreads, 4),
            "treasury_share": round(treasury / spreads, 4),
            "treasury_inflow_onchain": period_inflow(treasury_days, start.isoformat(), end.isoformat()),
            "lifetime_olp_pnl": money(rec.get("lifetime_olp_pnl")),
            "source_url": rec["source_url"],
            "notes": " ".join(notes) if notes else None,
        })
        prev_any = rec
        prev_full = rec

    if not periods:
        raise ValueError("no full financial update found")
    return {
        "as_of": prev_full["published_at"],
        "periods": periods,
        "lifetime_olp_pnl": {"value": money(prev_full["lifetime_olp_pnl"]),
                             "as_of": prev_full["published_at"][:10]},
        "cumulative": dict(
            [("ends", [p["end"] for p in periods])] +
            [(k, [money(sum(q[k] for q in periods[:i + 1])) for i in range(len(periods))]) for k in DOLLAR_LINES]),
        "annualised": {k: money(periods[-1][k] * PERIODS_PER_YEAR) for k in DOLLAR_LINES},
        "olp_summary": {
            "latest": periods[-1]["olp_pnl"],                       # OLP PnL of the latest period
            "lifetime": money(prev_full["lifetime_olp_pnl"]),       # as printed in the latest report
            "lifetime_as_of": prev_full["published_at"][:10],
            "positive": sum(1 for p in periods if p["olp_pnl"] > 0),
            "periods": len(periods),
        },
    }


def check(fin: dict) -> list[str]:
    """Identity and schema errors in a financials.json object (empty = pass)."""
    errors = []
    keys = ["end", "start", "days", "volume", "spreads_paid", "mm_costs", "rewards",
            "treasury_inflow", "olp_pnl", "net_revenue", "net_profit", "spreads_bps",
            "net_revenue_bps", "net_profit_bps", "olp_bps", "cost_share", "net_revenue_margin",
            "net_profit_margin", "rewards_share", "treasury_share", "treasury_inflow_onchain",
            "lifetime_olp_pnl", "source_url", "notes"]
    if sorted(fin.keys()) != ["annualised", "as_of", "cumulative", "lifetime_olp_pnl", "olp_summary", "periods"]:
        errors.append("financials: top-level keys %s" % sorted(fin.keys()))
        return errors
    last_end = None
    for p in fin["periods"]:
        tag = "financials %s" % p.get("end")
        if list(p.keys()) != keys:
            errors.append("%s: keys %s" % (tag, list(p.keys())))
            continue
        for k in ("spreads_paid", "mm_costs", "rewards", "treasury_inflow", "olp_pnl", "net_revenue", "net_profit"):
            if not isinstance(p[k], (int, float)) or isinstance(p[k], bool):
                errors.append("%s: %s is not a number" % (tag, k))
        if errors:
            continue
        c = lambda v: round(v * 100)        # compare in cents
        if c(p["spreads_paid"]) != c(p["mm_costs"]) + c(p["rewards"]) + c(p["treasury_inflow"]) + c(p["olp_pnl"]):
            errors.append("%s: spreads_paid != mm_costs + rewards + treasury_inflow + olp_pnl" % tag)
        if c(p["net_profit"]) != c(p["treasury_inflow"]) + c(p["olp_pnl"]):
            errors.append("%s: net_profit != treasury_inflow + olp_pnl" % tag)
        if c(p["net_revenue"]) != c(p["net_profit"]) + c(p["rewards"]):
            errors.append("%s: net_revenue != net_profit + rewards" % tag)
        if p["rewards"] < 0:
            errors.append("%s: rewards must be positive (a cost)" % tag)
        if p["spreads_paid"] <= 0:
            errors.append("%s: spreads_paid not positive" % tag)
        if p["cost_share"] != round(p["mm_costs"] / p["spreads_paid"], 4):
            errors.append("%s: cost_share != mm_costs / spreads_paid" % tag)
        for key, line in (("net_revenue_margin", "net_revenue"), ("net_profit_margin", "net_profit"),
                          ("rewards_share", "rewards"), ("treasury_share", "treasury_inflow")):
            if p[key] != round(p[line] / p["spreads_paid"], 4):
                errors.append("%s: %s != %s / spreads_paid" % (tag, key, line))
        if p["treasury_inflow_onchain"] is not None and (not isinstance(p["treasury_inflow_onchain"], (int, float)) or p["treasury_inflow_onchain"] < 0):
            errors.append("%s: treasury_inflow_onchain malformed" % tag)
        if not isinstance(p["source_url"], str) or not p["source_url"].startswith("https://"):
            errors.append("%s: source_url missing" % tag)
        if p["volume"] is None:
            if any(p[k] is not None for k in ("spreads_bps", "net_revenue_bps", "net_profit_bps", "olp_bps")):
                errors.append("%s: bps present without volume" % tag)
            if not p["notes"]:
                errors.append("%s: null volume without a note" % tag)
        else:
            if p["volume"] <= 0:
                errors.append("%s: volume not positive" % tag)
            elif p["spreads_bps"] != round(p["spreads_paid"] / p["volume"] * 1e4, 2) \
                    or p["net_revenue_bps"] != round(p["net_revenue"] / p["volume"] * 1e4, 2) \
                    or p["net_profit_bps"] != round(p["net_profit"] / p["volume"] * 1e4, 2) \
                    or p["olp_bps"] != round(p["olp_pnl"] / p["volume"] * 1e4, 2):
                errors.append("%s: bps do not tie to volume" % tag)
            if last_end is not None and p["start"] != last_end:
                errors.append("%s: start %s is not the previous period's end %s" % (tag, p["start"], last_end))
        if not isinstance(p["start"], str) or not isinstance(p["days"], int):
            errors.append("%s: start/days missing" % tag)
        else:
            span = (parse_date(p["end"]) - parse_date(p["start"])).days
            if span != p["days"] or not MIN_DAYS <= span <= MAX_DAYS:
                errors.append("%s: days %s does not match start/end (13 to 15 expected)" % (tag, p["days"]))
        if p["notes"] is not None and (not isinstance(p["notes"], str) or not p["notes"]):
            errors.append("%s: notes must be a non-empty string or null" % tag)
        if last_end is not None and p["end"] <= last_end:
            errors.append("%s: periods out of order" % tag)
        last_end = p["end"]
    life = fin["lifetime_olp_pnl"]
    if sorted(life.keys()) != ["as_of", "value"] or not isinstance(life["value"], (int, float)):
        errors.append("financials: lifetime_olp_pnl malformed")
    if fin["periods"] and not errors:
        ps = fin["periods"]
        cum, ann = fin["cumulative"], fin["annualised"]
        if not isinstance(cum, dict) or list(cum.keys()) != ["ends"] + list(DOLLAR_LINES) or cum["ends"] != [p["end"] for p in ps]:
            errors.append("financials: cumulative malformed or not aligned to period ends")
        else:
            for k in DOLLAR_LINES:
                run = 0
                for i, p in enumerate(ps):
                    run += round(p[k] * 100)
                    if len(cum[k]) != len(ps) or round(cum[k][i] * 100) != run:
                        errors.append("financials: cumulative.%s does not tie at %s" % (k, p["end"]))
                        break
        if not isinstance(ann, dict) or list(ann.keys()) != list(DOLLAR_LINES) \
                or any(round(ann[k] * 100) != round(ps[-1][k] * 100) * PERIODS_PER_YEAR for k in DOLLAR_LINES):
            errors.append("financials: annualised is not the latest period times %d" % PERIODS_PER_YEAR)
        if ps[-1]["lifetime_olp_pnl"] != life["value"]:
            errors.append("financials: the latest period's lifetime_olp_pnl differs from lifetime_olp_pnl.value")
    summary = fin["olp_summary"]
    if not isinstance(summary, dict) or list(summary.keys()) != ["latest", "lifetime", "lifetime_as_of", "positive", "periods"]:
        errors.append("financials: olp_summary malformed")
    elif fin["periods"] and not errors:
        want = {"latest": fin["periods"][-1]["olp_pnl"], "lifetime": life["value"], "lifetime_as_of": life["as_of"],
                "positive": sum(1 for p in fin["periods"] if p["olp_pnl"] > 0), "periods": len(fin["periods"])}
        for key, value in want.items():
            if summary[key] != value:
                errors.append("financials: olp_summary.%s is %r, the periods give %r" % (key, summary[key], value))
    if fin["periods"] and (fin["as_of"][:10] != fin["periods"][-1]["end"] or life["as_of"] != fin["periods"][-1]["end"]):
        errors.append("financials: as_of is not the latest period's end")
    return errors


def table(fin: dict) -> str:
    """Console table of every period (USD, whole dollars)."""
    head = ("end", "days", "volume $B", "spreads", "mm_costs", "rewards", "treasury",
            "olp_pnl", "net_rev", "net_profit", "spr bps", "nr bps", "cost%", "tsy%")
    rows = [head]
    for p in fin["periods"]:
        f = lambda v: "{:,}".format(v) if v is not None else "n/a"
        rows.append((
            p["end"], str(p["days"]) if p["days"] is not None else "n/a",
            "%.2f" % (p["volume"] / 1e9) if p["volume"] is not None else "n/a",
            f(p["spreads_paid"]), f(p["mm_costs"]), f(p["rewards"]), f(p["treasury_inflow"]),
            f(p["olp_pnl"]), f(p["net_revenue"]), f(p["net_profit"]),
            "%.2f" % p["spreads_bps"] if p["spreads_bps"] is not None else "n/a",
            "%.2f" % p["net_revenue_bps"] if p["net_revenue_bps"] is not None else "n/a",
            "%.1f" % (p["cost_share"] * 100),
            "%.2f" % (p["treasury_inflow"] / p["spreads_paid"] * 100),
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(head))]
    lines = ["  ".join(c.rjust(w) for c, w in zip(r, widths)) for r in rows]
    for p in fin["periods"]:
        if p["notes"]:
            lines.append("  note %s: %s" % (p["end"], p["notes"]))
    lines.append("  lifetime OLP PnL %s as of %s" % (usd(fin["lifetime_olp_pnl"]["value"]), fin["lifetime_olp_pnl"]["as_of"]))
    return "\n".join(lines)
