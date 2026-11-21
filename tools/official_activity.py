#!/usr/bin/env python3
"""Dated first-party report activity; never reconstructed daily venue history.

Import reviewed public X replays and bounded official Telegram HTML captures.
Refresh reuses the existing financial watcher's public feed/cursor/lookback.
Legacy revenue vocabulary is retained separately from modern financial totals.
"""
from __future__ import annotations
import argparse
import copy
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlparse, parse_qs
try:
    from . import financials as f
except ImportError:
    import financials as f

METRICS = ("cumulative_volume", "dual_sided_oi", "tvl", "tokens_listed", "markets", "total_trades_placed", "daily_active_users_week_average", "weekly_active_users", "total_rewards_claimed", "total_losses_refunded", "total_referral_rewards_claimed")
LEGACY = ("revenue_2w", "average_daily_revenue", "revenue_directed_to_olp", "revenue_directed_to_loss_refund_pool", "revenue_directed_to_referral_rewards_pool", "revenue_directed_to_protocol_treasury", "total_olp_pnl")
COUNTS = {"tokens_listed", "markets", "total_trades_placed", "daily_active_users_week_average", "weekly_active_users"}
LABELS = {
    "total volume traded": "cumulative_volume", "current dual-sided oi": "dual_sided_oi",
    "tvl (excludes some olp hedging accounts)": "tvl", "tvl (excludes olp hedging accounts)": "tvl",
    "total tokens currently listed": "tokens_listed", "total markets currently listed": "markets",
    "total trades placed": "total_trades_placed", "daily active users (avg. over past week)": "daily_active_users_week_average",
    "daily active users (avg over past week)": "daily_active_users_week_average", "weekly active users": "weekly_active_users",
    "total rewards claimed": "total_rewards_claimed", "total losses refunded": "total_losses_refunded",
    "total losses refunded (sunset)": "total_losses_refunded", "total referral rewards claimed": "total_referral_rewards_claimed",
    "2w revenue": "revenue_2w", "average daily revenue": "average_daily_revenue",
    "revenue directed to olp": "revenue_directed_to_olp", "revenue directed to loss refund pool": "revenue_directed_to_loss_refund_pool",
    "revenue directed to referral rewards pool": "revenue_directed_to_referral_rewards_pool",
    "revenue directed to protocol treasury": "revenue_directed_to_protocol_treasury",
    "revenue directed to protocol treasury (just deployed for testing yesterday, numbers under construction, docs soon)": "revenue_directed_to_protocol_treasury",
    "total olp pnl": "total_olp_pnl",
}
MAX_FILE_BYTES = 2_000_000
# This reviewed October 3 report has Product/Community/Metrics sections but
# omits the word biweekly. It is an explicit source identity exception.
LEGACY_HEADER_EXCEPTIONS = {("telegram", "217")}


def kind(field):
    if field == "daily_active_users_week_average": return "reported_daily_average_over_past_week"
    if field == "weekly_active_users": return "reported_weekly_users"
    if field in {"dual_sided_oi", "tvl", "tokens_listed", "markets"}: return "reported_snapshot"
    if field == "average_daily_revenue": return "legacy_reported_daily_average"
    if field in LEGACY and field != "total_olp_pnl": return "legacy_reported_2w_amount"
    return "cumulative_reported"


def parse_field(label, raw, field):
    if not isinstance(raw,str) or not 0 < len(raw) <= 4000:
        raise f.SourceError("Invalid bounded activity field")
    # Strip Markdown emphasis only at the edges; keep every parenthetical
    # qualifier, including explicit manual-deposit exclusions.
    raw=raw.strip().strip("*").strip()
    if field in COUNTS:
        match=re.fullmatch(r"(?P<amount>\d{1,3}(?:,\d{3})+|\d{1,16})(?:\s+(?P<qualifier>.*))?",raw)
        if not match: raise f.SourceError("Invalid reported activity count")
        value=str(int(match["amount"].replace(",",""))); unit="count"; increment="1"
    else:
        match=re.fullmatch(r"(?P<amount>\(?\s*-?\s*\$(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*[KMB]?\)?)(?:\s+(?P<qualifier>.*))?",raw,re.I)
        if not match: raise f.SourceError("Invalid reported activity amount")
        value,unit=f.parse_amount(match["amount"],field)
        scale=re.search(r"(\d+(?:\.\d+)?)\s*([KMB])\)?$",match["amount"],re.I)
        if scale:
            places=len(scale[1].partition(".")[2]); increment=f.decimal_text(Decimal({"K":1000,"M":1_000_000,"B":1_000_000_000}[scale[2].upper()])/(Decimal(10)**places))
        else:
            fraction=re.search(r"\.(\d+)\)?$",match["amount"]);increment=f.decimal_text(Decimal(10)**(-len(fraction[1]))) if fraction else "1"
    qualifier=(match["qualifier"] or "").strip().strip("*").strip() or None
    if qualifier is not None and (len(qualifier)>3000 or not qualifier.startswith("(")):
        raise f.SourceError("Unrecognized source qualifier")
    if field in METRICS and Decimal(value)<0:
        raise f.SourceError("Negative reported activity stock/count")
    return {"value":value,"raw":raw,"label":label,"unit":unit,"kind":kind(field),"precision_increment":increment,"qualifier":qualifier}


