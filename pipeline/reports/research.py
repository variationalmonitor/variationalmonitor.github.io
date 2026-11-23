"""Research Library: the owner's pieces, as a plain list. No indicators are tracked per piece.

One config per piece, `pipeline/config/research/<slug>.json` (the slug is the file name):
  title         the piece's title
  publisher     who publishes it
  published_at  UTC timestamp (or date) of publication; null while the piece is scheduled
  scheduled_for date (YYYY-MM-DD) the piece is scheduled for; required while published_at is null
  url           public https address once published; null until then
A piece is never invented here; a config is added when one is scheduled.

Output `site/data/research.json` = {as_of, pieces: [{slug, title, publisher, date, url, status}]}, newest first
(by date, then slug): status is "published" when published_at is set, otherwise "scheduled"; date is the UTC day
of publication, or scheduled_for while unpublished; as_of is the build time.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .util import CONFIG, iso_z, parse_date, parse_time, read_json, read_json_or_none, write_json

RESEARCH_CONFIG = CONFIG / "research"
CONFIG_KEYS = {"title", "publisher", "published_at", "scheduled_for", "url"}
PIECE_KEYS = ["slug", "title", "publisher", "date", "url", "status"]
STATUSES = ("published", "scheduled")
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,99}")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class ConfigError(Exception):
    """A research config is malformed; the message names the file."""


def _piece(cfg, slug: str, now: datetime) -> dict:
    """One validated list entry from a config."""
    def need(cond, msg):
        if not cond:
            raise ConfigError("research/%s.json: %s" % (slug, msg))
    need(isinstance(cfg, dict), "not an object")
    need(SLUG.fullmatch(slug), "the file name is not a slug (lowercase letters, digits, hyphens)")
    extra = sorted(k for k in cfg if k not in CONFIG_KEYS and not k.startswith("_"))
    need(not extra, "unknown keys %s (allowed: %s)" % (extra, ", ".join(sorted(CONFIG_KEYS))))
    need(isinstance(cfg.get("title"), str) and cfg["title"].strip(), "title missing")
    need(isinstance(cfg.get("publisher"), str) and cfg["publisher"].strip(), "publisher missing")
    if cfg.get("published_at") is not None:
        try:
            published = parse_time(cfg["published_at"]) if isinstance(cfg["published_at"], str) else None
        except ValueError:
            published = None
        need(published is not None, "published_at must be a UTC timestamp or a date")
        need(published <= now, "published_at is in the future")
        url = urlparse(cfg.get("url") or "")
        need(url.scheme == "https" and url.hostname and not url.username
             and not url.hostname.endswith(("notion.so", "notion.com", "notion.site")),
             "a published piece needs its public https url")
        status, day = "published", iso_z(published)[:10]
    else:
        sched = cfg.get("scheduled_for")
        ok = isinstance(sched, str) and DATE.fullmatch(sched)
        try:
            ok = ok and parse_date(sched)
        except ValueError:
            ok = False
        need(ok, "a piece without published_at needs scheduled_for as a date (YYYY-MM-DD)")
        need(cfg.get("url") is None, "a scheduled piece has no url yet")
        status, day = "scheduled", sched
    return {"slug": slug, "title": cfg["title"].strip(), "publisher": cfg["publisher"].strip(),
            "date": day, "url": cfg.get("url"), "status": status}


def load_pieces(now: datetime, config_dir: Path | None = None) -> list[dict]:
    """Every configured piece as a list entry, newest first."""
    config_dir = config_dir or RESEARCH_CONFIG
    pieces = []
    for path in sorted(config_dir.glob("*.json")) if config_dir.exists() else []:
        try:
            cfg = read_json(path)
        except ValueError as exc:
            raise ConfigError("research/%s: %s" % (path.name, exc))
        pieces.append(_piece(cfg, path.stem, now))
    pieces.sort(key=lambda p: (p["date"], p["slug"]), reverse=True)
    return pieces


def build(now: datetime, config_dir: Path | None = None) -> dict:
    return {"as_of": iso_z(now), "pieces": load_pieces(now, config_dir)}


def write(site_data: Path, index: dict) -> list[str]:
    """Write research.json and remove what the per-piece library left behind. Returns the paths removed."""
    write_json(site_data / "research.json", index)
    removed = []
    old = site_data / "research"
    if old.is_dir():
        for path in sorted(old.glob("*")):
            if path.is_file():
                path.unlink()
                removed.append(str(path.relative_to(site_data)))
        try:
            old.rmdir()
        except OSError:
            pass
    return removed


def check(site_data: Path, now: datetime, config_dir: Path | None = None) -> list[str]:
    """research.json against the configs: shape, order, and every entry as the configs give it."""
    doc = read_json_or_none(site_data / "research.json")
    if not isinstance(doc, dict):
        return ["research.json missing or unreadable"]
    errors = []
    if sorted(doc) != ["as_of", "pieces"]:
        errors.append("research.json keys %s, expected as_of and pieces" % sorted(doc))
    try:
        if not isinstance(doc.get("as_of"), str) or parse_time(doc["as_of"]) > now:
            errors.append("research.json: as_of %r" % doc.get("as_of"))
    except ValueError:
        errors.append("research.json: as_of %r" % doc.get("as_of"))
    pieces = doc.get("pieces")
    if not isinstance(pieces, list):
        return errors + ["research.json: pieces is not a list"]
    for p in pieces:
        if not isinstance(p, dict) or list(p) != PIECE_KEYS:
            errors.append("research.json: entry keys %s, expected %s" % (list(p) if isinstance(p, dict) else p, PIECE_KEYS))
        elif p["status"] not in STATUSES or (p["status"] == "published") != (p["url"] is not None) \
                or not (isinstance(p["date"], str) and DATE.fullmatch(p["date"])):
            errors.append("research.json %s: status %r, date %r, url %r" % (p["slug"], p["status"], p["date"], p["url"]))
    if [(p.get("date"), p.get("slug")) for p in pieces if isinstance(p, dict)] != \
            sorted(((p.get("date"), p.get("slug")) for p in pieces if isinstance(p, dict)), reverse=True):
        errors.append("research.json: pieces are not newest first")
    try:
        want = load_pieces(now, config_dir)
    except ConfigError as exc:
        return errors + [str(exc)]
    if pieces != want:
        errors.append("research.json pieces differ from pipeline/config/research (%d listed, %d configured)" % (len(pieces), len(want)))
    if (site_data / "research").exists():
        errors.append("site/data/research/ still exists (the per-piece files are gone from the contract)")
    return errors
