#!/usr/bin/env python3
"""Import and monitor official Variational financial reports, using only stdlib.

refresh: public Telegram discovery; write atomically only for changed report data.
import: one-time import of public FxTwitter replay JSON, never private prose.
verify: validate the public dataset without network access or mutation.

Amounts are exact decimal strings. Deductions retain their reported negative sign.
Report publication times are not inferred accounting-period boundaries.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, HTTPRedirectHandler, build_opener

CHANNEL = "variational_io"
FEED_URL = f"https://t.me/s/{CHANNEL}"
OFFICIAL_LINKS = "https://docs.variational.io/getting-started/official-links"
SCHEMA_VERSION = 1
MAX_RESPONSE_BYTES = 512_000
MAX_FEED_PAGES = 6
MAX_REPORT_TEXT = 40_000
MAX_REVISIONS = 100
MAX_REPORTS = 2_000
PERIOD_FIELDS = (
    "gross_revenue", "market_making_costs", "net_revenue", "rewards",
    "net_profit", "treasury_inflow", "olp_pnl",
)
SNAPSHOT_FIELDS = (
    "cumulative_volume", "dual_sided_oi", "tvl", "markets", "tokens_listed",
    "lifetime_olp_pnl", "treasury_holdings", "onchain_treasury_value",
)
REQUIRED = set(PERIOD_FIELDS) - {"market_making_costs"}
REQUIRED |= {"lifetime_olp_pnl"}
# Exact historical source vocabulary, not fuzzy economic interpretation.
LABELS = {
    "2w spreads paid (gross revenue)": "gross_revenue",
    "2w market making costs (cost of revenue)": "market_making_costs",
    "2w net revenue": "net_revenue",
    "2w rewards": "rewards",
    "2w net profit": "net_profit",
    "protocol treasury": "treasury_inflow",
    "2w protocol treasury inflow": "treasury_inflow",
    "olp pnl": "olp_pnl",
    "2w olp pnl": "olp_pnl",
    "lifetime olp pnl": "lifetime_olp_pnl",
    "current protocol treasury holdings": "treasury_holdings",
    "total on-chain protocol treasury value": "onchain_treasury_value",
    "total volume traded": "cumulative_volume",
    "current dual-sided oi": "dual_sided_oi",
    "tvl (excludes some olp hedging accounts)": "tvl",
    "tvl (excludes olp hedging accounts)": "tvl",
    "total markets currently listed": "markets",
    "total tokens currently listed": "tokens_listed",
}


class SourceError(ValueError):
    """An ambiguous or untrusted record must not replace published data."""


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_time(value: str) -> str:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        try:
            result = parsedate_to_datetime(value)
        except (ValueError, TypeError) as exc:
            raise SourceError("Invalid source publication timestamp") from exc
    if result.tzinfo is None:
        raise SourceError("Source publication timestamp has no timezone")
    return result.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def date_time(value: str) -> datetime:
    return datetime.fromisoformat(iso_time(value).replace("Z", "+00:00"))


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise SourceError("Non-finite amount")
    return format(value, "f") if value else "0"


def parse_amount(raw: str, field: str) -> tuple[str, str]:
    raw = raw.strip().replace("−", "-")
    count = field in {"markets", "tokens_listed"}
    unit = "count" if count else ("USDC" if field == "treasury_holdings" else "$")
    if count:
        if not re.fullmatch(r"\d{1,7}", raw):
            raise SourceError(f"Invalid count for {field}")
        return str(int(raw)), unit
    pattern = r"(?P<open>\()?\s*(?P<sign>-)?\s*(?P<dollar>\$)?(?P<num>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(?P<scale>[KMB])?\s*(?P<usdc>USDC)?\s*(?P<close>\))?"
    match = re.fullmatch(pattern, raw, re.I)
    if not match or bool(match["open"]) != bool(match["close"]):
        raise SourceError(f"Invalid financial amount for {field}")
    if unit == "USDC":
        if not match["usdc"] or match["dollar"]:
            raise SourceError("Treasury holdings must explicitly state USDC")
    elif not match["dollar"] or match["usdc"]:
        raise SourceError(f"Unexpected source unit for {field}")
    value = Decimal(match["num"].replace(",", ""))
    value *= {None: 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[match["scale"].upper() if match["scale"] else None]
    if match["sign"] or match["open"]:
        value = -value
    if abs(value) > Decimal("1e16"):
        raise SourceError(f"Amount outside supported bounds for {field}")
    if field in {"rewards", "market_making_costs"} and value > 0:
        raise SourceError(f"Deduction sign changed for {field}; review required")
    return decimal_text(value), unit


def parse_report(text: str, source: dict) -> dict | None:
    """Recognize a complete official report; ordinary posts return None."""
    if len(text) > MAX_REPORT_TEXT:
        raise SourceError("Report text exceeds bounded parser size")
    if not re.search(r"\b(?:variational|our)\s+bi[- ]?weekly\s+update\b", text, re.I):
        if sum(label in text.casefold() for label in ("2w spreads paid", "2w net revenue", "2w net profit")) >= 2:
            raise SourceError("Financial report heading changed; review required")
        return None
    if source.get("author") != CHANNEL or source.get("forwarded"):
        raise SourceError("Report is not an original official-channel post")
    source = copy.deepcopy(source)
    source.pop("forwarded", None)
    source["published_at"] = iso_time(source["published_at"])
    if not isinstance(source.get("id"), str) or not source["id"].isdigit():
        raise SourceError("Source IDs must be decimal strings")
    expected_url = (f"https://x.com/{CHANNEL}/status/{source['id']}" if source["platform"] == "x"
                    else f"https://t.me/{CHANNEL}/{source['id']}")
    if source.get("url") != expected_url or source["platform"] not in {"x", "telegram"}:
        raise SourceError("Source URL or platform is outside the allowlist")
    amounts, snapshots, labels, units, raw_values = {}, {}, {}, {}, {}
    for raw_line in text.splitlines():
        line = re.sub(r"^[\s➡️◦•\-*]+", "", raw_line).strip()
        if ":" not in line:
            continue
        label, raw = line.split(":", 1)
        label = " ".join(label.split())
        field = LABELS.get(label.casefold())
        if field is None:
            # A new 2w accounting line needs explicit schema review.
            if re.match(r"^2w\s", label, re.I):
                raise SourceError(f"Unrecognized biweekly financial label: {label}")
            continue
        if field in labels:
            raise SourceError(f"Duplicate financial field: {field}")
        value, unit = parse_amount(raw, field)
        (amounts if field in PERIOD_FIELDS else snapshots)[field] = value
        labels[field], units[field], raw_values[field] = label, unit, raw.strip()
    if REQUIRED - set(labels):
        raise SourceError("Incomplete report: " + ", ".join(sorted(REQUIRED - set(labels))))
    amounts = {key: amounts.get(key) for key in PERIOD_FIELDS}
    snapshots = {key: snapshots.get(key) for key in SNAPSHOT_FIELDS}
    content = {"period_amounts": amounts, "snapshot": snapshots, "labels": labels, "units": units, "raw_values": raw_values}
    source["financial_sha256"] = fingerprint(content)
    source["matches_current"] = True
    return {
        "id": f"{source['platform']}:{source['id']}",
        "published_at": source["published_at"],
        "period_label": "2w", "period_start": None, "period_end": None,
        "source_urls": [source["url"]], "sources": [source],
        **content, "warnings": [], "revisions": [],
    }


def import_replay(path: Path, observed_at: str) -> dict | None:
    data = json.loads(path.read_text(encoding="utf-8"))
    tweet = data.get("tweet", {})
    author = tweet.get("author", {}).get("screen_name")
    # Non-report files are ignored; apparent reports with a wrong origin fail.
    text = tweet.get("text", "")
    source = {
        "platform": "x", "id": tweet.get("id"), "url": tweet.get("url"),
        "author": author, "published_at": tweet.get("created_at", ""),
        "observed_at": observed_at,
        "retrieval_method": "saved FxTwitter replay of official X post",
        "forwarded": bool(tweet.get("retweet") or tweet.get("retweeted_status")),
    }
    return parse_report(text, source)


class TelegramFeedParser(HTMLParser):
    """Read only original message text/owner metadata, excluding link previews."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.posts = []
        self.current = None
        self.depth = 0
        self.message_depth = None
        self.text_depth = None
        self.in_script = 0

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        classes = attrs.get("class", "").split()
        if tag == "div":
            self.depth += 1
        if "data-post" in attrs:
            if self.current is not None:
                raise SourceError("Nested Telegram message container")
            self.current = {"key": attrs["data-post"], "text_parts": [], "owner": None, "published_at": None, "forwarded": False}
            self.message_depth = self.depth
        if self.current is None:
            return
        if any("forwarded" in name for name in classes):
            self.current["forwarded"] = True
        if tag == "a" and "tgme_widget_message_owner_name" in classes:
            self.current["owner"] = attrs.get("href")
        if tag == "div" and "tgme_widget_message_text" in classes:
            self.text_depth = self.depth
        if tag == "time" and attrs.get("datetime"):
            self.current["published_at"] = attrs["datetime"]
        if tag in {"script", "style"}:
            self.in_script += 1
        if tag == "br" and self.text_depth is not None:
            self.current["text_parts"].append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in {"br", "img", "input", "meta", "link", "hr"}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.in_script:
            self.in_script -= 1
        if tag == "div":
            if self.text_depth == self.depth:
                self.text_depth = None
            if self.current is not None and self.message_depth == self.depth:
                self.current["text"] = "".join(self.current.pop("text_parts"))
                self.posts.append(self.current)
                self.current = None
                self.message_depth = None
            self.depth -= 1
            if self.depth < 0:
                raise SourceError("Malformed Telegram HTML")

    def handle_data(self, data):
        if self.current is not None and self.text_depth is not None and not self.in_script:
            self.current["text_parts"].append(data)


