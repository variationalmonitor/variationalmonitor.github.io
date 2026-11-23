"""Shared constants, RPC client and SQLite schema for the Variational on-chain indexer.

Python 3 standard library only. See findings.md for the evidence behind every constant here.
"""
import gzip
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE_DIR = os.path.join(ROOT, ".cache", "onchain")
DATA_DIR = os.path.join(ROOT, "data", "onchain")
DB_PATH = os.path.join(CACHE_DIR, "onchain.sqlite")
LOCK_PATH = os.path.join(CACHE_DIR, "indexer.lock")

CHAIN = "Arbitrum One (42161)"
START_BLOCK = 300_691_000  # just below the Oracle role setup (300,691,449) and factory deployment
START_DAY = "2025-01-30"
SEGMENT = int(os.environ.get("ONCHAIN_SEGMENT", "250000"))  # blocks per checkpointed segment (about 17 hours); fixed per store

USDC = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
FACTORY = "0x0f820b9afc270d658a9fd7d16b1bdc45b70f074c"
POOL_IMPL = "0x8db6c8b7a085c3839e93eb8dad45b93fb1ef5836"
ORACLE = "0x84be56470d45b7f6629a66a219a38681f6ba6172"
VAULT = "0x74bbbb0e7f0bad6938509dd4b556a39a4db1f2cd"
TREASURY = "0x5e91b40467fb8902c46a7b6cb90482363188d645"
ROUTER_SETTLE = "0x6df2af891379af92bfdc0a97e9feb12e53a047ba"  # batch OLP->pool settlement router
ROUTER_ONBOARD = "0x41d676e5c1a49bef69da0f17cb672c10184a3311"  # create-pool-and-deposit router
OWNER = "0x8e4d1ad423e4f37600cda314fd3d99629ceaeabf"  # factory owner / oracle admin
MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"

# Every address that ever held PROVIDER_ROLE on the Oracle (ProviderAdded events, see findings.md).
PROVIDERS = [
    "0x5da06c12ae6ee41a5ed61d4dec7bb50f8177d097",
    "0x5d5e2c6998239f5da55ff719518fec7d1baae184",
    "0xc47756133753280c37b227c24782984e021c4544",
    "0xdac4938100f09b6759dccdfde73ddeba854ddd63",
    "0x346ba983f8992424d60a964584fa72e6cb2164b5",
    "0xbacc0629c31c3d01ddf9c6e3c96c409996f9a3e0",
    "0x0af556530b34cc0f17320dfae0f9b6d8fa30aa58",
    "0xae2c4950e8b7de9eed802772b4d9d01cbdbb9691",
    "0xd4a300a9a1a314a758915320084fe19aa7fdfeaa",
    "0x6c3d445b40a01ce4af942bd0a0a05ca33ee7e7b2",
    "0xe32f967e34a1750ebacdf72b68c08dec5137dd72",
    "0xe4410a44c8861cfba931a8c485bfc9626625d24a",
    "0xc6cebcbdd3d0e39c2061e825a1b3829bf078da8b",
    "0x8f4db6876059a3c3cca4294f8e74098699d5e716",
    "0x5a03f57d8f38941331b29acee828c6d2208dbc8b",
]
REFERRAL_POOL = "0x6c3d445b40a01ce4af942bd0a0a05ca33ee7e7b2"
LEGACY_FEE_WALLET = "0xc47756133753280c37b227c24782984e021c4544"

# System address set S. Every USDC Transfer with from in S or to in S is indexed.
# Order is fixed: it defines the address ids 1..len(SYSTEM) in the store.
SYSTEM = [VAULT, TREASURY, ORACLE, ROUTER_SETTLE, ROUTER_ONBOARD, OWNER] + PROVIDERS
SYSTEM_LABEL = {
    VAULT: "core_olp_vault", TREASURY: "protocol_treasury", ORACLE: "oracle",
    ROUTER_SETTLE: "settlement_router", ROUTER_ONBOARD: "onboarding_router", OWNER: "owner",
    REFERRAL_POOL: "referral_reward_pool", LEGACY_FEE_WALLET: "loss_refund_pool",
}

T_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
T_POOL_CREATED = "0x0b33722ea9d97f05fb7128e0987e4060c182350308a3d0a19f8ec53512fe1493"
T_DEPOSITED = "0x6f7c6475a03ea665d7b001b4a434f753ce3aeb4927db94002435b25d7a1f65d0"
T_WITHDRAWN = "0x8bb469d5860a7e137df4a637fa11c38b03b3931d2802c0aeecc1542921239dbb"
T_ATOMIC = "0x8ce2f45dc19f8ac1e52e3ef7c23c279a26f1571a7999600919409765a05f1889"
T_BATCH_ATOMIC = "0xfbb0a38cbae996f73af4c8c69cef38216da86a2fca7062916e4b50f892d1df6a"
T_PARTY_ADDED = "0x4e29be303ee87985fe51fe881807224f36af654c51b531b1ca15661249c632c0"
POOL_TOPICS = [T_DEPOSITED, T_WITHDRAWN, T_ATOMIC, T_BATCH_ATOMIC, T_PARTY_ADDED]

