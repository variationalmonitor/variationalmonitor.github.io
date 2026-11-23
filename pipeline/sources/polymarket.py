"""Polymarket: event markets (gamma API) and price history (CLOB), stdlib only.

Store layout (`<store>/polymarket/`):
  events/<slug>.json      latest gamma response: {fetched_at, url, body}
  snapshots.jsonl         one line per fetch: {t, event, markets:{<id>:{p, bid, ask, last, closed}}};
                          lines older than SNAPSHOT_KEEP_DAYS are dropped once their day is sealed
  history/<id>.json       CLOB price history of the YES token: {fetched_at, url, token_id, body}
  history/_status.json    last CLOB attempt per market (time, HTTP status)
  daily.json              sealed daily closes per market: {markets:{<id>:{dates, p, src}}}

`p` is the YES price as Polymarket displays it (`outcomePrices`): the bid-ask
midpoint, or the last trade when the spread is wider than $0.10.

History comes only from our own gamma snapshots: a daily close is the last
snapshot inside the UTC day, one sealed point per day. A day is written to
daily.json once it is over and is never rewritten afterwards; later runs only
add missing days.

The CLOB price-history host is not part of the default build. `fetch_history`
and `update_daily(use_clob=True)` exist only for an explicit `--clob` run; that
host answers HTTP 451 (unavailable for legal reasons) from some countries,
which is recorded and not retried for 24 hours.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
CLOB_HISTORY = "https://clob.polymarket.com/prices-history"
USER_AGENT = "Mozilla/5.0"
EVENT_FDV = "variational-fdv-above-one-day-after-launch"
EVENT_LAUNCH = "will-variational-launch-a-token-in-2025"
CLOB_RETRY_HOURS = 24
SNAPSHOT_KEEP_DAYS = 3
CLOB_FIDELITY_MINUTES = (720, 1440)   # tried in order until a non-empty history comes back


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _dir(store: Path) -> Path:
    return store / "polymarket"


def _read(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def _get(url: str, timeout: int = 30):
    """Returns (status, parsed body or None). Never raises on HTTP errors."""
    try:
        req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read(8_000_000).decode("utf-8"))
    except HTTPError as exc:
        return exc.code, None
    except (URLError, TimeoutError, OSError, ValueError):
        return 0, None


# ------------------------------------------------------------------ events

def fetch_event(store: Path, slug: str) -> bool:
    """Fetch one event from gamma and store it. False leaves the old copy."""
    url = GAMMA_EVENTS + "?" + urlencode({"slug": slug})
    status, body = _get(url)
    if status != 200 or not isinstance(body, list) or len(body) != 1 or body[0].get("slug") != slug:
        return False
    _write(_dir(store) / "events" / (slug + ".json"),
           {"fetched_at": _iso(_now()), "url": url, "body": body})
    return True


def load_event(store: Path, slug: str):
    """{'fetched_at', 'title', 'markets': [...]} from the stored response, or None."""
    wrapper = _read(_dir(store) / "events" / (slug + ".json"))
    if wrapper is None:
        return None
    event = wrapper["body"][0]
    markets = []
    for m in event["markets"]:
        outcomes = json.loads(m["outcomes"])
        prices = json.loads(m["outcomePrices"]) if m.get("outcomePrices") else None
        tokens = json.loads(m["clobTokenIds"]) if m.get("clobTokenIds") else None
        if outcomes[:2] != ["Yes", "No"]:
            raise ValueError("market %s is not a Yes/No market" % m["id"])
        markets.append({
            "id": str(m["id"]),
            "slug": m["slug"],
            "question": m["question"],
            "title": m.get("groupItemTitle"),
            "closed": bool(m.get("closed")),
            "p": float(prices[0]) if prices else None,
            "bid": m.get("bestBid"),
            "ask": m.get("bestAsk"),
            "last": m.get("lastTradePrice"),
            "yes_token": tokens[0] if tokens else None,
            "start": m.get("startDate"),
            "end": m.get("endDate"),
        })
    return {"slug": event["slug"], "title": event["title"],
            "fetched_at": wrapper["fetched_at"], "url": wrapper["url"], "markets": markets}


def append_snapshot(store: Path, events: list[dict]) -> None:
    """Append the stored events' prices to snapshots.jsonl (once per fetch time)."""
    path = _dir(store) / "snapshots.jsonl"
    seen = set()
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    row = json.loads(line)
                    seen.add((row["event"], row["t"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for ev in events:
            if ev is None or (ev["slug"], ev["fetched_at"]) in seen:
                continue
            row = {"t": ev["fetched_at"], "event": ev["slug"],
                   "markets": {m["id"]: {"p": m["p"], "bid": m["bid"], "ask": m["ask"],
                                         "last": m["last"], "closed": m["closed"]}
                               for m in ev["markets"]}}
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")


def load_snapshots(store: Path) -> list[dict]:
    path = _dir(store) / "snapshots.jsonl"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ----------------------------------------------------------------- history

def fetch_history(store: Path, markets: list[dict], pause: float = 0.5) -> dict:
    """Fetch CLOB price history for markets that have none stored yet.

    Stops at the first refusal (HTTP 451/403) and does not retry a refused
    market within CLOB_RETRY_HOURS. Returns {"fetched": n, "status": code|None}.
    """
    status_path = _dir(store) / "history" / "_status.json"
    status = _read(status_path, {})
    now = _now()
    fetched = 0
    last_code = None
    for m in markets:
        if not m.get("yes_token"):
            continue
        path = _dir(store) / "history" / (m["id"] + ".json")
        stored = _read(path)
        if stored is not None and now - _parse(stored["fetched_at"]) < timedelta(hours=CLOB_RETRY_HOURS):
            continue
        prev = status.get(m["id"])
        if prev and prev.get("status") != 200 and now - _parse(prev["checked_at"]) < timedelta(hours=CLOB_RETRY_HOURS):
            last_code = prev.get("status")
            continue
        body = None
        url = None
        for fidelity in CLOB_FIDELITY_MINUTES:
            url = CLOB_HISTORY + "?" + urlencode({"market": m["yes_token"], "interval": "max", "fidelity": fidelity})
            code, body = _get(url)
            last_code = code
            if code != 200:
                body = None
                break
            if isinstance(body, dict) and body.get("history"):
                break
            body = None
        status[m["id"]] = {"checked_at": _iso(now), "status": last_code, "url": url}
        if body is not None:
            _write(path, {"fetched_at": _iso(now), "url": url, "token_id": m["yes_token"], "body": body})
            fetched += 1
        elif last_code in (451, 403):
            for other in markets:          # the whole host refuses: mark all, try nothing else
                status.setdefault(other["id"], {"checked_at": _iso(now), "status": last_code, "url": None})
                status[other["id"]].update({"checked_at": _iso(now), "status": last_code})
            break
        time.sleep(pause)
    _write(status_path, status)
    return {"fetched": fetched, "status": last_code}


def _day(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d")


def update_daily(store: Path, markets: list[dict], today: str | None = None, use_clob: bool = False) -> dict:
    """Add finished UTC days to daily.json; return it. Sealed days never change.

    Closes come from stored gamma snapshots. Only with use_clob=True is a stored
    CLOB history also used, to fill days that have no sealed value yet."""
    today = today or _day(_now())
    path = _dir(store) / "daily.json"
    daily = _read(path, {"markets": {}})
    snapshots = load_snapshots(store)
    changed = False
    for m in markets:
        candidates: dict = {}                      # date -> (time, p, src)
        for row in snapshots:                      # gamma snapshots first
            obs = row["markets"].get(m["id"])
            if obs is None or obs.get("p") is None or obs.get("closed"):
                continue
            t = _parse(row["t"])
            d = _day(t)
            if d not in candidates or t >= candidates[d][0]:
                candidates[d] = (t, float(obs["p"]), "gamma")
        hist = _read(_dir(store) / "history" / (m["id"] + ".json")) if use_clob else None
        if hist is not None:                       # CLOB closes take precedence
            clob: dict = {}
            for point in hist["body"].get("history", []):
                t = datetime.fromtimestamp(int(point["t"]), timezone.utc)
                d = _day(t)
                if d not in clob or t >= clob[d][0]:
                    clob[d] = (t, float(point["p"]), "clob")
            fetched_day = _day(_parse(hist["fetched_at"]))
            for d, value in clob.items():
                if d < fetched_day:                # the fetch day itself was still open
                    candidates[d] = value
        entry = daily["markets"].setdefault(m["id"], {"slug": m["slug"], "dates": [], "p": [], "src": []})
        have = set(entry["dates"])
        add = sorted(d for d in candidates if d < today and d not in have)
        if add:
            rows = list(zip(entry["dates"], entry["p"], entry["src"]))
            rows += [(d, round(candidates[d][1], 4), candidates[d][2]) for d in add]
            rows.sort()
            entry["dates"] = [r[0] for r in rows]
            entry["p"] = [r[1] for r in rows]
            entry["src"] = [r[2] for r in rows]
            changed = True
    if changed or not path.exists():
        _write(path, daily)
    # sealed days no longer need their raw snapshots
    cutoff = (datetime.fromisoformat(today) - timedelta(days=SNAPSHOT_KEEP_DAYS - 1)).strftime("%Y-%m-%d")
    if any(row["t"][:10] < cutoff for row in snapshots):
        snap_path = _dir(store) / "snapshots.jsonl"
        tmp = snap_path.with_name(snap_path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for row in snapshots:
                if row["t"][:10] >= cutoff:
                    fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        os.replace(tmp, snap_path)
    return daily
