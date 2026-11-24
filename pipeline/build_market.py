#!/usr/bin/python3
"""Market-data builder for Variational Monitor v2.

    /usr/bin/python3 pipeline/build_market.py              ingest snapshots, seal days, rebuild files, check
    /usr/bin/python3 pipeline/build_market.py --check      re-verify the written files only (exit 1 on failure)
    /usr/bin/python3 pipeline/build_market.py --offline    rebuild without calling DefiLlama
    /usr/bin/python3 pipeline/build_market.py --refresh    ignore the external caches' age
    /usr/bin/python3 pipeline/build_market.py --classify   add listings missing from config/classification.json

Writes (site/data/): volume_daily, oi_daily, peers_oi, markets_latest, markets_listed,
market/index + market/<TICKER>, quote_depth, swap_pairs, olp_market, tradfi, concentration, spread_majors,
markets_scope,
events, meta.
Store (.cache/store/): platform.jsonl, listings_seen.json, days/YYYY-MM-DD.json, defillama/ (open-interest cache
and the owner's export of DefiLlama's daily Perp Volume with its provenance note; see sources/defillama.py).
Daily volume before our own counter days is DefiLlama's figure, else the reported two-week average; CoinGecko
is not used for volume (sources/coingecko.py stays for token data after a launch).
Never calls the Omni API: it reads the capture loop's snapshot files. Python standard library only.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from market import check, classify, financing, history, intraday, listings, outputs, scope, store   # noqa: E402
from market.common import CONFIG, OUT, STORE, GROUPS, CLASSES, median, read_json, write_json   # noqa: E402
from sources import defillama   # noqa: E402


def log(msg=""):
    print(msg, flush=True)


def extend_classification(seen):
    """Add every seen listing that is missing from the file, by rule. Existing entries are kept."""
    cfg = read_json(classify.PATH) if os.path.exists(classify.PATH) else {}
    listings = cfg.get("listings", {})
    added = []
    for tk in sorted(seen):
        if tk in listings:
            continue
        e = listings[tk] = classify.entry_by_rule(tk, seen[tk])
        added.append((tk, e["group"], e["cls"], e["basis"], e["name"]))
    cfg["listings"] = {k: listings[k] for k in sorted(listings)}
    cfg.setdefault("swap_financing", {})
    write_classification(cfg)
    return added


def write_classification(cfg):
    head = {k: v for k, v in cfg.items() if k not in ("listings", "swap_financing")}
    lines = ["{"]
    for k, v in head.items():
        lines.append("  %s: %s," % (json.dumps(k), json.dumps(v, ensure_ascii=False)))
    lines.append('  "swap_financing": {')
    sf = cfg["swap_financing"]
    lines += ["    %s: %s%s" % (json.dumps(k), json.dumps(sf[k]), "," if i + 1 < len(sf) else "")
              for i, k in enumerate(sf)]
    lines.append("  },")
    lines.append('  "listings": {')
    ls = cfg["listings"]
    lines += ["    %s: %s%s" % (json.dumps(k), json.dumps(ls[k], ensure_ascii=False), "," if i + 1 < len(ls) else "")
              for i, k in enumerate(ls)]
    lines += ["  }", "}", ""]
    with open(classify.PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def run_check():
    k = check.run()
    if k.fail:
        log("CHECK FAILED: %d of %d" % (len(k.fail), k.n))
        for f in k.fail[:60]:
            log("  - " + f)
        if len(k.fail) > 60:
            log("  ... and %d more" % (len(k.fail) - 60))
        return 1
    log("check ok: %d assertions on %s" % (k.n, OUT))
    return 0


def fmt_b(x):
    return "%.2f" % (x / 1e9)


def main(argv):
    if "--check" in argv:
        return run_check()
    offline, refresh = "--offline" in argv, "--refresh" in argv
    financing.self_test()

    log("store: %s" % STORE)
    refs = store.list_refs()
    if not refs:
        log("no snapshots found")
        return 1
    st = store.ingest(refs)
    log("  snapshots %d (added %d, repeated responses %d), listings seen %d"
        % (st["snapshots"], st["added"], st["duplicates"], st["listings_seen"]))
    seen = store.load_seen()

    if "--classify" in argv:
        added = extend_classification(seen)
        log("classification: added %d listings" % len(added))
        for row in added:
            log("  %-18s %-13s %-10s %-14s %s" % row)
        # date every listing by its earliest evidence and store it beside the classification: the same function
        # dates a listing missing from the file at build time, so storing it does not move a date already shown
        classifier = classify.Classifier(seen)
        computed = listings.listing_dates(classifier, history.load_days(classifier), seen)[2]
        cfg = read_json(classify.PATH)
        changed = 0
        for tk, e in cfg["listings"].items():
            want = computed.get(tk)
            if e.get("listed") != want:
                changed += 1
                if want:
                    e["listed"] = want
                else:
                    e.pop("listed", None)
        write_classification(cfg)
        kinds = [v["evidence"] for v in computed.values()]
        log("listing evidence: %d listings (announcement %d, report %d, snapshot %d); %d entries updated"
            % (len(kinds), kinds.count("announcement"), kinds.count("report"), kinds.count("snapshot"), changed))
        return 0

    classifier = classify.Classifier(seen)
    pairs = read_json(os.path.join(CONFIG, "pairs.json"))["pairs"]
    store.seal(refs, classifier, classifier.fin_cfg, pairs, log=log)
    days = history.load_days(classifier)
    log("  sealed days: %s" % ", ".join("%s%s" % (d, "~" if days[d]["approx"] else "") for d in sorted(days)))

    reports, reports_file = history.load_reports()
    log("reports: %d from %s (%s to %s)" % (len(reports), reports_file, reports[0]["t"].date(), reports[-1]["t"].date()))
    dl = defillama.load(STORE, refresh=refresh, offline=offline, log=log)
    log("defillama open interest: %d days %s to %s (fetched %s)" % (len(dl["variational"]), min(dl["variational"]),
                                                                   max(dl["variational"]), dl["fetched_at"]))
    dl_vol, dl_vol_note = defillama.load_volume_export(STORE)
    if dl_vol:
        log("defillama volume: %d days %s to %s (export of %s by %s, row of the export date dropped)"
            % (len(dl_vol), min(dl_vol), max(dl_vol), dl_vol_note["exported_on"], dl_vol_note["exported_by"]))
    else:
        log("DEFILLAMA VOLUME EXPORT MISSING from %s/defillama/: volume before our counter days falls back to report averages" % STORE)

    volume, periods, notes = history.build_volume(reports, dl_vol, days, store.load_platform(), log=log)
    log("\nDefiLlama daily volume against the change in the reported counter (used as published, never rescaled)")
    log("  period start       period end         days  reported $B  defillama $B  residual  whole-day")
    for p in periods:
        if p["defillama"] is None:
            continue
        log("  %s  %s  %5.2f  %11s  %12s  %+7.2f%%  %+8.2f%%%s" % (
            p["start"].strftime("%Y-%m-%d %H:%M"), p["end"].strftime("%Y-%m-%d %H:%M"), p["days"],
            fmt_b(p["reported"]), fmt_b(p["defillama"]), p["residual"] * 100,
            (p["residual_whole_days"] or 0) * 100,
            "  (closed by Omni counter)" if p["closed_by"] == "omni" else ""))
    rep_res = sorted((p["residual"] for p in periods if p["residual"] is not None and p["closed_by"] == "report"), key=abs)
    if rep_res:
        log("  %d report periods covered: median residual %+.2f%%, median absolute %.2f%%, largest %+.2f%%"
            % (len(rep_res), median(rep_res) * 100, median([abs(x) for x in rep_res]) * 100, rep_res[-1] * 100))
    counts = [volume["src"].count(i) for i in (0, 1, 2)]
    log("volume_daily: %d days %s to %s  (omni %d, defillama %d, report_avg %d)"
        % (len(volume["dates"]), volume["dates"][0], volume["dates"][-1], counts[0], counts[1], counts[2]))
    for s in notes["split_days"]:
        log("  split %s from snapshot %s: 24h volume %.1fM, day total %.1fM (%s), factor %.4f%s"
            % (s["date"], s["boundary"], s["snapshot_24h"] / 1e6, s["total"] / 1e6, s["src"], s["factor"],
               "  approx" if s["approx"] else ""))
    for ds, kf in notes["split_dropped"]:
        log("  SPLIT DROPPED %s: listing volumes are %.4f of the exact day" % (ds, 1 / kf))
    if notes["provisional"]:
        log("  PROVISIONAL (snapshot trailing 24h until a daily source reaches the day): %s" % ", ".join(notes["provisional"]))
    if notes["gaps"]:
        log("  SERIES STOPS before %s: no source for these days: %s" % (notes["gaps"][0], ", ".join(notes["gaps"])))

    cn = notes["cumulative"]
    log("cumulative: %d counter anchors (%d Omni snapshots, %d reports); %d fix the day-end values"
        % (cn["anchors_total"], cn["anchors_omni"], cn["anchors_total"] - cn["anchors_omni"], len(volume["anchors"])))
    log("  last date %s: cumulative $%s; sum of daily totals from the first report would give $%s"
        % (volume["dates"][-1], format(volume["cumulative"][-1] / 100.0, ",.2f"),
           format((sum(volume["total"]) + history.cents(reports[0]["cum"])) / 100.0, ",.2f")))
    for t, c_, _ in cn["superseded"]:
        log("  report %s ($%.2fB) not used as an anchor: an Omni snapshot lies within 6 hours" % (t, c_ / 1e11))
    for t, c_, src in cn["dropped"]:
        log("  ANCHOR DROPPED %s %s ($%.4fB): the counter would fall" % (src, t, c_ / 1e11))
    if cn["unanchored"]:
        log("  no later anchor for: %s (daily totals added unscaled)" % ", ".join(cn["unanchored"]))

    oi, oi_checks = history.build_oi(reports, dl["variational"], days)
    oc = [oi["src"].count(i) for i in (0, 1, 2)]
    log("oi_daily: %d dates %s to %s  (omni %d, defillama %d, report_half %d, null %d)"
        % (len(oi["dates"]), oi["dates"][0], oi["dates"][-1], oc[0], oc[1], oc[2], oi["src"].count(None)))
    for ck in oi_checks:
        log("  %s omni %.1fM (snapshot %+.0f min from day end) | half of API dual-sided %.1fM | defillama %s"
            % (ck["date"], ck["omni"] / 1e6, ck["offset_min"], ck["api_dual_half"] / 1e6,
               "%.1fM (%+.1f%%)" % (ck["defillama"] / 1e6, (ck["defillama"] / ck["omni"] - 1) * 100)
               if ck["defillama"] else "n/a"))

    listed = history.build_listed(reports, days, outputs.swaps_launch_date())
    window, peers, runners_up = defillama.peer_venues(dl)
    log("peers_oi: DefiLlama category %s only (dropped: %s); 30-day average %s to %s"
        % (defillama.KEEP_CATEGORY, ", ".join(dl["dropped_categories"]), window[0], window[-1]))
    for v in peers:
        log("  kept   %-14s %8.1fM  = %s" % (v["name"], v["avg"] / 1e6, " + ".join(v["members"])))
    for v in runners_up:
        log("  next   %-14s %8.1fM" % (v["name"], v["avg"] / 1e6))

    rows_platform = store.load_platform()
    live = [r for r in refs if not rows_platform[r.key]["dup"]]
    listed_tf, li, dated = listings.build_listed(days, classifier, seen)
    latest, platform, fin_unresolved = outputs.latest_json(live[-1], classifier, min(days) if days else None, dated)
    intra = intraday.collect(refs, rows_platform, classifier)
    files, index = outputs.market_files(days, [r["ticker"] for r in latest["rows"]], intra)
    depth = outputs.quote_depth_json(days)
    pairs_out = outputs.swap_pairs_json(days, pairs)
    olp = outputs.olp_json(days)
    tradfi = outputs.tradfi_json(volume, oi, days, listed_tf)
    concentration = outputs.concentration_json(days)
    events = outputs.events_list()
    dl_as_of = defillama.as_of(dl)
    peers_out = outputs.peers_json(dl, peers, oi, dl_as_of)
    split_start = next((d for d, g in zip(volume["dates"], volume["group"]["crypto_majors"]) if g is not None), None)
    meta = outputs.meta_json(latest["as_of"], dl_as_of, split_start)

    out = {
        "volume_daily.json": outputs.volume_json(volume),
        "oi_daily.json": outputs.oi_json(oi),
        "peers_oi.json": peers_out,
        "markets_latest.json": latest,
        "markets_listed.json": outputs.listed_json(listed),
        "quote_depth.json": depth,
        "swap_pairs.json": pairs_out,
        "olp_market.json": olp,
        "tradfi.json": tradfi,
        "concentration.json": concentration,
        "events.json": events,
        "meta.json": meta,
        "market/index.json": index,
    }
    # the majors' quoted spread from our own snapshots
    out["spread_majors.json"] = outputs.spread_majors_json(days)
    outputs.add_trailing_shares(out["tradfi.json"], out["volume_daily.json"])
    # the Markets page's series per filter scope, from the files above and our snapshots
    out["markets_scope.json"] = scope.build(out["volume_daily.json"], out["oi_daily.json"], out["tradfi.json"],
                                            out["spread_majors.json"], days, classifier, seen, outputs.swaps_launch_date(), intra)
    for tk, f in files.items():
        out["market/%s.json" % tk] = f
    mdir = os.path.join(OUT, "market")
    os.makedirs(mdir, exist_ok=True)
    stale = [f for f in os.listdir(mdir) if f.endswith(".json") and "market/" + f not in out]
    for f in stale:
        os.remove(os.path.join(mdir, f))
    size = sum(write_json(os.path.join(OUT, name), obj) for name, obj in out.items())
    old = os.path.join(OUT, "execution_pairs.json")      # replaced by swap_pairs.json
    if os.path.exists(old):
        os.remove(old)

    log("\nfiles written to %s (%d files, %.1f MB; %d stale market files removed)" % (OUT, len(out), size / 1e6, len(stale)))
    span = lambda d: "%d rows, %s to %s" % (len(d["dates"]), d["dates"][0], d["dates"][-1])
    log("  volume_daily.json     %s, as of %s" % (span(out["volume_daily.json"]), out["volume_daily.json"]["as_of"]))
    log("  oi_daily.json         %s, as of %s" % (span(out["oi_daily.json"]), out["oi_daily.json"]["as_of"]))
    log("  peers_oi.json         %s, as of %s" % (span(peers_out), peers_out["as_of"]))
    log("  markets_latest.json   %d rows, as of %s" % (len(latest["rows"]), latest["as_of"]))
    log("  markets_listed.json   %s (report %d, omni %d)" % (span(out["markets_listed.json"]), listed["src"].count(0), listed["src"].count(1)))
    log("  market/               %d listings, dates %s" % (len(files), ", ".join(sorted(days))))
    msize = sum(os.path.getsize(os.path.join(mdir, f)) for f in os.listdir(mdir) if f.endswith(".json"))
    log("  intraday              %d snapshots %s to %s; site/data/market/ is %.1f MB (largest file %.0f KB)"
        % (len(intra["ts"]), intra["ts"][0] if intra["ts"] else "-", intra["ts"][-1] if intra["ts"] else "-", msize / 1e6,
           max(os.path.getsize(os.path.join(mdir, f)) for f in os.listdir(mdir)) / 1e3))
    log("  quote_depth.json      %s" % span(depth))
    log("  swap_pairs.json       %s; pairs %s" % (span(pairs_out), ", ".join(p["key"] for p in pairs)))
    log("  olp_market.json       %s" % span(olp))
    log("  tradfi.json           %s; listed %d rows, %s to %s (announcement %d, omni %d)"
        % (span(tradfi), len(listed_tf["dates"]), listed_tf["dates"][0], listed_tf["dates"][-1],
           listed_tf["src"].count("announcement"), listed_tf["src"].count("omni")))
    log("  concentration.json    %s" % span(concentration))
    for name_, sc in out["markets_scope.json"]["scopes"].items():
        def cover(series):
            have = [d for d, x in zip(sc["dates"], series) if x is not None]
            return "%s to %s (%d)" % (have[0], have[-1], len(have)) if have else "none"
        fseries = sc["funding"].get("series") or sc["funding"]["long"]
        log("  markets_scope %-11s volume %s | OI %s | long/short %s | funding %s | slippage %s | tickers %s"
            % (name_, cover(sc["volume"]["total"]), cover(sc["oi"]["total"]), cover(sc["long"]),
               cover([next((fseries[t][i] for t in fseries if fseries[t][i] is not None), None) for i in range(len(sc["dates"]))]),
               cover([next((sc["slippage"]["series"][t][i] for t in sc["slippage"]["series"] if sc["slippage"]["series"][t][i] is not None), None)
                      for i in range(len(sc["dates"]))]),
               ", ".join(t.rsplit("-PERP", 1)[0] for t in sc["funding"]["tickers"])))
    log("  spread_majors.json    %s (sources %s)" % (span(out["spread_majors.json"]), ", ".join(out["spread_majors.json"]["sources"])))
    log("  events.json           %s" % "; ".join("%s %s" % (e["date"], e["label"]) for e in events))
    log("  meta.json             data_as_of %s, split_start %s" % (meta["data_as_of"], meta["split_start"]))

    be = li["by_evidence"]
    log("\nTradFi listing evidence (%s): %d listings dated by announcement, %d by report, %d by first snapshot"
        % (li["source"], be["announcement"], be["report"], be["snapshot"]))
    log("  announcements: %d posts from %s, %d tickers mapped" % (li["posts"], li["first_date"], li["tickers_mapped"]))
    if li["unmapped_tokens"]:
        log("  tokens not mapped (the post's own link gave the ticker): %s"
            % ", ".join("%s %s" % x for x in li["unmapped_tokens"]))
    if li["posts_without_ticker"]:
        log("  POSTS WITH NO MAPPED TICKER: %s" % ", ".join("%s #%s" % x for x in li["posts_without_ticker"]))
    for r in li["reports"]:
        log("  report %s (#%s): mapped %s; not mapped: %s"
            % (r["date"], r["id"], ", ".join("%s=%s" % (k, v.rsplit("-", 1)[0]) for k, v in r["mapped"].items()) or "none",
               ", ".join(r["unmapped"]) or "none"))
    if li["first_snapshot_count"] is not None:
        bb = li["before_by_evidence"]
        log("  before the first snapshot (%s): %d dated (announcement %d, report %d); snapshot lists %d; step of %d: %s"
            % (li["first_snapshot_date"], li["before_first_snapshot"], bb["announcement"], bb["report"],
               li["first_snapshot_count"], len(li["step"]), ", ".join(t.rsplit("-", 1)[0] for t in li["step"])))
        if li["dated_but_not_in_first_snapshot"]:
            log("  dated earlier but gone by the first snapshot: %s" % ", ".join(li["dated_but_not_in_first_snapshot"]))
    if li["not_in_config"]:
        log("  LISTING EVIDENCE NOT IN classification.json (run --classify): %s" % ", ".join(li["not_in_config"]))
    if li["config_stale"]:
        log("  listing evidence in classification.json differs from the channel as read now: %s" % ", ".join(li["config_stale"]))

    by_group, by_cls = {}, {}
    for r in latest["rows"]:
        by_group[r["group"]] = by_group.get(r["group"], 0) + 1
        by_cls[r["cls"]] = by_cls.get(r["cls"], 0) + 1
    log("\nclassification of the %d listings in the latest snapshot" % len(latest["rows"]))
    log("  group: " + ", ".join("%s %d" % (g, by_group.get(g, 0)) for g in GROUPS + ["unclassified"]))
    log("  class: " + ", ".join("%s %d" % (c, by_cls.get(c, 0)) for c in CLASSES))
    if classifier.not_in_file:
        log("  NOT IN classification.json (classified and dated by rule; review, then --classify stores both): %s"
            % ", ".join("%s=%s/%s listed %s" % (k, v[0], v[1], "%(date)s (%(evidence)s)" % dated[k] if k in dated else "unknown")
                        for k, v in sorted(classifier.not_in_file.items())))
    if classifier.unclassified:
        log("  UNCLASSIFIED: %s" % ", ".join(sorted(classifier.unclassified)))
    if classifier.class_unresolved:
        log("  TradFi class not resolved from name (shown as other): %s" % ", ".join(sorted(classifier.class_unresolved)))
    if fin_unresolved:
        log("  swap financing not established in the latest snapshot: %s"
            % ", ".join("%s (%s)" % x for x in fin_unresolved))
    api_tie = abs(sum(check.c(r["vol24h"]) for r in latest["rows"]) / 100.0 - float(platform["volume_24h"]))
    log("  latest snapshot: listings' 24h volume minus API total = $%.2f" % api_tie)
    log("")
    return run_check()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
