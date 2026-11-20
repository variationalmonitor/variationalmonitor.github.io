"""Bounded reads from the public sources used by the daily collector."""

import json
from datetime import datetime, timezone
from decimal import Decimal
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(NoRedirect)


def now_utc():
    return datetime.now(timezone.utc)


def iso(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(stamp):
    value = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if value.utcoffset() is None:
        raise ValueError("Source timestamp must contain a UTC offset")
    return value


def get_bytes(url, payload=None, *, limit=1_500_000, headers=None):
    request_headers = {"Accept": "application/json, text/plain", "User-Agent": "VariationalMonitor/1.0"}
    request_headers.update(headers or {})
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        request_headers["Content-Type"] = "application/json"
    with OPENER.open(Request(url, data=body, headers=request_headers), timeout=12) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Source response exceeded the size limit")
    return data


def get_json(url, payload=None, *, limit=1_500_000, headers=None):
    return json.loads(get_bytes(url, payload, limit=limit, headers=headers), parse_float=Decimal)


def positive(value):
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("Expected a positive finite value")
    return parsed