def parse_feed(html: str, observed_at: str) -> tuple[list[dict], list[str]]:
    parser = TelegramFeedParser()
    parser.feed(html)
    parser.close()
    if not parser.posts or parser.current is not None:
        raise SourceError("No complete messages in Telegram feed")
    reports, ids = [], []
    for post in parser.posts:
        match = re.fullmatch(rf"{CHANNEL}/(\d+)", post["key"])
        if not match or post["owner"] != f"https://t.me/{CHANNEL}":
            raise SourceError("Telegram message origin failed allowlist validation")
        post_id = match.group(1)
        ids.append(post_id)
        if post["forwarded"]:
            continue
        source = {
            "platform": "telegram", "id": post_id,
            "url": f"https://t.me/{CHANNEL}/{post_id}", "author": CHANNEL,
            "published_at": post["published_at"], "observed_at": observed_at,
            "retrieval_method": "official public Telegram announcements feed",
        }
        report = parse_report(post["text"], source)
        if report:
            reports.append(report)
    if len(ids) != len(set(ids)):
        raise SourceError("Duplicate message IDs in feed")
    return reports, ids


def value_fingerprint(report: dict) -> str:
    return fingerprint({key: report[key] for key in ("period_amounts", "snapshot", "units")})


def source_key(source: dict) -> tuple[str, str]:
    return source["platform"], source["id"]


