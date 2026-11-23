#!/usr/bin/env python3
"""On-chain adapter: data/onchain/*.json (pipeline/onchain) -> site/data/ in the site's data contract.

Writes treasury.json, olp_chain.json, accounts.json, pools_snapshot.json, tvl.json, cohorts.json and
rewards.json. Python standard library only.

  /usr/bin/python3 pipeline/build_chain.py            rebuild the seven files (safe to re-run)
  /usr/bin/python3 pipeline/build_chain.py --check    validate them against the contract invariants and print
                                                      on-chain treasury inflow against each reported two-week inflow

Coverage is whatever the indexer has folded: only complete UTC days are published, and every `as_of` is the
end of the last complete day (pools_snapshot: the block time its balances are taken at). No value is invented:
a date the indexer has not reached is simply absent.
"""
import argparse
import calendar
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.environ.get("VM_ONCHAIN_DATA") or os.path.join(ROOT, "data", "onchain")
OUT = os.environ.get("VM_SITE_DATA") or os.path.join(ROOT, "site", "data")
STORE = os.environ.get("VM_STORE") or os.path.join(ROOT, ".cache", "store")
TIER_LABELS = ["Under $100", "$100 to $1K", "$1K to $10K", "$10K to $100K", "$100K to $1M", "Over $1M"]
FILES = ("treasury.json", "olp_chain.json", "accounts.json", "pools_snapshot.json", "tvl.json", "cohorts.json", "rewards.json")
# Reported "2W Protocol Treasury Inflow" against on-chain USDC received by the treasury. Reports are cut during
# the day they are published (the reported holdings always sit between the on-chain balance at the end of the
# day before and the end of the report day), so a period is compared over the UTC days start+1..end, where
# start and end are the publication dates of consecutive reports. The intra-day cut cannot be resolved from
# daily data: each period is held to 5%, and the sum over all covered periods to 1%.
INFLOW_TOLERANCE = 0.05
INFLOW_TOLERANCE_TOTAL = 0.01
HOLDINGS_TOLERANCE = 0.02


def load(path):
    with open(path) as handle:
        return json.load(handle)


def write(name, doc):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(doc, handle, separators=(",", ":"))
        handle.write("\n")
    os.replace(tmp, path)
    return path


def end_of_day(day):
    """The end-of-day observation of UTC day `day` is taken at the next midnight."""
    ts = calendar.timegm(time.strptime(day, "%Y-%m-%d")) + 86400
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def month_days(month):
    year, mon = int(month[:4]), int(month[5:7])
    return calendar.monthrange(year, mon)[1]


