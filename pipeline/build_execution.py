#!/usr/bin/env python3
"""Execution package: <store>/exec/*.jsonl -> site/data/execution_venue.json. Python standard library only.

  /usr/bin/python3 pipeline/build_execution.py            rebuild the file from the store (safe to re-run)
  /usr/bin/python3 pipeline/build_execution.py --check    validate the file against the contract and the store,
                                                          and print the match rate and fee state

Input is the sample store written by pipeline/capture_hl.py (one line per snapshot, market and size). A
snapshot stored twice counts once (the matched line wins, else the first).

Output, per the data contract
  {as_of, fee_note, venues:[{key, label, fee_note}], markets:[{key, label, omni, hl, lighter}], dates,
   by:{<key>:{"100k":{omni, hl_book, hl_fee, n, quoted, hl_round, hl_n,
                       lighter_book, lighter_fee, lighter_n, lighter_omni}, "1m":{...}}}, range:{...}, match:{...}}
  omni, hl_book   daily medians over samples matched with Hyperliquid, bps of mid, round trip (full bid-ask).
                  Each median is taken on its own, so hl_book - omni is a difference of medians.
  hl_fee          two base-tier taker fees in bps (the day's median over matched samples). Null when the market's
                  fee is null in venues.json, or a sample's fee is not one of the verified rates there.
  n               samples matched with Hyperliquid that day. omni, hl_book and hl_fee are null where n is 0.
  These five keep the meaning they had when Hyperliquid was the only venue. hl_n repeats n.
Other venues (key "lighter"; a market the venue does not list has nulls and 0 throughout)
  <venue>_book    daily median of that venue's order-book cost over the samples matched with that venue.
  <venue>_fee     two taker fees at the venue's verified entry-level rate; null if unverified.
  <venue>_n       samples matched with that venue. <venue>_book and <venue>_fee are null where it is 0.
  <venue>_omni    Omni's median over those same samples. It equals `omni` unless a venue missed a snapshot
                  the other caught; use it when comparing Omni with that venue alone.
Additions to the contract (the site may ignore them)
  quoted          samples that day in which Omni quoted the size; n / quoted is Hyperliquid's match rate.
  hl_round        median over matched samples of the most the book aggregation can overstate hl_book (bps);
                  0 when every sample walked the unaggregated book. Lighter's books are exact price levels,
                  so they have no such term.
  range           the same figures (omni, hl_book, hl_fee, n and each <venue>_*) pooled over every matched
                  sample in the trailing 7, 30, 90, 180 and 365 days ending on the last date, and over all
                  dates: the median over a range cannot be derived from daily medians.
  match           {quoted, matched, rate} for Hyperliquid over the whole store, and venues:{<key>:{quoted,
                  matched, rate, unavailable}} where quoted counts Omni-quoted samples in which that venue was
                  attempted and unavailable counts samples the venue refused.
`dates` runs without gaps from the first day with a sample to the last. Venues are the ones listed in venues.json;
sample fields of a venue no longer listed there (Binance, dropped 23 Nov 2026) are ignored.
"""
import argparse
import glob
import json
import os
import re
import sys
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "pipeline", "config", "venues.json")
STORE = os.environ.get("VM_STORE") or os.path.join(ROOT, ".cache", "store")
OUT = os.path.join(os.environ.get("VM_SITE_DATA") or os.path.join(ROOT, "site", "data"), "execution_venue.json")
RANGES = [("7D", 7), ("30D", 30), ("90D", 90), ("180D", 180), ("1Y", 365), ("All", None)]
TIME = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
OTHERS = ("lighter",)  # venues added beside Hyperliquid; sample fields are prefixed with the key
HL_COLUMNS = ("omni", "hl_book", "hl_fee", "n", "quoted", "hl_round", "hl_n")
VENUE_COLUMNS = ("book", "fee", "n", "omni")
RANGE_COLUMNS = ("omni", "hl_book", "hl_fee", "n", "hl_n")


def load(path):
    with open(path) as handle:
        return json.load(handle)


def median(values):
    ordered = sorted(values)
    half = len(ordered) // 2
    return ordered[half] if len(ordered) % 2 else (ordered[half - 1] + ordered[half]) / 2


