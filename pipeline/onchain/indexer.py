#!/usr/bin/env python3
"""Variational on-chain indexer (Arbitrum One). Standard library only.

Commands
  backfill   index from the store floor (default: factory deployment) to the finalized head
  update     incremental run from the last checkpoints to the finalized head (same code path)
  status     print checkpoints, coverage and request counts
  anchors    (re)build UTC-midnight block anchors up to the synced block
  snapshot   Multicall3 balanceOf for every pool + system address at a pinned block
  reconcile  ledger-reconstructed balances vs Multicall balances (full, or --delta A B)
  repair     fetch every USDC transfer of pools that failed reconciliation and add the direct ones
  audit      fetch the complete USDC transfer history of sample pools and diff it against the ledger
  verify     re-fetch random ranges from the other provider and diff against the store

Everything is resumable and idempotent: rows are keyed by (block, logIndex) and each
(stream, 250k-block segment) has a cursor that is advanced in the same SQLite transaction as its rows.
"""
import argparse
import fcntl
import multiprocessing as mp
import os
import queue
import random
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

STREAMS = ("events", "xfrom", "xto")
INIT_STEP = {"factory": 4_000_000, "events": 30_000, "xfrom": 8_000, "xto": 8_000}
STOP = False


def log(*parts):
    print(time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), *parts, flush=True)


# ----------------------------------------------------------------------------- filters / decoding
def make_filter(stream, lo, hi):
    flt = {"fromBlock": hex(lo), "toBlock": hex(hi)}
    sys_topics = [C.pad_topic(a) for a in C.SYSTEM]
    if stream == "factory":
        flt["address"] = C.FACTORY
    elif stream == "events":
        flt["topics"] = [C.POOL_TOPICS]
    elif stream == "xfrom":
        flt["address"] = C.USDC
        flt["topics"] = [C.T_TRANSFER, sys_topics]
    elif stream == "xto":
        flt["address"] = C.USDC
        flt["topics"] = [C.T_TRANSFER, None, sys_topics]
    else:
        raise ValueError(stream)
    return flt


def words(data):
    body = data[2:]
    return [body[i:i + 64] for i in range(0, len(body), 64)]


def check_log(entry, lo, hi):
    block = int(entry["blockNumber"], 16)
    if entry.get("removed"):
        raise C.RpcError("removed log returned at block %d" % block)
    if block < lo or block > hi:
        raise C.RpcError("log outside requested range: %d not in [%d, %d]" % (block, lo, hi))
    return block, int(entry["logIndex"], 16)


def decode_xfer(logs, lo, hi, known):
    rows, zero = [], 0
    for entry in logs:
        block, lidx = check_log(entry, lo, hi)
        topics = entry["topics"]
        if entry["address"].lower() != C.USDC or topics[0] != C.T_TRANSFER or len(topics) != 3:
            raise C.RpcError("unexpected log in transfer stream at %d/%d" % (block, lidx))
        amount = int(entry["data"], 16) if len(entry["data"]) > 2 else 0
        if amount == 0:
            zero += 1
            continue
        src, dst = C.topic_addr(topics[1]), C.topic_addr(topics[2])
        rows.append((block, lidx, known.get(src, src), known.get(dst, dst), amount))
    return rows, {"n_zero": zero}


def decode_events(logs, lo, hi, known, pool_map, bulk_payer):
    rows, n_skip, sum_skip = [], 0, 0
    for entry in logs:
        block, lidx = check_log(entry, lo, hi)
        pool = pool_map.get(bytes.fromhex(entry["address"][2:]))
        if pool is None:
            continue  # same topic0 emitted by a contract that is not a registered settlement pool
        topics = entry["topics"]
        t0 = topics[0]
        w = words(entry["data"])
        if t0 == C.T_DEPOSITED or t0 == C.T_WITHDRAWN:
            party = C.topic_addr(topics[2])
            amount = int(w[0], 16)
            if t0 == C.T_DEPOSITED and party == bulk_payer:
                n_skip += 1
                sum_skip += amount
                continue
            kind = C.EV_DEPOSITED if t0 == C.T_DEPOSITED else C.EV_WITHDRAWN
            rows.append((block, lidx, 0, kind, pool, known.get(party, party), 0, amount, 0))
        elif t0 == C.T_ATOMIC:
            p1, p2 = C.topic_addr(topics[2]), C.topic_addr(topics[3])
            rows.append((block, lidx, 0, C.EV_ATOMIC, pool, known.get(p1, p1), known.get(p2, p2),
                         int(w[0], 16), int(w[1], 16)))
        elif t0 == C.T_BATCH_ATOMIC:
            creator = C.topic_addr(topics[2])
            creator_amt = int(w[0], 16)
            base = int(w[1], 16) // 32
            count = int(w[base], 16)
            for i in range(count):
                item = w[base + 1 + 4 * i: base + 5 + 4 * i]
                other = bytes.fromhex(item[0][24:])
                rows.append((block, lidx, i, C.EV_BATCH_ATOMIC, pool, known.get(creator, creator),
                             known.get(other, other), creator_amt, int(item[1], 16)))
        elif t0 == C.T_PARTY_ADDED:
            party = C.topic_addr(topics[2])
            rows.append((block, lidx, 0, C.EV_PARTY_ADDED, pool, known.get(party, party), 0, 0, 0))
    return rows, {"n_skip": n_skip, "sum_skip": sum_skip}


def decode_factory(logs, lo, hi):
    pools, misc = [], []
    for entry in logs:
        block, lidx = check_log(entry, lo, hi)
        if entry["address"].lower() != C.FACTORY:
            raise C.RpcError("unexpected emitter in factory stream")
        topics = entry["topics"]
        if topics[0] != C.T_POOL_CREATED:
            misc.append((block, lidx, topics[0], C.json.dumps(entry, sort_keys=True)))
            continue
        w = words(entry["data"])
        base = int(w[0], 16) // 32
        count = int(w[base], 16)
        others = [bytes.fromhex(x[24:]) for x in w[base + 1: base + 1 + count]]
        if count != 1:
            raise C.RpcError("pool with %d other parties at %d/%d: extend the schema" % (count, block, lidx))
        pools.append((block, lidx, bytes.fromhex(w[1][24:]), others[0], C.topic_addr(topics[1]),
                      int(w[5], 16), bytes.fromhex(w[4][24:]), bytes.fromhex(entry["transactionHash"][2:])))
    return pools, misc


# ----------------------------------------------------------------------------- chunk fetching
COOLDOWN = {}  # provider -> time until which this process avoids it


def fetch_chunk(stream, lo, step, end, order):
    """Fetch [lo, hi] where hi is chosen from step and provider limits. Returns (provider, hi, logs)."""
    last = None
    now = time.time()
    usable = [p for p in order if COOLDOWN.get(p, 0) < now] or list(order)
    for provider in usable:
        cfg = C.PROVIDERS_RPC[provider]
        hi = min(lo + min(step, cfg["span"][stream]) - 1, end)
        try:
            logs = C.rpc(provider, "eth_getLogs", [make_filter(stream, lo, hi)], tries=5)
            return provider, hi, logs
        except C.RpcSplit as exc:
            if hi > lo:
                exc.span = hi - lo + 1
                raise
            COOLDOWN[provider] = time.time() + 300  # refused a single block: provider trouble, not size
            last = exc
        except C.RpcError as exc:
            COOLDOWN[provider] = time.time() + 600
            last = exc
    raise C.RpcError(str(last))


def next_step(step, n_logs, provider, stream):
    cfg = C.PROVIDERS_RPC[provider]
    if n_logs > cfg["cap"] * 0.75:
        return max(1, int(step * 0.6))
    if n_logs < cfg["cap"] * 0.35:
        return min(step * 2, cfg["span"][stream])
    return step