def shift_day(day, n):
    ts = calendar.timegm(time.strptime(day, "%Y-%m-%d")) + n * 86400
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def next_month(month):
    year, mon = int(month[:4]), int(month[5:7])
    return "%04d-%02d" % (year + mon // 12, mon % 12 + 1)


def last_complete_month(dates):
    """Latest calendar month whose final UTC day is a complete indexed day."""
    month = dates[-1][:7]
    if dates[-1][8:] == "%02d" % month_days(month):
        return month
    year, mon = int(month[:4]), int(month[5:7])
    return "%04d-%02d" % (year - (mon == 1), 12 if mon == 1 else mon - 1)


def reported_holdings():
    """Protocol Treasury holdings as Variational reported them, oldest first (from the reports store)."""
    path = os.path.join(STORE, "reports.json")
    if not os.path.exists(path):
        return []
    out = []
    for report in load(path):
        value = report.get("treasury_holdings")
        if value is not None and report.get("published_at"):
            out.append({"date": report["published_at"][:10], "holdings": value})
    return sorted(out, key=lambda item: item["date"])


def inflow_table(treasury, financials):
    """Rows of (start, end, reported, on-chain start+1..end, residual, share, on-chain start..end-1, note)."""
    by_day = dict(zip(treasury.get("dates", []), treasury.get("inflow", [])))
    first = treasury["dates"][0] if treasury.get("dates") else None
    table = []
    for period in financials.get("periods", []):
        reported = period.get("treasury_inflow")
        if reported is None:
            continue

        def window(a, b):
            total, day = 0.0, a
            while day <= b:
                if day not in by_day:
                    return None
                total += by_day[day]
                day = shift_day(day, 1)
            return round(total, 2)

        onchain = window(shift_day(period["start"], 1), period["end"])
        late = window(period["start"], shift_day(period["end"], -1))
        if onchain is None:
            table.append((period["start"], period["end"], reported, None, None, None, late,
                          "before first on-chain inflow" if first and period["start"] < first else "not indexed yet"))
            continue
        table.append((period["start"], period["end"], reported, onchain, round(onchain - reported, 2),
                      round(onchain / reported - 1, 4) if reported else None, late, ""))
    return table


def build(args):
    daily = load(os.path.join(SRC, "daily.json"))
    cols = daily["columns"]
    rows = [dict(zip(cols, r)) for r in daily["rows"] if not r[cols.index("partial")]]
    if not rows:
        raise SystemExit("data/onchain/daily.json has no complete day yet")
    dates = [r["date"] for r in rows]
    as_of = end_of_day(dates[-1])

    # treasury.json: starts on the first day the treasury wallet holds USDC (it was empty before)
    first = next((i for i, r in enumerate(rows) if r["treasury_balance_usd"] > 0), None)
    t_rows = rows[first:] if first is not None else []
    write("treasury.json", {
        "as_of": as_of if t_rows else None,
        "dates": [r["date"] for r in t_rows],
        "balance": [r["treasury_balance_usd"] for r in t_rows],
        "inflow": [r["treasury_inflow_usd"] for r in t_rows],
        "outflow": [r["treasury_outflow_usd"] for r in t_rows],
        "reported": reported_holdings(),
    })

    # tvl.json: on-chain USDC in settlement pools + Core OLP Vault + Protocol Treasury + referral reward pool
    write("tvl.json", {
        "as_of": as_of, "dates": dates,
        "total": [round(r["pool_tvl_usd"] + r["olp_vault_balance_usd"] + r["treasury_balance_usd"] + r["referral_pool_balance_usd"], 2)
                  for r in rows],
    })

    # olp_chain.json: settlement stays null. The gross vault<->pool transfers are exact, but OLP margin
    # posting cannot be separated on-chain from realised PnL and funding (pipeline/onchain, flow semantics).
    settlement = None
    if args.with_settlement:
        settlement = {"to_pools": [r["settlement_to_pools_usd"] for r in rows],
                      "from_pools": [r["settlement_from_pools_usd"] for r in rows]}
    write("olp_chain.json", {
        "as_of": as_of, "dates": dates,
        "vault_balance": [r["olp_vault_balance_usd"] for r in rows],
        "settlement": settlement,
    })

    # accounts.json
    deposits = [r["user_deposits_usd"] for r in rows]
    withdrawals = [r["user_withdrawals_usd"] for r in rows]
    net_new = [r["net_flow_new_usd"] for r in rows]
    # daily.json rounds each figure to cents on its own; derive the returning leg so the identity holds to the cent
    net_returning = [round(d - w - n, 2) for d, w, n in zip(deposits, withdrawals, net_new)]
    for r, value in zip(rows, net_returning):
        if abs(value - r["net_flow_returning_usd"]) > 0.011:
            raise SystemExit("net flow legs do not tie on %s" % r["date"])
    volume_by_month, volume_days = {}, {}
    vol_path = os.path.join(OUT, "volume_daily.json")
    if os.path.exists(vol_path):
        vol = load(vol_path)
        for day, total in zip(vol.get("dates", []), vol.get("total", [])):
            if total is not None:
                volume_by_month[day[:7]] = volume_by_month.get(day[:7], 0.0) + total
                volume_days[day[:7]] = volume_days.get(day[:7], 0) + 1
    by_month = {}
    for r in rows:
        by_month.setdefault(r["date"][:7], []).append(r)
    months, m_volume, m_active, m_ratio = [], [], [], []
    for month in sorted(by_month):
        days = by_month[month]
        whole = len(days) == month_days(month) or (month == dates[0][:7] and days[-1]["date"][8:] == "%02d" % month_days(month))
        if not whole:
            continue  # the month in progress (or not fully indexed) is left out rather than shown partial
        active = days[-1]["active_pools_30d"]
        volume = round(volume_by_month[month], 2) if volume_days.get(month) == month_days(month) else None
        months.append(month)
        m_active.append(active)
        m_volume.append(volume)
        m_ratio.append(round(volume / active, 2) if (volume is not None and active) else None)
    write("accounts.json", {
        "as_of": as_of, "dates": dates,
        "first_funded": [r["accounts_first_funded"] for r in rows],
        "active_1d": [r["active_pools"] for r in rows],
        "active_7d": [r["active_pools_7d"] for r in rows],
        "active_30d": [r["active_pools_30d"] for r in rows],
        "deposits": deposits, "withdrawals": withdrawals,
        "net_new": net_new, "net_returning": net_returning,
        "pool_balance": [r["pool_tvl_usd"] for r in rows],
        # holding: balance of at least 1 USDC. never_funded: the pool has never received USDC of any kind.
        # emptied: everything else (received USDC at some point, now under 1 USDC). The three sum to pools created.
        "states": {
            "holding": [r["pools_balance_ge_1usd"] for r in rows],
            "emptied": [r["pools_created_cum"] - r["pools_balance_ge_1usd"] - r["pools_unactivated"] for r in rows],
            "never_funded": [r["pools_unactivated"] for r in rows],
        },
        "monthly": {"months": months, "volume": m_volume, "active_30d": m_active, "vol_per_active": m_ratio},
    })

    # cohorts.json: complete months only; active[k] = share of the cohort with any USDC movement k months after funding
    final_month = last_complete_month(dates)
    src_cohorts = load(os.path.join(SRC, "cohorts.json"))
    cohorts = []
    for item in src_cohorts["cohorts"]:
        if item["cohort"] > final_month or not item["size"]:
            continue
        counts = {m: n for m, n, _ in item["active"]}
        active, month = [], item["cohort"]
        while month <= final_month:
            active.append(round(counts.get(month, 0) / item["size"], 4))
            month = next_month(month)
        cohorts.append({"month": item["cohort"], "size": item["size"], "active": active})
    write("cohorts.json", {
        "as_of": end_of_day("%s-%02d" % (final_month, month_days(final_month))) if cohorts else None,
        "cohorts": cohorts,
    })

    # rewards.json: USDC paid into users' pools from the referral reward wallet and the loss refund wallet
    ref = load(os.path.join(SRC, "referrals.json"))
    start = next((i for i, r in enumerate(rows) if r["referral_claims_usd"] or r["loss_refunds_usd"]), None)
    r_rows = rows[start:] if start is not None else []
    claims_total = ref["claims_total_usd"]
    ranked = ref["top_claimants"]  # rows: rank, wallet, pool, claims, total_usd, share
    write("rewards.json", {
        "as_of": ref["as_of_utc"] if r_rows else None,
        "dates": [r["date"] for r in r_rows],
        "referral": [r["referral_claims_usd"] for r in r_rows],
        "loss_refund": [r["loss_refunds_usd"] for r in r_rows],
        "claimants": [{"wallet": row[1], "claims": row[3], "total": row[4],
                       "share": round(row[4] / claims_total, 4) if claims_total else None} for row in ranked[:25]],
        "top10_share": round(sum(row[4] for row in ranked[:10]) / claims_total, 4) if claims_total else None,
    })

    # pools_snapshot.json
    snap = load(os.path.join(SRC, "pools_snapshot.json"))
    tiers = snap["tiers"]
    if len(tiers) != len(TIER_LABELS):
        raise SystemExit("unexpected tier count in data/onchain/pools_snapshot.json")
    write("pools_snapshot.json", {
        "as_of": snap["as_of_utc"],
        "total_balance": snap["total_pool_balance_usd"],
        "tiers": [{"label": label, "pools": t["pools"], "balance": t["balance_usd"]} for label, t in zip(TIER_LABELS, tiers)],
        "top_share": {str(n): round(snap["concentration"]["top_%d_share" % n], 4) for n in (1, 10, 100, 1000)},
        # rows of top_pools: rank, pool, user, balance_usd, share, first_funded
        "top": [{"pool": row[1], "balance": row[3],
                 "share": round(row[3] / snap["total_pool_balance_usd"], 4) if snap["total_pool_balance_usd"] else None,
                 "first_funded": row[5]} for row in snap["top_pools"][:100]],
    })
    cov = daily["coverage"]
    print("built %s: %d complete days %s to %s, as_of %s, snapshot as_of %s, backfill_complete=%s"
          % (", ".join(FILES), len(dates), dates[0], dates[-1], as_of, snap["as_of_utc"], cov.get("backfill_complete")))
    return 0


def check(args):
    problems = []

    def bad(name, text):
        problems.append("%s: %s" % (name, text))

    def series_ok(name, doc, keys, allow_null=()):
        dates = doc.get("dates", [])
        if dates != sorted(set(dates)):
            bad(name, "dates are not strictly ascending")
        for a, b in zip(dates, dates[1:]):
            if calendar.timegm(time.strptime(b, "%Y-%m-%d")) - calendar.timegm(time.strptime(a, "%Y-%m-%d")) != 86400:
                bad(name, "gap between %s and %s" % (a, b))
                break
        for key in keys:
            arr = doc.get(key)
            if not isinstance(arr, list) or len(arr) != len(dates):
                bad(name, "%s is missing or not the length of dates" % key)
                continue
            if key not in allow_null and any(v is None for v in arr):
                bad(name, "%s has nulls" % key)
        if dates and doc.get("as_of") and doc["as_of"][:10] < dates[-1]:
            bad(name, "as_of %s is before the last date %s" % (doc["as_of"], dates[-1]))
        return dates

    source = {}  # data/onchain daily rows, when present, for exact ties
    src_path = os.path.join(SRC, "daily.json")
    if os.path.exists(src_path):
        daily = load(src_path)
        source = {r[0]: dict(zip(daily["columns"], r)) for r in daily["rows"]}
    docs = {}
    for name in FILES:
        path = os.path.join(OUT, name)
        if not os.path.exists(path):
            bad(name, "missing")
            continue
        docs[name] = load(path)
        if docs[name].get("fixture"):
            bad(name, "is still a fixture")
        if not isinstance(docs[name].get("as_of"), str) or not docs[name]["as_of"].endswith("Z"):
            bad(name, "as_of missing or not UTC")

    doc = docs.get("treasury.json")
    if doc:
        series_ok("treasury.json", doc, ["balance"])
        if any(v == 0 for v in doc.get("balance", [])):
            bad("treasury.json", "zero balance inside the series (a zero must be a real reading, never a placeholder)")
        if not isinstance(doc.get("reported"), list):
            bad("treasury.json", "reported must be a list")
        t_dates = series_ok("treasury.json", doc, ["inflow", "outflow"])
        if all(isinstance(doc.get(k), list) and len(doc[k]) == len(t_dates) for k in ("balance", "inflow", "outflow")):
            if any(v < 0 for v in doc["inflow"]) or any(v < 0 for v in doc["outflow"]):
                bad("treasury.json", "inflow and outflow must be positive numbers")
            for i in range(1, len(t_dates)):
                if abs((doc["balance"][i] - doc["balance"][i - 1]) - (doc["inflow"][i] - doc["outflow"][i])) > 0.011:
                    bad("treasury.json", "balance change != inflow - outflow on %s" % t_dates[i])
                    break
            if t_dates and abs(doc["balance"][0] - (doc["inflow"][0] - doc["outflow"][0])) > 0.011:
                bad("treasury.json", "the series must start on the wallet's first day of USDC")
        seen = [item.get("date") for item in doc.get("reported", [])]
        if seen != sorted(set(seen)) or any(not isinstance(item.get("holdings"), (int, float)) or item["holdings"] <= 0 for item in doc.get("reported", [])):
            bad("treasury.json", "reported must be dated, ascending, with positive holdings")
        fin_path = os.path.join(OUT, "financials.json")
        if os.path.exists(fin_path) and isinstance(doc.get("inflow"), list):
            table = inflow_table(doc, load(fin_path))
            print("Protocol Treasury inflow: reported two-week figure against on-chain USDC received over UTC days start+1..end "
                  "(tolerance %.0f%% per period, %.0f%% on the total)" % (100 * INFLOW_TOLERANCE, 100 * INFLOW_TOLERANCE_TOTAL))
            print("  %-10s %-10s %12s %14s %12s %8s %16s" % ("start", "end", "reported", "on-chain", "residual", "pct", "start..end-1"))
            sum_reported = sum_onchain = 0.0
            for start, end, reported, onchain, residual, share, late, note in table:
                if onchain is None:
                    print("  %-10s %-10s %12.0f %14s %12s %8s %16s  %s" % (start, end, reported, "-", "-", "-", "-", note))
                    continue
                print("  %-10s %-10s %12.0f %14.2f %12.2f %7.2f%% %16s" % (start, end, reported, onchain, residual, 100 * share, "%.2f" % late if late is not None else "-"))
                sum_reported += reported
                sum_onchain += onchain
                if abs(share) > INFLOW_TOLERANCE:
                    bad("treasury.json", "on-chain inflow %s to %s is %.2f%% from the reported inflow (tolerance %.0f%%)" % (start, end, 100 * share, 100 * INFLOW_TOLERANCE))
            if sum_reported:
                total_share = sum_onchain / sum_reported - 1
                print("  %-10s %-10s %12.0f %14.2f %12.2f %7.2f%%" % ("all", "covered", sum_reported, sum_onchain, sum_onchain - sum_reported, 100 * total_share))
                if abs(total_share) > INFLOW_TOLERANCE_TOTAL:
                    bad("treasury.json", "on-chain inflow over all covered report periods is %.2f%% from the reported total (tolerance %.0f%%)" % (100 * total_share, 100 * INFLOW_TOLERANCE_TOTAL))
            by_day = dict(zip(doc["dates"], doc["balance"]))
            for item in doc.get("reported", []):
                if item["date"] in by_day and abs(by_day[item["date"]] / item["holdings"] - 1) > HOLDINGS_TOLERANCE:
                    bad("treasury.json", "reported holdings on %s differ from the on-chain balance by more than %.0f%%" % (item["date"], 100 * HOLDINGS_TOLERANCE))
    doc = docs.get("olp_chain.json")
    if doc:
        dates = series_ok("olp_chain.json", doc, ["vault_balance"])
        if any(v is not None and v <= 0 for v in doc.get("vault_balance", [])):
            bad("olp_chain.json", "non-positive vault balance")
        if doc.get("settlement") is not None:
            for key in ("to_pools", "from_pools"):
                if len(doc["settlement"].get(key, [])) != len(dates):
                    bad("olp_chain.json", "settlement.%s length" % key)
    doc = docs.get("accounts.json")
    if doc:
        keys = ["first_funded", "active_1d", "active_7d", "active_30d", "deposits", "withdrawals", "net_new", "net_returning", "pool_balance"]
        dates = series_ok("accounts.json", doc, keys)
        if all(isinstance(doc.get(k), list) and len(doc[k]) == len(dates) for k in keys):
            for i, day in enumerate(dates):
                lhs = doc["net_new"][i] + doc["net_returning"][i]
                rhs = doc["deposits"][i] - doc["withdrawals"][i]
                if abs(lhs - rhs) > 0.005:
                    bad("accounts.json", "net_new + net_returning != deposits - withdrawals on %s (%.2f vs %.2f)" % (day, lhs, rhs))
                    break
            if any(v < 0 for v in doc["deposits"]) or any(v < 0 for v in doc["withdrawals"]):
                bad("accounts.json", "deposits and withdrawals must be positive numbers")
            if any(v <= 0 for v in doc["pool_balance"]):
                bad("accounts.json", "non-positive pool balance")
            for i in range(len(dates)):
                if not (doc["active_1d"][i] <= doc["active_7d"][i] <= doc["active_30d"][i]):
                    bad("accounts.json", "active_1d <= active_7d <= active_30d broken on %s" % dates[i])
                    break
            if any(v == 0 for v in doc["active_1d"]):
                bad("accounts.json", "a day with zero active pools (check for missing data)")
        states = doc.get("states") or {}
        if not all(isinstance(states.get(k), list) and len(states[k]) == len(dates) for k in ("holding", "emptied", "never_funded")):
            bad("accounts.json", "states.holding / emptied / never_funded missing or not the length of dates")
        else:
            created = [a + b + c for a, b, c in zip(states["holding"], states["emptied"], states["never_funded"])]
            if any(v < 0 for k in states for v in states[k]):
                bad("accounts.json", "negative pool count in states")
            if any(b < a for a, b in zip(created, created[1:])):
                bad("accounts.json", "states do not sum to a non-decreasing count of pools created")
            if source:
                want = [source[d]["pools_created_cum"] for d in dates if d in source]
                if len(want) != len(dates) or want != created:
                    bad("accounts.json", "states do not sum to pools created to date")
            funded = 0
            for i, day in enumerate(dates):  # every first-funded pool has left never_funded
                funded += doc["first_funded"][i]
                if states["holding"][i] + states["emptied"][i] < funded:
                    bad("accounts.json", "holding + emptied is below cumulative first-funded accounts on %s" % day)
                    break
        monthly = doc.get("monthly", {})
        n = len(monthly.get("months", []))
        for key in ("volume", "active_30d", "vol_per_active"):
            if len(monthly.get(key, [])) != n:
                bad("accounts.json", "monthly.%s length" % key)
        for volume, ratio in zip(monthly.get("volume", []), monthly.get("vol_per_active", [])):
            if (volume is None) != (ratio is None) or volume == 0:
                bad("accounts.json", "monthly volume and vol_per_active must be null together and never 0")
                break
    doc = docs.get("pools_snapshot.json")
    if doc:
        tiers = doc.get("tiers", [])
        if [t.get("label") for t in tiers] != TIER_LABELS:
            bad("pools_snapshot.json", "tier labels differ from the contract")
        elif abs(sum(t["balance"] for t in tiers) - doc.get("total_balance", 0)) > 0.05:
            bad("pools_snapshot.json", "tier balances do not sum to total_balance")
        shares = [doc.get("top_share", {}).get(k) for k in ("1", "10", "100", "1000")]
        if any(s is None for s in shares) or shares != sorted(shares) or not all(0 < s <= 1 for s in shares):
            bad("pools_snapshot.json", "top_share must be four increasing fractions in (0, 1]")
        top = doc.get("top")
        pools_with_balance = sum(t.get("pools", 0) for t in tiers)
        if not isinstance(top, list) or len(top) != min(100, pools_with_balance):
            bad("pools_snapshot.json", "top must list the 100 largest pools")
        else:
            total = doc.get("total_balance") or 0
            balances = [row.get("balance") for row in top]
            if any(set(row) != {"pool", "balance", "share", "first_funded"} for row in top):
                bad("pools_snapshot.json", "top rows must be {pool, balance, share, first_funded}")
            elif balances != sorted(balances, reverse=True) or len({row["pool"] for row in top}) != len(top):
                bad("pools_snapshot.json", "top is not unique pools in descending balance")
            elif any(abs(row["share"] - row["balance"] / total) > 0.00006 for row in top):
                bad("pools_snapshot.json", "top share is not balance / total_balance")
            elif None not in shares:
                for n, key in ((1, "1"), (10, "10"), (100, "100")):
                    if len(top) >= n and abs(sum(balances[:n]) / total - doc["top_share"][key]) > 0.00006:
                        bad("pools_snapshot.json", "top_share[%s] does not tie to the top rows" % key)
    doc = docs.get("tvl.json")
    if doc:
        dates = series_ok("tvl.json", doc, ["total"])
        if isinstance(doc.get("total"), list) and len(doc["total"]) == len(dates):
            if any(v <= 0 for v in doc["total"]):
                bad("tvl.json", "non-positive total")
            pool = dict(zip(docs.get("accounts.json", {}).get("dates", []), docs.get("accounts.json", {}).get("pool_balance", [])))
            vault = dict(zip(docs.get("olp_chain.json", {}).get("dates", []), docs.get("olp_chain.json", {}).get("vault_balance", [])))
            treasury = dict(zip(docs.get("treasury.json", {}).get("dates", []), docs.get("treasury.json", {}).get("balance", [])))
            for day, total in zip(dates, doc["total"]):
                if day not in pool or day not in vault:
                    bad("tvl.json", "%s has no pool or vault balance behind it" % day)
                    break
                rest = total - pool[day] - vault[day] - treasury.get(day, 0.0)  # what is left is the referral reward pool
                if rest < -0.011:
                    bad("tvl.json", "total on %s is below pools + vault + treasury" % day)
                    break
                if source and day in source and abs(rest - source[day]["referral_pool_balance_usd"]) > 0.011:
                    bad("tvl.json", "total on %s is not pools + vault + treasury + referral reward pool" % day)
                    break
    doc = docs.get("cohorts.json")
    if doc:
        cohorts = doc.get("cohorts")
        if not isinstance(cohorts, list) or not cohorts:
            bad("cohorts.json", "no cohorts")
        else:
            months = [c.get("month") for c in cohorts]
            last = doc["as_of"][:7] if isinstance(doc.get("as_of"), str) else None
            if months != sorted(set(months)):
                bad("cohorts.json", "months are not strictly ascending")
            if last and doc["as_of"][7:] != "-01T00:00:00Z":
                bad("cohorts.json", "as_of must be the end of a complete month")
            for c in cohorts:
                active = c.get("active") or []
                span = 0
                month = c["month"]
                while last and next_month(month) <= last:
                    span += 1
                    month = next_month(month)
                if not c.get("size") or c["size"] <= 0:
                    bad("cohorts.json", "%s has no accounts" % c["month"])
                elif not active or active[0] != 1 or any(v is None or not 0 <= v <= 1 for v in active):
                    bad("cohorts.json", "%s: active must be shares in 0..1 starting at 1 in the funding month" % c["month"])
                elif len(active) != span:
                    bad("cohorts.json", "%s: active must cover every complete month since funding (%d, has %d)" % (c["month"], span, len(active)))
            acc = docs.get("accounts.json")
            if acc and months == sorted(set(months)):
                by_month = {}
                for day, n in zip(acc.get("dates", []), acc.get("first_funded", [])):
                    by_month[day[:7]] = by_month.get(day[:7], 0) + n
                for c in cohorts:
                    if by_month.get(c["month"]) != c.get("size"):
                        bad("cohorts.json", "%s size %s != accounts first funded that month (%s)" % (c["month"], c.get("size"), by_month.get(c["month"])))
                        break
    doc = docs.get("rewards.json")
    if doc:
        dates = series_ok("rewards.json", doc, ["referral", "loss_refund"])
        if all(isinstance(doc.get(k), list) and len(doc[k]) == len(dates) for k in ("referral", "loss_refund")):
            if any(v < 0 for v in doc["referral"]) or any(v < 0 for v in doc["loss_refund"]):
                bad("rewards.json", "payouts must be positive numbers")
            if dates and not (doc["referral"][0] or doc["loss_refund"][0]):
                bad("rewards.json", "the series must start on the first day with a payout")
        claimants = doc.get("claimants")
        if not isinstance(claimants, list) or not claimants or len(claimants) > 25:
            bad("rewards.json", "claimants must be the top 25 or fewer")
        else:
            totals = [c.get("total") for c in claimants]
            if any(set(c) != {"wallet", "claims", "total", "share"} for c in claimants):
                bad("rewards.json", "claimant rows must be {wallet, claims, total, share}")
            elif totals != sorted(totals, reverse=True) or len({c["wallet"] for c in claimants}) != len(claimants):
                bad("rewards.json", "claimants are not unique wallets in descending total")
            elif any(c["claims"] <= 0 or c["total"] <= 0 or not 0 < c["share"] <= 1 for c in claimants):
                bad("rewards.json", "claimant figures out of range")
            else:
                top10 = doc.get("top10_share")
                if top10 is None or not 0 < top10 <= 1 or abs(top10 - sum(c["share"] for c in claimants[:10])) > 0.0006:
                    bad("rewards.json", "top10_share does not tie to the ten largest claimants")
                paid = sum(doc.get("referral", []))
                if paid and sum(totals) > paid * 1.02 + 1:
                    bad("rewards.json", "the listed claimants received more than the referral series pays out")
                if paid and claimants[0]["share"] and abs(claimants[0]["total"] / claimants[0]["share"] / paid - 1) > 0.02:
                    bad("rewards.json", "claimant shares are not shares of the referral payouts in the series")
    for line in problems:
        print("FAIL", line)
    if not problems:
        def span(doc):
            if doc.get("dates"):
                return "%s to %s" % (doc["dates"][0], doc["dates"][-1])
            if doc.get("cohorts"):
                return "%s to %s" % (doc["cohorts"][0]["month"], doc["cohorts"][-1]["month"])
            return "snapshot"
        cov = "; ".join("%s as_of %s (%s)" % (n, docs[n]["as_of"], span(docs[n])) for n in FILES)
        print("OK", cov)
    return 1 if problems else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="validate site/data against the contract invariants")
    parser.add_argument("--with-settlement", action="store_true",
                        help="publish gross vault<->pool transfers in olp_chain.json (default: settlement is null)")
    args = parser.parse_args()
    return check(args) if args.check else build(args)


if __name__ == "__main__":
    sys.exit(main())