def bps(value):
    return None if value is None else round(value + 0.0, 2)


def read_store(store):
    """{(day, key, size): [sample]} with one sample per snapshot, plus read problems."""
    chosen, problems = {}, []
    for path in sorted(glob.glob(os.path.join(store, "exec", "*.jsonl"))):
        day = os.path.basename(path)[:-len(".jsonl")]
        with open(path) as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    ident = (day, row["key"], row["size"], row["snap"])
                except Exception:
                    problems.append("%s line %d unreadable" % (os.path.basename(path), number))
                    continue
                held = chosen.get(ident)
                if held is None or (row.get("matched") and not held.get("matched")):
                    chosen[ident] = row
    grouped = {}
    for (day, key, size, _), row in chosen.items():
        grouped.setdefault((day, key, size), []).append(row)
    return grouped, problems


def verified_rates(config, market):
    rates = config["hyperliquid"]["verified_taker_bps"]
    return rates[market["hl_dex"]] if market.get("hl_dex") else rates["core"]


def fee_of(config, market, samples):
    """(two taker fees in bps or None, reason when None and samples exist)."""
    if market.get("taker_fee_bps") is None:
        return None, "fee set to null in venues.json"
    fees = [row["hl_fee_side_bps"] for row in samples if row.get("hl_fee_side_bps") is not None]
    if not fees:
        return None, "no fee recorded on the samples"
    allowed = verified_rates(config, market)
    odd = sorted({fee for fee in fees if not any(abs(fee - rate) < 1e-6 for rate in allowed)})
    if odd:
        return None, "unverified rate %s bps per side" % ", ".join("%g" % fee for fee in odd)
    return 2 * median(fees), None


def other_rates(config, venue, spec):
    rates = config[venue]["verified_taker_bps"]
    return rates[spec["fee_class"]] if isinstance(rates, dict) else rates


def other_fee(config, venue, spec, samples):
    """(two taker fees in bps or None, reason when None) for a venue other than Hyperliquid."""
    if spec.get("taker_fee_bps") is None:
        return None, "fee set to null in venues.json"
    fees = [row.get(venue + "_fee_side_bps") for row in samples]
    if any(fee is None for fee in fees):
        return None, "a sample carries no verified fee"
    allowed = other_rates(config, venue, spec)
    odd = sorted({fee for fee in fees if not any(abs(fee - rate) < 1e-6 for rate in allowed)})
    if odd:
        return None, "unverified rate %s bps per side" % ", ".join("%g" % fee for fee in odd)
    return 2 * median(fees), None


def cell(config, market, samples):
    """One market, size and set of samples -> every published figure, plus the reasons any fee was withheld."""
    reasons = []
    matched = [row for row in samples
               if row.get("matched") and row.get("omni_bps") is not None and row.get("hl_book_bps") is not None]
    quoted = sum(1 for row in samples if row.get("omni_bps") is not None)
    values = {"omni": None, "hl_book": None, "hl_fee": None, "n": 0, "quoted": quoted, "hl_round": None, "hl_n": 0}
    if matched:
        fee, reason = fee_of(config, market, matched)
        if reason:
            reasons.append("hl: " + reason)
        values.update({"omni": bps(median([row["omni_bps"] for row in matched])),
                       "hl_book": bps(median([row["hl_book_bps"] for row in matched])),
                       "hl_fee": bps(fee), "n": len(matched), "hl_n": len(matched),
                       "hl_round": bps(median([row.get("hl_round_bps") or 0.0 for row in matched]))})
    for venue in OTHERS:
        spec = market.get(venue)
        paired = [row for row in samples if spec and row.get(venue + "_matched")
                  and row.get("omni_bps") is not None and row.get(venue + "_book_bps") is not None]
        values.update({venue + "_book": None, venue + "_fee": None, venue + "_n": 0, venue + "_omni": None})
        if paired:
            fee, reason = other_fee(config, venue, spec, paired)
            if reason:
                reasons.append("%s: %s" % (venue, reason))
            values.update({venue + "_book": bps(median([row[venue + "_book_bps"] for row in paired])),
                           venue + "_fee": bps(fee), venue + "_n": len(paired),
                           venue + "_omni": bps(median([row["omni_bps"] for row in paired]))})
    return values, reasons