EV_DEPOSITED, EV_WITHDRAWN, EV_ATOMIC, EV_BATCH_ATOMIC, EV_PARTY_ADDED = 1, 2, 3, 4, 5

# Provider limits measured 22 Nov 2026 (findings.md). cap = max logs per response,
# span = max block span per stream.
PROVIDERS_RPC = {
    "tenderly": {
        "url": "https://arbitrum.gateway.tenderly.co",
        "cap": 20000,
        "span": {"factory": 20_000_000, "events": 1_000_000, "xfrom": 5_000_000, "xto": 5_000_000},
        "min_interval": 0.30,
    },
    "arbitrum": {
        "url": "https://arb1.arbitrum.io/rpc",
        "cap": 10000,
        "span": {"factory": 10_000_000, "events": 30_000, "xfrom": 5_000_000, "xto": 5_000_000},
        "min_interval": 0.35,
    },
    "sequence": {  # getLogs fails with HTTP 500 "origin request failed" above ~10,000 blocks or ~10,000 logs
        "url": "https://nodes.sequence.app/arbitrum",
        "cap": 8000,
        "span": {"factory": 10_000, "events": 10_000, "xfrom": 10_000, "xto": 10_000},
        "min_interval": 0.35,
    },
    "thirdweb": {  # hard limit of 1,000 blocks per eth_getLogs
        "url": "https://42161.rpc.thirdweb.com",
        "cap": 10000,
        "span": {"factory": 1_000, "events": 1_000, "xfrom": 1_000, "xto": 1_000},
        "min_interval": 0.35,
    },
}
UA = "variational-onchain-indexer/1.0 (python-urllib)"


class RpcSplit(Exception):
    """The provider refused the range (too many logs or too wide): halve it."""


class RpcError(Exception):
    pass


_last_call = {}


