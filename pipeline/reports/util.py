"""Shared paths and small helpers for the reports, token and thesis package."""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STORE = Path(os.environ.get("VM_STORE") or ROOT / ".cache" / "store")
SITE_DATA = Path(os.environ.get("VM_SITE_DATA") or ROOT / "site" / "data")
CONFIG = ROOT / "pipeline" / "config"
TELEGRAM_CACHE = ROOT / ".cache" / "telegram"   # pre-fetched channel, seeds the store once


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str) -> datetime:
    """Parse '2026-11-22T08:30:00Z' (or with an offset) to an aware datetime."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def read_json(path: Path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def read_json_or_none(path: Path):
    try:
        return read_json(path)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_json(path: Path, obj, compact: bool = True) -> None:
    """Atomic write. NaN and Infinity are refused (they are not JSON)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        if compact:
            json.dump(obj, fh, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        else:
            json.dump(obj, fh, ensure_ascii=False, indent=1, allow_nan=False)
    os.replace(tmp, path)


def money(value):
    """USD rounded to cents; whole amounts stay integers."""
    if value is None:
        return None
    r = round(float(value), 2)
    return int(r) if r == int(r) else r


def usd(value) -> str:
    """'$1,374,753' / '−$36,821' for notes and the console table."""
    if value is None:
        return "n/a"
    v = money(value)
    body = "{:,}".format(abs(v)) if isinstance(v, int) else "{:,.2f}".format(abs(v))
    return ("−$" if v < 0 else "$") + body
