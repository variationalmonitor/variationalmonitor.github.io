#!/usr/bin/env python3
"""Build data/onchain/*.json from the indexer store. Standard library only.

The aggregator walks UTC days in order. Each day it reads that day's block range from the store,
classifies every USDC movement (rules in findings.md) and updates a per-pool state (balance,
first-deposit day, last-active day). The state can be saved and resumed, so a CI run only needs the
rows after the last complete day.

  python3 aggregate.py                      full rebuild from the local store
  python3 aggregate.py --state-in S --state-out S   incremental (CI) run
"""
import argparse
import array
import base64
import bisect
import calendar
import gzip
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402
import indexer as I  # noqa: E402

SID = C.system_ids()
NSYS = len(C.SYSTEM)
VAULT, TREASURY, ORACLE = SID[C.VAULT], SID[C.TREASURY], SID[C.ORACLE]
R_SETTLE, R_ONBOARD = SID[C.ROUTER_SETTLE], SID[C.ROUTER_ONBOARD]
REFERRAL, LEGACY = SID[C.REFERRAL_POOL], SID[C.LEGACY_FEE_WALLET]
DEP_BINS = [10, 100, 1_000, 10_000, 100_000, 1_000_000]  # USD upper edges; last bin is open
DEP_BIN_LABELS = ["<$10", "$10-100", "$100-1K", "$1K-10K", "$10K-100K", "$100K-1M", ">$1M"]
TIERS = [100, 1_000, 10_000, 100_000, 1_000_000]
TIER_LABELS = ["<$100", "$100-1K", "$1K-10K", "$10K-100K", "$100K-1M", ">$1M"]
SOURCE = "Arbitrum One eth_getLogs (USDC Transfer + Variational SettlementPool/Factory events), independently indexed"

COLUMNS = [
    "date", "first_block", "last_block", "partial",
    "pools_created", "pools_created_cum",
    "accounts_first_funded", "accounts_funded_cum", "pools_first_inflow", "pools_funded_cum",
    "user_deposits_usd", "user_deposits_count", "user_deposits_cum_usd",
    "user_withdrawals_usd", "user_withdrawals_count", "user_withdrawals_cum_usd",
    "net_flow_usd", "net_flow_new_usd", "net_flow_returning_usd",
    "pool_balance_change_usd", "pool_balance_change_new_usd", "pool_balance_change_returning_usd",
    "unique_depositors", "unique_withdrawers",
    "active_pools", "active_pools_user_initiated",
    "active_pools_7d", "active_pools_30d", "active_pools_user_initiated_7d", "active_pools_user_initiated_30d",
    "settlement_to_pools_usd", "settlement_to_pools_count",
    "settlement_from_pools_usd", "settlement_from_pools_count",
    "fees_from_pools_usd", "fees_from_pools_count",
    "fees_to_treasury_usd", "fees_to_referral_pool_usd", "fees_to_legacy_wallet_usd", "fees_to_other_usd",
    "payouts_to_pools_usd", "payouts_to_pools_count",
    "direct_transfers_to_pools_usd", "direct_transfers_to_pools_count",
    "pool_tvl_usd", "pools_live", "pools_dormant", "pools_unactivated", "pools_negative", "pools_balance_ge_1usd",
    "olp_vault_balance_usd", "olp_vault_in_usd", "olp_vault_out_usd",
    "treasury_balance_usd", "treasury_inflow_usd", "treasury_outflow_usd",
    "referral_pool_balance_usd", "referral_claims_usd", "referral_claims_count", "referral_unique_claimants",
    "loss_refunds_usd", "loss_refunds_count", "unclassified_usd",
]


def usd(micro):
    return round(micro / 1e6, 2)


def month_index(day):
    return (int(day[:4]) - 2025) * 12 + int(day[5:7]) - 1


