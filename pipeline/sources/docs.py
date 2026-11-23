"""Variational docs (docs.variational.io/llms-full.txt): $VAR allocation and roadmap.

Store: `<store>/docs/llms-full.txt` (the file as served) and `<store>/docs/meta.json`
({fetched_at, url, sha256}). Parsing reads only the `# $VAR` and `# Roadmap`
sections and fails loudly if their shape changes; nothing is inferred.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

URL = "https://docs.variational.io/llms-full.txt"
ROADMAP_PAGE = "https://docs.variational.io/getting-started/roadmap"
USER_AGENT = "Mozilla/5.0"


def _dir(store: Path) -> Path:
    return store / "docs"


def fetch(store: Path, timeout: int = 60) -> bool:
    """Download llms-full.txt into the store. False leaves the old copy."""
    try:
        req = Request(URL, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read(5_000_000)
        text = body.decode("utf-8")
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return False
    if "# Roadmap" not in text or "# $VAR" not in text:
        return False
    d = _dir(store)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "llms-full.txt.tmp"
    tmp.write_bytes(body)
    os.replace(tmp, d / "llms-full.txt")
    meta = {"fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "url": URL, "sha256": hashlib.sha256(body).hexdigest()}
    (d / "meta.json").write_text(json.dumps(meta, separators=(",", ":")), encoding="utf-8")
    return True


def load(store: Path):
    """(text, meta) from the store, or (None, None)."""
    d = _dir(store)
    try:
        text = (d / "llms-full.txt").read_text(encoding="utf-8")
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None, None
    return text, meta


def _section(text: str, heading: str) -> str:
    """Lines of the page that starts with `# <heading>`, up to the next page."""
    lines = text.split("\n")
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "# " + heading)
    except StopIteration:
        raise ValueError("docs: section %r not found" % heading)
    out = []
    for line in lines[start + 1:]:
        if line.startswith("# "):
            break
        out.append(line)
    return "\n".join(out)


_ALLOC = re.compile(r"^\*\s+(?P<label>[^\[\\]+?)\s*\\?\[(?P<pct>\d+(?:\.\d+)?)%\\?\]\s*:", re.M)
_CHECK = re.compile(r"^\*\s+\[(?P<mark>[xX ])\]\s+(?P<item>.+?)\s*$", re.M)


def allocation(text: str) -> list[dict]:
    """[{label, pct}] from the `$VAR` page: labels in sentence case, pct as a
    fraction of supply (0.32 for the printed 32%), summing to 1."""
    rows = []
    for m in _ALLOC.finditer(_section(text, "$VAR")):
        label = " ".join(m.group("label").split())
        label = label[:1].upper() + label[1:].lower()
        rows.append({"label": label, "pct": round(float(m.group("pct")) / 100, 4)})
    if not rows or abs(sum(r["pct"] for r in rows) - 1) > 1e-9:
        raise ValueError("docs: $VAR allocation does not sum to 100%%: %s" % rows)
    return rows


def roadmap(text: str) -> dict:
    """{'checked', 'total', 'open': [...]} counted over the roadmap checklist."""
    items = [(m.group("mark").lower() == "x", m.group("item")) for m in _CHECK.finditer(_section(text, "Roadmap"))]
    if not items:
        raise ValueError("docs: roadmap checklist not found")
    return {"checked": sum(1 for done, _ in items if done),
            "total": len(items),
            "open": [item for done, item in items if not done]}