def merge_report(reports: list[dict], incoming: dict, observed_at: str) -> bool:
    new_source = incoming["sources"][0]
    existing = next((r for r in reports if any(source_key(s) == source_key(new_source) for s in r["sources"])), None)
    if existing:
        old_source = next(s for s in existing["sources"] if source_key(s) == source_key(new_source))
        if old_source["financial_sha256"] == new_source["financial_sha256"]:
            return False
        if len(existing["revisions"]) >= MAX_REVISIONS:
            raise SourceError("Revision limit reached; explicit archive review required")
        revision = {"observed_at": observed_at, "reason": "official source financial content changed"}
        for key in ("period_amounts", "snapshot", "labels", "units", "raw_values", "sources"):
            revision[key] = copy.deepcopy(existing[key])
        existing["revisions"].append(revision)
        for key in ("period_amounts", "snapshot", "labels", "units", "raw_values"):
            existing[key] = copy.deepcopy(incoming[key])
        for index, old in enumerate(existing["sources"]):
            if source_key(old) == source_key(new_source):
                existing["sources"][index] = new_source
            else:
                old["matches_current"] = False
        return True
    # Match independent mirrors only within a bounded publication window.
    nearby = [r for r in reports if abs(date_time(r["published_at"]) - date_time(incoming["published_at"])) <= timedelta(hours=36)]
    for existing in nearby:
        if value_fingerprint(existing) == value_fingerprint(incoming):
            existing["sources"].append(new_source)
            existing["source_urls"] = sorted({s["url"] for s in existing["sources"]})
            existing["published_at"] = min(existing["published_at"], incoming["published_at"])
            return True
        raise SourceError("Conflicting report sources within the same publication window")
    reports.append(copy.deepcopy(incoming))
    return True


