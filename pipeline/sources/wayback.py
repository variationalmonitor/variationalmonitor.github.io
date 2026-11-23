#!/usr/bin/env python3
"""Archived copies of Variational's public stats API, from the Internet Archive.

The Omni stats API returns the present only. The Wayback Machine saved its earlier endpoint
(`/metadata/stats`) on a few days in 2026, and each capture is a complete response: every
listing's 24h volume, long and short open interest, funding rate and quotes. This command lists
the captures, keeps each response exactly as archived under <store>/wayback/, and writes it in
the shape of today's response into .cache/snapshots_seed/, where the market builder reads it
like any other snapshot.

    /usr/bin/python3 pipeline/sources/wayback.py            fetch what is not yet on disk, convert all
    /usr/bin/python3 pipeline/sources/wayback.py --offline  convert what is on disk

What differs in the archived response, and how it is read:
  tickers        carried no suffix: BTC is BTC-PERP today. Swaps were US100S or USOILP and are
                 US100-SWAP and USOIL-SWAP today; they are matched to today's ticker by listing name.
  contract type  not stated: a swap is a listing named "Swap on ...".
  funding        one annualised rate (`funding_rate`), today's `funding.last.annualized_rate`.
                 A swap's two financing rates did not exist in that response and are left out.
Nothing is estimated. A capture whose totals do not tie to its own listings is refused.
"""
import glob
import gzip
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STORE = os.environ.get("VM_STORE") or os.path.join(ROOT, ".cache", "store")
RAW_DIR = os.path.join(STORE, "wayback")
SEED_DIR = os.path.join(ROOT, ".cache", "snapshots_seed")
LIVE_DIR = os.path.join(ROOT, ".cache", "snapshots")
HOST = "omni-client-api.prod.ap-northeast-1.variational.io"
PATHS = ["/metadata/stats"]
CDX = "http://web.archive.org/cdx/search/cdx?url=%s&output=json&fl=timestamp,statuscode,digest&filter=statuscode:200"
UA = "variational-monitor (historical research)"


def curl(url):
    r = subprocess.run(["curl", "-s", "-m", "90", "-L", "--compressed", "-A", UA, url], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("curl failed (%d) for %s" % (r.returncode, url))
    return r.stdout


def fetch():
    os.makedirs(RAW_DIR, exist_ok=True)
    got = []
    for path in PATHS:
        rows = json.loads(curl(CDX % (HOST + path)) or b"[]")[1:]
        seen = set()
        for ts, _code, digest in rows:
            if digest in seen:
                continue
            seen.add(digest)
            out = os.path.join(RAW_DIR, ts + ".json")
            if not os.path.exists(out):
                body = curl("https://web.archive.org/web/%sid_/https://%s%s" % (ts, HOST, path))
                json.loads(body)                      # an archived error page is not kept
                with open(out, "wb") as fh:
                    fh.write(body)
                got.append(ts)
    return got


def names_today():
    """Swap listing name -> today's ticker, from the newest snapshot the capture loop saved."""
    files = sorted(glob.glob(os.path.join(LIVE_DIR, "*.json.gz")))
    if not files:
        return {}
    with gzip.open(files[-1], "rt", encoding="utf-8") as fh:
        d = json.load(fh)
    return {l["name"]: l["ticker"] for l in d["listings"] if l.get("contract_type") == "swap"}


def convert(v1, ts, swaps):
    listings, unmatched = [], []
    for l in v1["listings"]:
        name = l.get("name") or ""
        swap = name.startswith("Swap on ")
        if swap:
            ticker = swaps.get(name)
            if not ticker:
                ticker = re.sub(r"[SP]$", "", l["ticker"]) + "-SWAP"
                unmatched.append((l["ticker"], name, ticker))
        else:
            ticker = l["ticker"] + "-PERP"
        m = {"ticker": ticker, "contract_type": "swap" if swap else "perpetual", "name": name,
             "mark_price": l.get("mark_price"), "volume_24h": l["volume_24h"],
             "open_interest": l.get("open_interest"), "quotes": l.get("quotes")}
        if not swap and l.get("funding_rate") is not None:
            m["funding"] = {"last": {"annualized_rate": l["funding_rate"]}, "interval_seconds": l.get("funding_interval_s")}
        listings.append(m)
    # the capture must be whole: the stated totals tie to its own listings
    vol = sum(float(l["volume_24h"]) for l in v1["listings"])
    oi = sum(float((l.get("open_interest") or {}).get("long_open_interest") or 0)
             + float((l.get("open_interest") or {}).get("short_open_interest") or 0) for l in v1["listings"])
    tv, to = float(v1["total_volume_24h"]), float(v1["open_interest"])
    if abs(vol - tv) > max(1.0, 1e-6 * tv) or abs(2 * oi - to) > max(1.0, 1e-6 * to) or len(v1["listings"]) != int(v1["num_markets"]):
        raise ValueError("capture %s does not tie to its listings: volume %.2f vs %.2f, dual-sided open interest %.2f vs %.2f, "
                         "%d listings vs %s" % (ts, vol, tv, 2 * oi, to, len(v1["listings"]), v1["num_markets"]))
    out = {k: v1[k] for k in ("cumulative_volume", "total_volume_24h", "open_interest", "tvl", "num_markets")}
    out["loss_refund"] = v1.get("loss_refund")
    out["listings"] = listings
    out["_archive"] = {"source": "Internet Archive", "capture": ts,
                       "url": "https://web.archive.org/web/%s/https://%s%s" % (ts, HOST, PATHS[0]),
                       "note": "earlier response shape, converted by pipeline/sources/wayback.py"}
    return out, unmatched


def main():
    new = [] if "--offline" in sys.argv else fetch()
    swaps = names_today()
    os.makedirs(SEED_DIR, exist_ok=True)
    for path in sorted(glob.glob(os.path.join(RAW_DIR, "*.json"))):
        ts = os.path.basename(path)[:14]
        with open(path, encoding="utf-8") as fh:
            v1 = json.load(fh)
        out, unmatched = convert(v1, ts, swaps)
        name = "%sT%sZ.json.gz" % (ts[:8], ts[8:12])
        with gzip.open(os.path.join(SEED_DIR, name), "wt", encoding="utf-8") as fh:
            json.dump(out, fh, separators=(",", ":"))
        n_swap = sum(1 for l in out["listings"] if l["contract_type"] == "swap")
        print("%s  %s  %d listings (%d swaps)  24h volume %.0f  cumulative %.0f%s%s" % (
            name, "new" if ts in new else "on disk", len(out["listings"]), n_swap, float(out["total_volume_24h"]),
            float(out["cumulative_volume"]),
            "  swaps named by rule: %s" % ", ".join("%s->%s" % (a, c) for a, _b, c in unmatched) if unmatched else "", ""))


if __name__ == "__main__":
    main()
