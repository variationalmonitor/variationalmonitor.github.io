#!/usr/bin/python3
"""The deploy gate for Variational Monitor: numbers first, then the page. Python standard library only.

    /usr/bin/python3 qa/gate.py                         check site/data against the store (local layout)
    /usr/bin/python3 qa/gate.py --render                also run qa/render.mjs (Playwright) and merge its findings
    /usr/bin/python3 qa/gate.py --previous DIR          compare history with the previously deployed files
                                                        (DIR is what `ci/live.py fetch --out DIR` wrote)
    /usr/bin/python3 qa/gate.py --now 2026-11-22T10:00:00Z   freshness against a fixed time (testing)

Exit status is 1 when any blocking check fails, otherwise 0. Warnings never change the exit status.
Writes <out>/report.md and <out>/report.json ({"numbers_ok", "page_ok", "blocking", "warnings"}); the publish
job reads report.json: the store is saved when numbers_ok, the site is deployed only when everything passed.

Blocking
  builders     every builder's own --check
  files        every contract file present, strict JSON, no "fixture": true anywhere, as_of on every file
  schema       contract keys, aligned arrays, ascending dates, group and class keys, rounding, value ranges
  identities   recomputed here, not by the builders: split sums (volume_daily.split: crypto + TradFi = total),
               long + short, statement identities, per-dollar figures, flow legs, tiers, match rate
  ties         across files and against the raw store: oi_daily against the listings' long and short,
               olp_market against oi_daily and the listings, peers against oi_daily, quote_depth against the
               listings, markets_latest against the newest raw snapshot (every figure, quotes recomputed with
               the two-sided rule), monthly volume against volume_daily, swap_pairs against the listings,
               tradfi against the group and class splits, dual-sided open interest, every venue of
               execution_venue against the sample store, on-chain treasury inflow against the reported
               two-week inflow, volume_daily's DefiLlama days against the stored export (with DefiLlama's
               residual against each reported two-week total), research.json against its configs
  freshness    newest snapshot under 90 minutes old, on-chain under 26 hours, reports parsed; the intraday window
               (markets_scope.json, scope all.all) ends within 90 minutes of the data time and, once capture has
               run seven days, spans at least 6.5 days: a window cut short by missing raw snapshots fails
  history      values already deployed may not change beyond the tolerances in HISTORY (a date whose source
               source changed is a restatement the contract defines: reported, not blocked); no date may vanish
  page         qa/render.mjs: every route, range and width; console errors; empty cards; horizontal scroll; residency
Warnings: word budgets, banned words, colons in titles, non-monotone Polymarket strikes, stale external caches,
          a reported two-week period where DefiLlama's daily volume is more than 7% off the reported total,
          a venue without a matched sample in 48 hours (a venue that refuses the runner's location is expected).

Paths: VM_SITE_DATA, VM_STORE, VM_SNAPSHOTS, VM_ONCHAIN_DATA (see ci/env.sh), with the local layout as default.
"""
import argparse
import datetime as dt
import glob
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

SITE_DATA = os.environ.get("VM_SITE_DATA") or os.path.join(ROOT, "site", "data")
STORE = os.environ.get("VM_STORE") or os.path.join(ROOT, ".cache", "store")
SNAPSHOT_DIRS = [p for p in os.environ.get("VM_SNAPSHOTS", "").split(os.pathsep) if p] or [
    os.path.join(ROOT, ".cache", "snapshots_seed"), os.path.join(ROOT, ".cache", "snapshots"), os.path.join(STORE, "snapshots")]
ONCHAIN_DATA = os.environ.get("VM_ONCHAIN_DATA") or os.path.join(ROOT, "data", "onchain")

GROUPS = ["crypto_majors", "other_crypto", "tradfi_perp", "tradfi_swap"]
CLASSES = ["crypto", "equity", "etf", "index", "commodity", "fx", "pre_ipo"]      # Variational's own class names (contract v3)
TRADFI_CLASSES = CLASSES[1:]
TIERS = ["Under $100", "$100 to $1K", "$1K to $10K", "$10K to $100K", "$100K to $1M", "Over $1M"]
OTHER_VENUES_STALE_H = 48          # a venue with no matched sample for this long is a warning
INFLOW_TOLERANCE, INFLOW_TOLERANCE_TOTAL = 0.05, 0.01      # on-chain treasury inflow against the reported two-week inflow
TOP_GROUPS = ["crypto_majors", "other_crypto", "tradfi"]   # contract v3.1: the three groups of the Markets page
SPREADS_START = "2026-04-17"                               # spreads_daily is published from this day only
SPREADS_TOLERANCE, SPREADS_TOLERANCE_TOTAL = 0.05, 0.015   # implied spreads against reported spreads paid: per period, overall
LIFETIME_PRINT_TOLERANCE = 500                             # a stated lifetime OLP figure against the previous one plus the period's PnL
PERIODS_PER_YEAR = 26
DOLLAR_LINES = ["spreads_paid", "net_revenue", "net_profit", "treasury_inflow", "olp_pnl"]
RANGE_KEYS = ["7D", "30D", "90D", "180D", "1Y", "All"]
BUILDERS = ["market", "execution", "chain", "reports"]
BANNED = ["revolutionary", "game-changing", "notably", "comprehensive", "robust", "landscape", "delve"]

INTRADAY_DAYS = 7                 # the intraday series: one point per snapshot over the trailing seven days
INTRADAY_MIN_SPAN_DAYS = 6.5      # once capture has run for seven days, the window must be at least this long
INTRADAY_MAX_LAG_MIN = 90         # and must end this close to the data time
INTRADAY_MAX_MISSING = 0.10       # share of the raw snapshots in the window that may be absent (repeated API responses)
MAX_SNAPSHOT_AGE_MIN = 90
MAX_ONCHAIN_AGE_H = 26
MAX_TELEGRAM_CHECK_H = 3
MAX_EXTERNAL_AGE_D = 3
RECONCILE_MAX_SHARE = 1e-4        # on-chain ledger against balanceOf: unexplained USD over all pool balances

# Contract keys per file (extra keys are allowed and listed as information).
REQUIRED = {
    "meta": ["built_at", "data_as_of", "publication_date", "article_url", "split_start", "continuous_start",
             "stale_after_minutes", "sources"],
    "volume_daily": ["as_of", "sources", "dates", "total", "cumulative", "src", "group", "cls", "top", "split", "approx"],
    "oi_daily": ["as_of", "sources", "dates", "total", "src", "group", "cls", "top", "long", "short", "dual"],
    "peers_oi": ["as_of", "dates", "variational", "venues"],
    "markets_latest": ["as_of", "rows"],
    "markets_listed": ["as_of", "sources", "dates", "total", "src", "perp", "swap"],
    "market/index": ["tickers"],
    "quote_depth": ["as_of", "dates", "quoted_1m", "median_ba100k"],
    "swap_pairs": ["as_of", "pairs", "dates", "perp", "swap", "range", "approx"],
    "execution_venue": ["as_of", "fee_note", "venues", "markets", "dates", "by", "range", "match"],
    "financials": ["as_of", "periods", "lifetime_olp_pnl", "olp_summary", "cumulative", "annualised"],
    "spreads_daily": ["as_of", "dates", "implied", "share"],
    "treasury": ["as_of", "dates", "balance", "inflow", "outflow", "reported"],
    "olp_market": ["as_of", "dates", "net_long", "matched_share"],
    "olp_chain": ["as_of", "dates", "vault_balance", "settlement"],
    "accounts": ["as_of", "dates", "first_funded", "active_1d", "active_7d", "active_30d", "deposits", "withdrawals",
                 "net_new", "net_returning", "pool_balance", "monthly", "states"],
    "pools_snapshot": ["as_of", "total_balance", "tiers", "top_share", "top"],
    "token": ["as_of", "fdv", "launch", "points", "allocation"],
    "research": ["as_of", "pieces"],
    "tvl": ["as_of", "dates", "total"],
    "cohorts": ["as_of", "cohorts"],
    "rewards": ["as_of", "dates", "referral", "loss_refund", "claimants", "top10_share"],
    "markets_scope": ["as_of", "scopes"],
    "tradfi": ["as_of", "dates", "share_volume", "share_oi", "volume_cls", "oi_cls", "volume_inst", "oi_inst", "approx", "listed"],
    "concentration": ["as_of", "dates", "top10_share", "approx"],
}
RESEARCH_KEYS = ["slug", "title", "publisher", "date", "url", "status"]        # research.json: one entry per piece
RESEARCH_CONFIG_KEYS = {"title", "publisher", "published_at", "scheduled_for", "url"}
VOLUME_SOURCES = ["omni", "defillama", "report_avg"]
SPLIT_KEYS = ["crypto", "tradfi"]                          # volume_daily.split: crypto majors + other crypto, TradFi perps + swaps
DEFILLAMA_RESIDUAL_WARN = 0.07                             # DefiLlama's daily volume against a reported two-week total
ROW_KEYS = ["ticker", "name", "type", "group", "cls", "vol24h", "oi_long", "oi_short", "funding_ann", "fin_long_ann",
            "fin_short_ann", "ba1k_bps", "ba100k_bps", "ba1m_bps", "quote_at", "turnover", "long_share", "listed"]
MARKET_KEYS = ["ticker", "name", "type", "group", "cls", "dates", "vol24h", "oi_long", "oi_short", "funding_ann",
               "fin_long_ann", "fin_short_ann", "ba1k_bps", "ba100k_bps", "ba1m_bps"]
PERIOD_KEYS = ["end", "start", "days", "volume", "spreads_paid", "mm_costs", "rewards", "treasury_inflow", "olp_pnl",
               "net_revenue", "net_profit", "spreads_bps", "net_revenue_bps", "net_profit_bps", "olp_bps", "cost_share",
               "net_revenue_margin", "net_profit_margin", "rewards_share", "treasury_share", "treasury_inflow_onchain",
               "lifetime_olp_pnl", "source_url", "notes"]

# History against the previous deployment. Per file: series paths ("a.*" = every key under a), relative and
# absolute tolerance for settled dates, how many trailing dates of the previous file are still provisional and
# the relative tolerance there. `src` names the array of source codes: a date whose code changed is a
# restatement by the contract's source order and is reported, not blocked. `reanchor` names series that the
# pipeline re-ties to a newer counter reading (volume_daily.cumulative after a new report or snapshot).
HISTORY = {
    "volume_daily": {"series": ["total", "group.*", "cls.*", "top.*", "split.*"], "rel": 0.005, "abs": 1.0, "recent": 3, "recent_rel": 0.35,
                     "src": "src", "reanchor": ["cumulative"], "reanchor_days": 16, "reanchor_rel": 0.02},
    "oi_daily": {"series": ["total", "long", "short", "dual", "group.*", "cls.*", "top.*"], "rel": 0.005, "abs": 1.0, "recent": 3,
                 "recent_rel": 0.15, "src": "src"},
    # peers_oi.variational is oi_daily.total (tied above), so only the other venues are compared here
    "peers_oi": {"series": [], "rel": 0.02, "abs": 1.0, "recent": 3, "recent_rel": 0.25},
    "markets_listed": {"series": ["total", "perp", "swap"], "rel": 0.0, "abs": 0.0, "recent": 1, "recent_rel": 0.05, "src": "src"},
    "quote_depth": {"series": ["quoted_1m.*", "median_ba100k.*"], "rel": 0.0, "abs": 0.011, "recent": 0, "recent_rel": 0.0},
    "olp_market": {"series": ["net_long.total", "net_long.group.*", "matched_share"], "rel": 0.0, "abs": 0.011, "recent": 0, "recent_rel": 0.0},
    "swap_pairs": {"series": ["perp.ba100k.*", "perp.carry.*", "swap.ba100k.*", "swap.carry.*"], "rel": 0.0, "abs": 0.011, "recent": 0, "recent_rel": 0.0},
    "tradfi": {"series": ["share_volume", "share_oi", "volume_cls.*", "oi_cls.*", "volume_inst.*", "oi_inst.*"], "rel": 0.005, "abs": 1.0,
               "recent": 3, "recent_rel": 0.35},
    "concentration": {"series": ["top10_share"], "rel": 0.0, "abs": 0.00011, "recent": 0, "recent_rel": 0.0},
    "tvl": {"series": ["total"], "rel": 1e-6, "abs": 1.0, "recent": 0, "recent_rel": 0.0},
    # days after the newest report use its treasury share until the next report states its own: those are recomputed
    "spreads_daily": {"series": ["implied", "share"], "rel": 1e-6, "abs": 1.0, "recent": 16, "recent_rel": 1.0},
    "rewards": {"series": ["referral", "loss_refund"], "rel": 1e-6, "abs": 1.0, "recent": 0, "recent_rel": 0.0},
    "treasury": {"series": ["balance", "inflow", "outflow"], "rel": 1e-6, "abs": 1.0, "recent": 0, "recent_rel": 0.0},
    "olp_chain": {"series": ["vault_balance"], "rel": 1e-6, "abs": 1.0, "recent": 0, "recent_rel": 0.0},
    "accounts": {"series": ["first_funded", "active_1d", "active_7d", "active_30d", "deposits", "withdrawals", "net_new",
                            "net_returning", "pool_balance", "states.*"], "rel": 1e-6, "abs": 1.0, "recent": 0, "recent_rel": 0.0},
}
MARKET_HISTORY = {"series": ["vol24h", "oi_long", "oi_short", "funding_ann", "fin_long_ann", "fin_short_ann", "ba1k_bps",
                             "ba100k_bps", "ba1m_bps", "turnover", "long_share"], "rel": 0.0, "abs": 0.011, "recent": 0, "recent_rel": 0.0}


# ---------------------------------------------------------------- report

class Report:
    def __init__(self):
        self.rows = []          # (section, name, status, [detail lines])

    def add(self, section, name, status, details=None):
        self.rows.append((section, name, status, list(details or [])))

    def check(self, section, name, problems, ok_note=None, warn=False):
        """Record one check. `problems` is a list of strings; empty means it passed."""
        problems = list(problems)
        if problems:
            self.add(section, name, "warn" if warn else "fail", problems)
        else:
            self.add(section, name, "pass", [ok_note] if ok_note else [])
        return not problems

    def count(self, status, page=None):
        return sum(1 for s, _, st, _ in self.rows if st == status and (page is None or (s == "page") == page))

    def render(self, head):
        lines = [head, ""]
        fails = [r for r in self.rows if r[2] == "fail"]
        warns = [r for r in self.rows if r[2] == "warn"]
        skips = [r for r in self.rows if r[2] == "skip"]
        passes = [r for r in self.rows if r[2] == "pass"]

        def block(title, rows, cap):
            if not rows:
                return
            lines.append(title)
            for section, name, _, details in rows:
                lines.append("  [%s] %s" % (section, name))
                for d in details[:cap]:
                    lines.append("      " + d)
                if len(details) > cap:
                    lines.append("      ... and %d more" % (len(details) - cap))
            lines.append("")

        block("BLOCKING (%d)" % len(fails), fails, 12)
        block("WARNINGS (%d)" % len(warns), warns, 8)
        block("NOT RUN (%d)" % len(skips), skips, 3)
        lines.append("PASSED (%d)" % len(passes))
        for section, name, _, details in passes:
            lines.append("  [%s] %s%s" % (section, name, ("  ·  " + details[0]) if details else ""))
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- helpers

def strict_load(path):
    def refuse(token):
        raise ValueError("%s is not JSON" % token)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh, parse_constant=refuse)


