"""Parse a Variational biweekly update (Telegram/Discord or X text) into fields.

Every value is read from a `Label: value` line of the post. The label map is
exact vocabulary taken from the posts themselves (labels changed over time);
a label that is not in the map is never interpreted, only reported.
Printed signs are kept: an amount in parentheses is negative.
"""
from __future__ import annotations

import re
from decimal import Decimal

# normalised label (lower case, single spaces, no markdown) -> field
LABELS = {
    # snapshot
    "total volume traded": "cumulative_volume",
    "current dual-sided oi": "dual_sided_oi",
    "tvl (excludes some olp hedging accounts)": "tvl",
    "tvl (excludes olp hedging accounts)": "tvl",
    "total markets currently listed": "markets",
    "total tokens currently listed": "tokens_listed",
    # period lines, 6 Mar 2026 onward
    "2w spreads paid (gross revenue)": "spreads_paid",
    "2w market making costs (cost of revenue)": "market_making_costs",
    "2w net revenue": "net_revenue",
    "2w rewards": "rewards",
    "loss refunds": "loss_refunds",
    "loss refunds (sunset)": "loss_refunds",
    "referral rewards": "referral_rewards",
    "2w net profit": "net_profit",
    "protocol treasury": "treasury_inflow",
    "2w protocol treasury inflow": "treasury_inflow",
    "olp pnl": "olp_pnl",
    "2w olp pnl": "olp_pnl",
    "lifetime olp pnl": "lifetime_olp_pnl",
    "current protocol treasury holdings": "treasury_holdings",
    # cumulative rewards and users
    "total rewards claimed": "total_rewards_claimed",
    "total losses refunded": "total_losses_refunded",
    "total losses refunded (sunset)": "total_losses_refunded",
    "total referral rewards claimed": "total_referral_rewards_claimed",
    "daily active users (avg. over past week)": "daily_active_users_week_avg",
    "daily active users (avg over past week)": "daily_active_users_week_avg",
    "weekly active users": "weekly_active_users",
    "total trades placed": "total_trades_placed",
    # legacy revenue vocabulary, before 6 Mar 2026 (not comparable with the lines above)
    "2w revenue": "legacy_revenue_2w",
    "average daily revenue": "legacy_average_daily_revenue",
    "revenue directed to olp": "legacy_revenue_to_olp",
    "revenue directed to loss refund pool": "legacy_revenue_to_loss_refund_pool",
    "revenue directed to referral rewards pool": "legacy_revenue_to_referral_rewards_pool",
    "revenue directed to protocol treasury": "legacy_revenue_to_protocol_treasury",
    "revenue directed to protocol treasury (just deployed for testing yesterday, numbers under construction, docs soon)": "legacy_revenue_to_protocol_treasury",
    "total olp pnl": "legacy_total_olp_pnl",
}

# Printed in some posts, deliberately not carried (no card or check uses them).
IGNORED = {
    "# of tokens listed in past 2 weeks",
    "# of new tokens listed in past 2 weeks",
    "current olp apy",
    "percent of total platform losses refunded (since implementation of loss refunds)",
    "total on-chain protocol treasury value",
}

SNAPSHOT_FIELDS = ("cumulative_volume", "dual_sided_oi", "tvl", "markets", "tokens_listed")
PERIOD_FIELDS = ("spreads_paid", "market_making_costs", "net_revenue", "rewards",
                 "loss_refunds", "referral_rewards", "net_profit", "treasury_inflow",
                 "olp_pnl", "lifetime_olp_pnl", "treasury_holdings")
# a post is a full financial update when all of these are printed
FULL_REPORT_FIELDS = ("spreads_paid", "net_revenue", "rewards", "net_profit",
                      "treasury_inflow", "olp_pnl", "lifetime_olp_pnl")

_BULLET = re.compile(r"^[\s*•◦\-–➡️]+")
_LINE = re.compile(r"^(?P<label>[^:]{2,140}):(?P<value>.*)$")
_AMOUNT = re.compile(
    r"^(?P<open>\()?\s*(?P<minus>-)?\s*(?P<cur>\$)?\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<suffix>[KMBT])?(?![A-Za-z])\s*(?P<close>\))?\s*(?P<unit>USDC|%)?",
)
_SCALE = {None: Decimal(1), "K": Decimal(10) ** 3, "M": Decimal(10) ** 6,
          "B": Decimal(10) ** 9, "T": Decimal(10) ** 12}


def norm_label(label: str) -> str:
    return " ".join(label.replace("*", "").replace("_", " ").split()).casefold()


def parse_amount(raw: str):
    """'($36,821)' -> (-36821, '($36,821)'); '$178.52B' -> (178520000000, …).

    Returns (number, printed) or (None, None) when the value is not an amount.
    The number is an int when whole, else a float; computed in Decimal.
    """
    text = raw.strip().lstrip("*").strip()
    m = _AMOUNT.match(text)
    if not m:
        return None, None
    if bool(m.group("open")) != bool(m.group("close")):
        return None, None
    value = Decimal(m.group("num").replace(",", "")) * _SCALE[m.group("suffix")]
    if m.group("open") or m.group("minus"):
        value = -value
    printed = text[: m.end()].strip()
    number = int(value) if value == value.to_integral_value() else float(value)
    return number, printed


def parse_post(text: str) -> dict:
    """Return {"fields", "raw", "labels", "blank", "unmapped", "duplicates"}.

    fields     field -> number, as printed (signed)
    raw        field -> the printed amount
    labels     field -> the label as printed
    blank      fields whose label is printed with no figure
    unmapped   [label, value] pairs in the Metrics block that could not be read:
               an amount under a label missing from LABELS, or a known label
               whose value is not an amount
    duplicates fields printed more than once with different values
    """
    fields: dict = {}
    raw: dict = {}
    labels: dict = {}
    blank: list = []
    unmapped: list = []
    duplicates: list = []
    in_metrics = False
    for line in text.split("\n"):
        stripped = _BULLET.sub("", line).strip()
        if re.fullmatch(r"[*_\s]*metrics[*_\s]*", stripped, re.I):
            in_metrics = True
            continue
        m = _LINE.match(stripped)
        if not m:
            continue
        label = " ".join(m.group("label").replace("*", "").split())
        value = m.group("value").strip().lstrip("*").strip()
        key = norm_label(label)
        if key in IGNORED:
            continue
        field = LABELS.get(key)
        number, printed = parse_amount(value)
        if field is None:
            if in_metrics and number is not None:
                unmapped.append([label, value[:80]])
            continue
        if number is None:
            if value in ("", "USDC", "$"):      # label printed, figure left out
                blank.append(field)
            else:
                unmapped.append([label, value[:80]])
            continue
        if field in fields:
            if fields[field] != number:
                duplicates.append(field)
            continue
        fields[field] = number
        raw[field] = printed
        labels[field] = label
    return {"fields": fields, "raw": raw, "labels": labels, "blank": blank,
            "unmapped": unmapped, "duplicates": duplicates}


def is_report(parsed: dict) -> bool:
    return "cumulative_volume" in parsed["fields"]


def is_full_report(parsed: dict) -> bool:
    return all(f in parsed["fields"] for f in FULL_REPORT_FIELDS)
