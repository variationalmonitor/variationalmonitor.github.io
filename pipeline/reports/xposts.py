"""Variational's X posts of the biweekly update, as a second primary source.

The Telegram channel is the main feed, but it is not complete: the update of
6 Mar 2026 was never mirrored there, and a few Telegram posts print a blank
where the X post has the figure. The X posts listed here are read once through
the public FxTwitter JSON mirror (api.fxtwitter.com) and kept in the store at
`<store>/telegram/x/<status id>.json`; the response is stored unmodified.

A new status id can be appended to STATUS_IDS at any time; the build works
without it (the Telegram post is enough).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ACCOUNT = "variational_io"
API = "https://api.fxtwitter.com/" + ACCOUNT + "/status/{id}"
USER_AGENT = "Mozilla/5.0"

# Biweekly updates on X, 6 Mar 2026 to 13 Nov 2026.
STATUS_IDS = (
    "2029983510824636491",  # 6 Mar 2026, not on Telegram
    "2035011447072440602", "2040164994764124195", "2045195227615613390",
    "2050242620912537798", "2055324287922507828", "2060738032580567428",
    "2065467514486993157", "2070877921158586432", "2075603602081824975",
    "2080694730460151877", "2085772888293228829", "2090861899730812971",
    "2095924511921336444", "2100975039118225440", "2106063274114723997",
    "2111139394547818854", "2116210079758524489", "2121317860740063501",
)


def _path(store: Path, status_id: str) -> Path:
    return store / "telegram" / "x" / (status_id + ".json")


def fetch(store: Path, status_id: str, timeout: int = 30) -> bool:
    """Download one status into the store. Returns True when stored."""
    url = API.format(id=status_id)
    try:
        req = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read(1_000_000)
        data = json.loads(body.decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return False
    tweet = data.get("tweet") or {}
    if str(tweet.get("id")) != status_id:
        return False
    if (tweet.get("author") or {}).get("screen_name", "").lower() != ACCOUNT:
        return False
    path = _path(store, status_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return True


def refresh(store: Path, pause: float = 1.0) -> dict:
    """Fetch every listed status that is not in the store yet."""
    fetched, failed = [], []
    for status_id in STATUS_IDS:
        if _path(store, status_id).exists():
            continue
        (fetched if fetch(store, status_id) else failed).append(status_id)
        time.sleep(pause)
    return {"fetched": fetched, "failed": failed}


def load(store: Path) -> list[dict]:
    """Stored posts as [{id, time, text, url}], oldest first."""
    out = []
    for status_id in STATUS_IDS:
        path = _path(store, status_id)
        if not path.exists():
            continue
        tweet = json.loads(path.read_text(encoding="utf-8"))["tweet"]
        if str(tweet["id"]) != status_id:
            raise ValueError("stored X post %s has the wrong id" % status_id)
        if tweet["author"]["screen_name"].lower() != ACCOUNT:
            raise ValueError("stored X post %s is not from @%s" % (status_id, ACCOUNT))
        text = (tweet.get("raw_text") or {}).get("text") or tweet["text"]
        when = datetime.fromtimestamp(int(tweet["created_timestamp"]), timezone.utc)
        out.append({
            "id": status_id,
            "time": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "text": text,
            "url": "https://x.com/%s/status/%s" % (ACCOUNT, status_id),
        })
    return sorted(out, key=lambda p: p["time"])
