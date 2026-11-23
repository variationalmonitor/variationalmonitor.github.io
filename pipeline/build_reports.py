#!/usr/bin/python3
"""Reports, token and research package: rebuilds its files from the store.

    /usr/bin/python3 pipeline/build_reports.py                   refresh sources, rebuild, verify
    /usr/bin/python3 pipeline/build_reports.py --offline         rebuild from the store only (no network)
    /usr/bin/python3 pipeline/build_reports.py --check           verify the written files, change nothing
    /usr/bin/python3 pipeline/build_reports.py --clob            also read Polymarket's CLOB price history (off by default)

Writes  <store>/reports.json
        site/data/financials.json, site/data/spreads_daily.json, site/data/token.json,
        site/data/research.json (the Research Library: a plain list of pieces, see reports/research.py)
Reads   pipeline/config/research/<slug>.json (one per piece); site/data/treasury.json from the on-chain
        package (run that builder first) for the on-chain inflow per period and spreads_daily.json
Store   <store>/telegram/, <store>/polymarket/, <store>/docs/
        (<store> is .cache/store, or $VM_STORE; site/data can be moved with $VM_SITE_DATA)

Exit status is non-zero when a file could not be built or a check fails.
Python standard library only. Safe to re-run.
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reports import collect, financials, research, spreads, token, xposts   # noqa: E402
from reports.util import (SITE_DATA, STORE, TELEGRAM_CACHE, parse_time,  # noqa: E402
                          read_json_or_none, utc_now, write_json)
from sources import docs, polymarket, telegram                           # noqa: E402

DOCS_REFRESH_HOURS = 12


def say(msg: str = "") -> None:
    print(msg, flush=True)


# ------------------------------------------------------------------ refresh

def refresh_sources(now, clob: bool = False) -> None:
    say("Sources")
    try:
        r = telegram.refresh(STORE, TELEGRAM_CACHE)
        say("  telegram   %d posts (%d new, %d revised, %d pages read) checked %s"
            % (r["posts"], r["new"], r["revised"], r["pages"], r["checked_at"]))
    except Exception as exc:                       # network trouble: keep the store
        say("  telegram   refresh failed, using the store: %s" % exc)
    r = xposts.refresh(STORE)
    if r["fetched"] or r["failed"]:
        say("  x          fetched %d, failed %d" % (len(r["fetched"]), len(r["failed"])))
    for slug in (polymarket.EVENT_FDV, polymarket.EVENT_LAUNCH):
        ok = polymarket.fetch_event(STORE, slug)
        say("  polymarket %s %s" % (slug, "fetched" if ok else "fetch failed, using the store"))
    events = [polymarket.load_event(STORE, s) for s in (polymarket.EVENT_FDV, polymarket.EVENT_LAUNCH)]
    polymarket.append_snapshot(STORE, events)
    if clob:                                       # explicit opt-in only
        open_markets = [m for ev in events if ev for m in ev["markets"] if not m["closed"]]
        h = polymarket.fetch_history(STORE, open_markets)
        say("  polymarket CLOB price history: %d markets fetched, last HTTP status %s" % (h["fetched"], h["status"]))
    _, meta = docs.load(STORE)
    if meta is None or now - parse_time(meta["fetched_at"]) > timedelta(hours=DOCS_REFRESH_HOURS):
        say("  docs       %s" % ("fetched" if docs.fetch(STORE) else "fetch failed, using the store"))


# -------------------------------------------------------------------- build

def build(args) -> int:
    now = utc_now()
    failures = []
    if not args.offline:
        refresh_sources(now, clob=args.clob)

    # 1. reports.json (other packages read it)
    records = collect.collect(STORE, TELEGRAM_CACHE)
    collect.write(STORE, records)
    full = [r for r in records if r["full"]]
    say("Reports    %d report posts, %d full financial updates, newest %s -> %s"
        % (len(records), len(full), records[-1]["published_at"], STORE / "reports.json"))
    for r in records:
        for w in r["warnings"]:
            say("  %s %s: %s" % (r["published_at"][:10], r["id"], w))
        if r["filled_from_x"]:
            say("  %s %s: filled from the X post: %s" % (r["published_at"][:10], r["id"], ", ".join(r["filled_from_x"])))

    # 2. financials.json
    try:
        treasury_days, treasury_note = spreads.load_treasury(SITE_DATA)
        fin = financials.build(records, treasury_days)
        errors = financials.check(fin)
        if errors:
            raise ValueError("; ".join(errors))
        write_json(SITE_DATA / "financials.json", fin)
        say("Financials %d periods, as of %s" % (len(fin["periods"]), fin["as_of"]))
        say(financials.table(fin))
        if treasury_days is None:
            say("  on-chain treasury inflow per period: not available (%s)" % treasury_note)
    except Exception as exc:
        fin = None
        failures.append("financials.json: %s" % exc)

    # 2b. spreads_daily.json (needs the on-chain package's treasury.json)
    try:
        if fin is None:
            raise ValueError("financials.json was not built")
        daily, rows = spreads.build(fin, SITE_DATA)
        say("Spreads    implied daily spreads paid, %s to %s, treasury share %s; against reported spreads paid per period:"
            % (daily["dates"][0], daily["dates"][-1], ", ".join(sorted({"%.4g" % s for s in daily["share"]}))))
        say(spreads.table(rows))
        outside = [r for r in rows if abs(r[4]) > spreads.TOLERANCE]
        if outside or not rows:
            raise ValueError("%d period(s) outside %.0f%%" % (len(outside), spreads.TOLERANCE * 100) if rows else "no report period covered")
        write_json(SITE_DATA / "spreads_daily.json", daily)
    except Exception as exc:
        failures.append("spreads_daily.json: %s" % exc)

    # 3. token.json
    try:
        posts = telegram.load(STORE, TELEGRAM_CACHE)["posts"]
        tok = token.build(STORE, posts, use_clob=args.clob)
        errors = token.check(tok)
        if errors:
            raise ValueError("; ".join(errors))
        write_json(SITE_DATA / "token.json", tok)
        say("Token      as of %s" % tok["as_of"])
        say("  FDV above strike: " + "  ".join("%s %.4f" % (s["label"], s["p"]) for s in tok["fdv"]["strikes"]))
        say("  implied median: %s; daily history %d days (%s to %s)" % (
            "{:,.2f}".format(tok["fdv"]["median"]) if tok["fdv"]["median"] is not None else "null",
            len(tok["fdv"]["history"]["dates"]),
            tok["fdv"]["history"]["dates"][0] if tok["fdv"]["history"]["dates"] else "-",
            tok["fdv"]["history"]["dates"][-1] if tok["fdv"]["history"]["dates"] else "-"))
        for m in tok["launch"]:
            say("  launch by %s: %.4f; history %d days" % (m["label"], m["p"], len(m["dates"])))
        say("  points: %d weeks (week %d to %d); allocation %s" % (
            len(tok["points"]), tok["points"][0]["week"], tok["points"][-1]["week"],
            ", ".join("%s %s" % (a["label"], a["pct"]) for a in tok["allocation"])))
    except Exception as exc:
        failures.append("token.json: %s" % exc)

    # 4. research library: the list of pieces from their configs
    try:
        index = research.build(now)
        removed = research.write(SITE_DATA, index)
        stale = SITE_DATA / "thesis.json"          # the single Thesis page is gone
        if stale.exists():
            stale.unlink()
        errors = research.check(SITE_DATA, now)
        if errors:
            raise ValueError("; ".join(errors))
        say("research.json  %d piece(s): %s" % (len(index["pieces"]), "; ".join(
            "%s %s %s" % (p["slug"], p["status"], p["date"]) for p in index["pieces"]) or "none"))
        if removed:
            say("  removed the per-piece files: %s" % ", ".join(removed))
    except Exception as exc:
        failures.append("research: %s" % exc)

    for f in failures:
        say("FAILED     " + f)
    return 1 if failures else 0


# -------------------------------------------------------------------- check

def check() -> int:
    """Re-verify the written outputs against the store. No network, no writes."""
    errors = []

    stored = read_json_or_none(STORE / "reports.json")
    records = collect.collect(STORE, TELEGRAM_CACHE)
    if stored is None:
        errors.append("reports.json missing")
    elif stored != records:
        errors.append("reports.json differs from a fresh parse of the store")

    fin = read_json_or_none(SITE_DATA / "financials.json")
    if fin is None:
        errors.append("financials.json missing")
    elif fin.get("fixture") is True:
        errors.append("financials.json is a fixture")
    else:
        errors += financials.check(fin)
        if fin != financials.build(records, spreads.load_treasury(SITE_DATA)[0]):
            errors.append("financials.json differs from a rebuild from reports.json and treasury.json")
        errors += spreads.check(read_json_or_none(SITE_DATA / "spreads_daily.json"), fin, SITE_DATA)

    tok = read_json_or_none(SITE_DATA / "token.json")
    if tok is None:
        errors.append("token.json missing")
    elif tok.get("fixture") is True:
        errors.append("token.json is a fixture")
    else:
        errors += token.check(tok)
        posts = telegram.load(STORE, TELEGRAM_CACHE)["posts"]
        want_points = sorted(
            ({"date": p["time"][:10], "week": int(m.group("week")),
              "accounts": int(m.group("accounts").replace(",", "")),
              "points": int(m.group("points").replace(",", ""))}
             for p in posts for m in [token.POINTS.search(p["text"])] if m),
            key=lambda r: r["week"])
        if tok["points"] != want_points:
            errors.append("token: points differ from the Telegram store")
        text, _ = docs.load(STORE)
        if text is None:
            errors.append("token: docs not in the store")
        elif tok["allocation"] != docs.allocation(text):
            errors.append("token: allocation differs from the docs in the store")
        for slug, markets in ((polymarket.EVENT_FDV, tok["fdv"]["strikes"]), (polymarket.EVENT_LAUNCH, tok["launch"])):
            ev = polymarket.load_event(STORE, slug)
            if ev is None:
                errors.append("token: %s not in the store" % slug)
                continue
            live = sorted(round(m["p"], 4) for m in ev["markets"] if not m["closed"] and m["p"] is not None)
            if live != sorted(m["p"] for m in markets):
                errors.append("token: probabilities differ from the stored %s response" % slug)

    errors += research.check(SITE_DATA, utc_now())

    if errors:
        for e in errors:
            say("CHECK FAILED  " + e)
        return 1
    say("check passed: reports.json, financials.json (%d periods, identities exact), spreads_daily.json, token.json, "
        "research.json (%d piece(s))" % (len(fin["periods"]), len(research.load_pieces(utc_now()))))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true", help="rebuild from the store, no network")
    ap.add_argument("--check", action="store_true", help="verify the written files and exit")
    ap.add_argument("--clob", action="store_true", help="also read Polymarket's CLOB price history (off by default)")
    args = ap.parse_args()
    if args.check:
        if args.clob or args.offline:
            ap.error("--check runs alone")
        return check()
    return build(args)


if __name__ == "__main__":
    sys.exit(main())