def month_label(idx):
    return "%04d-%02d" % (2025 + idx // 12, idx % 12 + 1)


class State:
    """Per-pool arrays are indexed by pool creation order (0-based), not by store ids."""

    def __init__(self):
        self.day = -1  # index of the last complete day folded into the state
        self.bal = array.array("q")
        self.first_dep = array.array("h")  # day index of first user deposit, -1 = never
        self.ever_in = array.array("b")
        self.first_in = array.array("h")  # day index of the first USDC inflow of any kind, -1 = never
        self.last_any = array.array("h")
        self.last_user = array.array("h")
        self.cohort_seen = array.array("h")  # last month index counted for the cohort matrix
        self.sys_bal = [0] * (NSYS + 1)
        self.hist_any = {}
        self.hist_user = {}
        self.cohort = {}  # cohort month -> {month -> pools active}
        self.cohort_size = {}
        self.claims = {}  # pool index -> [count, micro]
        self.dep_hist = {}  # day index -> [counts[7], micro[7]]
        self.totals = {"tvl": 0, "live": 0, "dormant": 0, "negative": 0, "funded": 0,
                       "ge1": 0, "dep": 0, "wd": 0, "oracle_unmatched_in": 0, "third_party_deposits": 0,
                       "atomic_user_deposits": 0, "onboard_router_deposits": 0}
        self.fee_other = {}  # system id -> micro collected from pools (non treasury/referral/legacy)
        self.payout_other = {}
        self.rows = []

    PER_POOL = (("bal", "q"), ("first_dep", "h"), ("ever_in", "b"), ("first_in", "h"), ("last_any", "h"), ("last_user", "h"), ("cohort_seen", "h"))

    def save(self, path, extra):
        blob = {name: base64.b64encode(getattr(self, name).tobytes()).decode() for name, _ in self.PER_POOL}
        doc = {"version": 1, "day": self.day, "arrays": blob, "sys_bal": self.sys_bal,
               "hist_any": self.hist_any, "hist_user": self.hist_user, "cohort": self.cohort,
               "cohort_size": self.cohort_size, "claims": self.claims, "dep_hist": self.dep_hist,
               "totals": self.totals, "fee_other": self.fee_other, "payout_other": self.payout_other,
               "rows": self.rows, "extra": extra}
        tmp = path + ".tmp"
        with gzip.open(tmp, "wt", compresslevel=6) as handle:
            json.dump(doc, handle, separators=(",", ":"))
        os.replace(tmp, path)

    @classmethod
    def load(cls, path):
        with gzip.open(path, "rt") as handle:
            doc = json.load(handle)
        st = cls()
        st.day = doc["day"]
        for name, code in cls.PER_POOL:
            arr = array.array(code)
            arr.frombytes(base64.b64decode(doc["arrays"][name]))
            setattr(st, name, arr)
        st.sys_bal = doc["sys_bal"]
        for name in ("hist_any", "hist_user", "cohort_size", "claims", "dep_hist", "fee_other", "payout_other"):
            setattr(st, name, {int(k): v for k, v in doc[name].items()})
        st.cohort = {int(k): {int(m): n for m, n in v.items()} for k, v in doc["cohort"].items()}
        st.totals = doc["totals"]
        st.rows = doc["rows"]
        return st, doc.get("extra", {})


def fold_day(db, st, didx, day, lo, hi, idx_of, partial):
    """Fold blocks [lo, hi] (UTC day `day`) into the state and return the daily row."""
    T = st.totals
    # 1. pools created today
    created = db.execute("SELECT id FROM pools WHERE block BETWEEN ? AND ? ORDER BY block, lidx", (lo, hi)).fetchall()
    for (pid,) in created:
        idx_of[pid] = len(st.bal)
        st.bal.append(0)
        st.first_dep.append(-1)
        st.ever_in.append(0)
        st.first_in.append(-1)
        st.last_any.append(-1)
        st.last_user.append(-1)
        st.cohort_seen.append(-1)

    delta, inflow = {}, set()
    dep, wd = {}, {}
    m = dict.fromkeys(("dep_n", "wd_n", "set_in", "set_in_n", "set_out", "set_out_n", "fee", "fee_n", "fee_tre", "fee_ref",
                       "fee_leg", "fee_oth", "pay", "pay_n", "pay_leg", "pay_leg_n", "claim", "claim_n", "unclassified", "direct", "direct_n"), 0)
    claimants = set()
    bins_n, bins_s = [0] * 7, [0] * 7

    def add_in(p, amount):
        delta[p] = delta.get(p, 0) + amount
        inflow.add(p)

    def add_dep(p, amount, count=1, sized=True):
        dep[p] = dep.get(p, 0) + amount
        m["dep_n"] += count
        if sized:
            b = bisect.bisect_right(DEP_BINS, amount / 1e6)
            bins_n[b] += 1
            bins_s[b] += amount

    # 2. transfers that do not touch the Oracle, grouped by (from, to)
    for f, t, n, s in db.execute(
            "SELECT f, t, COUNT(*), SUM(amt) FROM xfer WHERE block BETWEEN ? AND ? AND f<>? AND t<>? GROUP BY f, t", (lo, hi, ORACLE, ORACLE)):
        tp = idx_of.get(t)
        if tp is not None:
            add_in(tp, s)
            if f == R_SETTLE or f == VAULT:
                m["set_in"] += s
                m["set_in_n"] += n
            elif f == R_ONBOARD:
                T["onboard_router_deposits"] += n
                if n == 1:
                    add_dep(tp, s)
                else:
                    for (amount,) in db.execute("SELECT amt FROM xfer WHERE block BETWEEN ? AND ? AND f=? AND t=?", (lo, hi, f, t)):
                        add_dep(tp, amount)
            elif f > NSYS:  # plain ERC-20 transfer from an outside wallet straight to the pool (found by repair)
                m["direct"] += s
                m["direct_n"] += n
                if f in idx_of:  # pool-to-pool, never observed; keep both balances right
                    delta[idx_of[f]] = delta.get(idx_of[f], 0) - s
            else:
                m["unclassified"] += s
            continue
        fp = idx_of.get(f)
        if fp is not None:
            delta[fp] = delta.get(fp, 0) - s
            if t == VAULT:
                m["set_out"] += s
                m["set_out_n"] += n
            elif t <= NSYS:
                m["fee"] += s
                m["fee_n"] += n
                if t == TREASURY:
                    m["fee_tre"] += s
                elif t == REFERRAL:
                    m["fee_ref"] += s
                elif t == LEGACY:
                    m["fee_leg"] += s
                else:
                    m["fee_oth"] += s
                    st.fee_other[t] = st.fee_other.get(t, 0) + s
            else:
                m["unclassified"] += s

    # 3. Oracle pass-through: X -> Oracle -> pool in the same block with the same amount
    pend = []
    for block, lidx, f, t, amt in db.execute(
            "SELECT block, lidx, f, t, amt FROM xfer WHERE block BETWEEN ? AND ? AND (f=? OR t=?) ORDER BY block, lidx", (lo, hi, ORACLE, ORACLE)):
        if pend and pend[0][0] != block:
            T["oracle_unmatched_in"] += sum(p[2] for p in pend)
            pend = []
        if t == ORACLE:
            pend.append((block, f, amt))
            continue
        src = None
        for j in range(len(pend) - 1, -1, -1):
            if pend[j][2] == amt:
                src = pend[j][1]
                del pend[j]
                break
        tp = idx_of.get(t)
        if tp is None:
            m["unclassified"] += amt
            continue
        add_in(tp, amt)
        if src == VAULT:
            m["set_in"] += amt
            m["set_in_n"] += 1
        elif src is None:
            m["unclassified"] += amt
        elif src > NSYS:  # a user wallet funding its pool through Oracle.atomicDeposit
            T["atomic_user_deposits"] += 1
            add_dep(tp, amt)
        else:
            m["pay"] += amt
            m["pay_n"] += 1
            if src == REFERRAL:
                m["claim"] += amt
                m["claim_n"] += 1
                claimants.add(tp)
                rec = st.claims.setdefault(tp, [0, 0])
                rec[0] += 1
                rec[1] += amt
            elif src == LEGACY:
                m["pay_leg"] += amt
                m["pay_leg_n"] += 1
            else:
                st.payout_other[src] = st.payout_other.get(src, 0) + amt
    if pend:
        T["oracle_unmatched_in"] += sum(p[2] for p in pend)

    # 4. pool events whose counterparty is outside S (invisible to the transfer filter)
    for kind, pid, p1, p2, a1, a2 in db.execute("SELECT kind, pool, p1, p2, a1, a2 FROM ev WHERE block BETWEEN ? AND ? AND kind<5", (lo, hi)):
        p = idx_of[pid]
        if kind == 1:
            if p1 > NSYS:
                add_in(p, a1)
                add_dep(p, a1)
        elif kind == 2:
            if p1 > NSYS:
                delta[p] = delta.get(p, 0) - a1
                wd[p] = wd.get(p, 0) + a1
                m["wd_n"] += 1
        elif not (kind == 3 and a1 > 0 and a2 == 0):
            for party, amount in ((p1, a1), (p2, a2)):
                if amount > 0 and party > NSYS:
                    T["atomic_user_deposits"] += 1
                    add_in(p, amount)
                    add_dep(p, amount)

    # 5. apply to per-pool state
    bal, ever_in, first_in = st.bal, st.ever_in, st.first_in
    first_inflow = 0
    for p in inflow:
        if not ever_in[p]:
            ever_in[p] = 1
            first_in[p] = didx
            first_inflow += 1
            T["funded"] += 1
            if bal[p] == 0:
                T["dormant"] += 1  # corrected right below when the balance turns positive
    change_new = change_ret = 0
    for p, d in delta.items():
        old = bal[p]
        new = old + d
        bal[p] = new
        T["tvl"] += d
        if first_in[p] == didx:
            change_new += d
        else:
            change_ret += d
        if old >= 1_000_000:
            T["ge1"] -= 1
        if new >= 1_000_000:
            T["ge1"] += 1
        if old > 0:
            T["live"] -= 1
        elif old < 0:
            T["negative"] -= 1
        elif ever_in[p]:
            T["dormant"] -= 1
        if new > 0:
            T["live"] += 1
        elif new < 0:
            T["negative"] += 1
        elif ever_in[p]:
            T["dormant"] += 1

    new_accounts = 0
    flow_new = flow_ret = 0
    first_dep = st.first_dep
    cur_month = month_index(day)
    for p in dep:
        if first_dep[p] < 0:
            first_dep[p] = didx
            new_accounts += 1
            st.cohort_size[cur_month] = st.cohort_size.get(cur_month, 0) + 1
    for p in set(dep) | set(wd):
        net = dep.get(p, 0) - wd.get(p, 0)
        if first_dep[p] == didx:
            flow_new += net
        else:
            flow_ret += net
    dep_total, wd_total = sum(dep.values()), sum(wd.values())
    T["dep"] += dep_total
    T["wd"] += wd_total

    def touch(active, last, hist):
        for p in active:
            prev = last[p]
            if prev != didx:
                if prev >= 0:
                    hist[prev] -= 1
                last[p] = didx
                hist[didx] = hist.get(didx, 0) + 1

    active_any = set(delta)
    active_user = set(dep) | set(wd)
    touch(active_any, st.last_any, st.hist_any)
    touch(active_user, st.last_user, st.hist_user)
    seen = st.cohort_seen
    for p in active_any:
        fd = first_dep[p]
        if fd >= 0 and seen[p] != cur_month:
            seen[p] = cur_month
            row = st.cohort.setdefault(month_index(st.day_names[fd]), {})
            row[cur_month] = row.get(cur_month, 0) + 1

    def rolling(hist, span):
        return sum(hist.get(d, 0) for d in range(didx - span + 1, didx + 1))

    # 6. system balances (every transfer in or out of a system address is in the store)
    sys_in, sys_out = [0] * (NSYS + 1), [0] * (NSYS + 1)
    for t, s in db.execute("SELECT t, SUM(amt) FROM xfer WHERE block BETWEEN ? AND ? AND t<=? GROUP BY t", (lo, hi, NSYS)):
        sys_in[t] = s
    for f, s in db.execute("SELECT f, SUM(amt) FROM xfer WHERE block BETWEEN ? AND ? AND f<=? GROUP BY f", (lo, hi, NSYS)):
        sys_out[f] = s
    for sid in range(1, NSYS + 1):
        st.sys_bal[sid] += sys_in[sid] - sys_out[sid]

    st.dep_hist[didx] = [bins_n, bins_s]
    for old in [d for d in st.dep_hist if d < didx - 29]:
        del st.dep_hist[old]

    n_pools = len(bal)
    row = {
        "date": day, "first_block": lo, "last_block": hi, "partial": bool(partial),
        "pools_created": len(created), "pools_created_cum": n_pools,
        "accounts_first_funded": new_accounts, "accounts_funded_cum": sum(st.cohort_size.values()),
        "pools_first_inflow": first_inflow, "pools_funded_cum": T["funded"],
        "pool_balance_change_usd": usd(change_new + change_ret), "pool_balance_change_new_usd": usd(change_new),
        "pool_balance_change_returning_usd": usd(change_ret),
        "user_deposits_usd": usd(dep_total), "user_deposits_count": m["dep_n"], "user_deposits_cum_usd": usd(T["dep"]),
        "user_withdrawals_usd": usd(wd_total), "user_withdrawals_count": m["wd_n"], "user_withdrawals_cum_usd": usd(T["wd"]),
        "net_flow_usd": usd(dep_total - wd_total), "net_flow_new_usd": usd(flow_new), "net_flow_returning_usd": usd(flow_ret),
        "unique_depositors": len(dep), "unique_withdrawers": len(wd),
        "active_pools": len(active_any), "active_pools_user_initiated": len(active_user),
        "active_pools_7d": rolling(st.hist_any, 7), "active_pools_30d": rolling(st.hist_any, 30),
        "active_pools_user_initiated_7d": rolling(st.hist_user, 7), "active_pools_user_initiated_30d": rolling(st.hist_user, 30),
        "settlement_to_pools_usd": usd(m["set_in"]), "settlement_to_pools_count": m["set_in_n"],
        "settlement_from_pools_usd": usd(m["set_out"]), "settlement_from_pools_count": m["set_out_n"],
        "fees_from_pools_usd": usd(m["fee"]), "fees_from_pools_count": m["fee_n"],
        "fees_to_treasury_usd": usd(m["fee_tre"]), "fees_to_referral_pool_usd": usd(m["fee_ref"]),
        "fees_to_legacy_wallet_usd": usd(m["fee_leg"]), "fees_to_other_usd": usd(m["fee_oth"]),
        "payouts_to_pools_usd": usd(m["pay"]), "payouts_to_pools_count": m["pay_n"],
        "pool_tvl_usd": usd(T["tvl"]), "pools_live": T["live"], "pools_dormant": T["dormant"],
        "pools_unactivated": n_pools - T["funded"], "pools_negative": T["negative"], "pools_balance_ge_1usd": T["ge1"],
        "direct_transfers_to_pools_usd": usd(m["direct"]), "direct_transfers_to_pools_count": m["direct_n"],
        "olp_vault_balance_usd": usd(st.sys_bal[VAULT]), "olp_vault_in_usd": usd(sys_in[VAULT]), "olp_vault_out_usd": usd(sys_out[VAULT]),
        "treasury_balance_usd": usd(st.sys_bal[TREASURY]), "treasury_inflow_usd": usd(sys_in[TREASURY]), "treasury_outflow_usd": usd(sys_out[TREASURY]),
        "referral_pool_balance_usd": usd(st.sys_bal[REFERRAL]), "referral_claims_usd": usd(m["claim"]),
        "referral_claims_count": m["claim_n"], "referral_unique_claimants": len(claimants),
        "loss_refunds_usd": usd(m["pay_leg"]), "loss_refunds_count": m["pay_leg_n"], "unclassified_usd": usd(m["unclassified"]),
    }
    return [row[c] for c in COLUMNS]


def write_json(path, doc):
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(doc, handle, separators=(",", ":"))
        handle.write("\n")
    os.replace(tmp, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=None)
    parser.add_argument("--out", default=C.DATA_DIR)
    parser.add_argument("--state-in", default=None, help="resume from a saved state (state.json.gz)")
    parser.add_argument("--state-out", default=None, help="save the state at the last complete UTC day")
    parser.add_argument("--no-anchor-fetch", action="store_true", help="do not look up missing midnight blocks")
    parser.add_argument("--through-block", type=int, default=None, help="stop at this block (default: contiguous coverage)")
    parser.add_argument("--reconcile", action="store_true", help="Multicall3 balanceOf for every pool at the as-of block and compare with the ledger")
    args = parser.parse_args()
    started = time.time()
    db = C.open_db(args.db)
    floor = I.floor_block(db)
    covered = I.synced_block(db)
    if args.through_block:
        covered = min(covered, args.through_block)
    covered_ts = int(C.rpc_any("eth_getBlockByNumber", [hex(covered), False])["timestamp"], 16)
    if not args.no_anchor_fetch:
        I.build_anchors(db, covered, covered_ts)
    anchors = db.execute("SELECT day, block, ts FROM anchors ORDER BY day").fetchall()
    day_names = [a[0] for a in anchors]

    if args.state_in:
        st, extra = State.load(args.state_in)
        first_day = st.day + 1
        if floor > anchors[first_day][1]:
            raise SystemExit("store floor %d is above the first block needed %d" % (floor, anchors[first_day][1]))
        # truncate anything after the saved complete day (previous partial rows are recomputed)
        st.rows = [r for r in st.rows if not r[COLUMNS.index("partial")]]
    else:
        st = State()
        first_day = 0
        if floor != C.START_BLOCK:
            raise SystemExit("a full rebuild needs a store that starts at the factory deployment")
    st.day_names = day_names

    idx_of = {}
    first_block = anchors[first_day][1] if first_day < len(anchors) else covered + 1
    for n, (pid,) in enumerate(db.execute("SELECT id FROM pools WHERE block<? ORDER BY block, lidx", (max(first_block, C.START_BLOCK) if first_day else 0,))):
        idx_of[pid] = n
    if len(idx_of) != len(st.bal):
        raise SystemExit("state has %d pools but the store has %d before block %d" % (len(st.bal), len(idx_of), first_block))

    saved_at = None
    for didx in range(first_day, len(anchors)):
        day, block, _ = anchors[didx]
        lo = max(block, C.START_BLOCK) if didx == 0 else block
        if lo > covered:
            break
        nxt = anchors[didx + 1][1] if didx + 1 < len(anchors) else None
        complete = nxt is not None and nxt - 1 <= covered
        hi = nxt - 1 if complete else covered
        if not complete and args.state_out and saved_at is None:
            st.save(args.state_out, {"last_block": lo - 1, "next_block": lo, "saved_utc": C.utc_iso(time.time())})
            saved_at = lo - 1
        row = fold_day(db, st, didx, day, lo, hi, idx_of, not complete)
        st.rows.append(row)
        if complete:
            st.day = didx
        if didx % 60 == 0:
            I.log("aggregated through", day)
    if args.state_out and saved_at is None:
        last = anchors[st.day + 1][1] - 1
        st.save(args.state_out, {"last_block": last, "next_block": last + 1, "saved_utc": C.utc_iso(time.time())})

    recon_doc = None
    if args.reconcile:
        chain = I.take_snapshot(db, covered)
        bad = [(pid, st.bal[i], chain.get(pid, 0)) for pid, i in idx_of.items() if st.bal[i] != chain.get(pid, 0)]
        sys_bad = {C.SYSTEM[sid - 1]: [usd(st.sys_bal[sid]), usd(chain.get(sid, 0))] for sid in range(1, NSYS + 1)
                   if st.sys_bal[sid] != chain.get(sid, 0)}
        chain_total = sum(chain.get(pid, 0) for pid in idx_of)
        recon_doc = {"block": covered, "method": "Multicall3 aggregate3 of USDC.balanceOf for every pool and system address at the as-of block",
                     "pools_checked": len(idx_of), "pools_exact": len(idx_of) - len(bad), "pools_mismatched": len(bad),
                     "abs_residual_usd": round(sum(abs(a - b) for _, a, b in bad) / 1e6, 6),
                     "chain_total_usd": round(chain_total / 1e6, 6), "ledger_total_usd": round(st.totals["tvl"] / 1e6, 6),
                     "system_addresses_checked": NSYS, "system_mismatches": sys_bad, "checked_utc": C.utc_iso(time.time())}
        C.set_meta(db, "last_reconcile", json.dumps(recon_doc))
        C.set_meta(db, "last_reconcile_bad", json.dumps([p for p, _, _ in bad][:5000]))
        db.commit()
        I.log("reconcile @%d: %d pools, %d mismatched, residual %.6f USDC, system mismatches %d"
              % (covered, len(idx_of), len(bad), recon_doc["abs_residual_usd"], len(sys_bad)))
        for pid, ledger, onchain in sorted(bad, key=lambda r: -abs(r[1] - r[2]))[:15]:
            address = C.b2a(db.execute("SELECT a FROM addr WHERE id=?", (pid,)).fetchone()[0])
            I.log("  mismatch %s ledger %.6f chain %.6f" % (address, ledger / 1e6, onchain / 1e6))

    target = int(C.get_meta(db, "target_block", 0))
    coverage = {
        "first_block": C.START_BLOCK, "last_block": covered, "first_day": C.START_DAY,
        "last_complete_day": day_names[st.day] if st.day >= 0 else None,
        "backfill_complete": bool(covered >= target and target > 0 and floor == C.START_BLOCK) or bool(args.state_in),
        "note": "rows cover every block from first_block to last_block; the final row is partial when partial=true",
    }
    head = {"as_of_block": covered, "as_of_utc": C.utc_iso(covered_ts), "source": SOURCE, "chain": C.CHAIN,
            "generated_utc": C.utc_iso(time.time()), "coverage": coverage}
    recon = json.dumps(recon_doc) if recon_doc else C.get_meta(db, "last_reconcile")
    head["reconciliation"] = json.loads(recon) if recon else None
    os.makedirs(args.out, exist_ok=True)

    # ---- daily.json
    write_json(os.path.join(args.out, "daily.json"), dict(head, **{
        "method": ("One row per UTC day (midnight blocks found by binary search on block timestamps). User deposits = SettlementPool "
                   "Deposited events paid by a non-system wallet plus onboarding-router and atomic deposits; withdrawals = Withdrawn "
                   "events; settlement, fees and payouts = USDC Transfer logs between pools and Variational system addresses. Pool "
                   "balances are the running sum of every indexed movement. Amounts in USDC, 2 decimals. Wallet roles for "
                   "referral_claims and loss_refunds are inferred from flow shape and match Variational's disclosed cumulative totals."),
        "definitions": {
            "accounts_first_funded": "pools receiving their first user deposit that day",
            "pools_first_inflow": "pools receiving their first USDC of any kind that day (deposit, settlement or payout)",
            "net_flow": "user deposits minus user withdrawals; new = accounts first funded that day, returning = all others",
            "pool_balance_change": ("end-of-day change in all-pool balance (deposits - withdrawals + settlement in - settlement out "
                                    "- fees + payouts); new = pools with their first inflow that day, returning = all others"),
            "active_pools": "pools with any USDC movement that day (deposit, withdrawal, settlement, fee, payout)",
            "active_pools_user_initiated": "pools with a user deposit or withdrawal that day",
            "pools_live": "pools with a USDC balance > 0 at end of day",
            "pools_dormant": "pools that received USDC at some point and hold 0 at end of day",
            "pools_unactivated": "pools that never received USDC",
            "pools_balance_ge_1usd": "pools holding at least 1 USDC at end of day",
            "direct_transfers_to_pools": "plain ERC-20 transfers sent straight to a pool by a non-system wallet (no pool event); counted in balances, not in deposits",
            "settlement_to_pools": "Core OLP Vault -> pool (direct, via settlement router, or via Oracle)",
            "settlement_from_pools": "pool -> Core OLP Vault",
            "fees_from_pools": "Oracle.collectFees pulls: pool -> treasury / referral reward pool / legacy fee wallet / other provider",
            "payouts_to_pools": "Oracle.transferFromOLPToPool from a non-vault system wallet (referral pool, legacy wallet) into a pool",
            "referral_claims": "payouts_to_pools funded by wallet 0x6c3d445b... (referral reward pool; label inferred, cumulative total tracks the officially disclosed referral rewards on 17 bi-weekly reports)",
            "loss_refunds": "payouts_to_pools funded by wallet 0xc4775613... (loss refund pool, 5 Sep 2025 to 24 Mar 2026; label inferred, cumulative total matches the officially disclosed losses refunded on 6 and 20 Mar 2026)",
        },
        "columns": COLUMNS, "rows": st.rows}))

    # ---- pools_snapshot.json
    T = st.totals
    order = sorted((i for i in range(len(st.bal)) if st.bal[i] > 0), key=lambda i: -st.bal[i])
    total = sum(st.bal[i] for i in order)
    inv = {v: k for k, v in idx_of.items()}
    top = order[:1000]
    top_ids = [inv[i] for i in top]
    pool_addr, pool_user = {}, {}
    for start in range(0, len(top_ids), 500):
        marks = ",".join(map(str, top_ids[start:start + 500]))
        for pid, pa, ua in db.execute("SELECT p.id, a.a, u.a FROM pools p JOIN addr a ON a.id=p.id JOIN addr u ON u.id=p.user WHERE p.id IN (%s)" % marks):
            pool_addr[pid], pool_user[pid] = C.b2a(pa), C.b2a(ua)
    tiers_n, tiers_s = [0] * 6, [0] * 6
    for i in order:
        b = bisect.bisect_right(TIERS, st.bal[i] / 1e6)
        tiers_n[b] += 1
        tiers_s[b] += st.bal[i]
    hist_n, hist_s = [0] * 7, [0] * 7
    for counts, sums in st.dep_hist.values():
        for b in range(7):
            hist_n[b] += counts[b]
            hist_s[b] += sums[b]
    hist_days = sorted(st.dep_hist)
    snap_doc = dict(head, **{
        "method": ("Balances are ledger-reconstructed at as_of_block (sum of every indexed USDC movement per pool) and are checked "
                   "against Multicall3 balanceOf at a pinned block (see reconciliation). Shares are of the all-pool total."),
        "total_pool_balance_usd": usd(total), "pools_with_balance": len(order), "pools_total": len(st.bal),
        "concentration": {"top_%d_share" % n: (round(sum(st.bal[i] for i in order[:n]) / total, 6) if total else None) for n in (1, 10, 100, 1000)},
        "tiers": [{"tier": TIER_LABELS[b], "pools": tiers_n[b], "balance_usd": usd(tiers_s[b]),
                   "share": round(tiers_s[b] / total, 6) if total else None} for b in range(6)],
        "deposit_size_histogram_30d": {
            "first_day": day_names[hist_days[0]] if hist_days else None, "last_day": day_names[hist_days[-1]] if hist_days else None,
            "bins": [{"bin": DEP_BIN_LABELS[b], "deposits": hist_n[b], "usd": usd(hist_s[b])} for b in range(7)]},
        "top_pools_columns": ["rank", "pool", "user", "balance_usd", "share", "first_funded"],
        "top_pools": [[n + 1, pool_addr[inv[i]], pool_user[inv[i]], usd(st.bal[i]), round(st.bal[i] / total, 6),
                       day_names[st.first_dep[i]] if st.first_dep[i] >= 0 else None] for n, i in enumerate(top)]})
    write_json(os.path.join(args.out, "pools_snapshot.json"), snap_doc)

    # ---- cohorts.json
    months = sorted(set(m for row in st.cohort.values() for m in row) | set(st.cohort_size))
    cohorts = []
    for cm in sorted(st.cohort_size):
        size = st.cohort_size[cm]
        active = st.cohort.get(cm, {})
        cohorts.append({"cohort": month_label(cm), "size": size,
                        "active": [[month_label(mm), active.get(mm, 0), round(active.get(mm, 0) / size, 4)] for mm in months if mm >= cm]})
    write_json(os.path.join(args.out, "cohorts.json"), dict(head, **{
        "method": ("Cohort = UTC month of a pool's first user deposit. A pool is active in a month when it has any USDC movement "
                   "(deposit, withdrawal, settlement, fee, payout) in that month. active rows are [month, pools active, share of cohort]. "
                   "The latest month is partial."),
        "cohorts": cohorts}))

    # ---- referrals.json
    ranked = sorted(st.claims.items(), key=lambda kv: -kv[1][1])
    claim_total = sum(v[1] for v in st.claims.values())
    top_claim = ranked[:200]
    ids = [inv[int(i)] for i, _ in top_claim]
    who = {}
    for start in range(0, len(ids), 500):
        marks = ",".join(map(str, ids[start:start + 500]))
        for pid, pa, ua in db.execute("SELECT p.id, a.a, u.a FROM pools p JOIN addr a ON a.id=p.id JOIN addr u ON u.id=p.user WHERE p.id IN (%s)" % marks):
            who[pid] = (C.b2a(ua), C.b2a(pa))
    ci = {c: n for n, c in enumerate(COLUMNS)}
    write_json(os.path.join(args.out, "referrals.json"), dict(head, **{
        "method": ("A referral claim is a USDC payout referral reward pool wallet -> Oracle -> claimant's settlement pool "
                   "(Oracle.transferFromOLPToPool). The wallet is funded by Oracle.collectFees pulls from settlement pools. "
                   "The wallet label is inferred, see findings.md."),
        "referral_reward_pool": C.REFERRAL_POOL,
        "referral_reward_pool_balance_usd": usd(st.sys_bal[REFERRAL]),
        "claims_total_usd": usd(claim_total), "claims_count": sum(v[0] for v in st.claims.values()), "claimants": len(st.claims),
        "top_claimants_columns": ["rank", "wallet", "pool", "claims", "total_usd", "share"],
        "top_claimants": [[n + 1, who[inv[int(i)]][0], who[inv[int(i)]][1], v[0], usd(v[1]), round(v[1] / claim_total, 6)]
                          for n, (i, v) in enumerate(top_claim)],
        "daily_columns": ["date", "claims_usd", "claims_count", "unique_claimants", "funding_from_pools_usd", "balance_usd"],
        "daily": [[r[ci["date"]], r[ci["referral_claims_usd"]], r[ci["referral_claims_count"]], r[ci["referral_unique_claimants"]],
                   r[ci["fees_to_referral_pool_usd"]], r[ci["referral_pool_balance_usd"]]] for r in st.rows
                  if r[ci["referral_claims_count"]] or r[ci["fees_to_referral_pool_usd"]] or r[ci["referral_pool_balance_usd"]]]}))

    diag = {"totals": T, "fee_other": {C.SYSTEM[k - 1]: usd(v) for k, v in st.fee_other.items()},
            "payout_other": {C.SYSTEM[k - 1]: usd(v) for k, v in st.payout_other.items()},
            "system_balances": {C.SYSTEM[i - 1]: usd(st.sys_bal[i]) for i in range(1, NSYS + 1) if st.sys_bal[i]},
            "days": len(st.rows), "pools": len(st.bal), "as_of_block": covered, "seconds": round(time.time() - started, 1)}
    with open(os.path.join(C.CACHE_DIR, "aggregate_diag.json"), "w") as handle:
        json.dump(diag, handle, indent=1)
    I.log("aggregate done:", json.dumps({k: diag[k] for k in ("days", "pools", "as_of_block", "seconds")}))
    return 3 if (recon_doc and (recon_doc["pools_mismatched"] or recon_doc["system_mismatches"])) else 0


if __name__ == "__main__":
    sys.exit(main())
