"""Normalise every report-like post into `<store>/reports.json`.

A post is report-like when it prints `Total Volume Traded`. Telegram (the
Discord announcement, mirrored) is the main source; the X post of the same
update, when it is in the store, fills figures the Telegram post leaves blank
and is compared line by line. Where both print a figure and they differ, the
Telegram figure is kept and the difference is recorded.

Output: a JSON list, oldest first. Each record starts with
  published_at, source_url, cumulative_volume, dual_sided_oi, tvl, markets, tokens_listed
(null when not printed), then every other line the post prints, then
  id, full (a full financial update), x_url, x_published_at, filled_from_x,
  x_differences, edited, raw (the printed strings), warnings.
Amounts keep their printed sign: costs printed in parentheses are negative.
"""
from __future__ import annotations

from pathlib import Path

from sources import telegram

from . import parse, xposts
from .util import parse_time, write_json

HEAD = ("cumulative_volume", "dual_sided_oi", "tvl", "markets", "tokens_listed")
MATCH_WINDOW_DAYS = 2      # an X post and a Telegram post are the same update within this


def _record(pid, when, url, parsed, edited=False):
    rec = {"published_at": when, "source_url": url}
    for key in HEAD:
        rec[key] = parsed["fields"].get(key)
    for key, value in parsed["fields"].items():
        if key not in HEAD:
            rec[key] = value
    rec.update({
        "id": pid,
        "full": parse.is_full_report(parsed),
        "x_url": None,
        "x_published_at": None,
        "filled_from_x": [],
        "x_differences": {},
        "edited": bool(edited),
        "raw": dict(parsed["raw"]),
        "warnings": [],
    })
    if parsed["blank"]:
        rec["warnings"].append("printed without a figure: " + ", ".join(parsed["blank"]))
    if parsed["unmapped"]:
        rec["warnings"].append("not read: " + "; ".join("%s = %s" % (a, b) for a, b in parsed["unmapped"]))
    if parsed["duplicates"]:
        rec["warnings"].append("printed twice with different values: " + ", ".join(parsed["duplicates"]))
    return rec


def collect(store: Path, cache_dir: Path | None = None) -> list[dict]:
    posts = telegram.load(store, cache_dir)["posts"]
    records = []
    for post in posts:
        parsed = parse.parse_post(post["text"])
        if not parse.is_report(parsed):
            continue
        records.append(_record("telegram:%d" % post["id"], post["time"],
                               telegram.POST_URL.format(id=post["id"]), parsed,
                               post.get("edited")))

    for xp in xposts.load(store):
        parsed = parse.parse_post(xp["text"])
        if not parse.is_report(parsed):
            raise ValueError("X post %s is not a report" % xp["id"])
        when = parse_time(xp["time"])
        match = None
        for rec in records:
            if rec["id"].startswith("telegram:") and rec["x_url"] is None \
                    and rec["cumulative_volume"] == parsed["fields"]["cumulative_volume"] \
                    and abs((parse_time(rec["published_at"]) - when).total_seconds()) <= MATCH_WINDOW_DAYS * 86400:
                match = rec
                break
        if match is None:
            rec = _record("x:" + xp["id"], xp["time"], xp["url"], parsed)
            rec["x_url"] = xp["url"]
            rec["x_published_at"] = xp["time"]
            records.append(rec)
            continue
        match["x_url"] = xp["url"]
        match["x_published_at"] = xp["time"]
        for key, value in parsed["fields"].items():
            mine = match.get(key)
            if mine is None:
                match[key] = value
                match["raw"][key] = parsed["raw"][key]
                match["filled_from_x"].append(key)
            elif mine != value:
                match["x_differences"][key] = {"telegram": mine, "x": value}
        match["full"] = all(match.get(f) is not None for f in parse.FULL_REPORT_FIELDS)

    records.sort(key=lambda r: r["published_at"])
    # two reports can never share a publication time; cumulative volume never falls
    for a, b in zip(records, records[1:]):
        if a["published_at"] == b["published_at"]:
            raise ValueError("duplicate report time %s" % a["published_at"])
        if b["cumulative_volume"] < a["cumulative_volume"]:
            b["warnings"].append("cumulative volume below the previous report")
    return records


def write(store: Path, records: list[dict]) -> Path:
    path = store / "reports.json"
    write_json(path, records)
    return path
