#!/usr/bin/env python3
"""Official report reward disclosures, separate from chain claims and P&L totals.

Reuses the bounded official Telegram financial-report discovery and identity
checks. Refresh never changes financials.json. Missing fields remain null;
rounded M/K/B source notation remains explicitly rounded, not exact cash flow.
"""
from __future__ import annotations
import argparse
import copy
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import sys
import tempfile
try:
    from . import financials as f
except ImportError:
    import financials as f

LABELS = {
    "total rewards claimed": "total_rewards_claimed",
    "total losses refunded": "total_losses_refunded",
    "total losses refunded (sunset)": "total_losses_refunded",
    "total referral rewards claimed": "total_referral_rewards_claimed",
    "referral rewards": "referral_rewards",
    "loss refunds": "loss_refunds",
    "loss refunds (sunset)": "loss_refunds",
}
FIELDS = tuple(dict.fromkeys(LABELS.values()))
STOCKS = FIELDS[:3]


def precision(raw):
    scale=re.search(r"(\d+(?:\.\d+)?)\s*([KMB])\s*\)?$",raw,re.I)
    if not scale: return False,"1"
    places=len(scale[1].partition(".")[2])
    value=Decimal({"K":1000,"M":1_000_000,"B":1_000_000_000}[scale[2].upper()])/(Decimal(10)**places)
    return True,f.decimal_text(value)


def parse(text, source, financial_data):
    report = f.parse_report(text, source)
    if report is None:
        return None
    matching = [r for r in financial_data["reports"] if source["url"] in r["source_urls"]]
    if len(matching) != 1 or f.value_fingerprint(matching[0]) != f.value_fingerprint(report):
        raise f.SourceError("Reward disclosure does not match the validated financial report")
    fields = {key: None for key in FIELDS}
    for line in text.splitlines():
        line = re.sub(r"^[\s➡️◦•\-*]+", "", line).strip()
        if ":" not in line:
            continue
        label, raw = line.split(":", 1)
        label, raw = " ".join(label.split()), raw.strip()
        field = LABELS.get(label.casefold())
        if field is None:
            continue
        if fields[field] is not None:
            raise f.SourceError("Duplicate reward disclosure field")
        value, unit = f.parse_amount(raw, "rewards" if field not in STOCKS else field)
        if field in STOCKS and Decimal(value) < 0:
            raise f.SourceError("Cumulative reward disclosure has negative value")
        rounded,increment=precision(raw)
        fields[field] = {"value": value, "raw": raw, "unit": unit, "label": label, "rounded": rounded, "precision_increment": increment, "kind": "cumulative_reported" if field in STOCKS else "reported_2w_deduction"}
    if not any(fields.values()):
        raise f.SourceError("Recognized report omitted all reward disclosure fields")
    core = matching[0]
    return {"id": core["id"], "published_at": f.iso_time(source["published_at"]), "financial_report_published_at": core["published_at"], "source_url": source["url"], "source_urls": [source["url"]], "period_start": None, "period_end": None, "fields": fields, "sources": [{"url": source["url"], "author": f.CHANNEL, "platform": source["platform"], "id": source["id"], "published_at": f.iso_time(source["published_at"]), "observed_at": source["observed_at"], "retrieval_method": source["retrieval_method"], "disclosure_sha256": f.fingerprint(fields), "matches_current": True}], "warnings": [], "revisions": []}


def import_replay(path, observed_at, financial_data):
    data = json.loads(path.read_text())
    tweet = data.get("tweet", {})
    source = {"platform":"x", "id":tweet.get("id"), "url":tweet.get("url"), "author":tweet.get("author",{}).get("screen_name"), "published_at":tweet.get("created_at", ""), "observed_at":observed_at, "retrieval_method":"saved FxTwitter replay of official X post", "forwarded":bool(tweet.get("retweet") or tweet.get("retweeted_status"))}
    return parse(tweet.get("text", ""), source, financial_data)


