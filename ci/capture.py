#!/usr/bin/env python3
"""The capture job: one Omni stats snapshot per half hour, then the matched Hyperliquid books, then a commit to
the data branch. Python standard library, curl and git only.

    ci/capture.py tick --store .store          one capture now (fetch, validate, save, capture_hl, commit, push)
    ci/capture.py loop --store .store          capture on every :00 and :30 UTC for a bit over five hours, then return
    ci/capture.py once --store .store          capture the one :00 or :30 boundary that is due (tick.yml), then return
    ci/capture.py once --store .store --shadow the same wait, but nothing is called or written: prints its timing
    ci/capture.py probe --store .store         one look at Telegram: is there a report the store has not seen?

This is the only place that calls the Omni stats API, once per tick, plus at most one request every 10 s for
100 s when that reading is a stale cached copy and few pairs matched (refresh). The API is snapshot-only, so a missed
tick is history lost: a busy answer (HTTP 429, "retry_after_seconds") is retried politely inside a fixed
budget, and a tick that still fails is written to the capture log on the branch so that it is visible.

When the publish switch is on (environment VM_PUBLISH=on, which capture.yml takes from the repository
variable of that name), the loop also
  - asks publish.yml to run after every :00 tick (GitHub's own cron is not reliable enough for hourly);
  - watches Telegram for the biweekly report. Reports have landed every 13 to 15 days (always a Friday,
    15:40 to 18:51 UTC), so the page is read every REPORT_POLL_S seconds only from REPORT_WINDOW_DAYS after
    the last report until the next one appears; a new report dispatches publish.yml at once. Outside that
    window the hourly publish run reads the channel anyway.
The switch is off unless it says exactly "on": capture then runs alone, with no dispatch and no Telegram read
(the poll exists only to start publish.yml).

`once` does what the loop does for one boundary, with the loop's own sync, tick, dispatch and probe (see once()).

Testing without the real API: --url points the fetch at any HTTP server; --no-push commits locally;
--dry-run-dispatch prints the dispatches; --period-s shortens the half hour.
"""
import argparse
import calendar
import datetime as dt
import glob
import gzip
import json
import os
import subprocess
import sys
import tempfile
import time
from decimal import Decimal, InvalidOperation

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import datastore  # noqa: E402

STATS_URL = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/v2/stats"
PLATFORM_KEYS = ("cumulative_volume", "total_volume_24h", "open_interest", "tvl")
FETCH_BUDGET_S = 480          # the 00:00 snapshot must land within ten minutes of midnight to count as the day boundary
MAX_REQUESTS = 12
MAX_WAIT_S = 90
MIN_LISTINGS = 50
LOOP_MINUTES = 305
CATCH_UP_AFTER_S = 31 * 60    # a loop that starts with no snapshot this recent captures at once
REPORT_WINDOW_DAYS = 12.5
REPORT_POLL_S = 180
PUBLISH_SWITCH = "VM_PUBLISH"
REFRESH_SAMPLE_S = 60         # hard stop for sampling the books against a refreshed Omni reading
ONCE_MAX_WAIT_S = 600         # once: wait for the next boundary only when it is at most this far away
ONCE_LATE_S = 1200            # once: else capture a boundary that passed without a snapshot, up to this late
ONCE_SETTLE_S = 1500          # once: push again what a tick could not push, up to this long after the boundary
                              # (the next run's boundary is 1800 s after it)
SYNC_BEFORE_S = 75            # the loop's numbers: bring the checkout to the branch tip in the last 75 s,
TICK_AFTER_S = 1.5            # tick 1.5 s after the boundary,
PROBE_MARGIN_S = 100          # and read Telegram before a boundary only while it is more than 100 s away
SHADOW_CHECK_S = 150          # once --shadow: look for the boundary's snapshot on the branch this long after it
                              # (the loop's commit can land 100 s after the boundary when its refresh round runs)


def publish_on():
    """True only when VM_PUBLISH is exactly "on": the loop then dispatches publish.yml and polls Telegram for it."""
    return os.environ.get(PUBLISH_SWITCH, "").strip() == "on"


def log(msg):
    print("%s %s" % (dt.datetime.utcnow().strftime("%H:%M:%S"), msg), flush=True)


def utcnow():
    return dt.datetime.utcnow()


# ---------------------------------------------------------------- fetch