def warning(code: str, message: str, residual: Decimal | None = None) -> dict:
    result = {"code": code, "message": message}
    if residual is not None:
        result["residual"] = decimal_text(residual)
        result["unit"] = "$"
    return result


def annotate(reports: list[dict]) -> None:
    previous = None
    for report in reports:
        p = report["period_amounts"]
        report["warnings"] = []
        checks = [("net_profit_identity", Decimal(p["net_revenue"]) + Decimal(p["rewards"]) - Decimal(p["net_profit"]), "Reported Net Revenue plus signed Rewards differs from reported Net Profit."),
                  ("allocation_identity", Decimal(p["treasury_inflow"]) + Decimal(p["olp_pnl"]) - Decimal(p["net_profit"]), "Reported Protocol Treasury and OLP PnL do not sum to reported Net Profit.")]
        if p["market_making_costs"] is not None:
            checks.append(("net_revenue_identity", Decimal(p["gross_revenue"]) + Decimal(p["market_making_costs"]) - Decimal(p["net_revenue"]), "Reported gross revenue plus signed Market Making Costs differs from Net Revenue."))
        for code, residual, message in checks:
            if residual:
                report["warnings"].append(warning(code, message + " Source values are preserved.", residual))
        if previous:
            residual = Decimal(report["snapshot"]["lifetime_olp_pnl"]) - Decimal(previous["snapshot"]["lifetime_olp_pnl"]) - Decimal(p["olp_pnl"])
            if residual:
                report["warnings"].append(warning("lifetime_olp_change", "Change in reported Lifetime OLP PnL differs from the current report's OLP PnL; coverage or source discrepancy is unresolved.", residual))
            if date_time(report["published_at"]) - date_time(previous["published_at"]) > timedelta(days=21):
                report["warnings"].append(warning("possible_report_gap", "More than 21 days between covered report publications; completeness is not established."))
        if any(not source.get("matches_current", True) for source in report["sources"]):
            report["warnings"].append(warning("older_mirror_version", "This report was revised by its original source; an archived mirror has not been rechecked against the revision."))
        previous = report


def finalize(reports: list[dict], observed_at: str, discovery: dict | None = None) -> dict:
    reports = sorted(copy.deepcopy(reports), key=lambda r: (r["published_at"], r["id"]))
    if not reports or len(reports) > MAX_REPORTS:
        raise SourceError("Report coverage is empty or exceeds the supported bound")
    annotate(reports)
    sums, field_coverage = {}, {}
    for field in PERIOD_FIELDS:
        values = [Decimal(r["period_amounts"][field]) for r in reports if r["period_amounts"][field] is not None]
        sums[field] = decimal_text(sum(values, Decimal(0))) if values else None
        field_coverage[field] = {"reported_count": len(values), "report_count": len(reports), "complete": len(values) == len(reports)}
    result = {
        "schema_version": SCHEMA_VERSION, "generated_at": observed_at,
        "latest_report_id": reports[-1]["id"],
        "coverage": {
            "first_report_published_at": reports[0]["published_at"],
            "last_report_published_at": reports[-1]["published_at"],
            "report_count": len(reports), "missing_period_boundaries": True,
            "unresolved_warning_count": sum(len(r["warnings"]) for r in reports),
            "history_note": "Covered public report publications, not a complete protocol-lifetime ledger. Publication dates are not exact accounting cutoffs.",
            "discovery": discovery or {"feed_url": FEED_URL, "initialized": False, "last_seen_message_id": None, "history_gap": None},
        },
        "totals": {"label": "Sum of covered reports", "period_amounts": sums, "field_coverage": field_coverage,
                   "note": "Sums preserve reported source amounts and their accounting discrepancies. Balances, cumulative volume and lifetime OLP PnL are never summed."},
        "source_identity_url": OFFICIAL_LINKS,
        "reports": reports,
    }
    validate_dataset(result)
    return result


