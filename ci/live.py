#!/usr/bin/env python3
"""Read the deployed site. Python standard library only.

    ci/live.py age                      minutes since the deployed build (meta.json built_at); writes age_min and
                                        fresh=1|0 to $GITHUB_OUTPUT (fresh: younger than --fresh-minutes)
    ci/live.py fetch --out qa/out/previous
                                        download the deployed data files the gate compares history against

The base URL is $SITE_URL (default https://variationalmonitor.github.io). `fetch` writes _status.json:
  {"state": "ok"}       files downloaded
  {"state": "none"}     no Variational Monitor v2 build is deployed there (first deploy): the gate skips the comparison
  {"state": "error"}    the site could not be read: the gate treats that as a failure, it never skips silently
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("SITE_URL", "https://variationalmonitor.github.io").rstrip("/")
FILES = ["meta", "events", "volume_daily", "oi_daily", "peers_oi", "markets_latest", "markets_listed", "quote_depth",
         "swap_pairs", "tradfi", "concentration", "execution_venue", "olp_market", "financials", "treasury", "olp_chain",
         "accounts", "pools_snapshot", "tvl", "cohorts", "rewards", "spreads_daily", "token", "research", "market/index"]
ALWAYS = ["BTC-PERP", "ETH-PERP", "SOL-PERP", "HYPE-PERP", "XAU-PERP", "XAU-SWAP", "US500-SWAP"]
SAMPLE = 12


def get(path, tries=3):
    """(status, bytes). 404 is returned as such; other failures are retried."""
    url = "%s/data/%s.json?t=%d" % (BASE, path, int(time.time()))
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return 200, resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return 404, b""
            last = "HTTP %d" % exc.code
        except (urllib.error.URLError, OSError) as exc:
            last = str(exc)
        time.sleep(3 * (attempt + 1))
    return 0, (last or "error").encode()


def live_meta():
    status, body = get("meta")
    if status == 404:
        return "none", None
    if status != 200:
        return "error", None
    try:
        meta = json.loads(body.decode("utf-8"))
    except ValueError:
        return "none", None          # the old site answers with something else
    if not isinstance(meta, dict) or "continuous_start" not in meta or "built_at" not in meta:
        return "none", None
    return "ok", meta


def age(args):
    state, meta = live_meta()
    minutes = 99999
    if state == "ok":
        built = dt.datetime.strptime(meta["built_at"], "%Y-%m-%dT%H:%M:%SZ")
        minutes = int((dt.datetime.utcnow() - built).total_seconds() // 60)
    fresh = 1 if minutes < args.fresh_minutes else 0
    print("deployed build: %s, %s minutes old (%s)" % (state, minutes, "fresh" if fresh else "due"))
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a") as fh:
            fh.write("age_min=%d\nfresh=%d\n" % (minutes, fresh))
    return 0


def fetch(args):
    os.makedirs(os.path.join(args.out, "market"), exist_ok=True)
    state, meta = live_meta()
    doc = {"state": state, "base": BASE, "fetched": [], "missing": [], "errors": []}
    if state == "ok":
        names = list(FILES)
        status, body = get("market/index")
        tickers = []
        if status == 200:
            try:
                tickers = json.loads(body.decode("utf-8")).get("tickers", [])
            except ValueError:
                tickers = []
        # a fixed set plus a sample that rotates with the day, so every listing is compared over time
        day = dt.datetime.utcnow().strftime("%Y-%m-%d")
        ranked = sorted(tickers, key=lambda t: hashlib.sha256((day + t).encode()).hexdigest())
        for t in [t for t in ALWAYS if t in tickers] + ranked[:SAMPLE]:
            if "market/" + t not in names:
                names.append("market/" + t)
        for name in names:
            status, body = get(name)
            if status == 200:
                with open(os.path.join(args.out, name + ".json"), "wb") as fh:
                    fh.write(body)
                doc["fetched"].append(name)
            elif status == 404:
                doc["missing"].append(name)
            else:
                doc["errors"].append("%s: %s" % (name, body.decode("utf-8", "replace")))
        if doc["errors"]:
            doc["state"] = "error"
        doc["built_at"] = meta.get("built_at")
    with open(os.path.join(args.out, "_status.json"), "w") as fh:
        json.dump(doc, fh, indent=1)
    print("previous deployment: %s (%d files, %d missing, %d errors) from %s"
          % (doc["state"], len(doc["fetched"]), len(doc["missing"]), len(doc["errors"]), BASE))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("age")
    p.add_argument("--fresh-minutes", type=int, default=50)
    p = sub.add_parser("fetch")
    p.add_argument("--out", required=True)
    args = parser.parse_args()
    return age(args) if args.cmd == "age" else fetch(args)


if __name__ == "__main__":
    sys.exit(main())