def feed_records(html, observed_at, financial_data):
    parser = f.TelegramFeedParser(); parser.feed(html); parser.close()
    if parser.current is not None or not parser.posts:
        raise f.SourceError("Incomplete reward source feed")
    out = []
    for post in parser.posts:
        match = re.fullmatch(rf"{f.CHANNEL}/(\d+)", post["key"])
        if not match or post["owner"] != f"https://t.me/{f.CHANNEL}":
            raise f.SourceError("Invalid reward post origin")
        if post["forwarded"]:
            continue
        source = {"platform":"telegram", "id":match[1], "url":f"https://t.me/{f.CHANNEL}/{match[1]}", "author":f.CHANNEL, "published_at":post["published_at"], "observed_at":observed_at, "retrieval_method":"official public Telegram announcements feed"}
        parsed = parse(post["text"], source, financial_data)
        if parsed: out.append(parsed)
    return out


def merge(reports, incoming, observed_at):
    existing = next((r for r in reports if r["id"] == incoming["id"]), None)
    if existing is None:
        reports.append(copy.deepcopy(incoming)); return True
    incoming_source = incoming["sources"][0]
    previous = next((s for s in existing["sources"] if s["url"] == incoming_source["url"]), None)
    if previous and previous["disclosure_sha256"] == incoming_source["disclosure_sha256"]:
        return False
    if existing["fields"] != incoming["fields"]:
        if previous is None:
            raise f.SourceError("Reward disclosure conflict between independent source mirrors")
        if len(existing["revisions"]) >= f.MAX_REVISIONS:
            raise f.SourceError("Reward disclosure revision budget exhausted")
        existing["revisions"].append({"observed_at":observed_at, "fields":copy.deepcopy(existing["fields"]), "sources":copy.deepcopy(existing["sources"])})
        existing["fields"] = copy.deepcopy(incoming["fields"])
        for source in existing["sources"]: source["matches_current"] = False
        existing["source_url"] = incoming["source_url"]
        existing["published_at"] = incoming["published_at"]
    existing["sources"] = [s for s in existing["sources"] if s["url"] != incoming_source["url"]] + [incoming_source]
    existing["source_urls"] = sorted({s["url"] for s in existing["sources"]})
    return True


def finalize(reports, observed_at):
    reports = sorted(copy.deepcopy(reports), key=lambda r:(r["financial_report_published_at"],r["id"]))
    for report in reports:
        report["warnings"] = []
        values = [report["fields"][field] for field in STOCKS]
        if all(values) and all(not value["rounded"] for value in values):
            residual = Decimal(values[0]["value"])-Decimal(values[1]["value"])-Decimal(values[2]["value"])
            if residual: report["warnings"].append({"code":"reported_rewards_identity", "residual":f.decimal_text(residual), "unit":"$"})
        if any(not source["matches_current"] for source in report["sources"]):
            report["warnings"].append({"code":"older_mirror_version"})
    result = {"schema_version":1, "generated_at":observed_at, "latest_report_id":reports[-1]["id"] if reports else None, "coverage":{"report_count":len(reports), "first_report_published_at":reports[0]["published_at"] if reports else None, "last_report_published_at":reports[-1]["published_at"] if reports else None, "note":"Official source disclosures at publication dates. Cumulative claimed/refunded figures are not summed. Native 2w deductions are not reconstructed daily claims; rounded source notation is retained."}, "reports":reports}
    validate(result)
    return result