def validate_dataset(data: dict) -> None:
    if data.get("schema_version") != SCHEMA_VERSION or not isinstance(data.get("reports"), list):
        raise SourceError("Unsupported financial dataset schema")
    if not data["reports"] or len(data["reports"]) > MAX_REPORTS:
        raise SourceError("Invalid report coverage")
    if data["reports"] != sorted(data["reports"], key=lambda r: (r["published_at"], r["id"])):
        raise SourceError("Report chronology is inconsistent")
    ids, sources = set(), set()
    for report in data["reports"]:
        if report["id"] in ids:
            raise SourceError("Duplicate report ID")
        ids.add(report["id"])
        iso_time(report["published_at"])
        if report.get("period_start") is not None or report.get("period_end") is not None:
            raise SourceError("Unverified accounting boundaries")
        for group, fields in (("period_amounts", PERIOD_FIELDS), ("snapshot", SNAPSHOT_FIELDS)):
            for field in fields:
                value = report[group].get(field)
                if value is None:
                    if field in REQUIRED:
                        raise SourceError(f"Missing required stored value: {field}")
                    continue
                if not isinstance(value, str) or not re.fullmatch(r"-?\d+(?:\.\d+)?", value) or not Decimal(value).is_finite():
                    raise SourceError("Amounts must be finite decimal strings")
        populated = {field for group in ("period_amounts", "snapshot") for field, value in report[group].items() if value is not None}
        if any(set(report[key]) != populated for key in ("raw_values", "labels", "units")):
            raise SourceError("Source transcription coverage is inconsistent")
        for field, raw in report["raw_values"].items():
            if field not in LABELS.values() or LABELS.get(report["labels"].get(field, "").casefold()) != field:
                raise SourceError("Stored financial label is not recognized")
            parsed, unit = parse_amount(raw, field)
            group = "period_amounts" if field in PERIOD_FIELDS else "snapshot"
            if parsed != report[group].get(field) or unit != report["units"].get(field):
                raise SourceError("Stored normalized amount disagrees with source transcription")
        payload = {key: report[key] for key in ("period_amounts", "snapshot", "labels", "units", "raw_values")}
        if not report["sources"] or not any(s.get("matches_current") and s.get("financial_sha256") == fingerprint(payload) for s in report["sources"]):
            raise SourceError("Missing matching source provenance")
        for source in report["sources"]:
            key = source_key(source)
            if key in sources:
                raise SourceError("Source is attached to more than one report")
            sources.add(key)
            expected = (f"https://x.com/{CHANNEL}/status/{source['id']}" if source["platform"] == "x" else f"https://t.me/{CHANNEL}/{source['id']}")
            if source["platform"] not in {"x", "telegram"} or source["author"] != CHANNEL or source["url"] != expected or not isinstance(source["id"], str) or not source["id"].isdigit():
                raise SourceError("Invalid stored source identity")
    recalculated = copy.deepcopy(data["reports"])
    annotate(recalculated)
    if any(a["warnings"] != b["warnings"] for a, b in zip(recalculated, data["reports"])):
        raise SourceError("Stored source-warning flags are inconsistent")
    if data["coverage"]["unresolved_warning_count"] != sum(len(r["warnings"]) for r in recalculated):
        raise SourceError("Source-warning count is inconsistent")
    if data["coverage"]["report_count"] != len(data["reports"]):
        raise SourceError("Report coverage count is inconsistent")
    if data.get("latest_report_id") != data["reports"][-1]["id"]:
        raise SourceError("Latest report pointer is inconsistent")
    for field in PERIOD_FIELDS:
        values = [Decimal(r["period_amounts"][field]) for r in data["reports"] if r["period_amounts"][field] is not None]
        expected = decimal_text(sum(values, Decimal(0))) if values else None
        if data["totals"]["period_amounts"].get(field) != expected:
            raise SourceError(f"Cumulative total does not match covered reports: {field}")
        expected_coverage = {"reported_count": len(values), "report_count": len(data["reports"]), "complete": len(values) == len(data["reports"])}
        if data["totals"]["field_coverage"].get(field) != expected_coverage:
            raise SourceError(f"Field coverage is inconsistent: {field}")