def http_get(url, timeout=60):
    """(status, body bytes, headers dict) with curl, the client the interim loop has used since 22 Nov 2026."""
    with tempfile.TemporaryDirectory(prefix="vm-capture-") as tmp:
        body, head = os.path.join(tmp, "body"), os.path.join(tmp, "head")
        proc = subprocess.run(["curl", "-sS", "-m", str(timeout), "--compressed", "-o", body, "-D", head,
                               "-w", "%{http_code}", url], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True)
        if proc.returncode != 0:
            return 0, b"", {"error": proc.stderr.strip()[-200:]}
        headers = {}
        if os.path.exists(head):
            with open(head, errors="replace") as fh:
                for line in fh:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
        data = b""
        if os.path.exists(body):
            with open(body, "rb") as fh:
                data = fh.read()
        try:
            return int(proc.stdout.strip() or 0), data, headers
        except ValueError:
            return 0, data, headers


def validate(raw):
    """Parse a stats response. Returns (doc, summary) or raises ValueError saying what is wrong."""
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("not JSON (%s)" % exc)
    if not isinstance(doc, dict):
        raise ValueError("not a JSON object")
    listings = doc.get("listings")
    if not isinstance(listings, list) or len(listings) < MIN_LISTINGS:
        raise ValueError("listings missing or fewer than %d" % MIN_LISTINGS)
    for key in PLATFORM_KEYS:
        try:
            if Decimal(str(doc[key])) < 0:
                raise ValueError("%s is negative" % key)
        except (KeyError, InvalidOperation):
            raise ValueError("%s missing or not a number" % key)
    if not isinstance(doc.get("num_markets"), int):
        raise ValueError("num_markets missing")
    for item in listings:
        if not isinstance(item, dict) or not item.get("ticker") or "contract_type" not in item \
                or "volume_24h" not in item or not isinstance(item.get("open_interest"), dict):
            raise ValueError("a listing lacks ticker, contract_type, volume_24h or open_interest")
    return doc, {"listings": len(listings), "markets": doc["num_markets"], "cumulative_volume": str(doc["cumulative_volume"])}


def retry_after(status, body, headers, attempt):
    """Seconds to wait before the next request."""
    wait = None
    if status == 429:
        try:
            wait = float(json.loads(body.decode("utf-8")).get("retry_after_seconds"))
        except (ValueError, TypeError, AttributeError, UnicodeDecodeError):
            wait = None
        if wait is None:
            try:
                wait = float(headers.get("retry-after"))
            except (TypeError, ValueError):
                wait = None
    if wait is None:
        wait = 5 * attempt
    return max(2.0, min(MAX_WAIT_S, wait + 1.0))


def fetch_stats(url, budget_s=FETCH_BUDGET_S, sleep=time.sleep, timeout=60):
    """(raw bytes | None, info). Polite retries: honours retry_after_seconds, stops at the budget.
    budget_s=0 makes it a single request; `timeout` caps each request (curl -m)."""
    started, waited, problems = time.time(), 0.0, []
    for attempt in range(1, MAX_REQUESTS + 1):
        status, body, headers = http_get(url, timeout)
        if status == 200:
            try:
                _, summary = validate(body)
                return body, dict(summary, attempts=attempt, waited_s=round(waited, 1))
            except ValueError as exc:
                problems.append("200 but %s" % exc)
        else:
            problems.append("HTTP %s%s" % (status or "error", " busy" if status == 429 else ""))
        wait = retry_after(status, body, headers, attempt)
        if attempt == MAX_REQUESTS or time.time() - started + wait > budget_s:
            break
        log("  stats: %s; next request in %.0fs" % (problems[-1], wait))
        sleep(wait)
        waited += wait
    return None, {"attempts": len(problems), "waited_s": round(waited, 1), "problems": problems[-4:]}


# ---------------------------------------------------------------- one tick