def validate(data):
    if data.get("schema_version") != 1 or not isinstance(data.get("reports"), list) or not 1 <= len(data["reports"]) <= f.MAX_REPORTS:
        raise f.SourceError("Invalid reward dataset")
    if data["reports"] != sorted(data["reports"],key=lambda row:(row["financial_report_published_at"],row["id"])):
        raise f.SourceError("Reward report chronology is inconsistent")
    seen = set()
    for report in data["reports"]:
        if report["id"] in seen or set(report["fields"]) != set(FIELDS):
            raise f.SourceError("Invalid reward report identity/fields")
        seen.add(report["id"]); f.iso_time(report["published_at"]); f.iso_time(report["financial_report_published_at"])
        if report.get("period_start") is not None or report.get("period_end") is not None:
            raise f.SourceError("Unverified reward accounting boundary")
        for key, field in report["fields"].items():
            if field is None: continue
            if LABELS.get(field["label"].casefold()) != key:
                raise f.SourceError("Invalid reward label")
            value, unit = f.parse_amount(field["raw"], "rewards" if key not in STOCKS else key)
            if (value,unit) != (field["value"],field["unit"]):
                raise f.SourceError("Reward value differs from source text")
            if precision(field["raw"]) != (field["rounded"],field["precision_increment"]) or field["kind"] != ("cumulative_reported" if key in STOCKS else "reported_2w_deduction"):
                raise f.SourceError("Reward precision/type differs from source")
            if key in STOCKS and Decimal(value)<0:
                raise f.SourceError("Negative cumulative reward value")
        if not report["sources"] or not any(s["matches_current"] and s["disclosure_sha256"] == f.fingerprint(report["fields"]) for s in report["sources"]):
            raise f.SourceError("Missing reward source evidence")
        for source in report["sources"]:
            host = "x.com" if source["platform"] == "x" else "t.me" if source["platform"] == "telegram" else None
            path = f"/{f.CHANNEL}/status/{source['id']}" if source["platform"] == "x" else f"/{f.CHANNEL}/{source['id']}"
            if not host or source["author"] != f.CHANNEL or not isinstance(source["id"],str) or not source["id"].isdigit() or source["url"] != f"https://{host}{path}":
                raise f.SourceError("Reward source outside allowlist")
        if report["source_url"] not in report["source_urls"] or report["source_urls"] != sorted({s["url"] for s in report["sources"]}):
            raise f.SourceError("Invalid reward source links")
        if not any(s["url"]==report["source_url"] and s["matches_current"] for s in report["sources"]):
            raise f.SourceError("Canonical reward source is not current")
    if data["coverage"]["report_count"] != len(seen) or data["latest_report_id"] != data["reports"][-1]["id"]:
        raise f.SourceError("Invalid reward coverage")
    if data["coverage"].get("first_report_published_at") != data["reports"][0]["published_at"] or data["coverage"].get("last_report_published_at") != data["reports"][-1]["published_at"]:
        raise f.SourceError("Invalid reward coverage dates")


def refresh(existing, financial_data, fetch, observed_at):
    validate(existing); incoming=[]
    def capture(before=None):
        html=fetch(before)
        incoming.extend(feed_records(html,observed_at,financial_data))
        return html
    # Reuse cursor/lookback controls; the result is intentionally not written.
    f.refresh_dataset(financial_data, capture, observed_at)
    reports=copy.deepcopy(existing["reports"]); changed=False
    for row in incoming: changed=merge(reports,row,observed_at) or changed
    return (finalize(reports,observed_at),True) if changed else (existing,False)


def atomic_write(path,data):
    validate(data); path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix=".reported-rewards-",dir=path.parent)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as stream:
            json.dump(data,stream,ensure_ascii=False,indent=2); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=["import","refresh","verify"])
    parser.add_argument("--output",type=Path,default=Path("public/data/reported-rewards.json"))
    parser.add_argument("--financials",type=Path,default=Path("public/data/financials.json"))
    parser.add_argument("--input",type=Path,nargs="*",default=[])
    args=parser.parse_args(argv)
    try:
        financial_data=json.loads(args.financials.read_text()); f.validate_dataset(financial_data)
        existing=json.loads(args.output.read_text()) if args.output.exists() else None
        if existing: validate(existing)
        observed_at=f.now_utc(); changed=False
        if args.command=="verify":
            if existing is None: raise f.SourceError("No reward dataset")
            data=existing
        elif args.command=="import":
            reports=copy.deepcopy(existing["reports"]) if existing else []
            for path in args.input:
                for replay in sorted(path.glob("*.json")) if path.is_dir() else [path]:
                    row=import_replay(replay,observed_at,financial_data)
                    if row: changed=merge(reports,row,observed_at) or changed
            data=finalize(reports,observed_at) if changed else existing
            if data is None: raise f.SourceError("No recognized reward reports")
        else:
            if existing is None: raise f.SourceError("Seed reviewed public reward reports before refresh")
            data,changed=refresh(existing,financial_data,f.PublicFetcher(),observed_at)
        if changed: atomic_write(args.output,data)
        print(json.dumps({"changed":changed,"report_count":data["coverage"]["report_count"],"latest_report_published_at":data["coverage"]["last_report_published_at"]}))
        return 0
    except (f.SourceError,OSError,ValueError,KeyError,TypeError) as exc:
        print(json.dumps({"changed":False,"error":str(exc),"previous_dataset_retained":True}),file=sys.stderr)
        return 1


if __name__=="__main__": raise SystemExit(main())
