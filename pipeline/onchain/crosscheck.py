#!/usr/bin/env python3
"""Cross-check data/onchain/daily.json against a research pull of another dashboard's chart data.

Cross-check ONLY: nothing from the reference is ever written into data/onchain. The reference
directory is a local research folder (files with `columns` and `_all_rows`), not part of this repo.

  python3 crosscheck.py /path/to/reference/dir [--dates 2026-10-29 2026-11-02 ...]
"""
import argparse
import glob
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402


def load_ref(directory, prefix):
    paths = sorted(glob.glob(os.path.join(directory, prefix + "*.json")))
    if not paths:
        return None, None
    with open(paths[0]) as handle:
        doc = json.load(handle)
    cols = [c["name"] for c in doc["columns"]]
    return cols, doc["_all_rows"]


def series(cols, rows, date_col, value_col, where=None):
    out = {}
    di, vi = cols.index(date_col), cols.index(value_col)
    for row in rows:
        if where and not where(dict(zip(cols, row))):
            continue
        out[str(row[di])[:10]] = row[vi]
    return out


def compare(label, ours, theirs, dates, money=True, skip_last=None):
    common_days = sorted(d for d in ours if d in theirs and theirs[d] is not None and ours[d] is not None and d != skip_last)
    if not common_days:
        print("%-44s no overlapping days" % label)
        return
    diffs = [ours[d] - theirs[d] for d in common_days]
    tol = 0.01 if money else 0
    exact = sum(1 for x in diffs if abs(x) <= tol)
    rel = [abs(ours[d] - theirs[d]) / abs(theirs[d]) for d in common_days if theirs[d]]
    print("%-44s days %d  exact %d (%.1f%%)  median |diff| %s  max |diff| %s  median rel %.4f%%"
          % (label, len(common_days), exact, 100.0 * exact / len(common_days),
             ("%.2f" % statistics.median(map(abs, diffs))) if money else "%d" % statistics.median(map(abs, diffs)),
             ("%.2f" % max(map(abs, diffs))) if money else "%d" % max(map(abs, diffs)),
             100 * statistics.median(rel) if rel else 0))
    for d in dates:
        if d in ours and d in theirs and theirs[d] is not None:
            fmt = "    %s  ours %s  reference %s  diff %s" if money else "    %s  ours %d  reference %d  diff %d"
            if money:
                print(fmt % (d, "{:,.2f}".format(ours[d]), "{:,.2f}".format(theirs[d]), "{:,.2f}".format(ours[d] - theirs[d])))
            else:
                print(fmt % (d, ours[d], theirs[d], ours[d] - theirs[d]))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("reference")
    parser.add_argument("--daily", default=os.path.join(C.DATA_DIR, "daily.json"))
    parser.add_argument("--snapshot", default=os.path.join(C.DATA_DIR, "pools_snapshot.json"))
    parser.add_argument("--dates", nargs="*", default=["2025-06-30", "2025-12-31", "2026-03-31", "2026-06-30", "2026-10-29", "2026-11-02", "2026-11-17"])
    args = parser.parse_args()
    with open(args.daily) as handle:
        doc = json.load(handle)
    cols = doc["columns"]
    rows = [dict(zip(cols, r)) for r in doc["rows"]]
    partial_day = rows[-1]["date"] if rows and rows[-1]["partial"] else None

    def ours(col):
        return {r["date"]: r[col] for r in rows}

    print("ours: as of block %s (%s), %d days, last complete day %s" % (doc["as_of_block"], doc["as_of_utc"], len(rows), doc["coverage"]["last_complete_day"]))
    rc, rr = load_ref(args.reference, "tvl_0b31ff93")
    if rc:
        compare("pool_tvl_usd vs settlement_pool_tvl_usd", ours("pool_tvl_usd"), series(rc, rr, "date", "settlement_pool_tvl_usd"), args.dates, skip_last=partial_day)
        compare("olp_vault_balance vs core_olp_vault_tvl", ours("olp_vault_balance_usd"), series(rc, rr, "date", "core_olp_vault_tvl_usd"), args.dates, skip_last=partial_day)
        compare("treasury_balance vs treasury_tvl", ours("treasury_balance_usd"), series(rc, rr, "date", "treasury_tvl_usd"), args.dates, skip_last=partial_day)
        compare("referral_pool_balance vs referral_reward_pool_tvl", ours("referral_pool_balance_usd"), series(rc, rr, "date", "referral_reward_pool_tvl_usd"), args.dates, skip_last=partial_day)
    rc, rr = load_ref(args.reference, "treasury_balance")
    if rc:
        compare("treasury_balance vs treasury_balance chart", ours("treasury_balance_usd"), series(rc, rr, "day", "daily_total"), args.dates, skip_last=partial_day)
    rc, rr = load_ref(args.reference, "number_of_accounts_over_time")
    if rc:
        for mine, theirs in (("pools_live", "live_pools"), ("pools_dormant", "dormant_pools"), ("pools_unactivated", "unactivated_pools")):
            compare("%s vs %s" % (mine, theirs), ours(mine), series(rc, rr, "date", theirs), args.dates, money=False, skip_last=partial_day)
        compare("pools_balance_ge_1usd vs live_pools", ours("pools_balance_ge_1usd"), series(rc, rr, "date", "live_pools"), args.dates, money=False, skip_last=partial_day)
        total_ref = {str(r[0])[:10]: r[1] + r[2] + r[3] for r in rr}
        compare("pools_created_cum vs live+dormant+unactivated", ours("pools_created_cum"), total_ref, args.dates, money=False, skip_last=partial_day)
    rc, rr = load_ref(args.reference, "daily_net_flow_by_account")
    if rc:
        compare("active_pools vs daily_active_accounts", ours("active_pools"), series(rc, rr, "date", "daily_active_accounts"), args.dates, money=False, skip_last=partial_day)
        compare("active_pools_user_initiated vs daily_active", ours("active_pools_user_initiated"), series(rc, rr, "date", "daily_active_accounts"), args.dates, money=False, skip_last=partial_day)
        compare("accounts_first_funded vs new_accounts", ours("accounts_first_funded"), series(rc, rr, "date", "new_accounts"), args.dates, money=False, skip_last=partial_day)
        compare("accounts_funded_cum vs cumulative_accounts", ours("accounts_funded_cum"), series(rc, rr, "date", "cumulative_accounts"), args.dates, money=False, skip_last=partial_day)
        compare("active_pools_7d vs active_accounts_7d", ours("active_pools_7d"), series(rc, rr, "date", "active_accounts_7d"), args.dates, money=False, skip_last=partial_day)
        compare("active_pools_30d vs active_accounts_30d", ours("active_pools_30d"), series(rc, rr, "date", "active_accounts_30d"), args.dates, money=False, skip_last=partial_day)
        compare("net_flow_new_usd vs new_user_flow_usd", ours("net_flow_new_usd"), series(rc, rr, "date", "new_user_flow_usd"), args.dates, skip_last=partial_day)
        compare("net_flow_returning vs returning_user_flow", ours("net_flow_returning_usd"), series(rc, rr, "date", "returning_user_flow_usd"), args.dates, skip_last=partial_day)
        compare("pools_first_inflow vs new_accounts", ours("pools_first_inflow"), series(rc, rr, "date", "new_accounts"), args.dates, money=False, skip_last=partial_day)
        compare("pools_funded_cum vs cumulative_accounts", ours("pools_funded_cum"), series(rc, rr, "date", "cumulative_accounts"), args.dates, money=False, skip_last=partial_day)
        compare("pool_balance_change_new vs new_user_flow", ours("pool_balance_change_new_usd"), series(rc, rr, "date", "new_user_flow_usd"), args.dates, skip_last=partial_day)
        compare("pool_balance_change_returning vs returning", ours("pool_balance_change_returning_usd"), series(rc, rr, "date", "returning_user_flow_usd"), args.dates, skip_last=partial_day)
        ref_net = {str(r[0])[:10]: r[rc.index("new_user_flow_usd")] + r[rc.index("returning_user_flow_usd")] for r in rr}
        compare("net_flow_usd vs new+returning", ours("net_flow_usd"), ref_net, args.dates, skip_last=partial_day)
    rc, rr = load_ref(args.reference, "settlement_pool_ranking")
    if rc and os.path.exists(args.snapshot):
        with open(args.snapshot) as handle:
            snap = json.load(handle)
        mine_by_user = {row[2]: row[3] for row in snap["top_pools"]}
        mine_by_pool = {row[1]: row[3] for row in snap["top_pools"]}
        ref_addr = [str(r[rc.index("wallet_address")]).lower() for r in rr]
        hit_user = sum(1 for a in ref_addr if a in mine_by_user)
        hit_pool = sum(1 for a in ref_addr if a in mine_by_pool)
        print("ranking: reference top %d addresses found in our top 1000 as user wallet: %d, as pool address: %d (snapshots are from different days)"
              % (len(ref_addr), hit_user, hit_pool))
        print("    reference #1 %s %.2f | ours #1 pool %s user %s %.2f" % (ref_addr[0], rr[0][rc.index("balance_usd")], snap["top_pools"][0][1], snap["top_pools"][0][2], snap["top_pools"][0][3]))


if __name__ == "__main__":
    main()