def grouped_fees(pairs):
    """[(fee per side, label)] -> "5 bps (BTC, ETH), 4 bps (Gold)", larger groups first."""
    groups = {}
    for fee, label in pairs:
        groups.setdefault(fee, []).append(label)
    return ", ".join("%g bps (%s)" % (fee, ", ".join(labels)) for fee, labels in sorted(groups.items(), key=lambda kv: -len(kv[1])))


def fee_note(config, latest_fee):
    """One sentence built from the Hyperliquid fees actually published (latest per market, per side)."""
    known = [(latest_fee[m["key"]], m["label"]) for m in config["markets"] if latest_fee.get(m["key"]) is not None]
    if not known:
        return "Hyperliquid's taker fee is not verified for these markets; Omni charges no trading fee."
    hidden = [market["label"] for market in config["markets"] if latest_fee.get(market["key"]) is None]
    tail = ", not verified (%s)" % ", ".join(hidden) if hidden else ""
    return "Hyperliquid base-tier taker fee per side: %s%s; Omni charges no trading fee." % (grouped_fees(known), tail)


def venue_notes(config, latest_fee, withheld):
    """[{key, label, fee_note}]: one sentence per venue, opening with its name, on the fee applied and what is left out.

    The page prints these sentences one under another with nothing else to say whose fee each is, so the name is part
    of the sentence.
    """
    notes = []
    for venue in config["venues"]:
        key, name = venue["key"], venue["label"]
        if key == "hl":
            known = [(latest_fee[m["key"]], m["label"]) for m in config["markets"] if latest_fee.get(m["key"]) is not None]
            hidden = [m["label"] for m in config["markets"] if latest_fee.get(m["key"]) is None]
            tail = ", not verified (%s)" % ", ".join(hidden) if hidden else ""
            if known:
                text = ("%s base-tier taker fee per side: %s%s; volume, staking and referral discounts not applied."
                        % (name, grouped_fees(known), tail))
            else:
                text = "%s taker fee not verified." % name
        else:
            mapped = [m for m in config["markets"] if m.get(key)]
            known = [(m[key]["taker_fee_bps"], m["label"]) for m in mapped
                     if m[key].get("taker_fee_bps") is not None and (key, m["key"]) not in withheld]
            hidden = [m["label"] for m in mapped if (m[key]["taker_fee_bps"], m["label"]) not in known]
            tail = ", not verified (%s)" % ", ".join(hidden) if hidden else ""
            if not known:
                text = "%s taker fee not verified." % name
            elif key == "lighter" and all(fee == 0 for fee, _ in known):
                # the free account's condition belongs next to its price: takers wait, and the book cost shown is the
                # book as displayed (config: lighter.taker_latency_ms, read from Lighter's fee tiers)
                delay = (config.get("lighter") or {}).get("taker_latency_ms")
                text = ("%s Standard account, the default: no maker or taker fee on regular orders%s%s; opt-in Premium "
                        "accounts pay a taker fee%s and are not shown."
                        % (name, tail, ", taker orders delayed %d ms" % delay if delay else "",
                           " for a shorter delay" if delay else ""))
            else:
                text = "%s entry-level taker fee per side: %s%s." % (name, grouped_fees(known), tail)
        notes.append({"key": key, "label": name, "fee_note": text})
    return notes


def market_rows(config):
    return [{"key": m["key"], "label": m["label"], "omni": m["omni"], "hl": m["hl"],
             "lighter": (m.get("lighter") or {}).get("symbol")}
            for m in config["markets"]]