def validate_source(source):
    platform=source.get("platform"); identifier=source.get("id")
    if platform not in {"x","telegram"} or not isinstance(identifier,str) or not re.fullmatch(r"\d{1,32}",identifier) or source.get("author")!=f.CHANNEL or source.get("forwarded"):
        raise f.SourceError("Activity source is not an original official post")
    expected=f"https://x.com/{f.CHANNEL}/status/{identifier}" if platform=="x" else f"https://t.me/{f.CHANNEL}/{identifier}"
    if source.get("url")!=expected: raise f.SourceError("Activity source URL outside allowlist")
    f.iso_time(source["published_at"]);f.iso_time(source["observed_at"])
    if f.date_time(source["published_at"])>f.date_time(source["observed_at"]):
        raise f.SourceError("Future source publication")


def content(report):
    return {key:report[key] for key in ("metrics","legacy_financials")}


def values(report):
    return {group:{key:None if field is None else (field["value"],field["unit"],field["kind"],field["qualifier"]) for key,field in report[group].items()} for group in ("metrics","legacy_financials")}


def match_financial(report, financial_data):
    if not financial_data: return None
    source=report["sources"][0]
    candidates=[]
    for existing in financial_data["reports"]:
        if source["url"] in existing["source_urls"]:
            candidates.append(existing);continue
        if abs(f.date_time(source["published_at"])-f.date_time(existing["published_at"]))>timedelta(hours=36):continue
        compared=[]
        for key in ("cumulative_volume","dual_sided_oi","tvl","tokens_listed","markets"):
            field=report["metrics"][key];old=existing["snapshot"].get(key)
            if field is not None and old is not None:compared.append(Decimal(field["value"])==Decimal(old))
        if len(compared)>=2 and all(compared):candidates.append(existing)
    if len(candidates)>1:raise f.SourceError("Ambiguous financial cohort link")
    if not candidates:return None
    existing=candidates[0]
    for key in ("cumulative_volume","dual_sided_oi","tvl","tokens_listed","markets"):
        field=report["metrics"][key];old=existing["snapshot"].get(key)
        if field is not None and old is not None and Decimal(field["value"])!=Decimal(old):
            raise f.SourceError("Activity disagrees with validated financial cohort")
    return existing


def parse_report(text, source, financial_data=None):
    if not isinstance(text,str) or len(text)>f.MAX_REPORT_TEXT:raise f.SourceError("Activity source text exceeds bound")
    if not re.search(r"\bbi[- ]?weekly\s+updates?\b",text,re.I) and (source.get("platform"),source.get("id")) not in LEGACY_HEADER_EXCEPTIONS:return None
    validate_source(source)
    source=copy.deepcopy(source);source.pop("forwarded",None)
    source["published_at"]=f.iso_time(source["published_at"])
    source["source_text_sha256"]=hashlib.sha256(text.encode()).hexdigest()
    metrics={key:None for key in METRICS};legacy={key:None for key in LEGACY}
    for line in text.splitlines():
        line=re.sub(r"^[\s➡️◦•\-*]+","",line).strip()
        if ":" not in line:continue
        label,raw=line.split(":",1)
        label=" ".join(label.replace("*","").split());field=LABELS.get(label.casefold())
        if field is None:continue
        # Some historical channel mirrors print an empty token-count line.
        # It is missing data, never zero or the X mirror's guessed value.
        if not raw.strip().strip("*").strip():continue
        group=metrics if field in METRICS else legacy
        if group[field] is not None:raise f.SourceError("Duplicate reported activity field")
        group[field]=parse_field(label,raw,field)
    if metrics["cumulative_volume"] is None:return None
    if sum(value is not None for value in metrics.values())<2:raise f.SourceError("Incomplete activity disclosure")
    report={"id":f"{source['platform']}:{source['id']}","published_at":source["published_at"],"financial_report_id":None,"source_urls":[source["url"]],"sources":[source],"period_start":None,"period_end":None,"metrics":metrics,"legacy_financials":legacy,"reported_phase":"mainnet_private_beta" if re.search(r"Omni (?:Mainnet )?Private Beta",text,re.I) else None,"revisions":[]}
    match=match_financial(report,financial_data)
    if match:
        report["id"]=match["id"];report["financial_report_id"]=match["id"];report["published_at"]=match["published_at"]
    source["normalized_sha256"]=f.fingerprint(content(report));source["matches_current"]=True
    return report