def allowed_feed_url(url: str) -> bool:
    parsed = urlparse(url)
    return (parsed.scheme == "https" and parsed.netloc == "t.me"
            and parsed.path == f"/s/{CHANNEL}"
            and (not parsed.query or re.fullmatch(r"before=\d+", parsed.query) is not None)
            and not parsed.fragment)


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        if not allowed_feed_url(new_url):
            raise SourceError("Source redirect is outside the feed allowlist")
        return super().redirect_request(request, fp, code, message, headers, new_url)


class PublicFetcher:
    def __init__(self):
        self.deadline = time.monotonic() + 45
        self.calls = 0

    def __call__(self, before: str | None = None) -> str:
        if self.calls >= MAX_FEED_PAGES:
            raise SourceError("Bounded page budget exhausted")
        if before is not None and not re.fullmatch(r"\d+", before):
            raise SourceError("Invalid Telegram pagination cursor")
        self.calls += 1
        url = FEED_URL + (f"?before={before}" if before else "")
        for attempt in range(2):
            if time.monotonic() >= self.deadline:
                raise SourceError("Report watcher runtime budget exhausted")
            try:
                request = Request(url, headers={"User-Agent": "VariationalCommunityDashboard/1.0 (+public-financial-report-monitor)", "Accept": "text/html"})
                with build_opener(SafeRedirect()).open(request, timeout=min(10, max(1, self.deadline - time.monotonic()))) as response:
                    if not allowed_feed_url(response.geturl()):
                        raise SourceError("Unexpected source redirect")
                    if "text/html" not in response.headers.get("Content-Type", ""):
                        raise SourceError("Unexpected source content type")
                    body = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise SourceError("Telegram response exceeds size budget")
                    return body.decode("utf-8", errors="strict")
            except HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt:
                    raise SourceError(f"Telegram HTTP {exc.code}; previous dataset retained") from exc
                retry_after = exc.headers.get("Retry-After", "1")
                if not retry_after.isdigit() or int(retry_after) > 2:
                    raise SourceError("Source asks for longer backoff; defer to a later run") from exc
                time.sleep(max(1, int(retry_after)))
            except (URLError, TimeoutError) as exc:
                if attempt:
                    raise SourceError("Telegram unavailable; previous dataset retained") from exc
                time.sleep(1)
        raise SourceError("Source fetch failed")