def rpc_raw(provider, payload, timeout=90):
    cfg = PROVIDERS_RPC[provider]
    wait = cfg["min_interval"] - (time.time() - _last_call.get(provider, 0))
    if wait > 0:
        time.sleep(wait)
    _last_call[provider] = time.time()
    req = urllib.request.Request(
        cfg["url"], data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", "user-agent": UA, "accept-encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        if resp.headers.get("content-encoding") == "gzip":
            body = gzip.decompress(body)
    return json.loads(body)


# Errors that will not go away on retry with the same provider (pruned state, body size limits).
FATAL_HINTS = ("missing trie node", "is not available", "request body exceeds", "not supported", "does not exist")
SPLIT_HINTS = ("exceeds limit", "more than", "too many", "response size", "are allowed",
               "block range", "limit exceeded", "query timeout", "too large", "timed out", "origin request failed")


def rpc(provider, method, params, timeout=90, tries=7):
    """One JSON-RPC call with polite retry/backoff. Raises RpcSplit when the range must shrink."""
    delay = 1.0
    last = None
    for attempt in range(tries):
        try:
            out = rpc_raw(provider, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout)
        except urllib.error.HTTPError as exc:
            text = ""
            try:
                raw = exc.read()
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                text = raw[:400].decode("utf-8", "replace")
            except Exception:
                pass
            last = "HTTP %s %s" % (exc.code, text)
            low = text.lower()
            if exc.code == 429 and exc.headers.get("x-tdly-egress-remaining") == "0":
                # Tenderly public gateway: 1 GB of response bytes per day, then 429 until the reset
                raise RpcError("%s daily egress quota exhausted (resets at ms %s)" % (provider, exc.headers.get("x-tdly-egress-reset-timestamp")))
            if method == "eth_getLogs" and exc.code in (400, 413, 500) and any(h in low for h in SPLIT_HINTS):
                raise RpcSplit(last)
            if any(h in low for h in FATAL_HINTS):
                raise RpcError("%s %s: %s" % (provider, method, last))
            time.sleep(delay * (3 if exc.code == 429 else 1))
            delay = min(delay * 2, 30)
            continue
        except Exception as exc:  # timeouts, resets, bad JSON
            last = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            time.sleep(delay)
            delay = min(delay * 2, 30)
            continue
        if isinstance(out, dict) and "result" in out and out.get("error") is None:
            return out["result"]
        err = out.get("error") if isinstance(out, dict) else out
        last = json.dumps(err)[:400]
        low = last.lower()
        if method == "eth_getLogs" and any(h in low for h in SPLIT_HINTS):
            raise RpcSplit(last)
        if any(h in low for h in FATAL_HINTS):
            raise RpcError("%s %s: %s" % (provider, method, last))
        time.sleep(delay)
        delay = min(delay * 2, 30)
    raise RpcError("%s %s failed after %d tries: %s" % (provider, method, tries, last))


def rpc_any(method, params, order=("tenderly", "arbitrum", "sequence"), timeout=60):
    last = None
    for provider in order:
        try:
            return rpc(provider, method, params, timeout=timeout, tries=4)
        except (RpcError, RpcSplit) as exc:
            last = exc
    raise RpcError(str(last))


def pad_topic(address):
    return "0x" + "0" * 24 + address[2:].lower()


def topic_addr(topic):
    return bytes.fromhex(topic[26:])


def a2b(address):
    return bytes.fromhex(address[2:])


def b2a(raw):
    return "0x" + bytes(raw).hex()


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS addr(id INTEGER PRIMARY KEY, a BLOB NOT NULL UNIQUE);
-- one row per PoolCreated; id is the pool address id
CREATE TABLE IF NOT EXISTS pools(
  id INTEGER PRIMARY KEY, user INTEGER NOT NULL, creator INTEGER NOT NULL,
  block INTEGER NOT NULL, lidx INTEGER NOT NULL, fee INTEGER NOT NULL, fee_payer INTEGER NOT NULL,
  tx BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS pools_block ON pools(block);
-- other factory logs (ownership, oracle changes) kept verbatim
CREATE TABLE IF NOT EXISTS factory_misc(block INTEGER, lidx INTEGER, topic0 TEXT, raw TEXT, PRIMARY KEY(block, lidx));
-- USDC Transfer logs with from in S or to in S (amount > 0), amounts in micro-USDC
CREATE TABLE IF NOT EXISTS xfer(
  block INTEGER NOT NULL, lidx INTEGER NOT NULL, f INTEGER NOT NULL, t INTEGER NOT NULL, amt INTEGER NOT NULL,
  PRIMARY KEY(block, lidx)) WITHOUT ROWID;
-- pool events emitted by registered pools. kind: 1 Deposited 2 Withdrawn 3 DepositedAtomic
-- 4 BatchDepositedAtomic (one row per item, sub = item index) 5 OtherPartyAdded
CREATE TABLE IF NOT EXISTS ev(
  block INTEGER NOT NULL, lidx INTEGER NOT NULL, sub INTEGER NOT NULL, kind INTEGER NOT NULL,
  pool INTEGER NOT NULL, p1 INTEGER NOT NULL, p2 INTEGER NOT NULL, a1 INTEGER NOT NULL, a2 INTEGER NOT NULL,
  PRIMARY KEY(block, lidx, sub)) WITHOUT ROWID;
-- per (stream, segment) cursor: every block < cur inside the segment is fully indexed
CREATE TABLE IF NOT EXISTS progress(
  stream TEXT NOT NULL, seg INTEGER NOT NULL, cur INTEGER NOT NULL,
  n_req INTEGER NOT NULL DEFAULT 0, n_logs INTEGER NOT NULL DEFAULT 0, n_rows INTEGER NOT NULL DEFAULT 0,
  n_skip INTEGER NOT NULL DEFAULT 0, sum_skip INTEGER NOT NULL DEFAULT 0, n_zero INTEGER NOT NULL DEFAULT 0,
  secs REAL NOT NULL DEFAULT 0, PRIMARY KEY(stream, seg));
-- first block of each UTC day (timestamp >= 00:00:00 UTC), verified against block-1
CREATE TABLE IF NOT EXISTS anchors(day TEXT PRIMARY KEY, block INTEGER NOT NULL, ts INTEGER NOT NULL);
-- Multicall3 balanceOf snapshots (only non-zero balances are stored)
CREATE TABLE IF NOT EXISTS snap(block INTEGER NOT NULL, id INTEGER NOT NULL, bal INTEGER NOT NULL, PRIMARY KEY(block, id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS snap_meta(block INTEGER PRIMARY KEY, ts INTEGER, n_pools INTEGER, total INTEGER, provider TEXT, taken_utc TEXT);
"""


def open_db(path=None):
    path = path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path, timeout=120)
    db.executescript(SCHEMA)
    have = db.execute("SELECT COUNT(*) FROM addr").fetchone()[0]
    if have == 0:
        db.executemany("INSERT INTO addr(id, a) VALUES (?, ?)", [(i + 1, a2b(a)) for i, a in enumerate(SYSTEM)])
        db.commit()
    else:
        rows = dict(db.execute("SELECT id, a FROM addr WHERE id <= ?", (len(SYSTEM),)))
        for i, a in enumerate(SYSTEM):
            if rows.get(i + 1) != a2b(a):
                raise SystemExit("store was built with a different SYSTEM address list; rebuild it")
    return db


def get_meta(db, key, default=None):
    row = db.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(db, key, value):
    db.execute("INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, str(value)))


def system_ids():
    return {a: i + 1 for i, a in enumerate(SYSTEM)}


def utc_iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def day_of(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))