def merge(reports, incoming, observed_at):
    new=incoming["sources"][0]
    existing=next((r for r in reports if r["id"]==incoming["id"] or new["url"] in r["source_urls"]),None)
    if existing is None:
        nearby=[r for r in reports if abs(f.date_time(r["published_at"])-f.date_time(incoming["published_at"]))<=timedelta(hours=36)]
        for row in nearby:
            if values(row)!=values(incoming):raise f.SourceError("Conflicting activity mirrors in one publication window")
            existing=row
    if existing is None:reports.append(copy.deepcopy(incoming));return True
    old=next((s for s in existing["sources"] if s["url"]==new["url"]),None)
    cohort_date_changed=bool(incoming['financial_report_id'] and incoming['published_at']!=existing['published_at'])
    if old and old["normalized_sha256"]==new["normalized_sha256"] and not cohort_date_changed:return False
    if values(existing)!=values(incoming):
        if old is None:raise f.SourceError("Conflicting activity mirror")
        if len(existing["revisions"])>=f.MAX_REVISIONS:raise f.SourceError("Activity revision budget exhausted")
        existing["revisions"].append({"observed_at":observed_at,"metrics":copy.deepcopy(existing["metrics"]),"legacy_financials":copy.deepcopy(existing["legacy_financials"]),"sources":copy.deepcopy(existing["sources"])})
        existing.update(copy.deepcopy(content(incoming)))
        for s in existing["sources"]:s["matches_current"]=False
    elif old is not None and old["normalized_sha256"]!=new["normalized_sha256"]:
        # Preserve source-label/precision edits too, even if values did not move.
        if len(existing["revisions"])>=f.MAX_REVISIONS:raise f.SourceError("Activity revision budget exhausted")
        existing["revisions"].append({"observed_at":observed_at,**copy.deepcopy(content(existing)),"sources":copy.deepcopy(existing["sources"])})
        existing.update(copy.deepcopy(content(incoming)))
        for s in existing["sources"]:s["matches_current"]=False
    new=copy.deepcopy(new)
    new['matches_current']=new['normalized_sha256']==f.fingerprint(content(existing))
    existing["sources"]=[s for s in existing["sources"] if s["url"]!=new["url"]]+[new]
    existing["source_urls"]=sorted({s["url"] for s in existing["sources"]})
    if existing["financial_report_id"] is None:existing["published_at"]=min(s["published_at"] for s in existing["sources"])
    elif cohort_date_changed:existing['published_at']=incoming['published_at']
    return True


def coverage(reports):
    counts={key:sum(r["metrics"][key] is not None for r in reports) for key in METRICS}
    gaps=[]
    for previous,current in zip(reports,reports[1:]):
        if f.date_time(current["published_at"])-f.date_time(previous["published_at"])>timedelta(days=21):
            gaps.append({"after_report_id":previous["id"],"before_report_id":current["id"],"reason":"More than21days between covered publications; missing coverage or publication gap unresolved."})
    return {"report_count":len(reports),"first_report_published_at":reports[0]["published_at"],"last_report_published_at":reports[-1]["published_at"],"field_counts":counts,"possible_gaps":gaps,"note":"Dated official disclosures, not daily observations or a complete ledger. Linked report dates are canonical financial cohort dates; source_published_at is original post publication. Conservative source_available_at is the earliest retained matching capture, not an assertion that an unversioned historical value was known at original publication. Publication dates are not exact accounting cutoffs. Active-user methodology is source-reported and distinct from on-chain active accounts. Legacy revenue labels are not modern Net Revenue or Net Profit."}