def refresh_dataset(existing: dict, fetch, observed_at: str) -> tuple[dict, bool]:
    validate_dataset(existing)
    reports = copy.deepcopy(existing["reports"])
    previous_discovery = existing["coverage"].get("discovery", {})
    cursor = previous_discovery.get("last_seen_message_id")
    pages, all_ids, incoming, seen = 0, [], [], set()
    before = None
    reached = False
    anchor = date_time(reports[-1]["published_at"]) - timedelta(hours=36)
    while pages < MAX_FEED_PAGES - 2:
        candidates, ids = parse_feed(fetch(before), observed_at)
        pages += 1
        all_ids.extend(ids)
        for report in candidates:
            key = report["sources"][0]["id"]
            if key not in seen:
                incoming.append(report)
                seen.add(key)
        if cursor:
            reached = min(map(int, ids)) <= int(cursor)
        else:
            reached = any(date_time(r["published_at"]) <= anchor for r in candidates)
            # Archive overlap can be proven by an exact report value match.
            reached |= any(value_fingerprint(r) == value_fingerprint(old) for r in candidates for old in reports)
        if reached:
            break
        next_before = str(min(map(int, ids)))
        if before is not None and int(next_before) >= int(before):
            raise SourceError("Telegram catch-up pagination did not advance")
        before = next_before
    if not reached:
        raise SourceError("Discovery history gap exceeds bounded catch-up; no cursor or data advanced")
    # Edits retain IDs: recheck the latest two known Telegram reports if not in feed.
    targets = [s["id"] for r in reports[-2:] for s in r["sources"] if s["platform"] == "telegram"]
    for target in targets:
        if target in all_ids:
            if target not in seen:
                raise SourceError("A recent report no longer has a complete recognized financial body")
            continue
        candidates, ids = parse_feed(fetch(str(int(target) + 1)), observed_at)
        pages += 1
        if target not in ids or not any(r["sources"][0]["id"] == target for r in candidates):
            raise SourceError("A recent report is missing or no longer recognized in lookback; review required")
        all_ids.extend(ids)
        for report in candidates:
            key = report["sources"][0]["id"]
            if key not in seen:
                incoming.append(report)
                seen.add(key)
    changed = False
    for report in sorted(incoming, key=lambda r: r["published_at"]):
        changed = merge_report(reports, report, observed_at) or changed
    if not changed:
        return existing, False  # No generated_at/cursor mutation, no publication.
    discovery = {
        "feed_url": FEED_URL, "initialized": True,
        "last_successful_check_in_release": observed_at,
        "last_seen_message_id": str(max(map(int, all_ids))),
        "pages_read": pages, "history_gap": None,
        "revision_coverage": "Latest two known Telegram reports and bounded current-feed catch-up; older edits need explicit re-audit.",
        "status_note": "Check recorded in this release, not live watcher status. Unchanged or failed checks do not update this static file.",
    }
    return finalize(reports, observed_at, discovery), True


def atomic_write(path: Path, data: dict) -> None:
    validate_dataset(data)
    encoded = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("refresh", "import", "verify"))
    parser.add_argument("--output", type=Path, default=Path("public/data/financials.json"))
    parser.add_argument("--input", type=Path, nargs="*", default=[])
    args = parser.parse_args(argv)
    try:
        observed_at = now_utc()
        existing = json.loads(args.output.read_text()) if args.output.exists() else None
        changed = False
        if args.command == "verify":
            if existing is None:
                raise SourceError("No financial dataset to verify")
            validate_dataset(existing)
            data = existing
        elif args.command == "import":
            if not args.input:
                raise SourceError("Import needs public replay JSON inputs")
            reports = copy.deepcopy(existing["reports"]) if existing else []
            paths = []
            for path in args.input:
                paths.extend(sorted(path.glob("*.json")) if path.is_dir() else [path])
            for path in paths:
                report = import_replay(path, observed_at)
                if report:
                    changed = merge_report(reports, report, observed_at) or changed
            data = finalize(reports, observed_at, existing["coverage"].get("discovery") if existing else None) if changed else existing
            if data is None:
                raise SourceError("No recognized public reports in import")
        else:
            if existing is None:
                raise SourceError("Seed a reviewed historical dataset before refreshing")
            data, changed = refresh_dataset(existing, PublicFetcher(), observed_at)
        if changed:
            atomic_write(args.output, data)
        print(json.dumps({"changed": changed, "report_count": data["coverage"]["report_count"], "latest_report_published_at": data["coverage"]["last_report_published_at"], "warnings": data["coverage"]["unresolved_warning_count"]}))
        return 0
    except (SourceError, OSError, ValueError, KeyError, InvalidOperation) as exc:
        print(json.dumps({"changed": False, "error": str(exc), "previous_dataset_retained": True}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
