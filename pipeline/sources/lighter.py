"""Lighter public market data: full order books over its websocket, with a REST fallback. Stdlib only.

Books are returned in the shape `sources/hyperliquid.py` walks ({"time": ms, "levels": [bids, asks]} with
"px"/"sz" strings, best first), so the same cost function prices every venue.

Websocket (primary)
  wss://mainnet.zklighter.elliot.ai/stream, channel `order_book/<market_id>`. The first message per channel
  (`subscribed/order_book`) is a full snapshot: every resting price level, exact prices, no aggregation, with
  the server's `timestamp` in milliseconds, which is used as the book time. One connection carries all
  markets; opening it costs weight 90 against the REST rate limit (docs.lighter.xyz, Rate Limits). Python has
  no websocket client in its standard library, so `_Socket` below is a minimal RFC 6455 client (TLS, masked
  client frames, fragmentation, ping/pong): enough to subscribe and read snapshots, nothing more.

REST (fallback, used only when the socket fails)
  GET /api/v1/orderBookOrders?market_id=<id>&limit=250 returns the best 250 resting orders per side (the
  maximum) with no timestamp. The book time is then the response's HTTP `Date` header, which has one-second
  resolution; the endpoint sits behind a CDN that was seen serving the same body for about a second. The time
  is therefore good to roughly two seconds, against a 45-second match window. Books from this path carry
  "src": "rest".
"""
import base64
import email.utils
import json
import os
import socket
import ssl
import struct
import time
import urllib.request

HOST = "mainnet.zklighter.elliot.ai"
REST = "https://" + HOST + "/api/v1"
MAX_MESSAGE = 8_000_000


class _Socket:
    """Just enough of RFC 6455 to read JSON text messages from one TLS websocket."""

    def __init__(self, host, path, deadline):
        self.deadline = deadline
        raw = socket.create_connection((host, 443), timeout=self._left())
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, host, key)).encode())
        self.buffer = b""
        while b"\r\n\r\n" not in self.buffer:
            self._read()
            if len(self.buffer) > 65536:
                raise OSError("oversized handshake")
        head, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status + b" ":
            raise OSError("websocket refused: " + status.decode("latin-1")[:80])

    def _left(self):
        left = self.deadline - time.time()
        if left <= 0:
            raise socket.timeout("deadline passed")
        return left

    def _read(self):
        self.sock.settimeout(self._left())
        chunk = self.sock.recv(262144)
        if not chunk:
            raise OSError("connection closed")
        self.buffer += chunk

    def _take(self, count):
        while len(self.buffer) < count:
            self._read()
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def send(self, text, opcode=1):
        payload = text if isinstance(text, bytes) else text.encode()
        mask = os.urandom(4)
        size = len(payload)
        if size < 126:
            head = bytes([0x80 | opcode, 0x80 | size])
        elif size < 65536:
            head = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", size)
        else:
            head = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", size)
        self.sock.settimeout(self._left())
        self.sock.sendall(head + mask + bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload)))

    def message(self):
        """Next complete text or binary message as bytes; None when the server closes."""
        parts = []
        while True:
            first, second = self._take(2)
            size = second & 0x7F
            if size == 126:
                size = struct.unpack(">H", self._take(2))[0]
            elif size == 127:
                size = struct.unpack(">Q", self._take(8))[0]
            if size > MAX_MESSAGE:
                raise OSError("oversized frame")
            mask = self._take(4) if second & 0x80 else None
            payload = self._take(size)
            if mask:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            opcode = first & 0x0F
            if opcode == 8:
                return None
            if opcode == 9:
                self.send(payload, opcode=10)
                continue
            if opcode == 10:
                continue
            parts.append(payload)
            if sum(len(part) for part in parts) > MAX_MESSAGE:
                raise OSError("oversized message")
            if first & 0x80:
                return b"".join(parts)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def _levels(side, descending):
    """[{price, size}] -> [{"px", "sz"}] best first, dropping empty levels."""
    rows = [{"px": str(level["price"]), "sz": str(level["size"])} for level in side if float(level["size"]) > 0]
    rows.sort(key=lambda level: float(level["px"]), reverse=descending)
    return rows


def ws_books(market_ids, timeout=4.0):
    """{market_id: book} from one websocket connection. Markets that did not answer in time are absent.

    Never raises: a failed connection returns {} (the caller may then use `rest_book`).
    """
    wanted = {str(market) for market in market_ids}
    books = {}
    if not wanted:
        return books
    connection = None
    try:
        connection = _Socket(HOST, "/stream", time.time() + timeout)
        subscribed = False
        while len(books) < len(wanted):
            raw = connection.message()
            if raw is None:
                break
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            kind = message.get("type")
            if kind == "ping":
                connection.send('{"type":"pong"}')
            elif kind == "connected" and not subscribed:
                for market in sorted(wanted, key=int):
                    connection.send(json.dumps({"type": "subscribe", "channel": "order_book/" + market}))
                subscribed = True
            elif kind == "subscribed/order_book":
                market = str(message.get("channel", "")).rpartition(":")[2]
                book = message.get("order_book") or {}
                stamp = message.get("timestamp")
                if market in wanted and market not in books and isinstance(stamp, int) and stamp > 10 ** 12:
                    books[market] = {"time": stamp, "src": "ws",
                                     "levels": [_levels(book.get("bids") or [], True),
                                                _levels(book.get("asks") or [], False)]}
    except Exception:
        pass
    finally:
        if connection is not None:
            connection.close()
    return {int(market): book for market, book in books.items()}


def _get(url, timeout):
    request = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": "variational-monitor"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response), response.headers


def rest_book(market_id, timeout=2.5):
    """Best 250 resting orders per side; book time from the HTTP Date header. None on any failure."""
    try:
        body, headers = _get("%s/orderBookOrders?market_id=%d&limit=250" % (REST, int(market_id)), timeout)
        when = email.utils.parsedate_to_datetime(headers["Date"]).timestamp()
        side = lambda orders, descending: _levels(
            [{"price": order["price"], "size": order["remaining_base_amount"]} for order in orders], descending)
        return {"time": int(when * 1000), "src": "rest",
                "levels": [side(body["bids"], True), side(body["asks"], False)]}
    except Exception:
        return None


def markets(timeout=2.5):
    """{market_id: {"symbol", "status", "market_type", "taker_fee"}} from /orderBooks, or None when it fails."""
    try:
        body, _ = _get(REST + "/orderBooks", timeout)
        return {int(item["market_id"]): {"symbol": item.get("symbol"), "status": item.get("status"),
                                         "market_type": item.get("market_type"), "taker_fee": item.get("taker_fee")}
                for item in body["order_books"]}
    except Exception:
        return None