def worker_main(wid, order, job_q, res_q, known, pool_map):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    bulk = C.a2b(C.ROUTER_SETTLE)
    memo = {}  # last good step per stream: neighbouring segments have similar log density
    while True:
        job = job_q.get()
        if job is None:
            return
        stream, seg, cur, end = job
        step = memo.get(stream, C.SEGMENT)
        job_order = order
        try:
            while cur <= end:
                started = time.time()
                n_req = 0
                while True:
                    n_req += 1
                    try:
                        provider, hi, logs = fetch_chunk(stream, cur, step, end, job_order)
                        if stream == "events":
                            rows, stats = decode_events(logs, cur, hi, known, pool_map, bulk)
                        else:
                            rows, stats = decode_xfer(logs, cur, hi, known)
                        break
                    except C.RpcSplit as exc:
                        step = max(1, getattr(exc, "span", min(step, end - cur + 1)) // 2)
                stats.update(n_req=n_req, n_logs=len(logs), secs=time.time() - started, provider=provider)
                res_q.put(("rows", stream, seg, cur, hi, rows, stats))
                step = next_step(min(step, hi - cur + 1) if hi < end else step, len(logs), provider, stream)
                memo[stream] = step
                cur = hi + 1
            res_q.put(("done", stream, seg, wid))
        except Exception as exc:  # report and move on; the cursor stays where it was
            res_q.put(("error", stream, seg, wid, "%s: %s" % (type(exc).__name__, str(exc)[:300])))


# ----------------------------------------------------------------------------- store helpers
class AddrBook:
    def __init__(self, db):
        self.db = db
        self.map = {bytes(a): i for i, a in db.execute("SELECT id, a FROM addr")}
        self.next_id = (db.execute("SELECT MAX(id) FROM addr").fetchone()[0] or 0) + 1
        self.pending = []

    def get(self, raw):
        if isinstance(raw, int):
            return raw
        found = self.map.get(raw)
        if found is None:
            found = self.next_id
            self.next_id += 1
            self.map[raw] = found
            self.pending.append((found, raw))
        return found

    def flush(self):
        if self.pending:
            self.db.executemany("INSERT INTO addr(id, a) VALUES(?, ?)", self.pending)
            self.pending = []


def pin_head():
    block = C.rpc_any("eth_getBlockByNumber", ["finalized", False], order=("arbitrum", "tenderly", "sequence"))
    return int(block["number"], 16), int(block["timestamp"], 16)


def floor_block(db):
    return int(C.get_meta(db, "floor_block", C.START_BLOCK))


def stream_cursor(db, stream, target):
    """Highest block B such that every block in [floor, B] is indexed for this stream (min over segments)."""
    floor = floor_block(db)
    if stream == "factory":  # the registry always starts at the factory deployment
        row = db.execute("SELECT cur FROM progress WHERE stream='factory' AND seg=0").fetchone()
        return (row[0] - 1) if row else C.START_BLOCK - 1
    have = dict(db.execute("SELECT seg, cur FROM progress WHERE stream=?", (stream,)))
    covered = floor - 1
    for seg in range(floor // C.SEGMENT, target // C.SEGMENT + 1):
        seg_hi = min((seg + 1) * C.SEGMENT - 1, target)
        cur = have.get(seg)
        if cur is None:
            return covered
        covered = min(cur - 1, seg_hi)
        if cur <= seg_hi:
            return covered
    return covered


def synced_block(db, target=None):
    target = target if target is not None else int(C.get_meta(db, "target_block", 0))
    return min([stream_cursor(db, s, target) for s in STREAMS] + [stream_cursor(db, "factory", target)])


def sync_factory(db, book, target):
    row = db.execute("SELECT cur FROM progress WHERE stream='factory' AND seg=0").fetchone()
    cur = row[0] if row else C.START_BLOCK
    if row is None:
        db.execute("INSERT INTO progress(stream, seg, cur) VALUES('factory', 0, ?)", (cur,))
    step = INIT_STEP["factory"]
    while cur <= target and not STOP:
        started = time.time()
        n_req = 0
        while True:
            n_req += 1
            try:
                provider, hi, logs = fetch_chunk("factory", cur, step, target, ("tenderly", "arbitrum", "sequence"))
                break
            except C.RpcSplit as exc:
                step = max(1, getattr(exc, "span", step) // 2)
        pools, misc = decode_factory(logs, cur, hi)
        pools.sort()
        for block, lidx, pool, user, creator, fee, fee_payer, tx in pools:
            pid = book.get(pool)
            book.flush()
            db.execute("INSERT OR IGNORE INTO pools(id, user, creator, block, lidx, fee, fee_payer, tx) VALUES(?,?,?,?,?,?,?,?)",
                       (pid, book.get(user), book.get(creator), block, lidx, fee, book.get(fee_payer), tx))
            book.flush()
        db.executemany("INSERT OR IGNORE INTO factory_misc VALUES(?,?,?,?)", misc)
        db.execute("UPDATE progress SET cur=?, n_req=n_req+?, n_logs=n_logs+?, n_rows=n_rows+?, secs=secs+? WHERE stream='factory' AND seg=0",
                   (hi + 1, n_req, len(logs), len(pools), time.time() - started))
        db.commit()
        step = next_step(step, len(logs), provider, "factory")
        cur = hi + 1
    return cur > target


def build_jobs(db, target):
    floor = floor_block(db)
    have = {(s, k): c for s, k, c in db.execute("SELECT stream, seg, cur FROM progress")}
    jobs = []
    for seg in range(floor // C.SEGMENT, target // C.SEGMENT + 1):
        lo = max(seg * C.SEGMENT, floor)
        hi = min((seg + 1) * C.SEGMENT - 1, target)
        for stream in STREAMS:
            cur = have.get((stream, seg))
            if cur is None:
                db.execute("INSERT INTO progress(stream, seg, cur) VALUES(?,?,?)", (stream, seg, lo))
                cur = lo
            if cur <= hi:
                jobs.append((stream, seg, cur, hi))
    db.commit()
    return jobs


def run_sync(db, target, workers_spec):
    """Index all streams up to target. Returns True when every stream reached target."""
    book = AddrBook(db)
    C.set_meta(db, "target_block", target)
    db.commit()
    log("factory: syncing registry to", target)
    if not sync_factory(db, book, target):
        return False
    n_pools = db.execute("SELECT COUNT(*) FROM pools").fetchone()[0]
    log("factory: registry complete,", n_pools, "pools")
    jobs = build_jobs(db, target)
    if not jobs:
        log("nothing to do: all streams already at", target)
        return True
    total_blocks = sum(j[3] - j[2] + 1 for j in jobs)
    log("jobs:", len(jobs), "segments,", total_blocks, "stream-blocks to scan")

    pool_ids = set(i for (i,) in db.execute("SELECT id FROM pools"))
    pool_map = {a: i for a, i in book.map.items() if i in pool_ids}
    known = dict(pool_map)
    for address, sid in C.system_ids().items():
        known[C.a2b(address)] = sid

    ctx = mp.get_context("spawn")
    job_q, res_q = ctx.Queue(), ctx.Queue(maxsize=64)
    for job in jobs:
        job_q.put(job)
    procs = []
    for wid, order in enumerate(workers_spec):
        job_q.put(None)
        proc = ctx.Process(target=worker_main, args=(wid, order, job_q, res_q, known, pool_map), daemon=True)
        proc.start()
        procs.append(proc)

    pending = {(j[0], j[1]) for j in jobs}
    failed = []
    done_blocks = 0
    rows_total = 0
    req_total = 0
    by_provider = {}
    started = last_report = time.time()
    while pending and not STOP:
        try:
            msg = res_q.get(timeout=5)
        except queue.Empty:
            if not any(p.is_alive() for p in procs):
                log("all workers exited with", len(pending), "segments unfinished")
                break
            continue
        kind = msg[0]
        if kind == "rows":
            _, stream, seg, lo, hi, rows, stats = msg
            cur = db.execute("SELECT cur FROM progress WHERE stream=? AND seg=?", (stream, seg)).fetchone()[0]
            if cur != lo:
                raise SystemExit("cursor mismatch for %s/%d: store %d, worker %d" % (stream, seg, cur, lo))
            if stream == "events":
                db.executemany("INSERT OR IGNORE INTO ev VALUES(?,?,?,?,?,?,?,?,?)",
                               [(r[0], r[1], r[2], r[3], r[4], book.get(r[5]), book.get(r[6]), r[7], r[8]) for r in rows])
            else:
                db.executemany("INSERT OR IGNORE INTO xfer VALUES(?,?,?,?,?)",
                               [(r[0], r[1], book.get(r[2]), book.get(r[3]), r[4]) for r in rows])
            book.flush()
            db.execute("UPDATE progress SET cur=?, n_req=n_req+?, n_logs=n_logs+?, n_rows=n_rows+?, n_skip=n_skip+?, sum_skip=sum_skip+?, n_zero=n_zero+?, secs=secs+? WHERE stream=? AND seg=?",
                       (hi + 1, stats["n_req"], stats["n_logs"], len(rows), stats.get("n_skip", 0), stats.get("sum_skip", 0),
                        stats.get("n_zero", 0), stats["secs"], stream, seg))
            db.commit()
            done_blocks += hi - lo + 1
            rows_total += len(rows)
            req_total += stats["n_req"]
            tally = by_provider.setdefault(stats["provider"], [0, 0])
            tally[0] += 1
            tally[1] += stats["n_logs"]
        elif kind == "done":
            pending.discard((msg[1], msg[2]))
        elif kind == "error":
            failed.append(msg)
            pending.discard((msg[1], msg[2]))
            log("segment failed:", msg[1], msg[2], msg[4])
        now = time.time()
        if now - last_report > 30:
            last_report = now
            rate = done_blocks / max(now - started, 1)
            eta = (total_blocks - done_blocks) / rate if rate > 0 else 0
            frontier = synced_block(db, target)
            log("progress: %.2f%% of stream-blocks, %d rows, %d requests, %.0f rows/s, contiguous to block %d, block-rate ETA %.0f min (dense recent blocks are slower) | chunks/logs by provider: %s"
                % (100.0 * done_blocks / total_blocks, rows_total, req_total, rows_total / max(now - started, 1), frontier, eta / 60,
                   ", ".join("%s %d/%d" % (k, v[0], v[1]) for k, v in sorted(by_provider.items()))))
    for proc in procs:
        if proc.is_alive():
            proc.terminate()
    db.commit()
    complete = (not pending) and (not failed) and not STOP
    log("sync pass finished: complete=%s rows=%d requests=%d elapsed=%.0fs" % (complete, rows_total, req_total, time.time() - started))
    return complete


# ----------------------------------------------------------------------------- anchors
class BlockClock:
    """Block timestamps via batched eth_getBlockByNumber (20 per HTTP request), cached."""

    def __init__(self):
        self.cache = {}
        self.calls = 0
        self.requests = 0
        self.turn = 0

    def fetch(self, blocks):
        need = sorted(set(b for b in blocks if b not in self.cache))
        providers = ("sequence", "arbitrum")
        for start in range(0, len(need), 20):
            chunk = need[start:start + 20]
            payload = [{"jsonrpc": "2.0", "id": i, "method": "eth_getBlockByNumber", "params": [hex(b), False]} for i, b in enumerate(chunk)]
            delay = 1.0
            for attempt in range(40):
                provider = providers[(self.turn + attempt) % len(providers)]
                try:
                    out = C.rpc_raw(provider, payload, timeout=60)
                    got = {item["id"]: int(item["result"]["timestamp"], 16) for item in out}
                    if len(got) != len(chunk):
                        raise ValueError("short batch")
                    break
                except Exception:
                    time.sleep(delay)
                    delay = min(delay * 1.7, 30)
            else:
                raise C.RpcError("block timestamp batch failed 40 times")
            self.turn += 1
            self.requests += 1
            self.calls += len(chunk)
            for i, b in enumerate(chunk):
                self.cache[b] = got[i]

    def ts(self, block):
        if block not in self.cache:
            self.fetch([block])
        return self.cache[block]


def build_anchors(db, upto_block, upto_ts):
    """First block of every UTC day (timestamp >= 00:00:00) from START_DAY to the day of upto_block."""
    import bisect
    import calendar
    start_ts = calendar.timegm(time.strptime(C.START_DAY, "%Y-%m-%d"))
    have = dict(db.execute("SELECT day, block FROM anchors"))
    todo = []
    ts = start_ts
    while ts <= upto_ts:
        day = C.day_of(ts)
        if day not in have:
            todo.append((day, ts))
        ts += 86400
    if not todo:
        return 0
    clock = BlockClock()
    clock.cache[upto_block] = upto_ts
    prior = db.execute("SELECT block, ts FROM anchors ORDER BY day DESC LIMIT 1").fetchone()
    if prior:  # incremental: bracket from the latest known anchor
        ref_lo = prior[0] - 1
    else:
        ref_lo = C.START_BLOCK - 2_000_000
    grid_n = 2 if prior else 96
    grid = sorted(set([ref_lo, upto_block] + [ref_lo + (upto_block - ref_lo) * i // grid_n for i in range(1, grid_n)]))
    clock.fetch(grid)
    if clock.ts(ref_lo) >= todo[0][1]:
        raise SystemExit("anchor search floor is not before the first missing day")
    grid_ts = [clock.cache[b] for b in grid]
    state = {}
    for day, target in todo:
        pos = bisect.bisect_left(grid_ts, target)  # first grid point with ts >= target
        if pos >= len(grid):
            continue
        state[day] = [target, grid[pos - 1], grid[pos], 0]
    rounds = 0
    while True:
        probes = {}
        for day, (target, lo, hi, turn) in state.items():
            if hi - lo <= 1:
                continue
            t_lo, t_hi = clock.cache[lo], clock.cache[hi]
            if turn % 3 == 2 or t_hi == t_lo:
                mid = (lo + hi) // 2
            else:
                mid = lo + int((target - t_lo) * (hi - lo) / (t_hi - t_lo))
                mid = min(max(mid, lo + 1), hi - 1)
            probes[day] = mid
        if not probes:
            break
        clock.fetch(probes.values())
        for day, mid in probes.items():
            rec = state[day]
            if clock.cache[mid] >= rec[0]:
                rec[2] = mid
            else:
                rec[1] = mid
            rec[3] += 1
        rounds += 1
    rows = []
    for day, (target, lo, hi, _) in state.items():
        if not (clock.cache[hi] >= target > clock.cache[lo] and hi == lo + 1):
            raise SystemExit("anchor verification failed for %s" % day)
        rows.append((day, hi, clock.cache[hi]))
    db.executemany("INSERT OR REPLACE INTO anchors(day, block, ts) VALUES(?,?,?)", rows)
    db.commit()
    log("anchors: added %d days with %d block lookups in %d batched requests (%d rounds)" % (len(rows), clock.calls, clock.requests, rounds))
    return len(rows)


# ----------------------------------------------------------------------------- multicall snapshot
def encode_aggregate3(calls):
    """calls: list of (target_hex40, calldata_hex) with 36-byte calldata. allowFailure = true."""
    n = len(calls)
    out = ["82ad56cb", "%064x" % 32, "%064x" % n]
    out.extend("%064x" % (n * 32 + i * 192) for i in range(n))
    for target, data in calls:
        out.append("0" * 24 + target)
        out.append("%064x" % 1)
        out.append("%064x" % 96)
        out.append("%064x" % 36)
        out.append(data.ljust(128, "0"))
    return "0x" + "".join(out)


def decode_aggregate3(result, n):
    body = result[2:]
    base = int(body[0:64], 16) * 2
    count = int(body[base:base + 64], 16)
    if count != n:
        raise C.RpcError("multicall returned %d results for %d calls" % (count, n))
    values = []
    start = base + 64
    for i in range(n):
        off = start + int(body[start + i * 64: start + (i + 1) * 64], 16) * 2
        ok = int(body[off:off + 64], 16)
        data_off = off + int(body[off + 64:off + 128], 16) * 2
        length = int(body[data_off:data_off + 64], 16)
        if not ok or length != 32:
            raise C.RpcError("balanceOf failed inside multicall (index %d)" % i)
        values.append(int(body[data_off + 64:data_off + 128], 16))
    return values


def multicall_balances(addresses, block, batch=1000, threads=3):
    """addresses: list of 20-byte values. Returns list of balances (micro-USDC) at the given block."""
    chunks = [addresses[i:i + batch] for i in range(0, len(addresses), batch)]
    # arb1.arbitrum.io does not serve state at pinned historical blocks reliably, so it is last
    orders = [("tenderly", "sequence", "arbitrum"), ("sequence", "tenderly", "arbitrum"), ("tenderly", "sequence", "arbitrum")]

    def one(item):
        idx, chunk = item
        calls = [(C.USDC[2:], "70a08231" + "0" * 24 + bytes(a).hex()) for a in chunk]
        payload = [{"to": C.MULTICALL3, "data": encode_aggregate3(calls)}, hex(block)]
        result = C.rpc_any("eth_call", payload, order=orders[idx % len(orders)], timeout=90)
        return decode_aggregate3(result, len(chunk))

    with ThreadPoolExecutor(max_workers=threads) as pool:
        parts = list(pool.map(one, enumerate(chunks)))
    return [v for part in parts for v in part], len(chunks)


def take_snapshot(db, block, store=True):
    rows = db.execute("SELECT p.id, a.a FROM pools p JOIN addr a ON a.id=p.id WHERE p.block<=? ORDER BY p.id", (block,)).fetchall()
    sys_rows = db.execute("SELECT id, a FROM addr WHERE id<=? ORDER BY id", (len(C.SYSTEM),)).fetchall()
    ids = [r[0] for r in rows] + [r[0] for r in sys_rows]
    addresses = [r[1] for r in rows] + [r[1] for r in sys_rows]
    started = time.time()
    values, n_calls = multicall_balances(addresses, block)
    balances = dict(zip(ids, values))
    pool_total = sum(values[:len(rows)])
    if store:
        ts = int(C.rpc_any("eth_getBlockByNumber", [hex(block), False])["timestamp"], 16)
        db.execute("DELETE FROM snap WHERE block=?", (block,))
        db.executemany("INSERT INTO snap(block, id, bal) VALUES(?,?,?)", [(block, i, v) for i, v in balances.items() if v])
        db.execute("INSERT OR REPLACE INTO snap_meta(block, ts, n_pools, total, provider, taken_utc) VALUES(?,?,?,?,?,?)",
                   (block, ts, len(rows), pool_total, "multicall3 aggregate3 via tenderly/sequence/arbitrum", C.utc_iso(time.time())))
        db.commit()
    log("snapshot @%d: %d pools, pool total %.2f USDC, %d eth_calls, %.0fs" % (block, len(rows), pool_total / 1e6, n_calls, time.time() - started))
    return balances


def load_snapshot(db, block):
    if not db.execute("SELECT 1 FROM snap_meta WHERE block=?", (block,)).fetchone():
        return take_snapshot(db, block)
    return dict(db.execute("SELECT id, bal FROM snap WHERE block=?", (block,)))


# ----------------------------------------------------------------------------- ledger / reconcile
def ledger_net(db, lo, hi):
    """Net USDC flow per address id over blocks [lo, hi], from the store's ledger rules."""
    sys_n = len(C.SYSTEM)
    net = {}
    for aid, total in db.execute("SELECT t, SUM(amt) FROM xfer WHERE block BETWEEN ? AND ? GROUP BY t", (lo, hi)):
        net[aid] = net.get(aid, 0) + total
    for aid, total in db.execute("SELECT f, SUM(amt) FROM xfer WHERE block BETWEEN ? AND ? GROUP BY f", (lo, hi)):
        net[aid] = net.get(aid, 0) - total
    # Events add only the transfers that the S-filter cannot see (counterparty outside S).
    for pool, total in db.execute("SELECT pool, SUM(a1) FROM ev WHERE kind=1 AND p1>? AND block BETWEEN ? AND ? GROUP BY pool", (sys_n, lo, hi)):
        net[pool] = net.get(pool, 0) + total
    for pool, total in db.execute("SELECT pool, SUM(a1) FROM ev WHERE kind=2 AND p1>? AND block BETWEEN ? AND ? GROUP BY pool", (sys_n, lo, hi)):
        net[pool] = net.get(pool, 0) - total
    # Atomic deposits: single-party (a1>0, a2=0) goes party->oracle->pool and is already in xfer.
    # Otherwise each non-zero side is a direct transferFrom(party -> pool).
    for pool, p1, p2, a1, a2, kind, sub in db.execute(
            "SELECT pool, p1, p2, a1, a2, kind, sub FROM ev WHERE kind IN (3,4) AND block BETWEEN ? AND ?", (lo, hi)):
        if kind == 3 and a1 > 0 and a2 == 0:
            continue
        if a1 > 0 and p1 > sys_n:
            net[pool] = net.get(pool, 0) + a1
        if a2 > 0 and p2 > sys_n:
            net[pool] = net.get(pool, 0) + a2
    return net


def cmd_reconcile(db, args):
    floor = floor_block(db)
    if args.delta:
        a, b = args.delta
        if a < floor - 1:
            raise SystemExit("delta start is below the store floor %d" % floor)
        base = load_snapshot(db, a)
        lo = a + 1
    else:
        b = args.block or synced_block(db)
        if floor != C.START_BLOCK:
            raise SystemExit("full reconcile needs a store that starts at the factory deployment; use --delta A B")
        base = {}
        lo = floor
    covered = synced_block(db)
    if b > covered:
        raise SystemExit("block %d is beyond contiguous coverage %d" % (b, covered))
    snap = load_snapshot(db, b)
    net = ledger_net(db, lo, b)
    pool_ids = [i for (i,) in db.execute("SELECT id FROM pools WHERE block<=?", (b,))]
    sys_n = len(C.SYSTEM)
    bad = []
    for pid in pool_ids:
        expect = base.get(pid, 0) + net.get(pid, 0)
        actual = snap.get(pid, 0)
        if expect != actual:
            bad.append((pid, expect, actual))
    total_chain = sum(snap.get(p, 0) for p in pool_ids)
    total_ledger = sum(base.get(p, 0) + net.get(p, 0) for p in pool_ids)
    print("reconcile blocks [%d, %d]%s" % (lo, b, " (delta mode)" if args.delta else " (full history)"))
    print("  pools checked: %d   exact matches: %d   mismatches: %d" % (len(pool_ids), len(pool_ids) - len(bad), len(bad)))
    print("  all-pool balance on chain: %.6f   ledger: %.6f   difference: %.6f" % (total_chain / 1e6, total_ledger / 1e6, (total_chain - total_ledger) / 1e6))
    print("  sum of absolute residuals: %.6f USDC" % (sum(abs(e - a) for _, e, a in bad) / 1e6))
    names = {i + 1: C.SYSTEM_LABEL.get(a, a[:10]) for i, a in enumerate(C.SYSTEM)}
    for sid in range(1, sys_n + 1):
        expect = base.get(sid, 0) + net.get(sid, 0)
        actual = snap.get(sid, 0)
        flag = "ok" if expect == actual else "MISMATCH %.6f" % ((actual - expect) / 1e6)
        if expect or actual:
            print("  system %-22s chain %.6f  ledger %.6f  %s" % (names[sid], actual / 1e6, expect / 1e6, flag))
    if bad:
        book = dict(db.execute("SELECT id, a FROM addr WHERE id IN (%s)" % ",".join(str(p) for p, _, _ in bad[:400])))
        for pid, expect, actual in sorted(bad, key=lambda r: -abs(r[1] - r[2]))[:args.show]:
            print("  pool %s ledger %.6f chain %.6f residual %.6f" % (C.b2a(book.get(pid, b"")), expect / 1e6, actual / 1e6, (actual - expect) / 1e6))
    C.set_meta(db, "last_reconcile", C.json.dumps({
        "mode": "delta" if args.delta else "full", "from_block": lo, "to_block": b, "block": b, "pools": len(pool_ids),
        "mismatches": len(bad), "abs_residual_usd": sum(abs(e - a) for _, e, a in bad) / 1e6,
        "chain_total_usd": total_chain / 1e6, "ledger_total_usd": total_ledger / 1e6,
        "checked_utc": C.utc_iso(time.time())}))
    C.set_meta(db, "last_reconcile_bad", C.json.dumps([p for p, _, _ in bad][:5000]))
    db.commit()
    return bad


# ----------------------------------------------------------------------------- audit / verify
def scan_logs(flt, lo, hi, provider=None, step=5_000_000):
    """Address-filtered scan with adaptive splitting. Wide spans need arb1.arbitrum.io or Tenderly."""
    # Topic-filtered USDC queries over millions of blocks time out on arb1.arbitrum.io ("log query timed out",
    # then HTTP 429), so Tenderly goes first and the official endpoint gets shorter spans.
    order = (provider,) if provider else ("tenderly", "arbitrum", "sequence")
    span_cap = {"arbitrum": 1_000_000, "tenderly": 5_000_000, "sequence": 10_000, "thirdweb": 1_000}
    out = []
    cur = lo
    n_req = 0
    while cur <= hi:
        got = None
        for name in order:
            if COOLDOWN.get(name, 0) > time.time() and len(order) > 1:
                continue
            end = min(cur + min(step, span_cap[name]) - 1, hi)
            n_req += 1
            try:
                got = C.rpc(name, "eth_getLogs", [dict(flt, fromBlock=hex(cur), toBlock=hex(end))], tries=3)
                break
            except C.RpcSplit:
                step = max(1, (end - cur + 1) // 2)
                got = "split"
                break
            except C.RpcError:
                COOLDOWN[name] = time.time() + 120
        if got is None:
            time.sleep(20)
            continue
        if got == "split":
            continue
        out.extend(got)
        cur = end + 1
        if len(got) < 4000:
            step = min(step * 2, 5_000_000)
    return out, n_req


def eth_call_batch(calls, order=("sequence", "tenderly"), size=4):
    """calls: list of (to_hex, data_hex, block). Returns ints. Small JSON-RPC batches, archive state needed.

    Both archive-capable public endpoints count every call in a batch against their rate limit, so batches
    stay small and alternate between providers.
    """
    out = [None] * len(calls)
    turn = 0
    for start in range(0, len(calls), size):
        turn += 1
        chunk = calls[start:start + size]
        payload = [{"jsonrpc": "2.0", "id": i, "method": "eth_call", "params": [{"to": to, "data": data}, hex(block)]}
                   for i, (to, data, block) in enumerate(chunk)]
        delay = 1.0
        for attempt in range(30):
            provider = order[(turn + attempt) % len(order)]
            try:
                got = C.rpc_raw(provider, payload, timeout=60)
                values = {item["id"]: int(item["result"], 16) for item in got}
                if len(values) != len(chunk):
                    raise ValueError("short batch")
                break
            except Exception:
                time.sleep(delay)
                delay = min(delay * 1.7, 20)
        else:
            raise C.RpcError("eth_call batch failed 30 times")
        for i in range(len(chunk)):
            out[start + i] = values[i]
    return out


def cmd_repair(db, args):
    """Complete the ledger of pools whose balance did not reconcile.

    The stream filters see transfers with a system address on one side, plus pool events. A plain ERC-20
    transfer sent straight to a pool by an outside wallet (dust, mistakes) is invisible to both. For each
    mismatched pool this bisects on `balanceOf(pool) at block` minus the ledger balance to find the block
    range where the difference appears, fetches that pool's USDC transfers in the range and stores the ones
    the ledger does not already hold (xfer rows whose counterparty is neither a system address nor a pool).
    """
    last = C.json.loads(C.get_meta(db, "last_reconcile", "{}"))
    bad = C.json.loads(C.get_meta(db, "last_reconcile_bad", "[]"))
    top = last.get("block") or last.get("to_block")
    if not bad or not top:
        print("repair: nothing to do")
        return 0
    floor = floor_block(db)
    sys_n = len(C.SYSTEM)
    book = AddrBook(db)
    db.execute("CREATE TEMP TABLE bad(id INTEGER PRIMARY KEY)")
    db.executemany("INSERT INTO bad VALUES(?)", [(i,) for i in bad])
    info = {pid: (bytes(a), max(block - 1, floor - 1), block) for pid, a, block in db.execute(
        "SELECT p.id, a.a, p.block FROM pools p JOIN addr a ON a.id=p.id WHERE p.id IN (SELECT id FROM bad)")}
    # ledger events of the bad pools: (block, signed amount), plus keys to recognise known transfers
    events = {pid: [] for pid in info}
    xkeys = {pid: set() for pid in info}
    evamt = {pid: {} for pid in info}
    for block, lidx, f, t, amt in db.execute(
            "SELECT block, lidx, f, t, amt FROM xfer WHERE block<=? AND (t IN (SELECT id FROM bad) OR f IN (SELECT id FROM bad))", (top,)):
        if t in events:
            events[t].append((block, amt))
            xkeys[t].add((block, lidx))
        if f in events:
            events[f].append((block, -amt))
            xkeys[f].add((block, lidx))
    for block, kind, pid, p1, p2, a1, a2 in db.execute(
            "SELECT block, kind, pool, p1, p2, a1, a2 FROM ev WHERE block<=? AND kind<5 AND pool IN (SELECT id FROM bad)", (top,)):
        parts = []
        if kind == 1 and p1 > sys_n:
            parts.append(a1)
        elif kind == 2 and p1 > sys_n:
            parts.append(-a1)
        elif kind in (3, 4) and not (kind == 3 and a1 > 0 and a2 == 0):
            parts += [amount for party, amount in ((p1, a1), (p2, a2)) if amount > 0 and party > sys_n]
        for signed in parts:
            events[pid].append((block, signed))
            key = (block, abs(signed))
            evamt[pid][key] = evamt[pid].get(key, 0) + 1
    import bisect as _bisect
    blocks, cums = {}, {}
    for pid, rows in events.items():
        rows.sort()
        total = 0
        bl, cu = [], []
        for block, signed in rows:
            total += signed
            bl.append(block)
            cu.append(total)
        blocks[pid], cums[pid] = bl, cu

    def ledger_at(pid, block):
        pos = _bisect.bisect_right(blocks[pid], block)
        return cums[pid][pos - 1] if pos else 0

    def chain_at(pairs):
        calls = [(C.USDC, "0x70a08231" + "0" * 24 + info[pid][0].hex(), block) for pid, block in pairs]
        return eth_call_batch(calls)

    pids = sorted(info)
    log("repair: %d pools at block %d, ledger rows per pool min/median/max %d/%d/%d" % (
        len(pids), top, min(len(v) for v in events.values()), sorted(len(v) for v in events.values())[len(events) // 2], max(len(v) for v in events.values())))
    window = args.window
    base_block = {pid: info[pid][1] for pid in pids}
    snap_top = load_snapshot(db, top)
    added = n_req = 0
    added_usd = 0

    def absorb(pid, logs):
        """Store the transfers of this pool that the ledger does not hold. Returns their signed sum."""
        nonlocal added, added_usd
        found = 0
        for entry in logs:
            amount = int(entry["data"], 16) if len(entry["data"]) > 2 else 0
            if amount == 0:
                continue
            block, lidx = int(entry["blockNumber"], 16), int(entry["logIndex"], 16)
            if (block, lidx) in xkeys[pid]:
                continue
            if evamt[pid].get((block, amount), 0) > 0:  # the transfer behind a Deposited/Withdrawn/atomic event
                evamt[pid][(block, amount)] -= 1
                continue
            src, dst = book.get(C.topic_addr(entry["topics"][1])), book.get(C.topic_addr(entry["topics"][2]))
            book.flush()
            db.execute("INSERT OR IGNORE INTO xfer VALUES(?,?,?,?,?)", (block, lidx, src, dst, amount))
            xkeys[pid].add((block, lidx))
            added += 1
            added_usd += amount
            found += amount if dst == pid else -amount
        return found

    # 1. Quiet pools (few ledger rows) and short ranges: fetch their whole transfer history in grouped queries.
    by_addr = {info[pid][0]: pid for pid in pids}
    light = sorted((pid for pid in pids if len(events[pid]) <= args.light or top - base_block[pid] <= window), key=lambda i: base_block[i])
    for start in range(0, len(light), 100):
        group = light[start:start + 100]
        topics = [C.pad_topic(C.b2a(info[pid][0])) for pid in group]
        lo = min(base_block[pid] for pid in group) + 1
        ins, r1 = scan_logs({"address": C.USDC, "topics": [C.T_TRANSFER, None, topics]}, lo, top)
        outs, r2 = scan_logs({"address": C.USDC, "topics": [C.T_TRANSFER, topics]}, lo, top)
        n_req += r1 + r2
        per_pool = {}
        for entry in ins:
            per_pool.setdefault(by_addr[C.topic_addr(entry["topics"][2])], []).append(entry)
        for entry in outs:
            per_pool.setdefault(by_addr[C.topic_addr(entry["topics"][1])], []).append(entry)
        for pid, logs in per_pool.items():
            absorb(pid, logs)
        db.commit()
        log("repair: quiet pools %d/%d done, %d transfers read, %d direct transfers so far, %d requests" % (min(start + 100, len(light)), len(light), len(ins) + len(outs), added, n_req))

    # 2. Busy pools: bisect on (balanceOf at block) - (ledger balance at block) to find where the gap opens.
    heavy = [pid for pid in pids if pid not in set(light)]
    # window (CI) stores start mid-history: a pool created before the floor carries a balance in
    need_base = [(pid, base_block[pid]) for pid in heavy if info[pid][2] <= base_block[pid]]
    base_chain = dict(zip([p for p, _ in need_base], chain_at(need_base))) if need_base else {}
    base_ledger = {pid: ledger_at(pid, base_block[pid]) for pid in heavy}

    def diff(pid, block, chain_value):
        return (chain_value - base_chain.get(pid, 0)) - (ledger_at(pid, block) - base_ledger[pid])

    work = []
    for pid in heavy:
        d_top = diff(pid, top, snap_top.get(pid, 0))
        if d_top != 0:
            work.append((pid, base_block[pid], top, 0, d_top))
    final = []
    n_calls = len(need_base)
    rounds = 0
    while work:
        probes, rest = [], []
        for item in work:
            pid, lo, hi, d_lo, d_hi = item
            if hi - lo <= window:
                final.append(item)
            else:
                probes.append((item, (lo + hi) // 2))
        if not probes:
            break
        values = chain_at([(item[0], mid) for item, mid in probes])
        n_calls += len(probes)
        rounds += 1
        log("repair: bisection round %d, %d probes, %d intervals narrowed" % (rounds, len(probes), len(final)))
        for (item, mid), value in zip(probes, values):
            pid, lo, hi, d_lo, d_hi = item
            d_mid = diff(pid, mid, value)
            if d_mid != d_lo:
                rest.append((pid, lo, mid, d_lo, d_mid))
            if d_hi != d_mid:
                rest.append((pid, mid, hi, d_mid, d_hi))
        work = rest
    unexplained = 0
    for pid, lo, hi, d_lo, d_hi in final:
        topic = C.pad_topic(C.b2a(info[pid][0]))
        logs, r1 = scan_logs({"address": C.USDC, "topics": [C.T_TRANSFER, None, [topic]]}, lo + 1, hi, step=window + 1)
        more, r2 = scan_logs({"address": C.USDC, "topics": [C.T_TRANSFER, [topic]]}, lo + 1, hi, step=window + 1) if d_hi < d_lo else ([], 0)
        n_req += r1 + r2
        found = absorb(pid, logs + more)
        if found != d_hi - d_lo:
            unexplained += 1
            print("  pool %s blocks (%d, %d]: difference %.6f but direct transfers found %.6f" % (C.b2a(info[pid][0]), lo, hi, (d_hi - d_lo) / 1e6, found / 1e6))
        db.commit()
    C.set_meta(db, "repair_rows", int(C.get_meta(db, "repair_rows", 0)) + added)
    db.commit()
    print("repair @%d: %d pools (%d by full-history fetch, %d by bisection over %d intervals), %d direct transfers added (%.6f USDC), %d unexplained intervals; %d eth_calls in %d rounds, %d getLogs"
          % (top, len(pids), len(light), len(heavy), len(final), added, added_usd / 1e6, unexplained, n_calls, rounds, n_req))
    return 0


def cmd_audit(db, args):
    """Ground truth for sample pools: every USDC transfer touching them, diffed against the ledger."""
    floor = floor_block(db)
    hi = synced_block(db)
    lo = max(floor, args.from_block or floor)
    ids = []
    if args.pools:
        for address in args.pools:
            row = db.execute("SELECT id FROM addr WHERE a=?", (C.a2b(address.lower()),)).fetchone()
            if row:
                ids.append(row[0])
    if args.mismatched:
        ids += C.json.loads(C.get_meta(db, "last_reconcile_bad", "[]"))[:args.mismatched]
    if args.sample:
        rng = random.Random(args.seed)
        every = [i for (i,) in db.execute("SELECT id FROM pools WHERE block<=?", (hi,))]
        ids += rng.sample(every, min(args.sample, len(every)))
    if args.top:
        latest = db.execute("SELECT MAX(block) FROM snap_meta").fetchone()[0]
        if latest:
            n_sys = len(C.SYSTEM)
            ids += [i for (i,) in db.execute("SELECT id FROM snap WHERE block=? AND id>? ORDER BY bal DESC LIMIT ?", (latest, n_sys, args.top))]
    ids = sorted(set(ids))
    if not ids:
        raise SystemExit("no pools selected")
    addr_of = dict(db.execute("SELECT id, a FROM addr WHERE id IN (%s)" % ",".join(map(str, ids))))
    id_of = {bytes(a): i for i, a in addr_of.items()}
    sys_n = len(C.SYSTEM)
    problems = 0
    total_true = 0
    n_req_total = 0
    unknown_parties = {}
    for start in range(0, len(ids), 200):
        group = ids[start:start + 200]
        topics = [C.pad_topic(C.b2a(addr_of[i])) for i in group]
        outs, r1 = scan_logs({"address": C.USDC, "topics": [C.T_TRANSFER, topics]}, lo, hi)
        ins, r2 = scan_logs({"address": C.USDC, "topics": [C.T_TRANSFER, None, topics]}, lo, hi)
        n_req_total += r1 + r2
        truth = {}
        for entry in outs + ins:
            amount = int(entry["data"], 16) if len(entry["data"]) > 2 else 0
            if amount == 0:
                continue
            key = (int(entry["blockNumber"], 16), int(entry["logIndex"], 16))
            truth[key] = (C.topic_addr(entry["topics"][1]), C.topic_addr(entry["topics"][2]), amount)
        total_true += len(truth)
        marks = ",".join(map(str, group))
        ledger = {}
        for block, lidx, f, t, amt in db.execute(
                "SELECT block, lidx, f, t, amt FROM xfer WHERE block BETWEEN ? AND ? AND (f IN (%s) OR t IN (%s))" % (marks, marks), (lo, hi)):
            ledger[(block, lidx)] = amt
        for block, lidx, kind, pool, p1, p2, a1, a2 in db.execute(
                "SELECT block, lidx, kind, pool, p1, p2, a1, a2 FROM ev WHERE block BETWEEN ? AND ? AND pool IN (%s) AND kind IN (1,2,3,4)" % marks, (lo, hi)):
            if kind in (1, 2):
                if p1 > sys_n:
                    ledger[(block, lidx - 1)] = a1  # the Transfer log sits right before the pool event
            elif not (kind == 3 and a1 > 0 and a2 == 0):
                # two direct transferFrom calls precede the event; match by amount below
                for back, amount, party in ((2, a1, p1), (1, a2, p2)):
                    if amount > 0 and party > sys_n:
                        for delta in (1, 2, 3, 4):
                            if truth.get((block, lidx - delta), (None, None, None))[2] == amount and (block, lidx - delta) not in ledger:
                                ledger[(block, lidx - delta)] = amount
                                break
        for key, (src, dst, amount) in sorted(truth.items()):
            if ledger.get(key) != amount:
                problems += 1
                other = dst if src in id_of else src
                unknown_parties[C.b2a(other)] = unknown_parties.get(C.b2a(other), 0) + 1
                if problems <= args.show:
                    print("  MISSING in ledger: block %d log %d %s -> %s %.6f" % (key[0], key[1], C.b2a(src), C.b2a(dst), amount / 1e6))
        for key, amount in sorted(ledger.items()):
            if key not in truth:
                problems += 1
                if problems <= args.show:
                    print("  EXTRA in ledger: block %d log %d %.6f" % (key[0], key[1], amount / 1e6))
    print("audit: %d pools, blocks [%d, %d], %d on-chain USDC transfers checked, %d discrepancies, %d requests" % (len(ids), lo, hi, total_true, problems, n_req_total))
    if unknown_parties:
        print("  counterparties of missing transfers:", sorted(unknown_parties.items(), key=lambda kv: -kv[1])[:20])
    return problems


def cmd_verify(db, args):
    """Re-fetch random ranges from a second provider and compare with the store row by row."""
    floor = floor_block(db)
    hi = synced_block(db)
    rng = random.Random(args.seed)
    book = AddrBook(db)
    pool_ids = set(i for (i,) in db.execute("SELECT id FROM pools"))
    pool_map = {a: i for a, i in book.map.items() if i in pool_ids}
    known = dict(book.map)
    bulk = C.a2b(C.ROUTER_SETTLE)
    bad = 0
    for n in range(args.ranges):
        span = args.span
        lo = rng.randint(floor, max(floor, hi - span))
        end = min(lo + span - 1, hi)
        for stream in STREAMS:
            logs, _ = scan_logs(make_filter(stream, lo, end), lo, end, provider=args.provider, step=span)
            if stream == "events":
                rows, _ = decode_events(logs, lo, end, known, pool_map, bulk)
                got = set((r[0], r[1], r[2], r[3], r[4], known.get(r[5], r[5]), known.get(r[6], r[6]), r[7], r[8]) for r in rows)
                have = set(db.execute("SELECT * FROM ev WHERE block BETWEEN ? AND ?", (lo, end)))
            else:
                rows, _ = decode_xfer(logs, lo, end, known)
                got = set(rows)
                col = "f" if stream == "xfrom" else "t"
                have = set(db.execute("SELECT * FROM xfer WHERE block BETWEEN ? AND ? AND %s<=?" % col, (lo, end, len(C.SYSTEM))))
            diff = got ^ have
            bad += len(diff)
            print("verify %s [%d, %d] via %s: provider %d rows, store %d rows, differences %d" % (stream, lo, end, args.provider, len(got), len(have), len(diff)))
    print("verify: total differences", bad)
    return bad


# ----------------------------------------------------------------------------- compact state for CI
REG_RECORD = 46  # pool address (20) + user address (20) + creation block (u32) + log index (u16)


def cmd_export_registry(db, args):
    """Write the pool registry in creation order: the only part of the store CI needs besides the aggregator state."""
    import gzip
    import struct
    os.makedirs(args.state_dir, exist_ok=True)
    upto = args.block or synced_block(db)
    out = bytearray()
    n = 0
    for pool, user, block, lidx in db.execute(
            "SELECT a.a, u.a, p.block, p.lidx FROM pools p JOIN addr a ON a.id=p.id JOIN addr u ON u.id=p.user WHERE p.block<=? ORDER BY p.block, p.lidx", (upto,)):
        out += bytes(pool) + bytes(user) + struct.pack(">IH", block, lidx)
        n += 1
    path = os.path.join(args.state_dir, "registry.bin.gz")
    with gzip.open(path + ".tmp", "wb", compresslevel=6) as handle:
        handle.write(bytes(out))
    os.replace(path + ".tmp", path)
    anchors = db.execute("SELECT day, block, ts FROM anchors ORDER BY day").fetchall()
    with open(os.path.join(args.state_dir, "anchors.json"), "w") as handle:
        C.json.dump({"through_block": upto, "anchors": anchors}, handle, separators=(",", ":"))
    print("registry: %d pools through block %d -> %s (%d bytes)" % (n, upto, path, os.path.getsize(path)))


def cmd_bootstrap(db, args):
    """Create a small store from the compact state so `update` can run without the full SQLite (CI)."""
    import gzip
    import struct
    if db.execute("SELECT COUNT(*) FROM pools").fetchone()[0]:
        raise SystemExit("bootstrap needs an empty store")
    with gzip.open(os.path.join(args.state_dir, "registry.bin.gz"), "rb") as handle:
        raw = handle.read()
    with gzip.open(os.path.join(args.state_dir, "agg_state.json.gz"), "rt") as handle:
        extra = C.json.load(handle)["extra"]
    with open(os.path.join(args.state_dir, "anchors.json")) as handle:
        anchors = C.json.load(handle)["anchors"]
    next_block = extra["next_block"]
    book = AddrBook(db)
    vault = book.get(C.a2b(C.VAULT))
    n = 0
    for off in range(0, len(raw), REG_RECORD):
        pool, user = raw[off:off + 20], raw[off + 20:off + 40]
        block, lidx = struct.unpack(">IH", raw[off + 40:off + 46])
        if block >= next_block:
            break  # pools created after the saved state are re-read from the factory
        pid, uid = book.get(pool), book.get(user)
        book.flush()
        db.execute("INSERT INTO pools(id, user, creator, block, lidx, fee, fee_payer, tx) VALUES(?,?,?,?,?,0,?,x'')", (pid, uid, vault, block, lidx, uid))
        n += 1
    db.executemany("INSERT OR REPLACE INTO anchors(day, block, ts) VALUES(?,?,?)", [tuple(a) for a in anchors])
    C.set_meta(db, "floor_block", next_block)
    db.execute("INSERT INTO progress(stream, seg, cur) VALUES('factory', 0, ?)", (next_block,))
    db.commit()
    print("bootstrap: %d pools, store floor %d" % (n, next_block))


# ----------------------------------------------------------------------------- commands
def cmd_status(db, args):
    target = int(C.get_meta(db, "target_block", 0))
    floor = floor_block(db)
    print("store:", args.db or C.DB_PATH)
    print("floor block:", floor, " target block:", target)
    for stream in ("factory",) + STREAMS:
        row = db.execute("SELECT SUM(n_req), SUM(n_logs), SUM(n_rows), SUM(n_skip), SUM(sum_skip), SUM(n_zero), SUM(secs) FROM progress WHERE stream=?", (stream,)).fetchone()
        cur = stream_cursor(db, stream, target)
        segs = db.execute("SELECT COUNT(*) FROM progress WHERE stream=?", (stream,)).fetchone()[0]
        total = max(target - floor + 1, 1)
        if stream == "factory":
            done = max(cur - floor + 1, 0)
        else:
            done = 0
            for seg, c in db.execute("SELECT seg, cur FROM progress WHERE stream=?", (stream,)):
                seg_lo = max(seg * C.SEGMENT, floor)
                seg_hi = min((seg + 1) * C.SEGMENT - 1, target)
                done += max(min(c - 1, seg_hi) - seg_lo + 1, 0)
        print("  %-8s contiguous to %d | blocks done %d / %d (%.2f%%), remaining %d | segments %d | requests %s logs %s rows %s | bulk-router events skipped %s (%.2f USDC) zero-value skipped %s | fetch secs %.0f"
              % (stream, cur, done, total, 100.0 * done / total, total - done, segs, row[0], row[1], row[2], row[3], (row[4] or 0) / 1e6, row[5], row[6] or 0))
    print("contiguous coverage (all streams):", synced_block(db, target))
    for name, table in (("pools", "pools"), ("transfers", "xfer"), ("pool events", "ev"), ("addresses", "addr"), ("anchors", "anchors"), ("snapshots", "snap_meta")):
        print("  %-12s %d" % (name, db.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]))
    print("last reconcile:", C.get_meta(db, "last_reconcile", "none"))


def acquire_lock():
    os.makedirs(C.CACHE_DIR, exist_ok=True)
    handle = open(C.LOCK_PATH, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.seek(0)
        raise SystemExit("another indexer instance holds %s (%s)" % (C.LOCK_PATH, handle.read().strip()))
    handle.seek(0)
    handle.truncate()
    handle.write("pid %d started %s\n" % (os.getpid(), C.utc_iso(time.time())))
    handle.flush()
    return handle


def workers_spec(args):
    counts = {"tenderly": args.tenderly, "arbitrum": args.arbitrum, "sequence": args.sequence, "thirdweb": args.thirdweb}
    enabled = [p for p in ("arbitrum", "tenderly", "sequence", "thirdweb") if counts[p] > 0]
    spec = []
    for provider in enabled:
        order = tuple([provider] + [p for p in enabled if p != provider])
        spec.extend([order] * counts[provider])
    return spec


def cmd_sync(db, args):
    def stop(signum, frame):
        global STOP
        STOP = True
        log("signal %d received: finishing the current writes and stopping" % signum)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if args.from_block is not None and C.get_meta(db, "floor_block") is None:
        C.set_meta(db, "floor_block", args.from_block)
    elif C.get_meta(db, "floor_block") is None:
        C.set_meta(db, "floor_block", C.START_BLOCK)
    db.commit()
    head, head_ts = pin_head()
    target = args.to_block or head
    log("%s: floor %d -> target %d (finalized head %d, %s)" % (args.cmd, floor_block(db), target, head, C.utc_iso(head_ts)))
    spec = workers_spec(args)
    log("workers:", ", ".join("%s" % order[0] for order in spec))
    complete = False
    for attempt in range(args.passes):
        complete = run_sync(db, target, spec)
        if complete or STOP:
            break
        log("pass %d incomplete; retrying unfinished segments in 20s" % (attempt + 1))
        time.sleep(20)
    if complete:
        target_ts = int(C.rpc_any("eth_getBlockByNumber", [hex(target), False])["timestamp"], 16)
        C.set_meta(db, "synced_block", target)
        C.set_meta(db, "synced_ts", target_ts)
        db.commit()
        if floor_block(db) == C.START_BLOCK or db.execute("SELECT COUNT(*) FROM anchors").fetchone()[0]:
            build_anchors(db, target, target_ts)
        log("synced to block %d (%s)" % (target, C.utc_iso(target_ts)))
    else:
        log("stopped before reaching the target; rerun the same command to resume")
    return 0 if complete else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=None, help="SQLite path (default .cache/onchain/onchain.sqlite)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("backfill", "update"):
        p = sub.add_parser(name)
        p.add_argument("--from-block", type=int, default=None, help="floor block for a NEW store (window test); default factory deployment")
        p.add_argument("--to-block", type=int, default=None, help="pin the target block (default: finalized head)")
        p.add_argument("--tenderly", type=int, default=3, help="worker processes on the Tenderly gateway (1 GB/day egress cap)")
        p.add_argument("--arbitrum", type=int, default=2, help="worker processes on arb1.arbitrum.io")
        p.add_argument("--sequence", type=int, default=1, help="worker processes on nodes.sequence.app")
        p.add_argument("--thirdweb", type=int, default=0, help="worker processes on 42161.rpc.thirdweb.com (1,000 blocks per call)")
        p.add_argument("--passes", type=int, default=5)
    sub.add_parser("status")
    sub.add_parser("anchors")
    p = sub.add_parser("snapshot")
    p.add_argument("--block", type=int, default=None)
    p = sub.add_parser("reconcile")
    p.add_argument("--block", type=int, default=None)
    p.add_argument("--delta", type=int, nargs=2, metavar=("A", "B"), default=None, help="check balance(B)-balance(A) against flows in (A, B]")
    p.add_argument("--show", type=int, default=25)
    p = sub.add_parser("audit")
    p.add_argument("--pools", nargs="*", default=[])
    p.add_argument("--sample", type=int, default=0)
    p.add_argument("--top", type=int, default=0)
    p.add_argument("--mismatched", type=int, default=0)
    p.add_argument("--from-block", type=int, default=None)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--show", type=int, default=30)
    p = sub.add_parser("repair")
    p.add_argument("--window", type=int, default=500_000, help="stop bisecting and fetch logs when the block range is this small")
    p.add_argument("--light", type=int, default=3000, help="pools with at most this many ledger rows are fetched whole instead of bisected")
    p = sub.add_parser("export-registry")
    p.add_argument("--state-dir", required=True)
    p.add_argument("--block", type=int, default=None)
    p = sub.add_parser("bootstrap")
    p.add_argument("--state-dir", required=True)
    p = sub.add_parser("verify")
    p.add_argument("--ranges", type=int, default=3)
    p.add_argument("--span", type=int, default=4000)
    p.add_argument("--provider", default="arbitrum")
    p.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    lock = None
    # repair and bootstrap assign address ids like the indexer does, so they must never run beside it
    if args.cmd in ("backfill", "update", "repair", "bootstrap"):
        if args.db is None:
            lock = acquire_lock()
        else:
            C.LOCK_PATH = args.db + ".lock"
            lock = acquire_lock()
    db = C.open_db(args.db)
    try:
        if args.cmd in ("backfill", "update"):
            return cmd_sync(db, args)
        if args.cmd == "status":
            return cmd_status(db, args)
        if args.cmd == "anchors":
            block = synced_block(db)
            ts = int(C.rpc_any("eth_getBlockByNumber", [hex(block), False])["timestamp"], 16)
            return build_anchors(db, block, ts) and 0
        if args.cmd == "snapshot":
            take_snapshot(db, args.block or synced_block(db))
            return 0
        if args.cmd == "reconcile":
            return 1 if cmd_reconcile(db, args) else 0
        if args.cmd == "audit":
            return 1 if cmd_audit(db, args) else 0
        if args.cmd == "verify":
            return 1 if cmd_verify(db, args) else 0
        if args.cmd == "repair":
            return cmd_repair(db, args)
        if args.cmd == "export-registry":
            return cmd_export_registry(db, args)
        if args.cmd == "bootstrap":
            return cmd_bootstrap(db, args)
    finally:
        db.close()
        if lock:
            lock.close()


if __name__ == "__main__":
    sys.exit(main())