def parse_time(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")


def is_time(s):
    try:
        parse_time(s)
        return True
    except (TypeError, ValueError):
        return False


def is_date(s):
    try:
        return isinstance(s, str) and len(s) == 10 and bool(dt.date.fromisoformat(s))
    except ValueError:
        return False


def num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def pick(obj, path):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        else:
            return None
    return cur


def expand(doc, patterns):
    """Series paths, with 'a.*' expanded to every key of the object at a."""
    out = []
    for p in patterns:
        if p.endswith(".*"):
            node = pick(doc, p[:-2])
            if isinstance(node, dict):
                out += ["%s.%s" % (p[:-2], k) for k in node]
        else:
            out.append(p)
    return out


def decimals_ok(v, places):
    return v is None or (num(v) and abs(round(v, places) - v) < 1e-9)


def close(a, b, tol=0.011):
    return a is not None and b is not None and abs(a - b) <= tol


def median(values):
    s = sorted(values)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def two_sided_bps(quote):
    """Full quoted bid-ask over mid in bps under the contract's rule; None when it is not a two-sided quote."""
    if not isinstance(quote, dict):
        return None
    try:
        bid, ask = float(quote.get("bid")), float(quote.get("ask"))
    except (TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0 or bid < ask / 2.0 or ask < bid:
        return None
    return round((ask - bid) / ((ask + bid) / 2.0) * 1e4, 2)


def fmt_age(delta):
    minutes = delta.total_seconds() / 60.0
    if minutes < 180:
        return "%.0f minutes" % minutes
    if minutes < 72 * 60:
        return "%.1f hours" % (minutes / 60.0)
    return "%.0f days" % (minutes / 1440.0)


# ---------------------------------------------------------------- A. builders

def check_builders(rep):
    for name in BUILDERS:
        script = os.path.join(ROOT, "pipeline", "build_%s.py" % name)
        proc = subprocess.run([sys.executable or "python3", script, "--check"], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, universal_newlines=True)
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        if proc.returncode == 0:
            rep.check("builders", "build_%s.py --check" % name, [], (lines[-1] if lines else "ok")[:140])
        else:
            bad = [l.strip() for l in lines if re.search(r"FAIL|failed|Error|differs|missing", l)] or lines[-6:]
            rep.check("builders", "build_%s.py --check" % name, ["exit %d" % proc.returncode] + [b[:220] for b in bad])


# ---------------------------------------------------------------- B. files and schema

def load_site(rep):
    """Every JSON file under site/data, parsed strictly. Returns {name: doc}."""
    docs, problems, fixtures = {}, [], []
    for path in sorted(glob.glob(os.path.join(SITE_DATA, "**", "*.json"), recursive=True)):
        name = os.path.relpath(path, SITE_DATA)[:-5].replace(os.sep, "/")
        try:
            docs[name] = strict_load(path)
        except (ValueError, OSError) as exc:
            problems.append("%s.json: %s" % (name, str(exc)[:120]))
            continue
        if has_fixture(docs[name]):
            fixtures.append("%s.json carries \"fixture\": true" % name)
    missing = ["%s.json is missing" % n for n in list(REQUIRED) + ["events"] if n not in docs]
    # every file a catalog card reads must be produced
    wanted = catalog_files()
    missing += ["%s.json is read by a card in site/js/catalog.js but is not produced" % f
                for f in wanted if f not in docs and f not in REQUIRED]
    missing += ["%s.json is still in site/data: the per-piece research files are gone from the contract" % n
                for n in docs if n.startswith("research/")]
    rep.check("files", "every contract file is present and strict JSON", problems + missing,
              "%d files in %s; the catalog reads %d files" % (len(docs), os.path.relpath(SITE_DATA, ROOT) if SITE_DATA.startswith(ROOT) else SITE_DATA, len(wanted)))
    rep.check("files", "no file carries \"fixture\": true", fixtures)
    return docs


def catalog_files():
    """Data files named by the cards in site/js/catalog.js (file: '<name>'); market/<TICKER> is left out."""
    try:
        with open(os.path.join(ROOT, "site", "js", "catalog.js"), encoding="utf-8") as fh:
            return sorted({m for m in re.findall(r"\bfile:\s*'([A-Za-z0-9_/]+)'", fh.read())})
    except OSError:
        return []


def has_fixture(node):
    if isinstance(node, dict):
        return node.get("fixture") is True or any(has_fixture(v) for v in node.values())
    if isinstance(node, list):
        return any(has_fixture(v) for v in node)
    return False


def scope_walk(name, path, node, dates, problems):
    """Every array under an object is as long as its calendar: `dates`, or `ts` inside an intraday block."""
    if isinstance(node, dict):
        if isinstance(node.get("ts"), list):
            dates = node["ts"]
        for k, v in node.items():
            if k not in ("dates", "ts", "tickers"):
                scope_walk(name, path + "." + k, v, dates, problems)
    elif isinstance(node, list) and not (node and all(isinstance(v, dict) for v in node)) and len(node) != len(dates):
        problems.append("%s.json: %s has %d values for %d dates" % (name, path, len(node), len(dates)))


def check_schema(rep, docs, now):
    problems, extra = [], []
    for name, keys in REQUIRED.items():
        doc = docs.get(name)
        if not isinstance(doc, dict):
            continue
        lacking = [k for k in keys if k not in doc]
        if lacking:
            problems.append("%s.json lacks %s" % (name, ", ".join(lacking)))
        more = [k for k in doc if k not in keys]
        if more:
            extra.append("%s: %s" % (name, ", ".join(more)))
    for p in (docs.get("research") or {}).get("pieces") or []:
        if not isinstance(p, dict) or list(p) != RESEARCH_KEYS:
            problems.append("research.json: a piece has keys %s, the contract has %s" % (list(p) if isinstance(p, dict) else p, RESEARCH_KEYS))
    if list((docs.get("volume_daily") or {}).get("split") or {}) != SPLIT_KEYS:
        problems.append("volume_daily.json: split keys %s" % list((docs.get("volume_daily") or {}).get("split") or {}))
    if (docs.get("volume_daily") or {}).get("sources") != VOLUME_SOURCES:
        problems.append("volume_daily.json: sources %s, the contract has %s" % ((docs.get("volume_daily") or {}).get("sources"), VOLUME_SOURCES))
    for row in (docs.get("markets_latest") or {}).get("rows", [])[:1]:
        if [k for k in ROW_KEYS if k not in row]:
            problems.append("markets_latest.json rows lack %s" % ", ".join(k for k in ROW_KEYS if k not in row))
    rep.check("schema", "contract keys present in every file", problems,
              ("extra keys (allowed): " + "; ".join(extra))[:200] if extra else None)

    # as_of everywhere, well formed, not in the future
    problems = []
    for name, doc in docs.items():
        # the contract gives market/<TICKER> files no as_of of their own: they carry the dates of the sealed days
        if name in ("events", "meta") or name.startswith("market/") or not isinstance(doc, dict):
            continue
        as_of = doc.get("as_of")
        if not is_time(as_of):
            problems.append("%s.json: as_of %r is not a UTC time" % (name, as_of))
        elif parse_time(as_of) > now + dt.timedelta(minutes=10):
            problems.append("%s.json: as_of %s is in the future" % (name, as_of))
    meta = docs.get("meta") or {}
    for key in ("built_at", "data_as_of"):
        if not is_time(meta.get(key)):
            problems.append("meta.json: %s %r is not a UTC time" % (key, meta.get(key)))
    rep.check("schema", "as_of on every file, in UTC, not in the future", problems)

    # dates ascending and every daily array aligned
    problems = []
    for name, doc in docs.items():
        if name == "markets_scope" and isinstance(doc, dict):
            # one calendar per scope
            for key, scope in (doc.get("scopes") or {}).items():
                sd = scope.get("dates") if isinstance(scope, dict) else None
                if not isinstance(sd, list) or any(a >= b for a, b in zip(sd, sd[1:])):
                    problems.append("markets_scope.json: scope %s has no ascending dates" % key)
                else:
                    scope_walk(name, "scopes." + key, scope, sd, problems)
            continue
        if not isinstance(doc, dict) or not isinstance(doc.get("dates"), list):
            continue
        dates = doc["dates"]
        if not all(is_date(d) for d in dates):
            problems.append("%s.json: a date is not YYYY-MM-DD" % name)
            continue
        if any(a >= b for a, b in zip(dates, dates[1:])):
            problems.append("%s.json: dates are not strictly ascending" % name)
        # a listing on its first UTC day has no sealed day yet: its market file holds intraday points only
        first_day = name.startswith("market/") and isinstance(doc.get("intraday"), dict) \
            and isinstance(doc["intraday"].get("ts"), list) and len(doc["intraday"]["ts"]) > 0
        if not dates and name != "treasury" and not first_day:
            problems.append("%s.json: no dates" % name)

        def walk(node, path, dates):
            if isinstance(node, dict):
                if path and isinstance(node.get("dates"), list):
                    dates = node["dates"]             # an object with its own calendar, e.g. tradfi.listed
                if isinstance(node.get("ts"), list):
                    dates = node["ts"]                # intraday points: one per snapshot, their own calendar
                for k, v in node.items():
                    if k in ("dates", "ts", "tickers") or (path == "" and k in ("monthly", "range", "match", "markets", "pairs", "anchors",
                                                             "sources", "reported", "venues", "claimants", "top", "cohorts", "pieces")):
                        continue
                    walk(v, (path + "." + k) if path else k, dates)
            elif isinstance(node, list) and node and all(isinstance(v, dict) for v in node):
                for i, v in enumerate(node):          # a list of objects: check the arrays inside each
                    walk({k: x for k, x in v.items() if isinstance(x, list)}, "%s[%d]" % (path, i), dates)
            elif isinstance(node, list) and len(node) != len(dates):
                problems.append("%s.json: %s has %d values for %d dates" % (name, path, len(node), len(dates)))
        walk(doc, "", dates)
        if name == "peers_oi":
            for v in doc.get("venues", []):
                if len(v.get("values", [])) != len(dates):
                    problems.append("peers_oi.json: venue %s has %d values for %d dates" % (v.get("name"), len(v.get("values", [])), len(dates)))
    rep.check("schema", "dates ascending, every daily array as long as its dates", problems)

    # group and class keys
    problems = []
    for name in ("volume_daily", "oi_daily"):
        doc = docs.get(name) or {}
        if list((doc.get("group") or {}).keys()) != GROUPS:
            problems.append("%s.json: group keys %s" % (name, list((doc.get("group") or {}).keys())))
        if list((doc.get("cls") or {}).keys()) != CLASSES:
            problems.append("%s.json: cls keys %s" % (name, list((doc.get("cls") or {}).keys())))
    if list((pick(docs.get("olp_market") or {}, "net_long.group") or {}).keys()) != GROUPS:
        problems.append("olp_market.json: net_long.group keys")
    if list((pick(docs.get("quote_depth") or {}, "median_ba100k") or {}).keys()) != GROUPS:
        problems.append("quote_depth.json: median_ba100k keys")
    for name in ("volume_daily", "oi_daily"):
        if list(((docs.get(name) or {}).get("top") or {}).keys()) != TOP_GROUPS:
            problems.append("%s.json: top keys %s" % (name, list(((docs.get(name) or {}).get("top") or {}).keys())))
    for key in ("volume_cls", "oi_cls"):
        if list(((docs.get("tradfi") or {}).get(key) or {}).keys()) != TRADFI_CLASSES:
            problems.append("tradfi.json: %s keys %s" % (key, list(((docs.get("tradfi") or {}).get(key) or {}).keys())))
    for key in ("volume_inst", "oi_inst"):
        if list(((docs.get("tradfi") or {}).get(key) or {}).keys()) != ["perp", "swap"]:
            problems.append("tradfi.json: %s keys" % key)
    for name, m in docs.items():
        if name.startswith("market/") and name != "market/index" and isinstance(m, dict) \
                and (m.get("cls") not in CLASSES or m.get("group") not in GROUPS + ["unclassified"]):
            problems.append("%s.json: group %r, cls %r" % (name, m.get("group"), m.get("cls")))
    for row in (docs.get("markets_latest") or {}).get("rows", []):
        if row.get("group") not in GROUPS + ["unclassified"] or row.get("cls") not in CLASSES or row.get("type") not in ("perp", "swap"):
            problems.append("markets_latest.json: %s has group %r, cls %r, type %r" % (row.get("ticker"), row.get("group"), row.get("cls"), row.get("type")))
    rep.check("schema", "group, class and type keys are the contract's", problems)

    # rounding: money to cents, shares to four decimals inside 0..1, bps to two decimals
    problems = []

    def each(name, path, places, lo=None, hi=None):
        node = pick(docs.get(name) or {}, path)
        values = node if isinstance(node, list) else [node]
        for v in values:
            if v is None:
                continue
            if not num(v) or not decimals_ok(v, places):
                problems.append("%s.json %s: %r is not a number with at most %d decimals" % (name, path, v, places))
                return
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                problems.append("%s.json %s: %r is outside %s..%s" % (name, path, v, lo, hi))
                return
    for name, paths in (("volume_daily", ["total", "cumulative", "group.*", "cls.*"]),
                        ("oi_daily", ["total", "long", "short", "group.*", "cls.*"]),
                        ("peers_oi", ["variational"]), ("olp_market", ["net_long.total", "net_long.group.*"]),
                        ("treasury", ["balance"]), ("olp_chain", ["vault_balance"]),
                        ("accounts", ["deposits", "withdrawals", "net_new", "net_returning", "pool_balance",
                                      "monthly.volume", "monthly.vol_per_active"]),
                        ("pools_snapshot", ["total_balance"])):
        for path in expand(docs.get(name) or {}, paths):
            each(name, path, 2)
    each("olp_market", "matched_share", 4, 0, 1)
    for k in ("1", "10", "100", "1000"):
        each("pools_snapshot", "top_share." + k, 4, 0, 1)
    for path in expand(docs.get("quote_depth") or {}, ["median_ba100k.*"]):
        each("quote_depth", path, 2, 0)
    for path in expand(docs.get("swap_pairs") or {}, ["perp.ba100k.*", "swap.ba100k.*"]):
        each("swap_pairs", path, 2)
    for name, paths in (("tradfi", ["volume_cls.*", "oi_cls.*", "volume_inst.*", "oi_inst.*"]), ("tvl", ["total"]),
                        ("volume_daily", ["top.*", "split.*"]), ("oi_daily", ["top.*"]), ("spreads_daily", ["implied"]),
                        ("treasury", ["inflow", "outflow"]), ("rewards", ["referral", "loss_refund"]), ("oi_daily", ["dual"])):
        for path in expand(docs.get(name) or {}, paths):
            each(name, path, 2)
    for path in ("share_volume", "share_oi"):
        each("tradfi", path, 4, 0, 1)
    each("concentration", "top10_share", 4, 0, 1)
    for i, p in enumerate((docs.get("financials") or {}).get("periods", [])):
        for key, places in (("spreads_bps", 2), ("net_revenue_bps", 2), ("cost_share", 4)):
            if not decimals_ok(p.get(key), places):
                problems.append("financials.json period %s: %s %r" % (p.get("end"), key, p.get(key)))
    for row in (docs.get("markets_latest") or {}).get("rows", []):
        for key, places in (("vol24h", 2), ("oi_long", 2), ("oi_short", 2), ("ba1k_bps", 2), ("ba100k_bps", 2), ("ba1m_bps", 2)):
            if not decimals_ok(row.get(key), places):
                problems.append("markets_latest.json %s: %s %r" % (row.get("ticker"), key, row.get(key)))
                break
    rep.check("schema", "money to cents, shares to four decimals in 0..1, bps to two decimals", problems)


# ---------------------------------------------------------------- C. identities

def check_identities(rep, docs):
    vol, oi = docs.get("volume_daily") or {}, docs.get("oi_daily") or {}

    def split_sums(name, doc):
        bad = []
        dates, total = doc.get("dates", []), doc.get("total", [])
        for split in ("group", "cls"):
            parts = doc.get(split) or {}
            for i, day in enumerate(dates):
                vals = [arr[i] for arr in parts.values() if i < len(arr)]
                if all(v is None for v in vals):
                    continue
                if any(v is None for v in vals):
                    bad.append("%s %s %s: some keys are null and some are not" % (name, split, day))
                elif total[i] is None or abs(sum(vals) - total[i]) > 0.011:
                    bad.append("%s %s %s: keys sum to %.2f, total is %s" % (name, split, day, sum(vals), total[i]))
        return bad
    n_split = sum(1 for v in (vol.get("group") or {}).get("crypto_majors", []) if v is not None)
    rep.check("identities", "volume_daily and oi_daily: group and class keys sum to the total on every split date",
              split_sums("volume_daily", vol) + split_sums("oi_daily", oi), "%d split dates in volume_daily" % n_split)

    # the three groups of the Markets page: the two crypto groups as they are, TradFi perpetuals and swaps together
    bad, n_top = [], 0
    for name, doc in (("volume_daily", vol), ("oi_daily", oi)):
        top, group = doc.get("top") or {}, doc.get("group") or {}
        for i, day in enumerate(doc.get("dates", [])):
            vals = [(top.get(k) or [None] * (i + 1))[i] for k in TOP_GROUPS]
            g = [(group.get(k) or [None] * (i + 1))[i] for k in GROUPS]
            if all(v is None for v in vals) and all(v is None for v in g):
                continue
            n_top += 1
            if any(v is None for v in vals) or any(v is None for v in g):
                bad.append("%s %s: top and group are not null together" % (name, day))
            elif abs(sum(vals) - doc["total"][i]) > 0.011:
                bad.append("%s %s: top sums to %.2f, total is %s" % (name, day, sum(vals), doc["total"][i]))
            elif abs(vals[0] - g[0]) > 0.011 or abs(vals[1] - g[1]) > 0.011 or abs(vals[2] - (g[2] + g[3])) > 0.011:
                bad.append("%s %s: top %s differs from the market groups %s" % (name, day, vals, g))
    rep.check("identities", "volume_daily.top and oi_daily.top: three groups sum to the total and tie to the market groups", bad,
              "%d split dates across both files" % n_top)

    # crypto against TradFi: crypto majors + other crypto, TradFi perpetuals + swaps; null together where the groups are null
    bad, n_sp = [], 0
    sp, group = vol.get("split") or {}, vol.get("group") or {}
    for i, day in enumerate(vol.get("dates", [])):
        cr, tf = [(sp.get(k) or [None] * (i + 1))[i] for k in SPLIT_KEYS]
        g = [(group.get(k) or [None] * (i + 1))[i] for k in GROUPS]
        if cr is None and tf is None and all(v is None for v in g):
            continue
        n_sp += 1
        if cr is None or tf is None or any(v is None for v in g):
            bad.append("volume_daily %s: split and group are not null together" % day)
        elif round(cr * 100) + round(tf * 100) != round(vol["total"][i] * 100):
            bad.append("volume_daily %s: crypto %.2f + TradFi %.2f = %.2f, total is %s" % (day, cr, tf, cr + tf, vol["total"][i]))
        elif round(cr * 100) != round(g[0] * 100) + round(g[1] * 100) or round(tf * 100) != round(g[2] * 100) + round(g[3] * 100):
            bad.append("volume_daily %s: split %s differs from the market groups %s" % (day, [cr, tf], g))
    rep.check("identities", "volume_daily.split: crypto + TradFi = total on every split date, and ties to the market groups", bad,
              "%d split dates" % n_sp)

    bad = []
    for i, day in enumerate(oi.get("dates", [])):
        lo, sh, tot = oi["long"][i], oi["short"][i], oi["total"][i]
        if (lo is None) != (sh is None):
            bad.append("%s: long and short are not null together" % day)
        elif lo is not None and (tot is None or abs(lo + sh - tot) > 0.011):
            bad.append("%s: long + short = %.2f, total = %s" % (day, lo + sh, tot))
    rep.check("identities", "oi_daily: long + short = total", bad)

    bad = []
    cum = vol.get("cumulative", [])
    for i in range(1, len(cum)):
        if cum[i] is None or cum[i - 1] is None or cum[i] < cum[i - 1]:
            bad.append("cumulative falls or is null on %s" % vol["dates"][i])
    if any(v is None or v < 0 for v in vol.get("total", [])):
        bad.append("total has a null or negative day")
    for name, doc in (("volume_daily", vol), ("oi_daily", oi), ("markets_listed", docs.get("markets_listed") or {})):
        n = len(doc.get("sources", []))
        if any(s is not None and (not isinstance(s, int) or not 0 <= s < n) for s in doc.get("src", [])):
            bad.append("%s: a src code is not an index into sources" % name)
    rep.check("identities", "volume_daily: cumulative never falls, totals present, source codes valid", bad)

    listed = docs.get("markets_listed") or {}
    bad = ["%s: perp %s + swap %s != total %s" % (d, p, s, t) for d, t, p, s in
           zip(listed.get("dates", []), listed.get("total", []), listed.get("perp", []), listed.get("swap", []))
           if p is not None and s is not None and p + s != t]
    qd = docs.get("quote_depth") or {}
    q = qd.get("quoted_1m") or {}
    bad += ["quote_depth %s: perp + swap != total" % d for d, t, p, s in
            zip(qd.get("dates", []), q.get("total", []), q.get("perp", []), q.get("swap", [])) if None in (t, p, s) or p + s != t]
    rep.check("identities", "markets_listed and quote_depth: perpetuals + swaps = total", bad)

    fin = docs.get("financials") or {}
    bad = []
    periods = fin.get("periods", [])
    for p in periods:
        lacking = [k for k in PERIOD_KEYS if k not in p]
        if lacking:
            bad.append("%s: lacks %s" % (p.get("end"), ", ".join(lacking)))
            continue
        e = p["end"]
        if abs(p["spreads_paid"] - (p["mm_costs"] + p["rewards"] + p["treasury_inflow"] + p["olp_pnl"])) > 0.005:
            bad.append("%s: spreads_paid != mm_costs + rewards + treasury_inflow + olp_pnl" % e)
        if abs(p["net_profit"] - (p["treasury_inflow"] + p["olp_pnl"])) > 0.005:
            bad.append("%s: net_profit != treasury_inflow + olp_pnl" % e)
        if abs(p["net_revenue"] - (p["net_profit"] + p["rewards"])) > 0.005:
            bad.append("%s: net_revenue != net_profit + rewards" % e)
        if p["rewards"] < 0:
            bad.append("%s: rewards is negative (it is a cost, stored positive)" % e)
        if not p["spreads_paid"] or p["cost_share"] != round(p["mm_costs"] / p["spreads_paid"], 4):
            bad.append("%s: cost_share != mm_costs / spreads_paid" % e)
        if not (is_date(p["start"]) and is_date(e)) or (dt.date.fromisoformat(e) - dt.date.fromisoformat(p["start"])).days != p["days"]:
            bad.append("%s: days != end - start" % e)
        if p["volume"]:
            if p["spreads_bps"] != round(p["spreads_paid"] / p["volume"] * 1e4, 2):
                bad.append("%s: spreads_bps != spreads_paid / volume" % e)
            if p["net_revenue_bps"] != round(p["net_revenue"] / p["volume"] * 1e4, 2):
                bad.append("%s: net_revenue_bps != net_revenue / volume" % e)
        elif p["spreads_bps"] is not None or p["net_revenue_bps"] is not None:
            bad.append("%s: a per-dollar figure without a volume" % e)
        want = round(p["olp_pnl"] / p["volume"] * 1e4, 2) if p["volume"] else None
        if p["olp_bps"] != want:
            bad.append("%s: olp_bps %r != olp_pnl / volume = %r" % (e, p["olp_bps"], want))
        want = round(p["net_profit"] / p["volume"] * 1e4, 2) if p["volume"] else None
        if p["net_profit_bps"] != want:
            bad.append("%s: net_profit_bps %r != net_profit / volume = %r" % (e, p["net_profit_bps"], want))
        for key, line in (("net_revenue_margin", "net_revenue"), ("net_profit_margin", "net_profit"),
                          ("rewards_share", "rewards"), ("treasury_share", "treasury_inflow")):
            want = round(p[line] / p["spreads_paid"], 4) if p["spreads_paid"] else None
            if p[key] != want:
                bad.append("%s: %s %r != %s / spreads_paid = %r" % (e, key, p[key], line, want))
        if not str(p.get("source_url") or "").startswith("https://"):
            bad.append("%s: source_url is not a link" % e)
    if any(a["end"] >= b["end"] for a, b in zip(periods, periods[1:])):
        bad.append("periods are not in ascending order of end date")
    summ = fin.get("olp_summary")
    if summ is not None and periods and all("olp_pnl" in p for p in periods):
        want = {"latest": periods[-1]["olp_pnl"], "lifetime": (fin.get("lifetime_olp_pnl") or {}).get("value"),
                "lifetime_as_of": (fin.get("lifetime_olp_pnl") or {}).get("as_of"),
                "positive": sum(1 for p in periods if p["olp_pnl"] > 0), "periods": len(periods)}
        for key, value in want.items():
            if summ.get(key) != value:
                bad.append("olp_summary.%s is %r, the periods give %r" % (key, summ.get(key), value))
        if sorted(summ) != sorted(want):
            bad.append("olp_summary keys are %s" % sorted(summ))
    life = fin.get("lifetime_olp_pnl") or {}
    complete = bool(periods) and all(all(k in p for k in PERIOD_KEYS) for p in periods)
    cum, ann = fin.get("cumulative") or {}, fin.get("annualised") or {}
    if complete:
        if cum.get("ends") != [p["end"] for p in periods] or sorted(k for k in cum if k != "ends") != sorted(DOLLAR_LINES):
            bad.append("cumulative is not aligned to the period ends or lacks a line")
        else:
            for line in DOLLAR_LINES:
                run = 0
                for i, p in enumerate(periods):
                    run += round(p[line] * 100)
                    if len(cum[line]) != len(periods) or round(cum[line][i] * 100) != run:
                        bad.append("cumulative.%s at %s is %r, the periods sum to %.2f" % (line, p["end"], cum[line][i] if i < len(cum[line]) else None, run / 100.0))
                        break
        for line in DOLLAR_LINES:
            if ann.get(line) is None or round(ann[line] * 100) != round(periods[-1][line] * 100) * PERIODS_PER_YEAR:
                bad.append("annualised.%s is %r, the latest period times %d is %s" % (line, ann.get(line), PERIODS_PER_YEAR, periods[-1][line] * PERIODS_PER_YEAR))
        if sorted(ann) != sorted(DOLLAR_LINES):
            bad.append("annualised keys are %s" % sorted(ann))
        # the lifetime OLP figure stated in each update: the previous one plus the period's OLP PnL, to print rounding
        if periods[-1]["lifetime_olp_pnl"] != life.get("value"):
            bad.append("the latest period's lifetime_olp_pnl %r differs from lifetime_olp_pnl.value %r" % (periods[-1]["lifetime_olp_pnl"], life.get("value")))
        for a, b in zip(periods, periods[1:]):
            if a["lifetime_olp_pnl"] is not None and b["lifetime_olp_pnl"] is not None \
                    and abs(b["lifetime_olp_pnl"] - a["lifetime_olp_pnl"] - b["olp_pnl"]) > LIFETIME_PRINT_TOLERANCE:
                bad.append("%s: lifetime OLP PnL moves by %s, the period's OLP PnL is %s" % (b["end"], b["lifetime_olp_pnl"] - a["lifetime_olp_pnl"], b["olp_pnl"]))
    if not num(life.get("value")) or not is_date(life.get("as_of")):
        bad.append("lifetime_olp_pnl needs a value and a date")
    rep.check("identities", "financials: statement identities, margins, per-dollar figures, cumulative, annualised, lifetime OLP and olp_summary recomputed", bad,
              "%d periods; latest OLP PnL %s; olp_summary %s" % (len(periods), "{:,.0f}".format(periods[-1]["olp_pnl"]) if periods else "n/a",
                                                                "tied" if summ is not None else "absent"))

    ac = docs.get("accounts") or {}
    bad = []
    try:
        for i, day in enumerate(ac.get("dates", [])):
            if abs((ac["net_new"][i] + ac["net_returning"][i]) - (ac["deposits"][i] - ac["withdrawals"][i])) > 0.011:
                bad.append("%s: net_new + net_returning != deposits - withdrawals" % day)
            if not ac["active_1d"][i] <= ac["active_7d"][i] <= ac["active_30d"][i]:
                bad.append("%s: active 1d <= 7d <= 30d broken" % day)
            if ac["deposits"][i] < 0 or ac["withdrawals"][i] < 0:
                bad.append("%s: deposits and withdrawals are stored positive" % day)
        m = ac.get("monthly") or {}
        for month, v, a, r in zip(m.get("months", []), m.get("volume", []), m.get("active_30d", []), m.get("vol_per_active", [])):
            want = round(v / a, 2) if (v is not None and a) else None
            if (r is None) != (want is None) or (r is not None and abs(r - want) > 0.011):
                bad.append("monthly %s: vol_per_active %s != volume / active_30d = %s" % (month, r, want))
    except (KeyError, IndexError, TypeError) as exc:
        bad.append("accounts.json is not shaped as the contract says (%s)" % exc)
    rep.check("identities", "accounts: flow legs, active windows, monthly ratio", bad)

    ps = docs.get("pools_snapshot") or {}
    bad = []
    tiers = ps.get("tiers", [])
    if [t.get("label") for t in tiers] != TIERS:
        bad.append("tier labels differ from the contract")
    elif abs(sum(t["balance"] for t in tiers) - (ps.get("total_balance") or 0)) > 0.05:
        bad.append("tier balances sum to %.2f, total_balance is %s" % (sum(t["balance"] for t in tiers), ps.get("total_balance")))
    shares = [(ps.get("top_share") or {}).get(k) for k in ("1", "10", "100", "1000")]
    if any(s is None for s in shares) or shares != sorted(shares):
        bad.append("top_share must rise from 1 to 1000: %s" % shares)
    rep.check("identities", "pools_snapshot: tiers sum to the total, top shares rise", bad)

    olp = docs.get("olp_market") or {}
    bad = []
    for i, day in enumerate(olp.get("dates", [])):
        parts = [arr[i] for arr in (pick(olp, "net_long.group") or {}).values()]
        tot = pick(olp, "net_long.total")[i]
        if any(v is None for v in parts) or tot is None or abs(sum(parts) - tot) > 0.011:
            bad.append("%s: net_long groups do not sum to the total" % day)
    rep.check("identities", "olp_market: group keys sum to the total", bad)

    ev = docs.get("execution_venue") or {}
    bad = []
    match = ev.get("match") or {}
    if not match.get("quoted") or match.get("rate") != round(match.get("matched", 0) / match["quoted"], 4):
        bad.append("match.rate != matched / quoted")
    if list((ev.get("by") or {}).keys()) != [m.get("key") for m in ev.get("markets", [])] or not ev.get("markets"):
        bad.append("`by` keys %s differ from the markets list" % list((ev.get("by") or {}).keys()))
    if sorted((ev.get("range") or {}).keys()) != sorted(RANGE_KEYS):
        bad.append("range keys are %s" % list((ev.get("range") or {}).keys()))
    for key, sizes in (ev.get("by") or {}).items():
        for size, cols in sizes.items():
            for i, n in enumerate(cols.get("n", [])):
                if not isinstance(n, int) or n < 0:
                    bad.append("%s %s: n is not a count" % (key, size))
                    break
                if (n == 0) != (cols["omni"][i] is None) or (n == 0) != (cols["hl_book"][i] is None):
                    bad.append("%s %s %s: medians and n disagree about having samples" % (key, size, ev["dates"][i]))
                    break
                if n > (cols.get("quoted") or [n])[i]:
                    bad.append("%s %s %s: more matched than quoted samples" % (key, size, ev["dates"][i]))
                    break
    # the other venues: the same rules as Hyperliquid, each on its own columns
    venues = [v.get("key") for v in ev.get("venues", [])]
    if not venues or venues[0] != "hl" or len(venues) != len(set(venues)) or any(not v.get("label") for v in ev.get("venues", [])):
        bad.append("venues must start with hl and carry distinct keys and labels: %s" % venues)
    listed = {m.get("key"): m for m in ev.get("markets", [])}
    mv = match.get("venues") or {}
    if sorted(mv) != sorted(venues):
        bad.append("match.venues keys %s differ from the venues list" % sorted(mv))
    for v in venues:
        block = mv.get(v) or {}
        want = round(block["matched"] / block["quoted"], 4) if block.get("quoted") else None
        if block.get("rate") != want or not isinstance(block.get("unavailable", 0), int) or block.get("matched", 0) > block.get("quoted", 0):
            bad.append("match.venues.%s: rate %r, matched %r of quoted %r" % (v, block.get("rate"), block.get("matched"), block.get("quoted")))
        total = 0
        for key, sizes in (ev.get("by") or {}).items():
            for size, cols in sizes.items():
                ns = cols.get("n" if v == "hl" else v + "_n")
                book, omni = cols.get(v + "_book"), cols.get("omni" if v == "hl" else v + "_omni")
                fee = cols.get(v + "_fee")
                if not all(isinstance(x, list) and len(x) == len(ev.get("dates", [])) for x in (ns, book, omni, fee)):
                    bad.append("%s %s: a %s column is missing or misaligned" % (key, size, v))
                    continue
                total += sum(ns)
                for i, n in enumerate(ns):
                    if not isinstance(n, int) or n < 0 or (n == 0) != (book[i] is None) or (n == 0) != (omni[i] is None) or (n == 0 and fee[i] is not None):
                        bad.append("%s %s %s: %s medians and its sample count disagree" % (key, size, ev["dates"][i], v))
                        break
                    if n > cols["quoted"][i]:
                        bad.append("%s %s %s: more %s matches than quoted samples" % (key, size, ev["dates"][i], v))
                        break
                if v != "hl" and listed.get(key, {}).get(v) is None and (sum(ns) or any(x is not None for x in book)):
                    bad.append("%s %s: %s figures for a market the venue does not list" % (key, size, v))
                if v == "hl" and cols.get("hl_n") != ns:
                    bad.append("%s %s: hl_n differs from n" % (key, size))
                for rk, node in (ev.get("range") or {}).items():
                    r = ((node.get(key) or {}).get(size) or {})
                    rn = r.get("n" if v == "hl" else v + "_n")
                    if rn is None or (rn == 0) != (r.get(v + "_book") is None) or (rk == "All" and rn != sum(ns)):
                        bad.append("range %s %s %s: %s pooled figures and sample count disagree with the daily columns" % (rk, key, size, v))
        if total != block.get("matched"):
            bad.append("match.venues.%s.matched is %r, the daily columns sum to %d" % (v, block.get("matched"), total))
    hl = mv.get("hl") or {}
    if any(hl.get(k) != match.get(k) for k in ("quoted", "matched", "rate")):
        bad.append("match.venues.hl differs from the top-level match block")
    rep.check("identities", "execution_venue: for every venue, match rate, columns, medians present exactly where samples exist", bad,
              "; ".join("%s matched %s of %s" % (v, (mv.get(v) or {}).get("matched"), (mv.get(v) or {}).get("quoted")) for v in venues))

    tok = docs.get("token") or {}
    bad, soft = [], []
    alloc = tok.get("allocation", [])
    if not alloc or abs(sum(a.get("pct", 0) for a in alloc) - 1) > 1e-9:
        bad.append("allocation does not sum to 100%")
    strikes = (tok.get("fdv") or {}).get("strikes", [])
    if any(not num(s.get("p")) or not 0 <= s["p"] <= 1 for s in strikes):
        bad.append("a strike probability is outside 0..1")
    ordered = sorted(strikes, key=lambda s: s.get("strike") or 0)
    if any(a["p"] < b["p"] - 1e-9 for a, b in zip(ordered, ordered[1:]) if num(a.get("p")) and num(b.get("p"))):
        soft.append("Polymarket prices a higher strike above a lower one (market noise, shown as priced)")
    if any(not num(m.get("p")) or not 0 <= m["p"] <= 1 or len(m.get("dates", [])) != len(m.get("history", [])) for m in tok.get("launch", [])):
        bad.append("a launch market has a probability outside 0..1 or misaligned history")
    weeks = [p.get("week") for p in tok.get("points", [])]
    if weeks != sorted(set(weeks)):
        bad.append("points weeks are not strictly ascending")
    hist = (tok.get("fdv") or {}).get("history") or {}
    if len(hist.get("dates", [])) != len(hist.get("median", [])):
        bad.append("fdv.history is misaligned")
    rep.check("identities", "token: allocation sums to 100%, probabilities in 0..1, weeks ascending", bad)
    if soft:
        rep.check("identities", "token: strike probabilities fall as the strike rises", soft, warn=True)

    events = docs.get("events")
    bad = []
    if not isinstance(events, list) or len(events) > 5:
        bad.append("events.json must be a list of at most five")
    else:
        bad += ["event %r: label over three words or bad date" % e for e in events
                if not is_date(e.get("date")) or len(str(e.get("label", "")).split()) > 3 or set(e) != {"date", "label"}]
    meta = docs.get("meta") or {}
    if meta.get("publication_date") != "2026-11-24" or meta.get("stale_after_minutes") != 120:
        bad.append("meta: publication_date or stale_after_minutes differs from the contract")
    want = ["omni", "onchain", "onchain_block", "reports", "defillama", "polymarket", "hyperliquid", "telegram"]
    if sorted((meta.get("sources") or {}).keys()) != sorted(want):
        bad.append("meta.sources keys are %s" % sorted((meta.get("sources") or {}).keys()))
    rep.check("identities", "events and meta constants", bad)


# ---------------------------------------------------------------- D. ties

def market_files(docs):
    return {n[len("market/"):]: d for n, d in docs.items() if n.startswith("market/") and n != "market/index"}


def check_ties(rep, docs):
    markets = market_files(docs)
    oi, olp = docs.get("oi_daily") or {}, docs.get("olp_market") or {}
    listed, qd, peers = docs.get("markets_listed") or {}, docs.get("quote_depth") or {}, docs.get("peers_oi") or {}

    # market files: index, keys, alignment
    bad = []
    tickers = (docs.get("market/index") or {}).get("tickers", [])
    if sorted(tickers) != sorted(markets):
        bad.append("market/index.json lists %d tickers, %d files are on disk" % (len(tickers), len(markets)))
    for t, m in markets.items():
        lacking = [k for k in MARKET_KEYS if k not in m]
        if lacking or m.get("ticker") != t:
            bad.append("market/%s.json: lacks %s or names another ticker" % (t, ", ".join(lacking)))
    latest_rows = {r.get("ticker"): r for r in (docs.get("markets_latest") or {}).get("rows", [])}
    not_filed = sorted(t for t in latest_rows if t not in markets)
    if not_filed:
        bad.append("listed now but without a market file: %s" % ", ".join(not_filed[:8]))
    rep.check("ties", "market/<TICKER> files match the index and the latest table", bad, "%d listings" % len(markets))

    # per-date sums over the listings
    by_date = {}
    for t, m in markets.items():
        for j, day in enumerate(m.get("dates", [])):
            agg = by_date.setdefault(day, {"long": 0.0, "short": 0.0, "min": 0.0, "max": 0.0, "n": 0, "q1m": {"perp": 0, "swap": 0}, "ba": {}})
            lo, sh = m["oi_long"][j], m["oi_short"][j]
            agg["n"] += 1
            if lo is not None and sh is not None:
                agg["long"] += lo
                agg["short"] += sh
                agg["min"] += min(lo, sh)
                agg["max"] += max(lo, sh)
            if m["ba1m_bps"][j] is not None:
                agg["q1m"][m.get("type", "perp")] += 1
            if m["ba100k_bps"][j] is not None:
                agg["ba"].setdefault(m.get("group"), []).append(m["ba100k_bps"][j])

    omni = (oi.get("sources") or []).index("omni") if "omni" in (oi.get("sources") or []) else None
    bad, n = [], 0
    for i, day in enumerate(oi.get("dates", [])):
        if oi["src"][i] != omni or omni is None:
            continue
        agg = by_date.get(day)
        if agg is None:
            bad.append("%s: oi_daily is sourced from Omni but no listing has that date" % day)
            continue
        n += 1
        if not close(oi["total"][i], agg["long"] + agg["short"], 0.02):
            bad.append("%s: oi_daily total %.2f, listings' long + short %.2f" % (day, oi["total"][i], agg["long"] + agg["short"]))
        if oi["long"][i] is not None and (not close(oi["long"][i], agg["long"], 0.02) or not close(oi["short"][i], agg["short"], 0.02)):
            bad.append("%s: oi_daily long or short differs from the listings' sums" % day)
    rep.check("ties", "oi_daily against the sum of long and short in each day's listings", bad, "%d Omni-sourced dates" % n)

    bad = []
    oi_at = {d: i for i, d in enumerate(oi.get("dates", []))}
    for i, day in enumerate(olp.get("dates", [])):
        net = pick(olp, "net_long.total")[i]
        j = oi_at.get(day)
        if j is None or oi["long"][j] is None:
            bad.append("%s: olp_market has a date that oi_daily has no long and short for" % day)
        elif not close(net, oi["long"][j] - oi["short"][j], 0.02):
            bad.append("%s: net_long %.2f, oi_daily long - short %.2f" % (day, net, oi["long"][j] - oi["short"][j]))
        agg = by_date.get(day)
        if olp["matched_share"][i] is None:
            bad.append("%s: matched_share is null" % day)
        elif agg and agg["max"] > 0 and abs(round(agg["min"] / agg["max"], 4) - olp["matched_share"][i]) > 0.00011:
            bad.append("%s: matched_share %s, listings give %.4f" % (day, olp["matched_share"][i], agg["min"] / agg["max"]))
    rep.check("ties", "olp_market against oi_daily and the listings (net long, matched share)", bad, "%d dates" % len(olp.get("dates", [])))

    bad = []
    for i, day in enumerate(peers.get("dates", [])):
        j = oi_at.get(day)
        if j is not None and not close(peers["variational"][i], oi["total"][j], 0.011):
            bad.append("%s: peers_oi variational %s, oi_daily %s" % (day, peers["variational"][i], oi["total"][j]))
    venues = peers.get("venues", [])
    if len(venues) != 6 or any(len(v.get("values", [])) != len(peers.get("dates", [])) for v in venues):
        bad.append("peers_oi needs six venues aligned with dates")
    rep.check("ties", "peers_oi variational against oi_daily", bad)

    bad = []
    omni_l = (listed.get("sources") or []).index("omni") if "omni" in (listed.get("sources") or []) else None
    for i, day in enumerate(listed.get("dates", [])):
        if listed["src"][i] == omni_l and omni_l is not None and day in by_date and by_date[day]["n"] != listed["total"][i]:
            bad.append("%s: markets_listed %d, listings with that date %d" % (day, listed["total"][i], by_date[day]["n"]))
    for i, day in enumerate(qd.get("dates", [])):
        agg = by_date.get(day)
        if not agg:
            bad.append("%s: quote_depth has a date no listing has" % day)
            continue
        for kind in ("perp", "swap"):
            if qd["quoted_1m"][kind][i] != agg["q1m"][kind]:
                bad.append("%s: quoted_1m %s %s, listings with a $1M bid-ask %d" % (day, kind, qd["quoted_1m"][kind][i], agg["q1m"][kind]))
        for g in GROUPS:
            want = median(agg["ba"].get(g, []))
            have = qd["median_ba100k"][g][i]
            if (want is None) != (have is None) or (want is not None and abs(want - have) > 0.021):
                bad.append("%s: median_ba100k %s %s, listings' median %s" % (day, g, have, want))
    rep.check("ties", "markets_listed and quote_depth against the listings", bad)

    # monthly volume in accounts against volume_daily
    vol, ac = docs.get("volume_daily") or {}, docs.get("accounts") or {}
    by_month, days_in = {}, {}
    for day, total in zip(vol.get("dates", []), vol.get("total", [])):
        if total is not None:
            by_month[day[:7]] = by_month.get(day[:7], 0.0) + total
            days_in[day[:7]] = days_in.get(day[:7], 0) + 1
    bad = []
    m = ac.get("monthly") or {}
    for month, v in zip(m.get("months", []), m.get("volume", [])):
        if v is not None and abs(v - by_month.get(month, 0.0)) > 0.02:
            bad.append("%s: accounts.monthly.volume %.2f, volume_daily sums to %.2f over %d days"
                       % (month, v, by_month.get(month, 0.0), days_in.get(month, 0)))
    rep.check("ties", "accounts monthly volume against volume_daily", bad)

    meta, latest = docs.get("meta") or {}, docs.get("markets_latest") or {}
    bad = []
    if not (meta.get("data_as_of") == latest.get("as_of") == (meta.get("sources") or {}).get("omni")):
        bad.append("meta.data_as_of %s, meta.sources.omni %s, markets_latest.as_of %s"
                   % (meta.get("data_as_of"), (meta.get("sources") or {}).get("omni"), latest.get("as_of")))
    if is_time(meta.get("built_at")) and is_time(meta.get("data_as_of")) and parse_time(meta["built_at"]) < parse_time(meta["data_as_of"]):
        bad.append("meta.built_at is before data_as_of")
    rep.check("ties", "meta against markets_latest", bad)


def find_snapshot(stamp):
    for folder in SNAPSHOT_DIRS:
        path = os.path.join(folder, stamp + ".json.gz")
        if os.path.exists(path):
            return path
    return None


def check_latest_against_raw(rep, docs):
    """markets_latest.json against the raw snapshot it names, re-read here from the gzip."""
    latest = docs.get("markets_latest") or {}
    if not is_time(latest.get("as_of")):
        rep.add("ties", "markets_latest against the newest raw snapshot", "fail", ["markets_latest.as_of is missing"])
        return
    stamp = parse_time(latest["as_of"]).strftime("%Y%m%dT%H%MZ")
    path = find_snapshot(stamp)
    if path is None:
        rep.add("ties", "markets_latest against the newest raw snapshot", "fail",
                ["no raw snapshot %s.json.gz in %s" % (stamp, ", ".join(SNAPSHOT_DIRS))])
        return
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        raw = json.load(fh)
    bad = []
    listings = {l["ticker"]: l for l in raw["listings"]}
    rows = latest.get("rows", [])
    if sorted(listings) != sorted(r["ticker"] for r in rows):
        bad.append("%d rows, %d listings in the snapshot" % (len(rows), len(listings)))
    if abs(sum(r["vol24h"] or 0 for r in rows) - float(raw["total_volume_24h"])) > 1.0:
        bad.append("rows' 24h volume %.2f, API total %.2f" % (sum(r["vol24h"] or 0 for r in rows), float(raw["total_volume_24h"])))
    one_sided = sum((r["oi_long"] or 0) + (r["oi_short"] or 0) for r in rows)
    if abs(2 * one_sided - float(raw["open_interest"])) > 1.0:
        bad.append("rows' long + short doubled %.2f, API dual-sided open interest %.2f" % (2 * one_sided, float(raw["open_interest"])))
    for r in rows:
        l = listings.get(r["ticker"])
        if l is None:
            continue
        o = l.get("open_interest") or {}
        q = l.get("quotes") or {}
        f = (l.get("funding") or {}).get("last") or {}
        want = {
            "vol24h": round(float(l["volume_24h"]), 2),
            "oi_long": round(float(o["long_open_interest"]), 2) if o.get("long_open_interest") is not None else None,
            "oi_short": round(float(o["short_open_interest"]), 2) if o.get("short_open_interest") is not None else None,
            "ba1k_bps": two_sided_bps(q.get("size_1k")), "ba100k_bps": two_sided_bps(q.get("size_100k")),
            "ba1m_bps": two_sided_bps(q.get("size_1m")),
            "type": "swap" if l.get("contract_type") == "swap" else "perp",
        }
        if want["type"] == "perp":
            want["funding_ann"] = float(f["annualized_rate"]) if f.get("annualized_rate") is not None else None
        for key, value in want.items():
            have = r.get(key)
            # a raw figure that sits exactly on half a cent rounds either way (133.475 is 133.48 in decimal and
            # 133.47 as a float), so the two rounded figures may differ by one cent: the tolerance of close()
            if have != value and not (num(have) and num(value) and abs(have - value) < 0.011):
                bad.append("%s %s: file %r, snapshot %r" % (r["ticker"], key, have, value))
    # nothing newer on disk may be a different reading (a newer file with the same counters is a repeated response)
    newer = []
    for folder in SNAPSHOT_DIRS:
        for p in glob.glob(os.path.join(folder, "*.json.gz")):
            if os.path.basename(p)[:14] > stamp:
                newer.append(p)
    for p in sorted(newer):
        try:
            with gzip.open(p, "rt", encoding="utf-8") as fh:
                d = json.load(fh)
            if (d["cumulative_volume"], d["total_volume_24h"], d["open_interest"]) != (raw["cumulative_volume"], raw["total_volume_24h"], raw["open_interest"]):
                bad.append("a newer snapshot %s is in the store but markets_latest was built from %s" % (os.path.basename(p), stamp))
                break
        except (OSError, ValueError, KeyError):
            bad.append("snapshot %s is unreadable" % os.path.basename(p))
    rep.check("ties", "markets_latest against the newest raw snapshot (every listing, quotes recomputed)", bad,
              "%d listings, snapshot %s" % (len(rows), stamp))


def check_sealed_volume(rep, docs):
    """volume_daily against the sealed day files for days measured as the change in the API counter."""
    from decimal import Decimal
    vol = docs.get("volume_daily") or {}
    at = {d: i for i, d in enumerate(vol.get("dates", []))}
    bad, n = [], 0
    for path in sorted(glob.glob(os.path.join(STORE, "days", "*.json"))):
        try:
            day = strict_load(path)
        except (ValueError, OSError):
            bad.append("%s is unreadable" % os.path.basename(path))
            continue
        p = day.get("platform") or {}
        if p.get("vol_method") != "cumulative_diff" or day.get("approx"):
            continue
        n += 1
        want = float(Decimal(p["cumulative_volume_close"]) - Decimal(p["cumulative_volume_open"]))
        i = at.get(day["date"])
        if i is None:
            bad.append("%s: sealed with an exact volume but absent from volume_daily" % day["date"])
        elif abs(vol["total"][i] - want) > 0.011:
            bad.append("%s: volume_daily %.2f, change in the API counter %.2f" % (day["date"], vol["total"][i], want))
        elif abs(vol["cumulative"][i] - float(Decimal(p["cumulative_volume_close"]))) > 0.011:
            bad.append("%s: cumulative %.2f, API counter at the day's end %.2f" % (day["date"], vol["cumulative"][i], float(p["cumulative_volume_close"])))
    rep.check("ties", "volume_daily against the API counter on exactly measured days (sealed day files)", bad,
              "%d exactly measured days so far" % n)


# ---- the contract's v2 additions (all required and blocking since the on-chain files became current)

def check_v2(rep, docs):
    """Ties for the files and fields the contract added in v2."""
    results = []        # (name, problems, note)
    vol, oi, ac = docs.get("volume_daily") or {}, docs.get("oi_daily") or {}, docs.get("accounts") or {}
    markets = market_files(docs)

    def at(doc):
        return {d: i for i, d in enumerate(doc.get("dates", []))}

    tf = docs.get("tradfi")
    if isinstance(tf, dict):
        bad = []
        for label, share_key, cls_key, inst_key, base in (("volume", "share_volume", "volume_cls", "volume_inst", vol),
                                                          ("open interest", "share_oi", "oi_cls", "oi_inst", oi)):
            ix = at(base)
            for i, day in enumerate(tf.get("dates", [])):
                j = ix.get(day)
                share = tf[share_key][i]
                parts = [tf[cls_key][k][i] for k in TRADFI_CLASSES]
                perp, swap = tf[inst_key]["perp"][i], tf[inst_key]["swap"][i]
                if j is None or base["group"]["tradfi_perp"][j] is None:
                    if share is not None:
                        bad.append("%s: a TradFi share of %s without a group split in the source file" % (day, label))
                    continue
                tradfi = base["group"]["tradfi_perp"][j] + base["group"]["tradfi_swap"][j]
                want = round(tradfi / base["total"][j], 4) if base["total"][j] else None
                if (share is None) != (want is None) or (share is not None and abs(share - want) > 0.00011):
                    bad.append("%s: %s %s, groups give %s" % (day, share_key, share, want))
                if any(v is None for v in parts) or abs(sum(parts) - tradfi) > 0.03:
                    bad.append("%s: %s classes sum to %s, TradFi %s is %.2f" % (day, cls_key, parts, label, tradfi))
                elif any(abs(tf[cls_key][k][i] - base["cls"][k][j]) > 0.011 for k in TRADFI_CLASSES):
                    bad.append("%s: %s differs from the same classes in the source file" % (day, cls_key))
                # instruments: perpetuals and swaps are the two TradFi groups, and together the TradFi total
                if perp is None or swap is None or abs(perp - base["group"]["tradfi_perp"][j]) > 0.011 \
                        or abs(swap - base["group"]["tradfi_swap"][j]) > 0.011 or abs(perp + swap - tradfi) > 0.011:
                    bad.append("%s: %s perp %s + swap %s, TradFi %s is %.2f" % (day, inst_key, perp, swap, label, tradfi))
        listed = tf.get("listed") or {}
        n = len(listed.get("dates", []))
        if any(len(listed.get(k, [])) != n for k in TRADFI_CLASSES) or len(listed.get("src", [])) != n:
            bad.append("listed: a class or src is not aligned with listed.dates")
        elif isinstance(listed.get("evidence"), dict) and all(len(v) == n for v in listed["evidence"].values()):
            for i in range(n):
                if sum(listed[k][i] for k in TRADFI_CLASSES) != sum(v[i] for v in listed["evidence"].values()):
                    bad.append("listed %s: classes and evidence counts disagree" % listed["dates"][i])
                    break
        results.append(("tradfi: shares, class sums and instrument pairs against volume_daily and oi_daily", bad, "%d dates" % len(tf.get("dates", []))))

    conc = docs.get("concentration")
    if isinstance(conc, dict):
        bad = []
        for i, day in enumerate(conc.get("dates", [])):
            vols = sorted((m["vol24h"][m["dates"].index(day)] or 0.0 for m in markets.values() if day in m.get("dates", [])), reverse=True)
            have = conc["top10_share"][i]
            if have is not None and not 0 <= have <= 1:
                bad.append("%s: top10_share %s is outside 0..1" % (day, have))
            elif have is not None and vols and sum(vols) > 0 and abs(sum(vols[:10]) / sum(vols) - have) > 0.00011:
                bad.append("%s: top10_share %s, the listings give %.4f" % (day, have, sum(vols[:10]) / sum(vols)))
        results.append(("concentration: ten largest listings' share against the listings", bad, "%d dates" % len(conc.get("dates", []))))

    tvl = docs.get("tvl")
    if isinstance(tvl, dict):
        bad = []
        parts = [(at(ac), ac.get("pool_balance", [])), (at(docs.get("olp_chain") or {}), (docs.get("olp_chain") or {}).get("vault_balance", [])),
                 (at(docs.get("treasury") or {}), (docs.get("treasury") or {}).get("balance", []))]
        for i, day in enumerate(tvl.get("dates", [])):
            floor = sum(arr[ix[day]] for ix, arr in parts if day in ix and arr[ix[day]] is not None)
            if tvl["total"][i] is None or tvl["total"][i] < floor - 0.02:
                bad.append("%s: TVL %s is below pools + vault + treasury %.2f" % (day, tvl["total"][i], floor))
        results.append(("tvl: never below settlement pools + Core OLP Vault + Protocol Treasury", bad, "%d dates" % len(tvl.get("dates", []))))

    tre = docs.get("treasury") or {}
    if "inflow" in tre or "outflow" in tre:
        bad = []
        bal, inflow, outflow = tre.get("balance", []), tre.get("inflow", []), tre.get("outflow", [])
        if any(v is None or v < 0 for v in inflow + outflow):
            bad.append("inflow and outflow are stored positive, without nulls")
        for i in range(1, min(len(bal), len(inflow), len(outflow))):
            if abs((bal[i] - bal[i - 1]) - (inflow[i] - outflow[i])) > 0.02:
                bad.append("%s: balance change %.2f, inflow - outflow %.2f" % (tre["dates"][i], bal[i] - bal[i - 1], inflow[i] - outflow[i]))
        for r in tre.get("reported", []):
            if not is_date(r.get("date")) or not num(r.get("holdings")):
                bad.append("reported entry %r needs a date and holdings" % r)
        results.append(("treasury: inflow less outflow is the change in the balance", bad, "%d reported markers" % len(tre.get("reported", []))))

    states = ac.get("states")
    if isinstance(states, dict):
        bad = []
        if sorted(states) != ["emptied", "holding", "never_funded"]:
            bad.append("states keys are %s" % sorted(states))
        elif any(not isinstance(v, int) or v < 0 for arr in states.values() for v in arr):
            bad.append("a pool count is not a non-negative integer")
        else:
            tot = [a + b + c for a, b, c in zip(states["holding"], states["emptied"], states["never_funded"])]
            if any(y < x for x, y in zip(tot, tot[1:])):
                bad.append("the number of pools ever created falls on some date")
        results.append(("accounts.states: three counts per date, pools ever created never fall", bad, None))

    ps = docs.get("pools_snapshot") or {}
    if "top" in ps:
        bad = []
        top = ps.get("top") or []
        if len(top) > 100 or any(a["balance"] < b["balance"] for a, b in zip(top, top[1:])):
            bad.append("top must hold at most 100 pools in descending order of balance")
        elif ps.get("total_balance") and len(top) == 100 and abs(sum(t["balance"] for t in top) / ps["total_balance"] - ps["top_share"]["100"]) > 0.00011:
            bad.append("the 100 pools hold %.4f of balances, top_share.100 is %s" % (sum(t["balance"] for t in top) / ps["total_balance"], ps["top_share"]["100"]))
        if ps.get("total_balance") and any(abs(t["balance"] / ps["total_balance"] - t["share"]) > 0.00011 for t in top if num(t.get("share"))):
            bad.append("a pool's share is not its balance over total_balance")
        results.append(("pools_snapshot.top: ordered, shares tie to total_balance and top_share", bad, "%d pools" % len(top)))

    rw = docs.get("rewards")
    if isinstance(rw, dict):
        bad = []
        if any(v is None or v < 0 for v in rw.get("referral", []) + rw.get("loss_refund", [])):
            bad.append("referral and loss_refund are stored positive, without nulls")
        cl = rw.get("claimants") or []
        if len(cl) > 25 or any(a["total"] < b["total"] for a, b in zip(cl, cl[1:])):
            bad.append("claimants must hold at most 25 wallets in descending order of total")
        if not num(rw.get("top10_share")) or not 0 <= rw["top10_share"] <= 1:
            bad.append("top10_share %r is outside 0..1" % rw.get("top10_share"))
        elif len(cl) >= 10 and all(num(c.get("share")) for c in cl) and abs(sum(c["share"] for c in cl[:10]) - rw["top10_share"]) > 0.0011:
            bad.append("the ten largest claimants' shares sum to %.4f, top10_share is %s" % (sum(c["share"] for c in cl[:10]), rw["top10_share"]))
        results.append(("rewards: positive flows, claimants ordered, top ten share ties", bad, "%d claimants" % len(cl)))

    co = docs.get("cohorts")
    if isinstance(co, dict):
        bad = []
        rows = co.get("cohorts") or []
        if [r.get("month") for r in rows] != sorted({r.get("month") for r in rows}):
            bad.append("cohort months are not strictly ascending")
        for r in rows:
            if not isinstance(r.get("size"), int) or r["size"] < 0 or any(v is not None and not 0 <= v <= 1 for v in r.get("active", [])):
                bad.append("cohort %s: size must be a count and active shares must lie in 0..1" % r.get("month"))
        results.append(("cohorts: months ascending, shares in 0..1", bad, "%d cohorts" % len(rows)))

    rows = (docs.get("markets_latest") or {}).get("rows", [])
    if rows and ("turnover" in rows[0] or "long_share" in rows[0]):
        bad = []
        for r in rows:
            one = (r.get("oi_long") or 0) + (r.get("oi_short") or 0)
            if "turnover" in r:
                want = (r["vol24h"] or 0) / one if one else None
                if (r["turnover"] is None) != (want is None) or (want is not None and abs(r["turnover"] - want) > max(0.006, 1e-4 * want)):
                    bad.append("%s: turnover %r, volume over open interest %r" % (r["ticker"], r["turnover"], want))
            if "long_share" in r:
                want = r["oi_long"] / one if one and r.get("oi_long") is not None else None
                if (r["long_share"] is None) != (want is None) or (want is not None and abs(r["long_share"] - want) > 0.00011):
                    bad.append("%s: long_share %r, long over long + short %r" % (r["ticker"], r["long_share"], want))
        results.append(("markets_latest: turnover and long_share recomputed", bad, "%d rows" % len(rows)))

    for name, problems, note in results:
        rep.check("ties", name, problems, note)


# ---- the contract's v3 additions

def exec_samples():
    """One sample per (day, market, size, snapshot) from <store>/exec/*.jsonl; a matched line wins over an unmatched one."""
    chosen = {}
    for path in sorted(glob.glob(os.path.join(STORE, "exec", "*.jsonl"))):
        day = os.path.basename(path)[:-len(".jsonl")]
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    ident = (day, row["key"], row["size"], row["snap"])
                except (ValueError, KeyError):
                    continue
                held = chosen.get(ident)
                if held is None or (row.get("matched") and not held.get("matched")):
                    chosen[ident] = row
    return list(chosen.values())


def check_v3(rep, docs, now):
    oi, markets = docs.get("oi_daily") or {}, market_files(docs)

    # dual-sided open interest: twice the one-sided total everywhere, and the API's own figure on Omni-sourced days
    bad, n = [], 0
    dual = oi.get("dual", [])
    for i, day in enumerate(oi.get("dates", [])):
        t, d = oi["total"][i], dual[i] if i < len(dual) else None
        if (t is None) != (d is None) or (t is not None and abs(d - 2 * t) > 0.011):
            bad.append("%s: dual %s, one-sided %s" % (day, d, t))
    at = {d: i for i, d in enumerate(oi.get("dates", []))}
    omni = (oi.get("sources") or []).index("omni") if "omni" in (oi.get("sources") or []) else None
    for path in sorted(glob.glob(os.path.join(STORE, "days", "*.json"))):
        try:
            day = strict_load(path)
        except (ValueError, OSError):
            continue
        i = at.get(day.get("date"))
        if i is None or oi["src"][i] != omni or i >= len(dual) or dual[i] is None:
            continue
        n += 1
        api = float((day.get("platform") or {}).get("dual_sided_oi") or 0)
        if abs(dual[i] - api) > 1.0:
            bad.append("%s: dual %.2f, the API's dual-sided open interest at the day's end %.2f" % (day["date"], dual[i], api))
    rep.check("ties", "oi_daily.dual: twice the one-sided total, and the API's dual-sided figure on Omni-sourced days", bad,
              "%d dates, %d against the API" % (len(dual), n))

    # swap_pairs: both legs against the listings' own files, range medians recomputed
    sp = docs.get("swap_pairs") or {}
    bad = []
    keys = [p.get("key") for p in sp.get("pairs", [])]
    for leg in ("perp", "swap"):
        for metric in ("ba100k", "carry"):
            if list(((sp.get(leg) or {}).get(metric) or {}).keys()) != keys:
                bad.append("%s.%s keys differ from the pairs list" % (leg, metric))
    if sorted(sp.get("range") or {}) != sorted(RANGE_KEYS):
        bad.append("range keys are %s" % list((sp.get("range") or {}).keys()))
    dates = sp.get("dates", [])
    sealed = {}
    for path in glob.glob(os.path.join(STORE, "days", "*.json")):
        try:
            sealed[os.path.basename(path)[:10]] = strict_load(path).get("pairs") or {}
        except (ValueError, OSError):
            pass
    if not bad:
        for p in sp.get("pairs", []):
            k = p["key"]
            for leg, rate, scale in (("perp", "funding_ann", 1.0), ("swap", "fin_long_ann", 365.0 / 360.0)):
                m = markets.get(p.get(leg))
                if m is None:
                    bad.append("%s: no market file for %s" % (k, p.get(leg)))
                    continue
                mi = {d: i for i, d in enumerate(m.get("dates", []))}
                for i, day in enumerate(dates):
                    j = mi.get(day)
                    ba, carry = sp[leg]["ba100k"][k][i], sp[leg]["carry"][k][i]
                    # a leg's bid-ask is its median over the snapshots in which both legs were quoted: the sealed day holds it
                    med = (sealed.get(day, {}).get(k) or {}).get("%s_ba100k_med" % leg)
                    want_ba = round(med, 2) if med is not None else None
                    want_carry = m[rate][j] * scale if j is not None and m[rate][j] is not None else None
                    if day not in sealed:
                        bad.append("%s: swap_pairs has a date with no sealed day file" % day)
                    elif ba != want_ba and not (num(ba) and num(want_ba) and abs(ba - want_ba) < 0.006):
                        bad.append("%s %s %s: bid-ask %r, the sealed day has %r" % (k, leg, day, ba, want_ba))
                    if (sp["perp"]["ba100k"][k][i] is None) != (sp["swap"]["ba100k"][k][i] is None):
                        bad.append("%s %s: one bid-ask leg without the other" % (k, day))
                    if (carry is None) != (want_carry is None) or (carry is not None and abs(carry - want_carry) > 2e-5):
                        bad.append("%s %s %s: carry %r, the listing's rate on a 365-day basis is %r" % (k, leg, day, carry, want_carry))
            if dates:
                last = dt.date.fromisoformat(dates[-1])
                for rk, span in (("7D", 7), ("30D", 30), ("90D", 90), ("180D", 180), ("1Y", 365), ("All", None)):
                    idx = [i for i, d in enumerate(dates) if span is None or (last - dt.date.fromisoformat(d)).days < span]
                    node = (sp["range"].get(rk) or {}).get(k) or {}
                    for leg in ("perp", "swap"):
                        for metric, places in (("ba100k", 2), ("carry", 6)):
                            # the median runs over the dates in range on which both legs carry that figure
                            vals = [sp[leg][metric][k][i] for i in idx
                                    if sp["perp"][metric][k][i] is not None and sp["swap"][metric][k][i] is not None]
                            want = round(median(vals), places) if vals else None
                            have = node.get("%s_%s" % (leg, metric))
                            if (have is None) != (want is None) or (have is not None and abs(have - want) > 10 ** -places):
                                bad.append("range %s %s: %s_%s %r, the median of the dates in range is %r" % (rk, k, leg, metric, have, want))
                    both = sum(1 for i in idx if sp["perp"]["ba100k"][k][i] is not None and sp["swap"]["ba100k"][k][i] is not None)
                    if node.get("n") != both:
                        bad.append("range %s %s: n %r, dates with both legs quoted %d" % (rk, k, node.get("n"), both))
    rep.check("ties", "swap_pairs: bid-ask legs against the sealed days, carry against the listings on a 365-day basis, range medians recomputed", bad,
              "%d pairs, %d dates" % (len(keys), len(dates)))

    # markets_latest: class, group and listing date against classification.json
    bad = []
    try:
        cfg = strict_load(os.path.join(ROOT, "pipeline", "config", "classification.json"))["listings"]
    except (OSError, ValueError, KeyError) as exc:
        cfg = None
        bad.append("classification.json unreadable: %s" % exc)
    if cfg is not None:
        first_snapshot = min((v["listed"]["date"] for v in cfg.values() if (v.get("listed") or {}).get("evidence") == "snapshot"), default=None)
        dated = 0
        for r in (docs.get("markets_latest") or {}).get("rows", []):
            c = cfg.get(r.get("ticker"))
            if c is None:
                continue                         # classified by rule; the builder lists it in its build report
            if r.get("cls") != c.get("cls") or r.get("group") != c.get("group"):
                bad.append("%s: group %s and class %s, classification.json has %s and %s" % (r["ticker"], r.get("group"), r.get("cls"), c.get("group"), c.get("cls")))
            known = c.get("listed") or {}
            if r.get("listed") is not None:
                dated += 1
                if r["listed"] != known.get("date"):
                    bad.append("%s: listed %s, classification.json has %s" % (r["ticker"], r["listed"], known.get("date")))
            elif known.get("date") and not (known.get("evidence") == "snapshot" and known["date"] == first_snapshot):
                # only a listing already present in the first snapshot has an unknown listing date
                bad.append("%s: listed is null, classification.json dates it %s (%s)" % (r["ticker"], known["date"], known.get("evidence")))
        note = "%d rows dated; a listing already in the first snapshot (%s) has no known date" % (dated, first_snapshot)
    rep.check("ties", "markets_latest: group, class and listing date against classification.json", bad, note if cfg is not None else None)

    # Protocol Treasury: on-chain inflow per report period against the reported two-week inflow
    tre, fin = docs.get("treasury") or {}, docs.get("financials") or {}
    by_day = dict(zip(tre.get("dates", []), tre.get("inflow", [])))
    bad, covered, sum_rep, sum_chain = [], 0, 0.0, 0.0
    for p in fin.get("periods", []):
        reported = p.get("treasury_inflow")
        if not reported or not is_date(p.get("start")) or not is_date(p.get("end")):
            continue
        day, total, complete = dt.date.fromisoformat(p["start"]) + dt.timedelta(days=1), 0.0, True
        while day <= dt.date.fromisoformat(p["end"]):          # reports are cut during their day: UTC days start+1..end
            if day.isoformat() not in by_day or by_day[day.isoformat()] is None:
                complete = False
                break
            total += by_day[day.isoformat()]
            day += dt.timedelta(days=1)
        if not complete:
            continue
        covered += 1
        sum_rep += reported
        sum_chain += total
        if abs(total / reported - 1) > INFLOW_TOLERANCE:
            bad.append("%s to %s: on-chain inflow %.2f is %+.2f%% from the reported %s (tolerance %.0f%%)"
                       % (p["start"], p["end"], total, 100 * (total / reported - 1), reported, 100 * INFLOW_TOLERANCE))
    if covered and abs(sum_chain / sum_rep - 1) > INFLOW_TOLERANCE_TOTAL:
        bad.append("all covered periods: on-chain %.2f is %+.2f%% from the reported %.0f (tolerance %.0f%%)"
                   % (sum_chain, 100 * (sum_chain / sum_rep - 1), sum_rep, 100 * INFLOW_TOLERANCE_TOTAL))
    if not covered:
        bad.append("no report period is covered by the on-chain inflow series")
    try:
        want = sorted(({"date": r["published_at"][:10], "holdings": r["treasury_holdings"]} for r in strict_load(os.path.join(STORE, "reports.json"))
                       if r.get("treasury_holdings") is not None and r.get("published_at")), key=lambda x: x["date"])
        if tre.get("reported") != want:
            bad.append("treasury.reported differs from the holdings printed in the reports store (%d against %d)" % (len(tre.get("reported", [])), len(want)))
    except (OSError, ValueError, KeyError) as exc:
        bad.append("reports store unreadable: %s" % str(exc)[:100])
    rep.check("ties", "treasury: on-chain inflow within %.0f%% of each reported two-week inflow and %.0f%% in total; reported holdings from the reports"
              % (100 * INFLOW_TOLERANCE, 100 * INFLOW_TOLERANCE_TOTAL), bad,
              "%d periods, on-chain %+.2f%% in total" % (covered, 100 * (sum_chain / sum_rep - 1)) if covered else None)

    # execution_venue against the sample store, venue by venue; refused ticks are counted, never failed
    ev = docs.get("execution_venue") or {}
    venues = [v.get("key") for v in ev.get("venues", [])]
    samples = exec_samples()
    bad, soft = [], []
    # The builder dates the file by the newest moment of a matched pair (build_execution.py: omni_at and the venue's
    # own stamp), so the file is current when no matched pair in the store is newer than its as_of. Tick names cannot
    # decide this, and both ways of reading them held the site: a tick on which no pair matched leaves as_of at an
    # earlier tick (23 Nov 2026 21:00 UTC: the Omni reading was 78 s older than the books), and a book read just
    # before the tick dates the file a second before the tick's name (24 Nov 2026 00:00 UTC: as_of 23:59:59).
    moments = [m for r in samples
               for flag, stamp in [("matched", "hl_at")] + [(k, k[:-len("matched")] + "at") for k in r if k.endswith("_matched")]
               if r.get(flag) for m in (r.get("omni_at"), r.get(stamp)) if m]
    moment = max(moments)[:19] + "Z" if moments else None
    current = moment is None or (is_time(ev.get("as_of")) and is_time(moment) and parse_time(moment) <= parse_time(ev["as_of"]))
    for v in venues:
        flag, status = ("matched", None) if v == "hl" else (v + "_matched", v + "_status")
        matched = sum(1 for r in samples if r.get(flag))
        refused = sum(1 for r in samples if status and r.get(status) == "venue_unavailable")
        block = ((ev.get("match") or {}).get("venues") or {}).get(v) or {}
        if current:                                # the file is current with the store: counts must agree
            if block.get("matched") != matched:
                bad.append("%s: the file counts %r matched samples, the store has %d" % (v, block.get("matched"), matched))
            if v != "hl" and block.get("unavailable") != refused:
                bad.append("%s: the file counts %r refused samples, the store has %d" % (v, block.get("unavailable"), refused))
        last = max((r["snap"] for r in samples if r.get(flag)), default=None)
        age = now - dt.datetime.strptime(last[:14], "%Y%m%dT%H%MZ") if last else None
        if age is None or age > dt.timedelta(hours=OTHER_VENUES_STALE_H):
            all_refused = sum(1 for r in samples if status and r.get(status) == "venue_unavailable")
            soft.append("%s: no matched sample %s (%d ticks refused by the venue for this location)"
                        % (v, "in the store" if last is None else "since %s, %s ago" % (last, fmt_age(age)), all_refused))
    if not current:
        bad.append("execution_venue.json was built before the newest matched sample (%s after %s): rebuild" % (moment, ev.get("as_of")))
    rep.check("ties", "execution_venue: every venue's matched and refused samples against the sample store", bad,
              "%d samples; %s" % (len(samples), ", ".join(venues)))
    if soft:
        rep.check("freshness", "every venue has a matched sample in the last %d hours" % OTHER_VENUES_STALE_H, soft, warn=True)


# ---- the contract's v3.1 additions that tie financials to the on-chain treasury

def period_days(start, end):
    """The UTC days of a report period: start + 1 ... end (reports are cut during their day)."""
    a, b = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    return [(a + dt.timedelta(days=i)).isoformat() for i in range(1, (b - a).days + 1)]


def check_v31(rep, docs):
    fin, tre, sd = docs.get("financials") or {}, docs.get("treasury") or {}, docs.get("spreads_daily") or {}
    periods = [p for p in fin.get("periods", []) if is_date(p.get("start")) and is_date(p.get("end"))]
    # complete UTC days only: the day of the treasury file's as_of is still in progress
    inflow = {d: v for d, v in zip(tre.get("dates", []), tre.get("inflow", [])) if d != str(tre.get("as_of"))[:10] and num(v)}

    bad, covered = [], 0
    for p in periods:
        days = period_days(p["start"], p["end"])
        want = round(sum(inflow[d] for d in days), 2) if days and all(d in inflow for d in days) else None
        have = p.get("treasury_inflow_onchain")
        covered += want is not None
        if (have is None) != (want is None) or (want is not None and abs(have - want) > 0.011):
            bad.append("%s: treasury_inflow_onchain %r, treasury.json sums to %r over %s to %s" % (p["end"], have, want, days[0] if days else None, days[-1] if days else None))
    rep.check("ties", "financials.treasury_inflow_onchain recomputed from treasury.json", bad, "%d of %d periods covered on-chain" % (covered, len(periods)))

    bad = []
    dates, implied, share = sd.get("dates", []), sd.get("implied", []), sd.get("share", [])
    if sd.get("as_of") != tre.get("as_of"):
        bad.append("spreads_daily.as_of %s differs from treasury.as_of %s" % (sd.get("as_of"), tre.get("as_of")))
    if not dates or dates[0] < SPREADS_START:
        bad.append("the series must start on or after %s (it starts %s)" % (SPREADS_START, dates[0] if dates else None))
    if any((dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days != 1 for a, b in zip(dates, dates[1:])):
        bad.append("the series skips a day")
    want_dates = sorted(d for d in inflow if d >= SPREADS_START)
    if dates != want_dates:
        bad.append("dates differ from the complete days of treasury.json since %s (%d against %d)" % (SPREADS_START, len(dates), len(want_dates)))
    # the share in force on a day is the treasury share of the report period that contains it; days after the
    # newest report carry its share until the next report covers them
    in_force = {}
    for p in periods:
        for d in period_days(p["start"], p["end"]):
            in_force[d] = p.get("treasury_share")
    for i, d in enumerate(dates):
        want_share = in_force.get(d, periods[-1].get("treasury_share") if periods and d > periods[-1]["end"] else None)
        if i >= len(share) or share[i] != want_share or not want_share:
            bad.append("%s: share %r, the share in force is %r" % (d, share[i] if i < len(share) else None, want_share))
        elif d not in inflow or i >= len(implied) or abs(implied[i] - round(inflow[d] / want_share, 2)) > 0.011:
            bad.append("%s: implied %r, treasury inflow over the share gives %r" % (d, implied[i] if i < len(implied) else None,
                                                                                   round(inflow[d] / want_share, 2) if d in inflow else None))
    by_day = dict(zip(dates, implied))
    n, sum_rep, sum_imp = 0, 0.0, 0.0
    for p in periods:
        days = period_days(p["start"], p["end"])
        if not days or days[0] < SPREADS_START or any(d not in by_day for d in days) or not p.get("spreads_paid"):
            continue
        total = sum(by_day[d] for d in days)
        n += 1
        sum_rep += p["spreads_paid"]
        sum_imp += total
        if abs(total / p["spreads_paid"] - 1) > SPREADS_TOLERANCE:
            bad.append("%s to %s: implied %.2f is %+.2f%% from the reported spreads paid %s (tolerance %.0f%%)"
                       % (p["start"], p["end"], total, 100 * (total / p["spreads_paid"] - 1), p["spreads_paid"], 100 * SPREADS_TOLERANCE))
    if not n:
        bad.append("no report period since %s is covered: nothing validates the series" % SPREADS_START)
    elif abs(sum_imp / sum_rep - 1) > SPREADS_TOLERANCE_TOTAL:
        bad.append("all covered periods: implied %.2f is %+.2f%% from the reported %.0f (tolerance %.1f%%)"
                   % (sum_imp, 100 * (sum_imp / sum_rep - 1), sum_rep, 100 * SPREADS_TOLERANCE_TOTAL))
    rep.check("ties", "spreads_daily: treasury inflow over the share in force; within %.0f%% of reported spreads paid per period and %.1f%% overall"
              % (100 * SPREADS_TOLERANCE, 100 * SPREADS_TOLERANCE_TOTAL), bad[:40],
              "%d days, %d periods, implied %+.2f%% overall" % (len(dates), n, 100 * (sum_imp / sum_rep - 1)) if n else None)


# ---- intraday: the trailing seven days of half-hourly points must really be seven days

def check_intraday(rep, docs):
    """markets_scope.json scope all.all: the intraday window ends at the data time and is as long as the capture allows.

    The builder reads raw snapshots through VM_SNAPSHOTS and shortens the window without complaint when
    fewer than seven days of them are there. This check is what complains.
    """
    meta = docs.get("meta") or {}
    scope = ((docs.get("markets_scope") or {}).get("scopes") or {}).get("all.all") or {}
    ts = (scope.get("intraday") or {}).get("ts")
    as_of = meta.get("as_of") or meta.get("data_as_of")
    name = "intraday window (markets_scope all.all): ends within %d minutes of the data time, spans the capture" % INTRADAY_MAX_LAG_MIN
    if not isinstance(ts, list) or not ts:
        rep.add("freshness", name, "fail", ["markets_scope.json has no intraday ts for scope all.all"])
        return
    try:
        points = [dt.datetime.strptime(t, "%Y-%m-%dT%H:%MZ") for t in ts]
    except (TypeError, ValueError):
        rep.add("freshness", name, "fail", ["an intraday ts is not a UTC minute like 2026-11-22T07:30Z"])
        return
    bad = []
    if any(a >= b for a, b in zip(points, points[1:])) or len(points) > INTRADAY_DAYS * 48:
        bad.append("ts is not strictly ascending, or holds more than %d points" % (INTRADAY_DAYS * 48))
    first, last = points[0], points[-1]
    span = (last - first).total_seconds() / 86400.0
    if not is_time(as_of):
        bad.append("meta.json has no data time to compare with")
    elif abs((parse_time(as_of) - last).total_seconds()) > INTRADAY_MAX_LAG_MIN * 60:
        bad.append("the window ends %s, the data time is %s (limit %d minutes)" % (ts[-1], as_of, INTRADAY_MAX_LAG_MIN))
    # the raw snapshots the publish job can see, read here from the folders (file names only)
    stamps = set()
    for folder in SNAPSHOT_DIRS:
        for path in glob.glob(os.path.join(folder, "*.json.gz")):
            try:
                stamps.add(dt.datetime.strptime(os.path.basename(path)[:14], "%Y%m%dT%H%MZ"))
            except ValueError:
                pass
    window = sorted(s for s in stamps if last - dt.timedelta(days=INTRADAY_DAYS) < s <= last)
    start = meta.get("continuous_start")
    running = (last - dt.datetime.strptime(start, "%Y-%m-%d")).total_seconds() / 86400.0 if is_date(start) else None
    if running is None:
        bad.append("meta.continuous_start is missing: cannot tell how long capture has run")
    elif running >= INTRADAY_DAYS:
        if span < INTRADAY_MIN_SPAN_DAYS:
            bad.append("capture has run %.1f days but the window spans only %.2f days (%s to %s, %d points); it must span at least %.1f. "
                       "Fewer than seven days of raw snapshots were readable at VM_SNAPSHOTS (%d files in the window)"
                       % (running, span, ts[0], ts[-1], len(ts), INTRADAY_MIN_SPAN_DAYS, len(window)))
    # a repeated API response is left out of the series, so the first few files of the window may be absent
    if window and (first - window[0]).total_seconds() > INTRADAY_MAX_LAG_MIN * 60:
        bad.append("the window starts %s but a raw snapshot from %s is available: the series is shorter than the snapshots allow"
                   % (ts[0], window[0].strftime("%Y-%m-%dT%H:%MZ")))
    if not window:
        bad.append("no raw snapshot of the last %d days is readable at VM_SNAPSHOTS (%s)" % (INTRADAY_DAYS, os.pathsep.join(SNAPSHOT_DIRS)))
    else:
        have = set(points)
        absent = [s for s in window if s not in have]
        if len(absent) > max(2, INTRADAY_MAX_MISSING * len(window)):
            bad.append("%d of the %d raw snapshots in the window are not in the series (first: %s)"
                       % (len(absent), len(window), absent[0].strftime("%Y-%m-%dT%H:%MZ")))
    # every listing's own intraday block ends at the same snapshot
    btc = ((docs.get("market/BTC-PERP") or {}).get("intraday") or {}).get("ts")
    if btc is not None and (not btc or btc[-1] != ts[-1] or btc[0] != ts[0]):
        bad.append("market/BTC-PERP.json intraday runs %s to %s, the scope runs %s to %s"
                   % (btc[0] if btc else None, btc[-1] if btc else None, ts[0], ts[-1]))
    rep.check("freshness", name, bad, "%d points over %.2f days, %s to %s; capture has run %.1f days; %d raw snapshots in the window"
              % (len(ts), span, ts[0], ts[-1], running if running is not None else -1, len(window)))


# ---- research library: research.json read against the configs, independently of the builder

def research_day(value):
    """The UTC day of a config's published_at (a date, or a timestamp with Z or an offset); None when unreadable."""
    if not isinstance(value, str):
        return None
    if is_date(value):
        return value
    try:
        t = dt.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    if t.tzinfo is not None:
        t = t.astimezone(dt.timezone.utc)
    return t.strftime("%Y-%m-%d")


def check_research(rep, docs):
    """research.json: one entry per config in pipeline/config/research, as the config gives it, newest first."""
    index = docs.get("research") or {}
    bad, want = [], []
    for path in sorted(glob.glob(os.path.join(ROOT, "pipeline", "config", "research", "*.json"))):
        slug = os.path.basename(path)[:-5]
        try:
            cfg = strict_load(path)
        except (OSError, ValueError) as exc:
            bad.append("config %s unreadable: %s" % (slug, exc))
            continue
        extra = sorted(k for k in cfg if k not in RESEARCH_CONFIG_KEYS and not k.startswith("_"))
        if extra:
            bad.append("config %s: unknown keys %s" % (slug, extra))
        if cfg.get("published_at"):
            status, day = "published", research_day(cfg["published_at"])
            if day is None or not str(cfg.get("url") or "").startswith("https://"):
                bad.append("config %s: published, but published_at %r or url %r is not usable" % (slug, cfg.get("published_at"), cfg.get("url")))
        else:
            status, day = "scheduled", cfg.get("scheduled_for")
            if not is_date(day) or cfg.get("url") is not None:
                bad.append("config %s: scheduled, but scheduled_for %r is not a date or url %r is set" % (slug, day, cfg.get("url")))
        want.append({"slug": slug, "title": (cfg.get("title") or "").strip(), "publisher": (cfg.get("publisher") or "").strip(),
                     "date": day, "url": cfg.get("url"), "status": status})
    want.sort(key=lambda p: (p["date"] or "", p["slug"]), reverse=True)
    got = index.get("pieces") if isinstance(index.get("pieces"), list) else None
    if got is None:
        bad.append("research.json has no pieces list")
    else:
        have = {p.get("slug"): p for p in got if isinstance(p, dict)}
        for w in want:
            g = have.get(w["slug"])
            if g is None:
                bad.append("%s: configured but not listed" % w["slug"])
            elif g != w:
                bad.append("%s: listed as %s, the config gives %s" % (w["slug"], g, w))
        bad += ["%s: listed but not configured" % k for k in have if k not in {w["slug"] for w in want}]
        if [p.get("slug") for p in got if isinstance(p, dict)] != [w["slug"] for w in want if w["slug"] in have]:
            bad.append("pieces are not newest first (by date, then slug)")
    rep.check("ties", "research.json against pipeline/config/research", bad,
              "%d piece(s): %s" % (len(want), "; ".join("%s %s %s" % (w["slug"], w["status"], w["date"]) for w in want) or "none"))


# ---- daily volume: DefiLlama days against the stored export, and DefiLlama against the reported two-week totals

def check_defillama_volume(rep, docs):
    vol = docs.get("volume_daily") or {}
    folder = os.path.join(STORE, "defillama")
    csv_path, note_path = os.path.join(folder, "perp_volume_export.csv"), os.path.join(folder, "perp_volume_export.json")
    sources = vol.get("sources") or []
    code = sources.index("defillama") if "defillama" in sources else None
    used = [d for d, s in zip(vol.get("dates", []), vol.get("src", [])) if code is not None and s == code]
    if not os.path.exists(csv_path):
        rep.check("ties", "volume_daily: DefiLlama days against the stored export",
                  ["volume_daily uses DefiLlama on %d days, the store has no defillama/perp_volume_export.csv" % len(used)] if used else [],
                  "the store has no DefiLlama export: every day before our counter days is a reported average")
        return
    bad = []
    try:
        note = strict_load(note_path)
        with open(csv_path, "rb") as fh:
            raw = fh.read()
        if hashlib.sha256(raw).hexdigest() != note.get("sha256"):
            bad.append("perp_volume_export.csv is not the file its note describes (SHA-256 differs)")
        lines = raw.decode("utf-8-sig").strip().splitlines()
        if [c.strip() for c in lines[0].split(",")] != ["Timestamp", "Date", "Perp Volume"]:
            bad.append("export header %r" % lines[0])
        export = {}
        for line in lines[1:]:
            ts, day, v = [c.strip() for c in line.split(",")]
            if dt.datetime.utcfromtimestamp(int(ts) / 1000).strftime("%Y-%m-%d") != day:
                bad.append("export row %s: timestamp %s" % (day, ts))
            if day < note["exported_on"]:              # the export day itself is still in progress
                export[day] = int(v)
    except (OSError, ValueError, KeyError, IndexError) as exc:
        rep.add("ties", "volume_daily: DefiLlama days against the stored export", "fail", ["export unreadable: %s" % exc])
        return
    dates, total, src = vol.get("dates", []), vol.get("total", []), vol.get("src", [])
    omni = sources.index("omni") if "omni" in sources else None
    for i, day in enumerate(dates):
        if src[i] == code:
            if day not in export:
                bad.append("%s: tagged defillama, the export has no such day" % day)
            elif round(total[i] * 100) != export[day] * 100:
                bad.append("%s: volume_daily %.2f, the export %d" % (day, total[i], export[day]))
        elif day in export and src[i] != omni:
            bad.append("%s: the export has %d, volume_daily uses %s" % (day, export[day], sources[src[i]] if src[i] is not None else None))
    rep.check("ties", "volume_daily: DefiLlama days against the stored export", bad,
              "%d days from DefiLlama (export of %s, %s to %s)" % (len(used), note.get("exported_on"), min(export), max(export)))

    # DefiLlama summed over each report period (boundary days weighted by the hours inside it) against the reported change
    reports = []
    try:
        reports = sorted((parse_time(r["published_at"]), r["cumulative_volume"]) for r in strict_load(os.path.join(STORE, "reports.json"))
                         if r.get("cumulative_volume") is not None)
    except (OSError, ValueError, KeyError):
        pass
    rows, soft = [], []
    for (ta, ca), (tb, cb) in zip(reports, reports[1:]):
        s_, d_ = 0.0, ta.date()
        while d_ <= tb.date():
            lo = max(ta, dt.datetime(d_.year, d_.month, d_.day))
            hi = min(tb, dt.datetime(d_.year, d_.month, d_.day) + dt.timedelta(days=1))
            if hi > lo:
                if d_.isoformat() not in export:
                    s_ = None
                    break
                s_ += (hi - lo).total_seconds() / 86400.0 * export[d_.isoformat()]
            d_ += dt.timedelta(days=1)
        if s_ is None or cb <= ca:
            continue
        r_ = s_ / (cb - ca) - 1
        rows.append(r_)
        if abs(r_) > DEFILLAMA_RESIDUAL_WARN:
            soft.append("period %s to %s: DefiLlama %.2fB against the reported %.2fB (%+.2f%%)"
                        % (ta.strftime("%Y-%m-%d"), tb.strftime("%Y-%m-%d"), s_ / 1e9, (cb - ca) / 1e9, r_ * 100))
    note_ = ("%d report periods: DefiLlama against the reported two-week total, median %+.2f%%, largest %+.2f%%"
             % (len(rows), median(rows) * 100, max(rows, key=abs) * 100)) if rows else "no report period covered by the export"
    rep.check("ties", "volume_daily: DefiLlama's daily volume against each reported two-week total (within %.0f%%)"
              % (DEFILLAMA_RESIDUAL_WARN * 100), soft, note_, warn=True)


# ---------------------------------------------------------------- E. freshness

def check_freshness(rep, docs, now):
    latest = (docs.get("markets_latest") or {}).get("as_of")
    bad = []
    if not is_time(latest):
        bad.append("markets_latest.as_of is missing")
    elif now - parse_time(latest) > dt.timedelta(minutes=MAX_SNAPSHOT_AGE_MIN):
        bad.append("newest snapshot is %s old (as of %s, limit %d minutes)" % (fmt_age(now - parse_time(latest)), latest, MAX_SNAPSHOT_AGE_MIN))
    rep.check("freshness", "newest Omni snapshot under %d minutes old" % MAX_SNAPSHOT_AGE_MIN, bad,
              "as of %s, %s old" % (latest, fmt_age(now - parse_time(latest))) if not bad else None)

    bad, ages = [], []
    for name in ("treasury", "olp_chain", "accounts", "pools_snapshot", "tvl", "rewards", "spreads_daily"):
        as_of = (docs.get(name) or {}).get("as_of")
        if not is_time(as_of):
            bad.append("%s.json has no as_of" % name)
        elif now - parse_time(as_of) > dt.timedelta(hours=MAX_ONCHAIN_AGE_H):
            bad.append("%s.json is %s old (as of %s, limit %d hours)" % (name, fmt_age(now - parse_time(as_of)), as_of, MAX_ONCHAIN_AGE_H))
        else:
            ages.append(now - parse_time(as_of))
    # cohorts hold complete months only: their as_of is the end of the last complete month
    co = (docs.get("cohorts") or {}).get("as_of")
    month_start = now.strftime("%Y-%m-01T00:00:00Z")
    if not is_time(co) or (co < month_start and now - parse_time(month_start) > dt.timedelta(hours=MAX_ONCHAIN_AGE_H)):
        bad.append("cohorts.json ends at %s; the month that closed on %s is missing" % (co, month_start[:10]))
    rep.check("freshness", "on-chain files under %d hours old" % MAX_ONCHAIN_AGE_H, bad,
              "oldest is %s old" % fmt_age(max(ages)) if ages and not bad else None)

    # the indexer's own reconciliation of its ledger against balanceOf
    snap = None
    try:
        snap = strict_load(os.path.join(ONCHAIN_DATA, "pools_snapshot.json"))
    except (OSError, ValueError):
        pass
    if snap is None:
        rep.add("freshness", "on-chain ledger reconciles with balances", "skip", ["no aggregates at %s" % ONCHAIN_DATA])
    else:
        rec = snap.get("reconciliation") or {}
        bad = []
        share = (rec.get("abs_residual_usd") or 0) / max(1.0, rec.get("ledger_total_usd") or 1.0)
        if rec.get("system_mismatches"):
            bad.append("system addresses do not reconcile: %s" % json.dumps(rec["system_mismatches"])[:200])
        if share > RECONCILE_MAX_SHARE:
            bad.append("unexplained %.2f USD across %s pools is %.4f%% of pool balances (limit %.2f%%)"
                       % (rec.get("abs_residual_usd"), rec.get("pools_mismatched"), share * 100, RECONCILE_MAX_SHARE * 100))
        if not rec:
            bad.append("no reconciliation recorded")
        rep.check("freshness", "on-chain ledger reconciles with balanceOf within %.2f%% of pool balances" % (RECONCILE_MAX_SHARE * 100), bad,
                  "%s of %s pools exact, %.2f USD unexplained" % (rec.get("pools_exact"), rec.get("pools_checked"), rec.get("abs_residual_usd") or 0))

    # reports parsed: the channel was read recently and its newest report is the newest period
    bad = []
    fin = docs.get("financials") or {}
    try:
        reports = strict_load(os.path.join(STORE, "reports.json"))
        posts = strict_load(os.path.join(STORE, "telegram", "posts.json"))["posts"]
        state = strict_load(os.path.join(STORE, "telegram", "state.json"))
    except (OSError, ValueError, KeyError) as exc:
        reports, posts, state = None, None, None
        bad.append("the report store is unreadable (%s)" % str(exc)[:100])
    if reports is not None:
        full = [r for r in reports if r.get("full")]
        if not full or fin.get("as_of") != full[-1]["published_at"]:
            bad.append("financials.as_of %s is not the newest full report (%s)" % (fin.get("as_of"), full[-1]["published_at"] if full else None))
        elif not fin.get("periods") or fin["periods"][-1]["end"] != full[-1]["published_at"][:10]:
            bad.append("the newest period does not end on the newest full report's date")
        newest_report_post = max((p["time"] for p in posts if "Total Volume Traded" in p.get("text", "")), default=None)
        if newest_report_post and newest_report_post > max(r["published_at"] for r in reports):
            bad.append("a report posted %s is in the Telegram store but not in reports.json" % newest_report_post)
        checked = state.get("checked_at")
        if not is_time(checked) or now - parse_time(checked) > dt.timedelta(hours=MAX_TELEGRAM_CHECK_H):
            bad.append("Telegram was last read %s (%s ago, limit %d hours)"
                       % (checked, fmt_age(now - parse_time(checked)) if is_time(checked) else "?", MAX_TELEGRAM_CHECK_H))
    rep.check("freshness", "reports parsed: channel read within %d hours, newest report is the newest period" % MAX_TELEGRAM_CHECK_H, bad,
              "%d reports, newest %s" % (len(reports), reports[-1]["published_at"]) if reports and not bad else None)

    soft = []
    src = (docs.get("meta") or {}).get("sources") or {}
    for key in ("defillama",):
        if is_time(src.get(key)) and now - parse_time(src[key]) > dt.timedelta(days=MAX_EXTERNAL_AGE_D):
            soft.append("%s data is %s old" % (key, fmt_age(now - parse_time(src[key]))))
    tok = (docs.get("token") or {}).get("as_of")
    if is_time(tok) and now - parse_time(tok) > dt.timedelta(hours=6):
        soft.append("token.json (Polymarket) is %s old" % fmt_age(now - parse_time(tok)))
    empty = sorted(k for k, v in src.items() if v is None)
    if empty:
        soft.append("meta.sources has no time for: %s" % ", ".join(empty))
    if soft:
        rep.check("freshness", "external sources current, meta.sources filled", soft, warn=True)


# ---------------------------------------------------------------- F. history

def accepted(accept, name, series, day, now):
    for a in accept:
        if a.get("file") == name and a.get("series") in (series, "*") and a.get("from", "") <= day <= a.get("to", "9999") \
                and (not a.get("expires") or a["expires"] >= now.strftime("%Y-%m-%d")):
            return True
    return False


def compare_series(name, path, prev, new, rule, accept, now):
    """(blocking, restated, accepted) lists for one series of one file."""
    blocking, restated, okd = [], [], []
    p_dates, n_dates = prev.get("dates") or [], new.get("dates") or []
    p_arr, n_arr = pick(prev, path), pick(new, path)
    if not isinstance(p_arr, list):
        return blocking, restated, okd
    if not isinstance(n_arr, list):
        return ["%s %s: the series is gone" % (name, path)], restated, okd
    at = {d: i for i, d in enumerate(n_dates)}
    p_src, n_src = (pick(prev, rule["src"]) if rule.get("src") else None), (pick(new, rule["src"]) if rule.get("src") else None)
    recent_from = len(p_dates) - rule.get("recent", 0)
    reanchor = path in rule.get("reanchor", [])
    last_day = dt.date.fromisoformat(p_dates[-1]) if p_dates else None
    for i, day in enumerate(p_dates):
        was = p_arr[i] if i < len(p_arr) else None
        j = at.get(day)
        if j is None:
            if was is not None:
                (okd if accepted(accept, name, path, day, now) else blocking).append("%s %s %s: the date is gone (was %s)" % (name, path, day, was))
            continue
        now_v = n_arr[j]
        if was is None or was == now_v:
            continue
        if now_v is None:
            (okd if accepted(accept, name, path, day, now) else blocking).append("%s %s %s: %s became null" % (name, path, day, was))
            continue
        diff = abs(now_v - was)
        rel = rule["recent_rel"] if i >= recent_from else rule["rel"]
        # `recent_abs` (optional) replaces `abs` on the provisional dates: a relative tolerance cannot cover a value
        # that stood at zero, so a count that starts the day at zero needs its own allowance
        tol = rule.get("recent_abs", rule["abs"]) if i >= recent_from else rule["abs"]
        if reanchor and (last_day - dt.date.fromisoformat(day)).days <= rule["reanchor_days"]:
            rel = max(rel, rule["reanchor_rel"])
        if diff <= max(tol, rel * abs(was)):
            continue
        text = "%s %s %s: %s -> %s (%+.2f%%)" % (name, path, day, was, now_v, (now_v - was) / abs(was) * 100 if was else 0)
        # compare source names, not codes: a code may name another source after a contract change (volume_daily code 1
        # was coingecko and is defillama since 23 Nov 2026)
        p_name = (prev.get("sources") or [])[p_src[i]] if p_src is not None and i < len(p_src) and p_src[i] is not None else None
        n_name = (new.get("sources") or [])[n_src[j]] if n_src is not None and n_src[j] is not None else None
        if p_src is not None and n_src is not None and i < len(p_src) and p_name != n_name:
            restated.append(text + ", source %s -> %s" % (p_name, n_name))
        elif accepted(accept, name, path, day, now):
            okd.append(text)
        else:
            blocking.append(text)
    return blocking, restated, okd


def check_history(rep, docs, previous, accept, now):
    if not previous:
        rep.add("history", "history against the previous deployment", "skip", ["no --previous folder given"])
        return
    status = {}
    try:
        status = strict_load(os.path.join(previous, "_status.json"))
    except (OSError, ValueError):
        status = {"state": "ok"} if glob.glob(os.path.join(previous, "*.json")) else {"state": "error", "errors": ["no _status.json in %s" % previous]}
    if status.get("state") == "none":
        rep.add("history", "history against the previous deployment", "pass",
                ["no Variational Monitor v2 build is deployed yet: nothing to compare (first deploy)"])
        return
    if status.get("state") != "ok":
        rep.add("history", "history against the previous deployment", "fail",
                ["the deployed site could not be read: %s" % "; ".join(status.get("errors", ["unknown"]))[:300]])
        return
    blocking, restated, okd, compared = [], [], [], 0
    prev_docs = {}
    for path in glob.glob(os.path.join(previous, "**", "*.json"), recursive=True):
        name = os.path.relpath(path, previous)[:-5].replace(os.sep, "/")
        if name.startswith("_"):
            continue
        try:
            prev_docs[name] = strict_load(path)
        except (ValueError, OSError):
            blocking.append("%s.json of the previous deployment is unreadable" % name)
    for name, prev in sorted(prev_docs.items()):
        new = docs.get(name)
        rule = HISTORY.get(name) or (MARKET_HISTORY if name.startswith("market/") and name != "market/index" else None)
        if new is None:
            if name.startswith("market/"):
                restated.append("%s.json was deployed and is no longer built (delisted)" % name)
            elif name in ("thesis", "execution_pairs"):
                restated.append("%s.json was deployed and is replaced in contract v3" % name)
            elif name.startswith("research/"):
                restated.append("%s.json was deployed; the Research Library is a plain list in research.json since 23 Nov 2026" % name)
            else:
                blocking.append("%s.json was deployed and is no longer built" % name)
            continue
        if rule:
            series = expand(prev, rule["series"] + rule.get("reanchor", []))
            for path in series:
                b, r, a = compare_series(name, path, prev, new, rule, accept, now)
                blocking += b
                restated += r
                okd += a
                compared += 1
        if name == "peers_oi":
            new_v = {v["name"]: v for v in new.get("venues", [])}
            for v in prev.get("venues", []):
                if v["name"] not in new_v:
                    restated.append("peers_oi: %s left the six largest venues" % v["name"])
                    continue
                b, r, a = compare_series("peers_oi", "values", {"dates": prev["dates"], "values": v["values"]},
                                         {"dates": new["dates"], "values": new_v[v["name"]]["values"]}, HISTORY["peers_oi"], accept, now)
                blocking += [x.replace("values", v["name"]) for x in b]
                okd += a
                compared += 1
        if name == "financials":
            new_p = {p["end"]: p for p in new.get("periods", [])}
            for p in prev.get("periods", []):
                q = new_p.get(p["end"])
                if q is None:
                    blocking.append("financials: the period ending %s is gone" % p["end"])
                    continue
                for key in PERIOD_KEYS:
                    if key == "treasury_inflow_onchain" and (p.get(key) is None or (num(q.get(key)) and abs(q[key] - p[key]) <= 1.0)):
                        continue               # on-chain: a period becomes covered, or a repair moves it by cents
                    if key not in ("notes", "source_url") and key in p and p.get(key) != q.get(key):
                        text = "financials %s %s: %r -> %r" % (p["end"], key, p.get(key), q.get(key))
                        (okd if accepted(accept, "financials", key, p["end"], now) else blocking).append(text)
            compared += 1
        if name == "token":
            new_w = {p["week"]: p for p in new.get("points", [])}
            for p in prev.get("points", []):
                if new_w.get(p["week"]) != p:
                    blocking.append("token points week %s: %r -> %r" % (p["week"], p, new_w.get(p["week"])))
            ph, nh = (prev.get("fdv") or {}).get("history") or {}, (new.get("fdv") or {}).get("history") or {}
            b, r, a = compare_series("token", "median", {"dates": ph.get("dates", []), "median": ph.get("median", [])},
                                     {"dates": nh.get("dates", []), "median": nh.get("median", [])},
                                     {"rel": 0.0, "abs": 0.011, "recent": 1, "recent_rel": 1.0}, accept, now)
            blocking += b
            compared += 2
        if name == "execution_venue":
            dates = prev.get("dates", [])
            for key, sizes in (prev.get("by") or {}).items():
                for size, cols in sizes.items():
                    for col in ("omni", "hl_book", "hl_fee", "n"):
                        p = {"dates": dates, "v": cols.get(col, [])}
                        q = {"dates": new.get("dates", []), "v": pick(new, "by.%s.%s.%s" % (key, size, col))}
                        # the newest deployed day is still being sampled, so its medians and counts move freely until the
                        # day ends (23 Nov 2026: XAG 1m n 0 -> 2 held the site at the last build); the sample-store tie
                        # above still checks every figure of that day, and earlier days stay exact
                        b, r, a = compare_series("execution_venue", "v", p, q, {"rel": 0.0, "abs": 0.011, "recent": 1, "recent_rel": 10.0,
                                                                                "recent_abs": float("inf")}, accept, now)
                        blocking += [x.replace(" v ", " %s %s %s " % (key, size, col)) for x in b]
                        compared += 1
    notes = "%d series compared across %d files deployed %s" % (compared, len(prev_docs), status.get("built_at", ""))
    rep.check("history", "deployed history unchanged beyond tolerance", blocking, notes)
    if restated:
        rep.check("history", "restated by a change of source (allowed by the contract's source order)", restated, warn=True)
    if okd:
        rep.check("history", "changes accepted in qa/accept.json", okd, warn=True)


# ---------------------------------------------------------------- H. copy (warnings)

def check_copy(rep, docs):
    soft, texts = [], []
    texts += [p.get("title", "") for p in (docs.get("research") or {}).get("pieces") or [] if isinstance(p, dict)]
    texts += [e.get("label", "") for e in docs.get("events") or [] if isinstance(e, dict)]
    texts += [(docs.get("execution_venue") or {}).get("fee_note", "")] + [v.get("fee_note", "") for v in (docs.get("execution_venue") or {}).get("venues", [])]
    texts += [p.get("notes") or "" for p in (docs.get("financials") or {}).get("periods", [])]
    for t in texts:
        for w in BANNED:
            if re.search(r"\b%s\b" % re.escape(w), str(t), re.I):
                soft.append("banned word %r in data copy: %s" % (w, str(t)[:80]))
    if soft:
        rep.check("copy", "word budgets and banned words in data files", soft, warn=True)
    else:
        rep.add("copy", "word budgets and banned words in data files", "pass", [])


# ---------------------------------------------------------------- G. page

def run_render(rep, out, extra_args):
    script = os.path.join(HERE, "render.mjs")
    result = os.path.join(out, "render.json")
    if os.path.exists(result):
        os.remove(result)
    try:
        proc = subprocess.run(["node", script, "--out", result] + extra_args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, timeout=1500)
    except FileNotFoundError:
        rep.add("page", "qa/render.mjs", "fail", ["node is not installed"])
        return
    except subprocess.TimeoutExpired:
        rep.add("page", "qa/render.mjs", "fail", ["timed out after 25 minutes"])
        return
    try:
        doc = strict_load(result)
    except (OSError, ValueError):
        rep.add("page", "qa/render.mjs", "fail", ["no result written (exit %d)" % proc.returncode] + proc.stdout.strip().splitlines()[-8:])
        return
    for c in doc.get("checks", []):
        rep.add("page" if c.get("blocking", True) else "copy", c["name"],
                "pass" if not c.get("problems") else ("fail" if c.get("blocking", True) else "warn"),
                c.get("problems") or ([c["note"]] if c.get("note") else []))


# ---------------------------------------------------------------- main

def main():
    global MAX_SNAPSHOT_AGE_MIN, MAX_ONCHAIN_AGE_H
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--previous", help="folder with the previously deployed data files (ci/live.py fetch)")
    ap.add_argument("--render", action="store_true", help="also run qa/render.mjs")
    ap.add_argument("--render-args", default="", help="extra arguments for render.mjs, e.g. \"--widths 390\"")
    ap.add_argument("--out", default=os.path.join(HERE, "out"))
    ap.add_argument("--now", help="UTC time for the freshness checks, e.g. 2026-11-22T10:00:00Z")
    ap.add_argument("--skip-builders", action="store_true", help="do not run the builders' --check (testing)")
    ap.add_argument("--max-snapshot-age-minutes", type=int, default=MAX_SNAPSHOT_AGE_MIN, help="freshness limit for the newest Omni snapshot (default %(default)s)")
    ap.add_argument("--max-onchain-age-hours", type=int, default=MAX_ONCHAIN_AGE_H, help="freshness limit for the on-chain files (default %(default)s)")
    ap.add_argument("--build-outcome", help="outcome of pipeline/build.py in this run (CI passes success or failure)")
    ap.add_argument("--build-log", help="its output, quoted in the report when it failed")
    args = ap.parse_args()
    now = parse_time(args.now) if args.now else dt.datetime.utcnow().replace(microsecond=0)
    MAX_SNAPSHOT_AGE_MIN, MAX_ONCHAIN_AGE_H = args.max_snapshot_age_minutes, args.max_onchain_age_hours
    os.makedirs(args.out, exist_ok=True)
    try:
        accept = strict_load(os.path.join(HERE, "accept.json")).get("history", [])
    except (OSError, ValueError):
        accept = []

    rep = Report()
    if args.build_outcome:
        tail = []
        if args.build_outcome != "success" and args.build_log:
            try:
                with open(args.build_log, encoding="utf-8", errors="replace") as fh:
                    tail = [l.rstrip()[:220] for l in fh.read().splitlines() if l.strip()][-15:]
            except OSError:
                tail = ["no build log"]
        rep.check("builders", "pipeline/build.py ran to the end", [] if args.build_outcome == "success" else ["outcome: %s" % args.build_outcome] + tail)
    if not args.skip_builders:
        check_builders(rep)
    docs = load_site(rep)
    guarded = [
        ("schema", lambda: check_schema(rep, docs, now)),
        ("identities", lambda: check_identities(rep, docs)),
        ("ties", lambda: check_ties(rep, docs)),
        ("ties", lambda: check_latest_against_raw(rep, docs)),
        ("ties", lambda: check_sealed_volume(rep, docs)),
        ("ties", lambda: check_v2(rep, docs)),
        ("ties", lambda: check_v3(rep, docs, now)),
        ("ties", lambda: check_v31(rep, docs)),
        ("freshness", lambda: check_intraday(rep, docs)),
        ("ties", lambda: check_research(rep, docs)),
        ("ties", lambda: check_defillama_volume(rep, docs)),
        ("freshness", lambda: check_freshness(rep, docs, now)),
        ("history", lambda: check_history(rep, docs, args.previous, accept, now)),
        ("copy", lambda: check_copy(rep, docs)),
    ]
    for section, fn in guarded:
        try:
            fn()
        except Exception as exc:      # a file too broken to check is a failed check, never a crashed gate
            rep.add(section, "check could not run", "fail", ["%s: %s" % (type(exc).__name__, str(exc)[:200])])
    numbers_fail = rep.count("fail")
    page_ok = None
    if args.render:
        run_render(rep, args.out, args.render_args.split())
        page_ok = rep.count("fail", page=True) == 0
    else:
        rep.add("page", "qa/render.mjs", "skip", ["run with --render"])

    fails, warns = rep.count("fail"), rep.count("warn")
    meta = docs.get("meta") or {}
    head = "VARIATIONAL MONITOR GATE   %s UTC   build %s   data as of %s\nRESULT: %s   numbers %s · page %s   (%d blocking, %d warnings)" % (
        now.strftime("%Y-%m-%d %H:%M"), meta.get("built_at"), meta.get("data_as_of"),
        "PASS" if not fails else "FAIL", "PASS" if not numbers_fail else "FAIL",
        "not run" if page_ok is None else ("PASS" if page_ok else "FAIL"), fails, warns)
    text = rep.render(head)
    print(text)
    with open(os.path.join(args.out, "report.md"), "w", encoding="utf-8") as fh:
        fh.write("```\n" + text + "```\n")
    with open(os.path.join(args.out, "report.json"), "w", encoding="utf-8") as fh:
        json.dump({"checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "numbers_ok": numbers_fail == 0, "page_ok": page_ok,
                   "blocking": [{"section": s, "name": n, "details": d} for s, n, st, d in rep.rows if st == "fail"],
                   "warnings": [{"section": s, "name": n, "details": d} for s, n, st, d in rep.rows if st == "warn"]}, fh, indent=1)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