def finalize(reports, observed_at):
    reports=sorted(copy.deepcopy(reports),key=lambda r:(r["published_at"],r["id"]))
    if not reports:raise f.SourceError("No activity reports")
    for report in reports:
        matching=[s for s in report['sources'] if s.get('matches_current') and s.get('normalized_sha256')==f.fingerprint(content(report))]
        if not matching:raise f.SourceError("Missing availability source")
        report['source_published_at']=min(s['published_at'] for s in matching)
        report['source_available_at']=min(s['observed_at'] for s in matching)
    data={"schema_version":1,"generated_at":observed_at,"coverage":coverage(reports),"reports":reports}
    validate(data);return data


def validate(data):
    if data.get("schema_version")!=1 or not isinstance(data.get("reports"),list) or not 1<=len(data["reports"])<=f.MAX_REPORTS:raise f.SourceError("Invalid activity schema")
    f.iso_time(data["generated_at"])
    if data["reports"]!=sorted(data["reports"],key=lambda r:(r["published_at"],r["id"])):raise f.SourceError("Invalid activity chronology")
    ids=set();source_ids=set()
    for report in data["reports"]:
        if not re.fullmatch(r"(?:x|telegram):\d{1,32}",report["id"]) or report["id"] in ids:raise f.SourceError("Invalid/duplicate activity ID")
        ids.add(report["id"]);f.iso_time(report["published_at"])
        if report["period_start"] is not None or report["period_end"] is not None:raise f.SourceError("Unverified activity accounting interval")
        for group,fields in (("metrics",METRICS),("legacy_financials",LEGACY)):
            if set(report[group])!=set(fields):raise f.SourceError("Activity field coverage mismatch")
            for key,field in report[group].items():
                if field is None:continue
                if LABELS.get(field["label"].casefold())!=key or parse_field(field["label"],field["raw"],key)!=field:raise f.SourceError("Activity transcription/precision mismatch")
        if report["metrics"]["cumulative_volume"] is None:raise f.SourceError("Missing cumulative-volume anchor")
        if not report["sources"] or not any(s.get("matches_current") and s.get("normalized_sha256")==f.fingerprint(content(report)) for s in report["sources"]):raise f.SourceError("Missing activity source evidence")
        matching=[s for s in report['sources'] if s.get('matches_current') and s.get('normalized_sha256')==f.fingerprint(content(report))]
        if report.get('source_available_at')!=min(s['observed_at'] for s in matching) or report.get('source_published_at')!=min(s['published_at'] for s in matching):raise f.SourceError("Activity source availability mismatch")
        for source in report["sources"]:
            validate_source(source)
            if source["url"] in source_ids:raise f.SourceError("Source attached to multiple activity reports")
            source_ids.add(source["url"])
            if not re.fullmatch(r"[0-9a-f]{64}",source.get("source_text_sha256","")):raise f.SourceError("Invalid activity source fingerprint")
        if report["source_urls"]!=sorted({s["url"] for s in report["sources"]}):raise f.SourceError("Activity URL coverage mismatch")
        if report["financial_report_id"] is not None and report["financial_report_id"]!=report["id"]:raise f.SourceError("Invalid financial cohort identity")
        first_source=min(f.date_time(s['published_at']) for s in report['sources'])
        if report['financial_report_id'] is None and f.date_time(report['published_at'])!=first_source or abs(f.date_time(report['published_at'])-first_source)>timedelta(hours=36):raise f.SourceError("Activity publication not supported by source window")
    if data["coverage"]!=coverage(data["reports"]):raise f.SourceError("Activity coverage summary mismatch")


def feed_reports(html, observed_at, financial_data):
    parser=f.TelegramFeedParser();parser.feed(html);parser.close()
    if parser.current is not None or not parser.posts:raise f.SourceError("Incomplete activity source feed")
    out=[];seen=set()
    for post in parser.posts:
        match=re.fullmatch(rf"{f.CHANNEL}/(\d+)",post["key"])
        if not match or post["owner"]!=f"https://t.me/{f.CHANNEL}" or match[1] in seen:raise f.SourceError("Invalid activity feed origin/duplicate")
        seen.add(match[1])
        if post["forwarded"]:continue
        source={"platform":"telegram","id":match[1],"url":f"https://t.me/{f.CHANNEL}/{match[1]}","author":f.CHANNEL,"published_at":post["published_at"],"observed_at":observed_at,"retrieval_method":"official public Telegram announcements feed"}
        report=parse_report(post["text"],source,financial_data)
        if report:out.append(report)
    return out