def build(config, store):
    grouped, problems = read_store(store)
    days = sorted({day for day, _, _ in grouped})
    if not days:
        raise SystemExit("no samples in %s: run pipeline/capture_hl.py first" % os.path.join(store, "exec"))
    first, last = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
    dates = [(first + timedelta(days=offset)).isoformat() for offset in range((last - first).days + 1)]
    sizes = list(config["sizes"])
    names = list(HL_COLUMNS) + ["%s_%s" % (venue, column) for venue in OTHERS for column in VENUE_COLUMNS]
    range_names = list(RANGE_COLUMNS) + ["%s_%s" % (venue, column) for venue in OTHERS for column in VENUE_COLUMNS]
    by, ranges, warnings, latest_fee, withheld = {}, {label: {} for label, _ in RANGES}, [], {}, set()
    total_quoted = total_matched = 0
    tally = {venue: {"quoted": 0, "matched": 0, "unavailable": 0} for venue in OTHERS}
    newest = None
    for market in config["markets"]:
        key = market["key"]
        by[key] = {}
        for size in sizes:
            columns = {name: [] for name in names}
            for day in dates:
                samples = grouped.get((day, key, size), [])
                values, reasons = cell(config, market, samples)
                for reason in reasons:
                    venue = reason.split(":", 1)[0]
                    withheld.add(key if venue == "hl" else (venue, key))
                    warnings.append("%s %s %s: fee withheld (%s)" % (day, key, size, reason))
                for name in columns:
                    columns[name].append(values[name])
                total_quoted += values["quoted"]
                total_matched += values["n"]
                for row in samples:
                    for flag, stamp in [("matched", "hl_at")] + [(venue + "_matched", venue + "_at") for venue in OTHERS]:
                        if row.get(flag):
                            for moment in (row.get("omni_at"), row.get(stamp)):
                                if moment and (newest is None or moment > newest):
                                    newest = moment
                    for venue in OTHERS:
                        status = row.get(venue + "_status")
                        if status is None:
                            continue
                        tally[venue]["unavailable"] += status == "venue_unavailable"
                        if row.get("omni_bps") is not None:
                            tally[venue]["quoted"] += 1
                            tally[venue]["matched"] += bool(row.get(venue + "_matched") and row.get(venue + "_book_bps") is not None)
                if values["hl_fee"] is not None:
                    latest_fee[key] = values["hl_fee"] / 2
            by[key][size] = columns
            for label, span in RANGES:
                start = dates[0] if span is None else (last - timedelta(days=span - 1)).isoformat()
                pooled = [row for day in dates if day >= start for row in grouped.get((day, key, size), [])]
                values, _ = cell(config, market, pooled)
                ranges[label].setdefault(key, {})[size] = {name: values[name] for name in range_names}
    for market in config["markets"]:  # a market with no matched sample yet still gets its verified fee in the note
        if market["key"] not in latest_fee and market["key"] not in withheld and market.get("taker_fee_bps") is not None:
            latest_fee[market["key"]] = market["taker_fee_bps"]
    if newest is None:  # nothing matched yet: fall back to the newest reading of either venue
        stamps = [stamp for rows in grouped.values() for row in rows for stamp in (row.get("omni_at"), row.get("hl_at")) if stamp]
        newest = max(stamps) if stamps else dates[-1] + "T00:00:00.000Z"
    match = {"quoted": total_quoted, "matched": total_matched,
             "rate": round(total_matched / total_quoted, 4) if total_quoted else None,
             "venues": {"hl": {"quoted": total_quoted, "matched": total_matched,
                               "rate": round(total_matched / total_quoted, 4) if total_quoted else None, "unavailable": 0}}}
    for venue in OTHERS:
        counts = tally[venue]
        match["venues"][venue] = dict(counts, rate=round(counts["matched"] / counts["quoted"], 4) if counts["quoted"] else None)
    doc = {
        "as_of": newest[:19] + "Z",
        "fee_note": fee_note(config, latest_fee),
        "venues": venue_notes(config, latest_fee, withheld),
        "markets": market_rows(config),
        "dates": dates,
        "by": by,
        "range": ranges,
        "match": match,
    }
    return doc, warnings + problems


def write(doc, path=OUT):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(doc, handle, separators=(",", ":"), ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)