def write_log(store, entry):
    path = os.path.join(store, "capture", entry["t"][:10] + ".log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(entry, separators=(",", ":"), sort_keys=True) + "\n")


def quote_staleness(raw, fetched_at, parse_time):
    """Seconds from the newest quotes.updated_at in a stats response to the moment it was fetched; None if none parses."""
    newest = None
    for item in json.loads(raw.decode("utf-8")).get("listings") or []:
        try:
            at = parse_time(item["quotes"]["updated_at"])
        except Exception:
            continue
        newest = at if newest is None or at > newest else newest
    return None if newest is None else fetched_at - newest


def match_count(path, stamp, src):
    """(Omni-quoted, matched on Hyperliquid) over one tick's rows of one src in the day file."""
    quoted = matched = 0
    if os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("snap") == stamp and row.get("src") == src and row.get("omni_bps") is not None:
                    quoted += 1
                    matched += bool(row.get("matched"))
    return quoted, matched


def refresh(store, url, raw, fetched_at, stamp, fetch=fetch_stats, clock=time.time, sleep=time.sleep):
    """When the tick's Omni reading was a stale cached copy and few pairs matched, poll for a fresh copy and
    sample the books once more against it (rows src "refresh", same snap, appended; nothing else is written).
    Rule in venues.json "omni_refresh". Returns the one log text, or None when the rule did not fire.
    Bounded: each poll is one request capped by curl -m at the budget, the sampling by a subprocess timeout."""
    sys.path.insert(0, os.path.join(ROOT, "pipeline"))
    import capture_hl                       # noqa: E402  (parse_time, day_path and the config the sampler uses)
    rule = capture_hl.load_config()["omni_refresh"]
    stale = quote_staleness(raw, fetched_at, capture_hl.parse_time)
    path = capture_hl.day_path(store, stamp)
    quoted, matched = match_count(path, stamp, "live")
    if stale is None or stale <= rule["fresh_s"] or not quoted or matched >= rule["min_matched_share"] * quoted:
        return None
    deadline = fetched_at + rule["budget_s"]
    seen, fresh, slot = [], None, 0
    while fresh is None:
        slot += 1
        at = fetched_at + slot * rule["poll_s"]
        if at >= deadline:
            break
        if clock() > at:
            continue                        # this slot passed while the first sampling ran
        sleep(max(0.0, at - clock()))
        left = deadline - clock()
        if left < 1:
            break
        body, info = fetch(url, budget_s=0, timeout=round(left, 1))
        got = clock()
        if body is None:
            seen.append("fail(%s)" % ",".join(info.get("problems") or ["?"]))
            if any("busy" in p for p in info.get("problems") or []):
                break                       # 429: stop asking this tick
            continue
        age = quote_staleness(body, got, capture_hl.parse_time)
        seen.append("n/a" if age is None else "%.1fs" % age)
        if age is not None and age <= rule["fresh_s"]:
            fresh = body
    text = "refresh: first reading %.1fs stale, matched %d/%d; %d polls, staleness %s; " % (
        stale, matched, quoted, len(seen), " ".join(seen) or "-")
    if fresh is None:
        return text + "no fresh copy (stopped at +%.0fs of %ss), nothing added" % (clock() - fetched_at, rule["budget_s"])
    with tempfile.TemporaryDirectory(prefix="vm-refresh-") as tmp:
        copy = os.path.join(tmp, stamp + ".json")     # the stamp in the name gives the rows the tick's snap
        with open(copy, "wb") as fh:
            fh.write(fresh)
        try:
            proc = subprocess.run([sys.executable or "python3", os.path.join(ROOT, "pipeline", "capture_hl.py"),
                                   "--force", "--src", "refresh", copy], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  universal_newlines=True, env=dict(os.environ, VM_STORE=store), timeout=REFRESH_SAMPLE_S)
            tail = "" if proc.returncode == 0 else " (sampler exit %d: %s)" % (
                proc.returncode, (proc.stdout.strip().splitlines() or ["no output"])[-1][:200])
        except subprocess.TimeoutExpired:
            tail = " (sampler timed out after %ds)" % REFRESH_SAMPLE_S
    after = match_count(path, stamp, "refresh")
    return text + "matched after %d/%d%s" % (after[1], after[0], tail)


def tick(store, url=STATS_URL, push=True, skip_hl=False, budget_s=FETCH_BUDGET_S):
    """One capture. Returns True when a snapshot was saved."""
    raw, info = fetch_stats(url, budget_s)
    fetched_at = time.time()
    now = utcnow()
    entry = dict(info, t=now.strftime("%Y-%m-%dT%H:%M:%SZ"), ok=raw is not None)
    stamp = None
    if raw is not None:
        stamp = now.strftime("%Y%m%dT%H%MZ")
        path = os.path.join(store, "snapshots", stamp + ".json.gz")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            log("  %s already saved; this response is dropped" % stamp)
            entry.update(ok=True, duplicate=True)
        else:
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh, gzip.GzipFile(fileobj=fh, mode="wb", compresslevel=9, mtime=0) as gz:
                gz.write(raw)
            os.replace(tmp, path)
            entry.update(stamp=stamp, bytes=os.path.getsize(path))
            if not skip_hl:
                # venue books at once: every second here widens the gap between the Omni quote and the book
                env = dict(os.environ, VM_STORE=store)
                try:
                    proc = subprocess.run([sys.executable or "python3", os.path.join(ROOT, "pipeline", "capture_hl.py"), path],
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                                          env=env, timeout=180)
                    entry["hl"] = (proc.stdout.strip().splitlines() or ["no output"])[-1][:300]
                    entry["hl_ok"] = proc.returncode == 0
                except subprocess.TimeoutExpired:
                    entry.update(hl="timed out", hl_ok=False)
                try:                        # a stale Omni copy: one more sampling against a fresh one, never fatal
                    text = refresh(store, url, raw, fetched_at, stamp)
                except Exception as exc:
                    text = "refresh failed, tick goes on: %s: %s" % (type(exc).__name__, str(exc)[:200])
                if text:
                    log("  " + text)
                    entry["refresh"] = text
    log("tick %s %s" % (stamp or "FAILED", json.dumps({k: v for k, v in entry.items() if k not in ("t", "stamp")}, sort_keys=True)))
    write_log(store, entry)
    if os.path.isdir(os.path.join(store, ".git")):
        try:
            datastore.commit_push(store, "capture %s" % (stamp or "failed " + entry["t"]), push=push)
        except RuntimeError as exc:
            # the files stay in the checkout and go out with the next tick's commit
            log("  commit or push failed, will retry with the next tick: %s" % exc)
    return raw is not None


# ---------------------------------------------------------------- telegram

def last_full_report(store):
    try:
        with open(os.path.join(store, "reports.json")) as fh:
            times = [r["published_at"] for r in json.load(fh) if r.get("full")]
        return dt.datetime.strptime(max(times), "%Y-%m-%dT%H:%M:%SZ") if times else None
    except (OSError, ValueError, KeyError):
        return None


def in_report_window(store, now=None):
    last = last_full_report(store)
    return last is not None and (now or utcnow()) - last >= dt.timedelta(days=REPORT_WINDOW_DAYS)


def probe(store, fetch=None):
    """Ids of report-like posts on the channel's newest page that the store does not hold. One GET."""
    sys.path.insert(0, os.path.join(ROOT, "pipeline"))
    from sources import telegram            # noqa: E402
    from reports import parse               # noqa: E402
    try:
        with open(os.path.join(store, "telegram", "posts.json")) as fh:
            known = {p["id"] for p in json.load(fh)["posts"]}
    except (OSError, ValueError, KeyError):
        known = set()
    html = (fetch or telegram.fetch_page)()
    return [p["id"] for p in telegram.parse_page(html)
            if p["id"] not in known and parse.is_report(parse.parse_post(p["text"]))]


# ---------------------------------------------------------------- loop

def dispatch(workflow, fields, dry_run):
    cmd = ["gh", "workflow", "run", workflow]
    for k, v in fields.items():
        cmd += ["-f", "%s=%s" % (k, v)]
    if os.environ.get("GITHUB_REPOSITORY"):
        cmd += ["-R", os.environ["GITHUB_REPOSITORY"]]
    if dry_run:
        log("  would dispatch: %s" % " ".join(cmd))
        return True
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    log("  dispatch %s %s: %s" % (workflow, json.dumps(fields), "ok" if proc.returncode == 0 else "FAILED " + proc.stdout.strip()[-200:]))
    return proc.returncode == 0


def sync(store):
    """Bring a clean checkout to the branch tip (publish writes there too).

    commit_push either pushes or leaves the files uncommitted, so a clean checkout whose HEAD differs from the
    branch only ever means that the branch moved.
    """
    if not os.path.isdir(os.path.join(store, ".git")):
        return
    present, deleted = datastore.changed_paths(store)
    if present or deleted:
        return          # an unpushed tick is waiting: the next commit-push reconciles it
    if datastore.git(store, "fetch", "-q", "--depth", "1", datastore.REMOTE, datastore.BRANCH, check=False).returncode == 0:
        if datastore.git(store, "rev-parse", "HEAD").stdout != datastore.git(store, "rev-parse", "FETCH_HEAD").stdout:
            datastore.git(store, "reset", "-q", "--hard", "FETCH_HEAD")


def newest_snapshot(store):
    names = sorted(os.path.basename(p) for p in glob.glob(os.path.join(store, "snapshots", "*.json.gz")))
    try:
        return dt.datetime.strptime(names[-1][:14], "%Y%m%dT%H%MZ") if names else None
    except ValueError:
        return None


def safe_tick(args):
    """One tick that cannot end the loop: an unexpected error is logged and counts as a failed tick."""
    try:
        return tick(args.store, args.url, push=not args.no_push, skip_hl=args.skip_hl)
    except Exception as exc:
        log("tick raised %s: %s" % (type(exc).__name__, exc))
        return False


def loop(args):
    period = args.period_s
    started = time.time()
    publish = publish_on()
    log("publish switch (%s): %s" % (PUBLISH_SWITCH, "on, publish.yml is dispatched" if publish
                                     else "off, capture only: no dispatch, no Telegram poll"))
    seen_reports = set()
    next_probe = 0.0
    newest = newest_snapshot(args.store)
    to_boundary = period - time.time() % period
    if (newest is None or (utcnow() - newest).total_seconds() > CATCH_UP_AFTER_S) and to_boundary > min(600, period / 3):
        log("no snapshot in the last %d minutes: capturing now" % (CATCH_UP_AFTER_S // 60))
        safe_tick(args)
    ticks, retry_at = 0, None
    while True:
        target = (int(time.time() // period) + 1) * period
        synced = False
        while True:
            left = target - time.time()
            if left <= 0:
                break
            if not synced and left <= 75:
                if not args.no_push:
                    sync(args.store)    # so that the commit after the tick is a plain fast-forward
                synced = True
            if retry_at and time.time() >= retry_at:
                retry_at = None         # the tick at the boundary failed: one more try half a period later
                log("retrying the failed tick")
                safe_tick(args)
            if publish and left > 100 and time.time() >= next_probe and not args.no_probe and in_report_window(args.store):
                next_probe = time.time() + REPORT_POLL_S
                try:
                    new = [i for i in probe(args.store) if i not in seen_reports]
                except Exception as exc:        # Telegram trouble must never stop the capture
                    new = []
                    log("  telegram probe failed: %s" % exc)
                if new:
                    log("new report on Telegram: post %s" % ", ".join(map(str, new)))
                    if dispatch("publish.yml", {"reason": "report"}, args.dry_run_dispatch):
                        seen_reports.update(new)
            time.sleep(min(20, max(0.2, left)))
        time.sleep(1.5)                 # the stamp is taken after the response: keep it inside the boundary minute
        ok = safe_tick(args)
        retry_at = None if ok else target + period / 2.0
        ticks += 1
        if publish and (int(target) % 3600 == 0 or args.publish_every_tick):
            dispatch("publish.yml", {"reason": "hourly"}, args.dry_run_dispatch)
        if time.time() - started >= args.minutes * 60:
            break
    log("loop done: %d ticks in %.0f minutes" % (ticks, (time.time() - started) / 60))
    return 0


# ---------------------------------------------------------------- once

def iso(t):
    """Epoch seconds as UTC, ISO 8601 with milliseconds."""
    return dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def hhmm(t):
    return dt.datetime.utcfromtimestamp(t).strftime("%H:%M")


def boundary_stamp(names, boundary, period):
    """The earliest snapshot stamp taken for `boundary` (at or after it, before the next one), else None. A stamp is
    the UTC minute of the response: the 10:00 tick is 20261124T1000Z, a retry of it 20261124T1015Z."""
    found = []
    for name in names:
        try:
            t = calendar.timegm(time.strptime(name[:14], "%Y%m%dT%H%MZ"))
        except ValueError:
            continue
        if boundary <= t < boundary + period:
            found.append(name[:14])
    return min(found) if found else None


def stored_names(store):
    return [os.path.basename(p) for p in glob.glob(os.path.join(store, "snapshots", "*.json.gz"))]


def branch_names(store):
    """Snapshot file names at the tip of the data branch, from one fetch. Only FETCH_HEAD moves: the checkout, HEAD
    and the branch stay as they are. None when the fetch or the listing fails."""
    if not os.path.isdir(os.path.join(store, ".git")):
        return None
    if datastore.git(store, "fetch", "-q", "--depth", "1", datastore.REMOTE, datastore.BRANCH, check=False).returncode != 0:
        return None
    proc = datastore.git(store, "ls-tree", "--name-only", "FETCH_HEAD", "snapshots/", check=False)
    if proc.returncode != 0:
        return None
    return [os.path.basename(line) for line in proc.stdout.splitlines() if line.endswith(".json.gz")]


def choose(now, period, max_wait_s, late_s, holding):
    """(case, boundary, why) for a run starting at `now`:
      a  the next boundary is at most max_wait_s away: wait for it;
      b  else the boundary that just passed, at most late_s ago, has no snapshot: capture it now;
      c  else nothing to do.
    `holding(boundary)` returns the stamp the store holds for that boundary, or None; it is asked only for b or c."""
    nxt = (int(now // period) + 1) * period
    if nxt - now <= max_wait_s:
        return "a", nxt, "the next boundary is %.0f s away" % (nxt - now)
    last = nxt - period
    if now - last > late_s:
        return "c", last, "the %s boundary passed %.0f s ago, more than %.0f s; the next is %.0f s away" % (
            hhmm(last), now - last, late_s, nxt - now)
    held = holding(last)
    if held:
        return "c", last, "the %s boundary already has a snapshot (%s); the next is %.0f s away" % (
            hhmm(last), held, nxt - now)
    return "b", last, "the %s boundary passed %.0f s ago and has no snapshot" % (hhmm(last), now - last)


def wait_until(target, clock, sleep, each=None):
    """Sleep until `target` in steps of at most 20 s, as the loop does. `each(left)` runs before every step."""
    while True:
        left = target - clock()
        if left <= 0:
            return
        if each is not None:
            each(left)
            left = target - clock()     # each() may have taken a while (a sync): do not oversleep the boundary
            if left <= 0:
                return
        sleep(min(20, max(0.2, left)))


def report_probe(args, boundary, clock):
    """The run's one Telegram look (publish switch on, report window open). Never fatal."""
    try:
        if args.no_probe or not in_report_window(args.store, dt.datetime.utcfromtimestamp(clock())):
            return
        new = probe(args.store)
        if not new:
            log("  telegram: no new report")
        elif int(boundary) % 3600 == 0:
            log("new report on Telegram: post %s; the hourly dispatch after the %s tick covers it"
                % (", ".join(map(str, new)), hhmm(boundary)))
        else:
            log("new report on Telegram: post %s" % ", ".join(map(str, new)))
            dispatch_publish("report", args.dry_run_dispatch)
    except Exception as exc:        # Telegram trouble must never stop the capture
        log("  telegram probe failed: %s" % exc)


def gh(*args):
    """(exit code, output) of one gh call; never raises."""
    try:
        proc = subprocess.run(["gh"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, proc.stdout.strip()


def dispatch_publish(reason, dry_run):
    """Dispatch publish.yml. When that fails, undo only GitHub's inactivity disable, which handover.sh undoes for the
    loop: if the workflow's state is exactly disabled_inactivity, enable it and dispatch once more. Any other state
    (a workflow disabled on purpose) is left alone. A successful dispatch reads no state."""
    if dispatch("publish.yml", {"reason": reason}, dry_run):
        return True
    repo = os.environ.get("GITHUB_REPOSITORY")
    rc, state = gh("api", "repos/%s/actions/workflows/publish.yml" % (repo or "{owner}/{repo}"), "--jq", ".state")
    if rc != 0 or state != "disabled_inactivity":
        log("  publish.yml state %s: left alone" % (repr(state) if rc == 0 else "could not be read"))
        return False
    rc, out = gh("workflow", "enable", "publish.yml", *(["-R", repo] if repo else []))
    if rc != 0:
        log("  publish.yml was disabled for inactivity and could not be enabled: %s" % out[-200:])
        return False
    ok = dispatch("publish.yml", {"reason": reason}, dry_run)
    log("  publish.yml was disabled for inactivity: enabled again, dispatch retried: %s" % ("ok" if ok else "FAILED"))
    return ok


def shadow_run(args, case, target, started, why, clock, sleep):
    """The rehearsal: the same wait, then one fetch of the branch SHADOW_CHECK_S after the boundary, one JSON line."""
    log("shadow run, nothing is called or written: case %s, %s UTC boundary: %s" % (case, hhmm(target), why))
    reached = None
    if case == "a":
        wait_until(target, clock, sleep)
        sleep(TICK_AFTER_S)
        reached = clock()           # where the tick would start
    elif case == "b":
        reached = clock()
    wait_until(target + SHADOW_CHECK_S, clock, sleep)
    checked = clock()
    names = branch_names(args.store)
    stamp = None if names is None else boundary_stamp(names, target, args.period_s)
    print(json.dumps({"shadow": True, "case": case, "boundary": iso(target), "started": iso(started),
                      "reached": None if reached is None else iso(reached), "checked": iso(checked),
                      "branch_has_snapshot": None if names is None else stamp is not None,
                      "branch_snapshot": stamp}), flush=True)
    return 0


def settle(args, target, until, clock, sleep):
    """once: a tick whose push failed leaves its files (snapshot, book samples, log line) in the checkout, and no
    later tick takes them along as in the loop. Commit and push them again until the checkout is clean or `until`
    passes, waiting 20 s, 40 s, then 60 s between attempts and never sleeping past `until`; one log line per attempt
    and one for the outcome. True when nothing is left unpushed. A clean checkout costs one git status and no line;
    --no-push, or a store that is not a git checkout, costs nothing."""
    if args.no_push or not os.path.isdir(os.path.join(args.store, ".git")):
        return True
    attempts = 0
    while True:
        try:
            present, deleted = datastore.changed_paths(args.store)
            waiting = "%d paths not on the branch" % (len(present) + len(deleted)) if present or deleted else None
        except Exception as exc:    # a checkout that cannot be read counts as not pushed
            waiting = "the checkout could not be read (%s: %s)" % (type(exc).__name__, str(exc)[:200])
        if waiting is None:
            if attempts:
                log("  settled after %d attempt%s: nothing is left unpushed" % (attempts, "s" if attempts > 1 else ""))
            return True
        if clock() >= until:
            log("  settling stopped at %s UTC: %s" % (dt.datetime.utcfromtimestamp(until).strftime("%H:%M:%S"), waiting))
            return False
        attempts += 1
        sleep(min(20 * attempts, 60, until - clock()))
        try:
            result = datastore.commit_push(args.store, "capture %s, pushed again" % dt.datetime.utcfromtimestamp(
                target).strftime("%Y%m%dT%H%MZ"))
        except Exception as exc:    # RuntimeError once its own retries are spent; nothing may end the run early
            result = "failed, %s: %s" % (type(exc).__name__, str(exc)[:200])
        log("  settle attempt %d (%s): %s" % (attempts, waiting, result))


def once(args, clock=time.time, sleep=time.sleep):
    """Capture the one boundary that is due, with the loop's own sync, safe_tick, dispatch and probe, so that its
    reading cannot be told from one the loop took. The case is picked at the start (choose):
      a  the next :00 or :30 is at most --max-wait-s away: wait for it as the loop does (sync in the last 75 s,
         tick 1.5 s after the boundary);
      b  else the boundary that just passed has no snapshot on the branch and passed at most --late-s ago:
         capture now, since a late reading beats none;
      c  else nothing to do: one log line, exit 0.
    A tick whose push failed leaves its files in the checkout, and unlike the loop no later tick takes them along:
    after every tick they are pushed again (settle) until the checkout is clean or ONCE_SETTLE_S after the boundary
    passes; after a failed tick that has a retry ahead, only until the retry time, so settling never costs the retry.
    After the tick of a :00 boundary, and after settling, publish.yml is dispatched (reason=hourly) when the publish
    switch is on, as the loop does, whether or not the tick saved a snapshot; a failed dispatch undoes only GitHub's
    inactivity disable of publish.yml (dispatch_publish). A failed tick is tried once more at boundary + period/2, as
    in the loop; when that moment had already passed as the tick ended (only a late capture gets there) there is no
    retry, so the run ends before the next boundary, whose run waits behind this one. Exit 1 when no snapshot was
    saved, or when something is still unpushed at the end: that reading is lost.

    Telegram, with the publish switch on and the report window open: one probe per run, before the wait while the
    boundary is more than 100 s away (as the loop), else right after the tick, so it never delays a capture. A new
    report dispatches publish.yml (reason=report) unless the boundary is :00, whose hourly dispatch covers it.

    --shadow is a rehearsal of the timing: no Omni, venue or Telegram call, no snapshot, commit or dispatch, and no
    sync. It waits the same way, makes one fetch of the branch SHADOW_CHECK_S after the boundary and prints one JSON
    line.
    """
    period = args.period_s
    started = clock()

    def resync():
        if args.shadow or args.no_push:
            return      # --no-push commits locally, and a reset to the branch tip would drop them (the loop's rule)
        try:
            sync(args.store)
        except Exception as exc:    # a failed sync costs the fast-forward, never the capture
            log("  sync failed, going on: %s: %s" % (type(exc).__name__, exc))

    def holding(boundary):
        resync()                    # the branch, not a stale checkout, says whether the boundary was taken
        return boundary_stamp(stored_names(args.store), boundary, period)

    case, target, why = choose(started, period, args.max_wait_s, args.late_s, holding)
    if args.shadow:
        return shadow_run(args, case, target, started, why, clock, sleep)
    if case == "c":
        log("nothing to do: %s" % why)
        return 0
    publish = publish_on()
    log("publish switch (%s): %s" % (PUBLISH_SWITCH, "on, publish.yml is dispatched" if publish
                                     else "off, capture only: no dispatch, no Telegram probe"))
    probed = False
    if case == "a":
        log("waiting for the %s UTC boundary: %s" % (hhmm(target), why))
        if target - clock() > PROBE_MARGIN_S:
            if publish:
                report_probe(args, target, clock)
            probed = True
        synced = []

        def before(left):
            if not synced and left <= SYNC_BEFORE_S:
                resync()            # so that the commit after the tick is a plain fast-forward
                synced.append(True)
        wait_until(target, clock, sleep, before)
        sleep(TICK_AFTER_S)         # the stamp is taken after the response: keep it inside the boundary minute
    else:
        log("capturing the %s UTC boundary late: %s" % (hhmm(target), why))
    deadline = target + ONCE_SETTLE_S
    ok = safe_tick(args)
    retry_at = target + period / 2.0
    retry = not ok and clock() < retry_at   # decided before settling, which may run up to the retry time
    clean = settle(args, target, min(deadline, retry_at) if retry else deadline, clock, sleep)
    if publish and int(target) % 3600 == 0:
        dispatch_publish("hourly", args.dry_run_dispatch)
    if publish and not probed:
        report_probe(args, target, clock)
    if not ok:
        if not retry:
            log("the tick failed and its retry time %s UTC has passed: no retry" % dt.datetime.utcfromtimestamp(
                retry_at).strftime("%H:%M:%S"))
        else:
            log("the tick failed: one retry at %s UTC" % dt.datetime.utcfromtimestamp(retry_at).strftime("%H:%M:%S"))
            wait_until(retry_at, clock, sleep)
            log("retrying the failed tick")
            ok = safe_tick(args)
            clean = settle(args, target, deadline, clock, sleep)
    if clean:       # nothing is left behind (a no-op when clean); after a give-up at the deadline there is no more time
        clean = settle(args, target, deadline, clock, sleep)
    if ok and clean:
        log("once done: a snapshot was saved")
    elif ok:
        log("once done: a snapshot was saved, but the reading for the %s UTC boundary could not be pushed and is lost"
            % hhmm(target))
    else:
        log("once done: no snapshot for the %s boundary%s" % (
            hhmm(target), "" if clean else ", and what the run wrote could not be pushed"))
    return 0 if ok and clean else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("tick", "loop", "once", "probe"):
        p = sub.add_parser(name)
        p.add_argument("--store", required=True, help="checkout of the data branch")
        p.add_argument("--url", default=STATS_URL)
        p.add_argument("--no-push", action="store_true")
        p.add_argument("--skip-hl", action="store_true", help="do not call Hyperliquid (tests)")
        if name == "loop":
            p.add_argument("--minutes", type=float, default=LOOP_MINUTES)
            p.add_argument("--period-s", type=int, default=1800)
            p.add_argument("--no-probe", action="store_true")
            p.add_argument("--dry-run-dispatch", action="store_true")
            p.add_argument("--publish-every-tick", action="store_true", help="tests")
        if name == "once":
            p.add_argument("--period-s", type=int, default=1800)
            p.add_argument("--max-wait-s", type=float, default=ONCE_MAX_WAIT_S,
                           help="wait for the next boundary only when it is at most this far away")
            p.add_argument("--late-s", type=float, default=ONCE_LATE_S,
                           help="else capture the boundary that passed without a snapshot, up to this late")
            p.add_argument("--shadow", action="store_true",
                           help="rehearse the timing: no Omni, venue or Telegram call, nothing written, one JSON line")
            p.add_argument("--no-probe", action="store_true")
            p.add_argument("--dry-run-dispatch", action="store_true")
    args = parser.parse_args()
    if args.cmd == "tick":
        return 0 if safe_tick(args) else 1
    if args.cmd == "once":
        return once(args)
    if args.cmd == "probe":
        new = probe(args.store)
        print("window=%s new=%s" % ("open" if in_report_window(args.store) else "closed", ",".join(map(str, new)) or "none"))
        return 0
    return loop(args)


if __name__ == "__main__":
    sys.exit(main())