def import_replay(path, observed_at, financial_data):
    if path.stat().st_size>MAX_FILE_BYTES:raise f.SourceError("Activity replay exceeds byte budget")
    tweet=json.loads(path.read_text()).get("tweet",{})
    source={"platform":"x","id":tweet.get("id"),"url":tweet.get("url"),"author":tweet.get("author",{}).get("screen_name"),"published_at":tweet.get("created_at",""),"observed_at":observed_at,"retrieval_method":"saved FxTwitter replay of official X post","forwarded":bool(tweet.get("retweet") or tweet.get("retweeted_status"))}
    return parse_report(tweet.get("text",""),source,financial_data)


def refresh(existing, financial_data, fetch, observed_at):
    validate(existing);incoming=[]
    def capture(before=None):
        html=fetch(before);incoming.extend(feed_reports(html,observed_at,financial_data));return html
    f.refresh_dataset(financial_data,capture,observed_at)
    reports=copy.deepcopy(existing["reports"]);changed=False
    for report in incoming:changed=merge(reports,report,observed_at) or changed
    return (finalize(reports,observed_at),True) if changed else (existing,False)


def atomic_write(path,data):
    validate(data);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix=".official-activity-",dir=path.parent)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as stream:
            json.dump(data,stream,ensure_ascii=False,indent=2);stream.write("\n");stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def load_captures(manifest, financial_data):
    entries=json.loads(manifest.read_text());out=[]
    if not isinstance(entries,list) or not 1<=len(entries)<=30:raise f.SourceError("Capture manifest exceeds bounded import")
    for entry in entries:
        filename=entry["file"]
        if Path(filename).name!=filename or not filename.endswith('.html'):raise f.SourceError("Unsafe capture path")
        parsed=urlparse(entry["url"]);query=parse_qs(parsed.query)
        if parsed.scheme!='https' or parsed.netloc!='t.me' or parsed.path!=f'/s/{f.CHANNEL}' or set(query)-{'before','q'} or ('before' in query and (len(query['before'])!=1 or not query['before'][0].isdigit())) or ('q' in query and query['q']!=['biweekly']):raise f.SourceError("Capture URL outside official feed")
        body=(manifest.parent/filename).read_bytes()
        if len(body)>f.MAX_RESPONSE_BYTES or hashlib.sha256(body).hexdigest()!=entry['sha256']:raise f.SourceError("Activity capture fingerprint/size mismatch")
        # Historical captures backfill earlier cohorts. Existing financial
        # cohorts use the already reviewed complete X replays; do not replace
        # them with partially transcribed historical Telegram mirrors.
        out.extend(row for row in feed_reports(body.decode('utf-8'),entry['capture_completed_at'],financial_data) if row['financial_report_id'] is None)
    return out


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['import','refresh','verify'])
    parser.add_argument('--output',type=Path,default=Path('public/data/official-activity.json'))
    parser.add_argument('--financials',type=Path,default=Path('public/data/financials.json'))
    parser.add_argument('--input',type=Path,nargs='*',default=[])
    parser.add_argument('--capture-manifest',type=Path)
    args=parser.parse_args(argv)
    try:
        financial_data=json.loads(args.financials.read_text());f.validate_dataset(financial_data)
        existing=json.loads(args.output.read_text()) if args.output.exists() else None
        if existing:validate(existing)
        at=f.now_utc();changed=False
        if args.command=='verify':
            if existing is None:raise f.SourceError("No activity dataset")
            data=existing
        elif args.command=='import':
            reports=copy.deepcopy(existing['reports']) if existing else []
            incoming=[]
            for path in args.input:
                for replay in sorted(path.glob('*.json')) if path.is_dir() else [path]:
                    row=import_replay(replay,at,financial_data)
                    if row:incoming.append(row)
            if args.capture_manifest:incoming.extend(load_captures(args.capture_manifest,financial_data))
            for row in incoming:changed=merge(reports,row,at) or changed
            data=finalize(reports,at) if changed else existing
            if data is None:raise f.SourceError("No recognized activity reports")
        else:
            if existing is None:raise f.SourceError("Seed reviewed historical activity first")
            data,changed=refresh(existing,financial_data,f.PublicFetcher(),at)
        if changed:atomic_write(args.output,data)
        print(json.dumps({'changed':changed,**data['coverage']}));return 0
    except (ValueError,OSError,KeyError,TypeError) as exc:
        print(json.dumps({'changed':False,'error':str(exc),'previous_dataset_retained':True}),file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