def report(config, doc, store):
    lines = []
    grouped, _ = read_store(store)
    lines.append("as_of %s · dates %s to %s (%d) · matched %d of %d Omni-quoted samples (%s)" % (
        doc["as_of"], doc["dates"][0], doc["dates"][-1], len(doc["dates"]), doc["match"]["matched"],
        doc["match"]["quoted"], "n/a" if doc["match"]["rate"] is None else "%.1f%%" % (100 * doc["match"]["rate"])))
    by_source = {}
    for rows in grouped.values():
        for row in rows:
            if row.get("omni_bps") is not None:
                tally = by_source.setdefault(row.get("src", "?"), [0, 0])
                tally[0] += 1
                tally[1] += bool(row.get("matched"))
    for source, (quoted, matched) in sorted(by_source.items()):
        lines.append("  hl %-8s matched %d of %d (%.1f%%)" % (source, matched, quoted, 100.0 * matched / quoted))
    for venue in OTHERS:
        counts = doc["match"]["venues"][venue]
        lines.append("  %-11s matched %d of %d attempted%s%s" % (
            venue, counts["matched"], counts["quoted"],
            "" if counts["rate"] is None else " (%.1f%%)" % (100 * counts["rate"]),
            " · refused by the venue on %d samples" % counts["unavailable"] if counts["unavailable"] else ""))
    show = lambda value: "-" if value is None else "%.2f" % value
    lines.append("  all dates   %-5s %7s | %7s %6s %5s %3s | %7s %6s %3s" % (
        "size", "omni", "hl_book", "hl_fee", "round", "n", "lighter", "fee", "n"))
    close = 0
    for market in config["markets"]:
        for size in config["sizes"]:
            everything = doc["range"]["All"][market["key"]][size]
            columns = doc["by"][market["key"]][size]
            rounds = [value for value in columns["hl_round"] if value is not None]
            lines.append("  %-11s %-5s %7s | %7s %6s %5s %3d | %7s %6s %3d" % (
                market["key"], size, show(everything["omni"]), show(everything["hl_book"]), show(everything["hl_fee"]),
                show(max(rounds) if rounds else None), everything["n"],
                show(everything["lighter_book"]), show(everything["lighter_fee"]), everything["lighter_n"]))
            for omni, book, rounding in zip(columns["omni"], columns["hl_book"], columns["hl_round"]):
                if omni is not None and rounding and book - rounding < omni < book:
                    close += 1
    lines.append("market-days where Omni sits inside Hyperliquid's rounding band (cheaper venue not settled): %d" % close)
    lines.append("fee_note: " + doc["fee_note"])
    for venue in doc["venues"]:
        lines.append("  %s: %s" % (venue["key"], venue["fee_note"]))
    return lines


