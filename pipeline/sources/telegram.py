"""Variational's public Telegram channel (t.me/s/variational_io), stdlib only.

The channel mirrors every Discord announcement and X post. This module keeps a
store of all posts (`<store>/telegram/posts.json`) and refreshes it
incrementally from the public web view, walking back with `?before=<id>`.

Store:
  posts.json        {"channel", "posts": [{id, time, text, links, edited,
                                           first_seen, revisions:[{seen_at, text}]}]}
                    rewritten only when a post is added or revised
  state.json        {"checked_at", "pages"}: the last successful refresh
  page_latest.html  the newest page as served at the last refresh (overwritten)
`text` is the message body only (no link preview, reactions or view counter).
A post whose text changes after it was first stored keeps the old text in
`revisions`; nothing is ever dropped.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CHANNEL = "variational_io"
FEED_URL = "https://t.me/s/" + CHANNEL
POST_URL = "https://t.me/" + CHANNEL + "/{id}"
USER_AGENT = "Mozilla/5.0"
MAX_BYTES = 2_000_000


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_z(value: str) -> str:
    """'2026-05-29T14:51:40+00:00' -> '2026-05-29T14:51:40Z' (always UTC)."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("timestamp without zone: %r" % value)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _PageParser(HTMLParser):
    """Extract {id, time, text, links, edited} for each message on a page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.posts: list[dict] = []
        self.cur: dict | None = None
        self.text_depth = 0          # >0 while inside the message text div
        self.in_date_anchor = False
        self.in_meta = False
        self.meta_depth = 0

    @staticmethod
    def _classes(attrs) -> set:
        return set((dict(attrs).get("class") or "").split())

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = self._classes(attrs)
        if tag == "div" and a.get("data-post", "").startswith(CHANNEL + "/"):
            self._close()
            self.cur = {"id": int(a["data-post"].split("/")[1]), "time": None,
                        "parts": [], "links": [], "edited": False}
            return
        if self.cur is None:
            return
        if self.text_depth:
            if tag == "div":
                self.text_depth += 1
            elif tag == "br":
                self.cur["parts"].append("\n")
            elif tag == "a" and a.get("href"):
                self.cur["links"].append(a["href"])
            return
        if tag == "div" and "js-message_text" in cls and not self.cur["parts"]:
            self.text_depth = 1
        elif tag == "a" and "tgme_widget_message_date" in cls:
            self.in_date_anchor = True
        elif tag == "time" and self.in_date_anchor and a.get("datetime"):
            self.cur["time"] = _iso_z(a["datetime"])
        elif tag == "span" and "tgme_widget_message_meta" in cls:
            self.in_meta = True
            self.meta_depth = 1
        elif tag == "span" and self.in_meta:
            self.meta_depth += 1

    def handle_startendtag(self, tag, attrs):
        if tag == "br" and self.cur is not None and self.text_depth:
            self.cur["parts"].append("\n")

    def handle_endtag(self, tag):
        if self.cur is None:
            return
        if self.text_depth:
            if tag == "div":
                self.text_depth -= 1
            return
        if tag == "a":
            self.in_date_anchor = False
        elif tag == "span" and self.in_meta:
            self.meta_depth -= 1
            if self.meta_depth <= 0:
                self.in_meta = False

    def handle_data(self, data):
        if self.cur is None:
            return
        if self.text_depth:
            self.cur["parts"].append(data)
        elif self.in_meta and "edited" in data:
            self.cur["edited"] = True

    def _close(self):
        if self.cur is not None:
            text = "".join(self.cur.pop("parts")).replace("\xa0", " ")
            self.cur["text"] = text.strip()
            self.posts.append(self.cur)
            self.cur = None

    def close(self):
        super().close()
        self._close()


def parse_page(html: str) -> list[dict]:
    """All messages on one web-view page, oldest first."""
    parser = _PageParser()
    parser.feed(html)
    parser.close()
    out = []
    for post in parser.posts:
        if post["time"] is None:
            raise ValueError("post %s has no timestamp" % post["id"])
        out.append(post)
    return sorted(out, key=lambda p: p["id"])


def fetch_page(before: int | None = None, timeout: int = 30) -> str:
    url = FEED_URL if before is None else "%s?before=%d" % (FEED_URL, before)
    last = None
    for attempt in range(3):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=timeout) as resp:
                body = resp.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError("telegram page too large")
            return body.decode("utf-8")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("telegram fetch failed: %s (%s)" % (url, last))


# ---------------------------------------------------------------- the store

def _store_file(store: Path) -> Path:
    return store / "telegram" / "posts.json"


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def _merge(known: dict, fresh: list[dict], seen_at: str) -> tuple[int, int]:
    """Merge parsed posts into `known` (id -> post). Returns (new, revised)."""
    new = revised = 0
    for post in fresh:
        old = known.get(post["id"])
        if old is None:
            known[post["id"]] = {
                "id": post["id"], "time": post["time"], "text": post["text"],
                "links": post["links"], "edited": post["edited"],
                "first_seen": seen_at, "revisions": [],
            }
            new += 1
        elif old["text"] != post["text"]:
            old["revisions"].append({"seen_at": seen_at, "text": old["text"]})
            old["text"] = post["text"]
            old["links"] = post["links"]
            old["edited"] = True
            revised += 1
        else:
            old["edited"] = old["edited"] or post["edited"]
    return new, revised


def seed_from_cache(store: Path, cache_dir: Path) -> dict:
    """Build the store from the raw pages cached in `.cache/telegram/`."""
    pages = sorted(cache_dir.glob("page_*.html"))
    if not pages:
        raise FileNotFoundError("no cached telegram pages in %s" % cache_dir)
    known: dict = {}
    for page in pages:
        seen_at = datetime.fromtimestamp(page.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _merge(known, parse_page(page.read_text(encoding="utf-8")), seen_at)
    newest = max(datetime.fromtimestamp(p.stat().st_mtime, timezone.utc) for p in pages)
    data = {"channel": CHANNEL, "posts": [known[i] for i in sorted(known)]}
    _write_json(_store_file(store), data)
    _write_json(_state_file(store), {"checked_at": newest.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                     "pages": len(pages), "seeded_from": str(cache_dir)})
    return data


def _state_file(store: Path) -> Path:
    return store / "telegram" / "state.json"


def checked_at(store: Path):
    """Time of the last successful refresh (or of the seeding cache), or None."""
    try:
        with open(_state_file(store), encoding="utf-8") as fh:
            return json.load(fh).get("checked_at")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def load(store: Path, cache_dir: Path | None = None) -> dict:
    """Read the store; seed it from the local cache the first time."""
    path = _store_file(store)
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    if cache_dir is not None and cache_dir.exists():
        return seed_from_cache(store, cache_dir)
    return {"channel": CHANNEL, "posts": []}


def refresh(store: Path, cache_dir: Path | None = None, lookback_pages: int = 2,
            max_pages: int = 60, pause: float = 1.0) -> dict:
    """Fetch the newest pages and merge them into the store.

    Walks back from the newest page until a page holds no unknown post and at
    least `lookback_pages` pages were read (so recent edits are picked up).
    With an empty store it walks the whole channel. Returns a summary.
    """
    data = load(store, cache_dir)
    known = {p["id"]: p for p in data["posts"]}
    seen_at = _utc_now()
    before = None
    pages = new = revised = 0
    while pages < max_pages:
        html = fetch_page(before)
        if pages == 0:
            raw = store / "telegram" / "page_latest.html"
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_text(html, encoding="utf-8")
        posts = parse_page(html)
        pages += 1
        if not posts:
            break
        unknown = sum(1 for p in posts if p["id"] not in known)
        n, r = _merge(known, posts, seen_at)
        new += n
        revised += r
        lowest = posts[0]["id"]
        if lowest <= 1 or (unknown == 0 and pages >= lookback_pages):
            break
        before = lowest
        time.sleep(pause)
    if new or revised or not _store_file(store).exists():
        _write_json(_store_file(store), {"channel": CHANNEL, "posts": [known[i] for i in sorted(known)]})
    _write_json(_state_file(store), {"checked_at": seen_at, "pages": pages})
    return {"pages": pages, "new": new, "revised": revised,
            "posts": len(known), "checked_at": seen_at}