def check(config, store, path=OUT):
    errors = []
    try:
        doc = load(path)
    except Exception as error:
        return ["cannot read %s: %s" % (path, error)], None, []
    if doc.get("fixture"):
        errors.append("file is a frontend fixture, not pipeline output")
    for name in ("as_of", "fee_note", "venues", "markets", "dates", "by", "range", "match"):
        if name not in doc:
            errors.append("missing key " + name)
    if errors:
        return errors, doc, []
    if not TIME.match(str(doc["as_of"])):
        errors.append("as_of is not a UTC time: %r" % doc["as_of"])
    note = doc["fee_note"]
    if not isinstance(note, str) or not note.strip() or note.strip().count(". ") or not note.strip().endswith("."):
        errors.append("fee_note must be one sentence")
    if doc["markets"] != market_rows(config):
        errors.append("markets differ from pipeline/config/venues.json")
    if [venue.get("key") for venue in doc["venues"]] != [venue["key"] for venue in config["venues"]]:
        errors.append("venues differ from pipeline/config/venues.json")
    for venue in doc["venues"]:
        text = venue.get("fee_note")
        if not isinstance(text, str) or not text.strip().endswith(".") or text.strip().count(". "):
            errors.append("venues[%s].fee_note must be one sentence" % venue.get("key"))
    dates = doc["dates"]
    expected = []
    if dates:
        try:
            start, end = date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])
            expected = [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]
        except ValueError:
            pass
    if not dates or dates != expected:
        errors.append("dates must be consecutive UTC days")
    for market in config["markets"]:
        key = market["key"]
        allowed = [2 * rate for rate in verified_rates(config, market)]
        for size in config["sizes"]:
            columns = doc["by"].get(key, {}).get(size)
            if not columns:
                errors.append("%s %s missing" % (key, size))
                continue
            for name in ("omni", "hl_book", "hl_fee", "n"):
                if len(columns.get(name, [])) != len(dates):
                    errors.append("%s %s %s length differs from dates" % (key, size, name))
            if any(len(columns.get(name, [])) != len(dates) for name in ("omni", "hl_book", "hl_fee", "n")):
                continue
            for index, day in enumerate(dates):
                count, omni, book, fee = (columns[name][index] for name in ("n", "omni", "hl_book", "hl_fee"))
                where = "%s %s %s" % (key, size, day)
                if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                    errors.append(where + ": n is not a count")
                    continue
                if (count == 0) != (omni is None) or (count == 0) != (book is None):
                    errors.append(where + ": medians must be null exactly where n is 0")
                if count == 0 and fee is not None:
                    errors.append(where + ": hl_fee without samples")
                for name, value in (("omni", omni), ("hl_book", book), ("hl_fee", fee)):
                    if value is not None and (not isinstance(value, (int, float)) or value < 0 or round(value, 2) != value):
                        errors.append("%s: %s is not a non-negative bps figure with two decimals" % (where, name))
                if fee is not None and not any(abs(fee - rate) < 0.005 for rate in allowed):
                    errors.append(where + ": hl_fee is not two verified taker fees")
                if fee is not None and market.get("taker_fee_bps") is None:
                    errors.append(where + ": hl_fee published for a market whose fee is null in venues.json")
            for venue in OTHERS:
                spec = market.get(venue)
                series = [columns.get("%s_%s" % (venue, name)) for name in VENUE_COLUMNS]
                if any(not isinstance(values, list) or len(values) != len(dates) for values in series):
                    errors.append("%s %s %s columns missing or not the length of dates" % (key, size, venue))
                    continue
                rates = [2 * rate for rate in other_rates(config, venue, spec)] if spec else []
                for index, day in enumerate(dates):
                    book, fee, count, omni = (values[index] for values in series)
                    where = "%s %s %s %s" % (key, size, day, venue)
                    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                        errors.append(where + ": n is not a count")
                        continue
                    if not spec and (count or book is not None or fee is not None or omni is not None):
                        errors.append(where + ": figures for a market the venue does not list")
                    if (count == 0) != (book is None) or (count == 0) != (omni is None):
                        errors.append(where + ": medians must be null exactly where n is 0")
                    if count == 0 and fee is not None:
                        errors.append(where + ": fee without samples")
                    for name, value in (("book", book), ("fee", fee), ("omni", omni)):
                        if value is not None and (not isinstance(value, (int, float)) or value < 0 or round(value, 2) != value):
                            errors.append("%s: %s is not a non-negative bps figure with two decimals" % (where, name))
                    if fee is not None and not any(abs(fee - rate) < 0.005 for rate in rates):
                        errors.append(where + ": fee is not two verified taker fees")
    fresh, warnings = build(config, store)
    if not errors and fresh != doc:
        errors.append("file differs from a rebuild of the store: run pipeline/build_execution.py")
    for market in config["markets"]:  # the verified figure in venues.json against what the venue says now
        columns = fresh["by"][market["key"]][next(iter(config["sizes"]))]
        published = [fee for fee in columns["hl_fee"] if fee is not None]
        if published and market.get("taker_fee_bps") is not None and abs(published[-1] / 2 - market["taker_fee_bps"]) > 1e-6:
            warnings.append("%s: latest captured fee %g bps per side differs from venues.json (%g): re-verify and update"
                            % (market["key"], published[-1] / 2, market["taker_fee_bps"]))
    return errors, doc, warnings


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="validate the written file; exit 1 on any failure")
    parser.add_argument("--store", default=STORE)
    args = parser.parse_args()
    config = load(CONFIG)
    if args.check:
        errors, doc, warnings = check(config, args.store)
        if doc is not None and not errors:
            print("\n".join(report(config, doc, args.store)))
        for text in warnings:
            print("warning: " + text)
        for text in errors:
            print("FAIL: " + text)
        print("execution_venue.json: %s" % ("FAILED (%d)" % len(errors) if errors else "ok"))
        return 1 if errors else 0
    doc, warnings = build(config, args.store)
    write(doc)
    for text in warnings:
        print("warning: " + text)
    print("wrote %s · as_of %s · %d dates · matched %d of %d" % (
        os.path.relpath(OUT, ROOT), doc["as_of"], len(doc["dates"]), doc["match"]["matched"], doc["match"]["quoted"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
