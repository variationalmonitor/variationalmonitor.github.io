"""Local-only, resumable PRIMARY-source factory census. Never a public exporter.

This collects narrow PoolCreated pages for later independent reconciliation.
It must not be confused with arbitrum_index.py's two-provider verified pilot.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import arbitrum_index as core

MODE = "primary_only_unreconciled"


def private_path(path):
    resolved = core.private_state_path(path)
    if resolved.is_relative_to(core.ROOT.resolve()):
        raise core.IntegrityError("bulk_checkpoint_must_be_outside_repository")
    return resolved


def open_state(path):
    path = private_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    os.chmod(path, 0o600)
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if tables - {"bulk_meta", "bulk_pages", "bulk_events", "bulk_participants"}:
        connection.close()
        raise core.IntegrityError("not_a_primary_bulk_checkpoint")
    connection.executescript("""
      CREATE TABLE IF NOT EXISTS bulk_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS bulk_pages (from_block INTEGER PRIMARY KEY, to_block INTEGER UNIQUE NOT NULL, proof TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS bulk_events (transaction_hash TEXT NOT NULL, log_index INTEGER NOT NULL,
        pool TEXT UNIQUE NOT NULL, pool_uuid TEXT UNIQUE NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY(transaction_hash,log_index));
      CREATE TABLE IF NOT EXISTS bulk_participants (account TEXT PRIMARY KEY, is_creator INTEGER NOT NULL);
    """)
    mode = connection.execute("SELECT value FROM bulk_meta WHERE key='mode'").fetchone()
    if mode and mode[0] != MODE:
        connection.close()
        raise core.IntegrityError("bulk_checkpoint_mode_mismatch")
    with connection:
        connection.execute("INSERT OR IGNORE INTO bulk_meta VALUES ('mode',?)", (MODE,))
    return connection


def pages(connection):
    result = [json.loads(row[0]) for row in connection.execute("SELECT proof FROM bulk_pages ORDER BY from_block")]
    for left, right in zip(result, result[1:]):
        if left["to_block"] + 1 != right["from_block"]:
            raise core.IntegrityError("bulk_checkpoint_coverage_gap")
    if any(item.get("verification") != MODE for item in result):
        raise core.IntegrityError("bulk_checkpoint_verification_mismatch")
    return result


def primary_registry(client, expected_code):
    if core.quantity(client.call("eth_chainId", [])) != 42161:
        raise core.IntegrityError("wrong_chain")
    stable = core.header(client.call("eth_getBlockByNumber", ["finalized", False]))
    code = client.call("eth_getCode", [core.FACTORY, hex(stable["number"])])
    if not isinstance(code, str) or code.lower() != expected_code:
        raise core.IntegrityError("factory_runtime_changed")
    receipt = client.call("eth_getTransactionReceipt", [core.DEPLOYMENT_TX])
    if (not isinstance(receipt, dict) or receipt.get("transactionHash", "").lower() != core.DEPLOYMENT_TX
            or core.quantity(receipt.get("status")) != 1 or core.quantity(receipt.get("blockNumber")) != core.DEPLOYMENT_BLOCK
            or core.address(receipt.get("contractAddress")) != core.FACTORY):
        raise core.IntegrityError("factory_deployment_receipt_mismatch")
    deployment = client.block(core.DEPLOYMENT_BLOCK)
    if core.hash_value(receipt.get("blockHash")) != deployment["hash"]:
        raise core.IntegrityError("deployment_not_canonical")
    return stable


def primary_page(client, lower, upper):
    if lower < core.DEPLOYMENT_BLOCK or upper < lower or upper - lower >= 100000:
        raise core.IntegrityError("bulk_page_bounds")
    left, right = client.block(lower), client.block(upper)
    logs = core.logs_for(client, lower, upper)
    records = [core.event(value) for value in logs]
    # Dates are NOT taken from optional log timestamps. No per-log header/receipt
    # checks are claimed by this bulk mode; those belong to reconciliation.
    core.same_header(right, client.block(upper, fresh=True))
    return {"from_block": lower, "to_block": upper, "from_hash": left["hash"], "to_hash": right["hash"],
            "from_parent_hash": left["parent_hash"], "from_timestamp": core.iso(left["timestamp"]),
            "to_timestamp": core.iso(right["timestamp"]), "log_count": len(records),
            "normalized_logs_sha256": core.digest(logs), "primary_rpc": core.OFFICIAL,
            "verification": MODE, "retrieved_at": core.now()}, records


def commit_page(connection, proof, records):
    previous = pages(connection)
    if proof.get("verification") != MODE or proof.get("log_count") != len(records):
        raise core.IntegrityError("bulk_page_proof_mismatch")
    if previous and (previous[-1]["to_block"] + 1 != proof["from_block"] or previous[-1]["to_hash"] != proof["from_parent_hash"]):
        raise core.IntegrityError("bulk_page_not_contiguous_or_canonical")
    try:
        with connection:
            for record in records:
                connection.execute("INSERT INTO bulk_events VALUES (?,?,?,?,?)", (record["transaction_hash"], record["log_index"], record["pool"], record["pool_uuid"], core.canonical(record).decode()))
                for participant in record["participants_at_creation"]:
                    connection.execute("INSERT INTO bulk_participants VALUES (?,?) ON CONFLICT(account) DO UPDATE SET is_creator=MAX(is_creator,excluded.is_creator)", (participant, int(participant == record["creator"])))
            connection.execute("INSERT INTO bulk_pages VALUES (?,?,?)", (proof["from_block"], proof["to_block"], core.canonical(proof).decode()))
    except sqlite3.IntegrityError as error:
        raise core.IntegrityError("bulk_checkpoint_duplicate_or_conflict") from error


def summary(connection):
    completed = pages(connection)
    event_count = connection.execute("SELECT COUNT(*) FROM bulk_events").fetchone()[0]
    if event_count != sum(page["log_count"] for page in completed):
        raise core.IntegrityError("bulk_checkpoint_count_mismatch")
    return {"mode": MODE, "public_export": False, "completed_pages": len(completed),
            "from_block": completed[0]["from_block"] if completed else None,
            "to_block": completed[-1]["to_block"] if completed else None,
            "next_block": completed[-1]["to_block"] + 1 if completed else None,
            "created_pools": event_count,
            "distinct_creators": connection.execute("SELECT COUNT(*) FROM bulk_participants WHERE is_creator=1").fetchone()[0],
            "distinct_creation_participants": connection.execute("SELECT COUNT(*) FROM bulk_participants").fetchone()[0],
            "creation_participants_excluding_core_olp": connection.execute("SELECT COUNT(*) FROM bulk_participants WHERE account<>?", (core.OLP,)).fetchone()[0]}


def run(state, from_block=None, to_block=None, page_blocks=100000, max_pages=1, max_calls=80, transport=core.http_json):
    state = private_path(state)  # Must run before network access or file creation.
    if not 1 <= page_blocks <= 100000 or not 1 <= max_pages <= 20:
        raise core.IntegrityError("invalid_bulk_run_bounds")
    budget = core.Budget(max_calls, interval=1)
    client = core.RPC(core.OFFICIAL, budget, transport)
    proof, code = core.source_proof(transport(core.SOURCE_URL, limit=8_000_000))
    stable = primary_registry(client, code)
    target = stable["number"] if to_block is None else to_block
    if not isinstance(target, int) or not core.DEPLOYMENT_BLOCK <= target <= stable["number"]:
        raise core.IntegrityError("target_not_finalized")
    with closing(open_state(state)) as connection:
        existing = pages(connection)
        start = existing[-1]["to_block"] + 1 if existing else core.DEPLOYMENT_BLOCK if from_block is None else from_block
        if start < core.DEPLOYMENT_BLOCK or (from_block is not None and from_block != start):
            raise core.IntegrityError("bulk_resume_start_mismatch")
        prior = connection.execute("SELECT value FROM bulk_meta WHERE key='runtime_code_sha256'").fetchone()
        if prior and prior[0] != proof["runtime_code_sha256"]:
            raise core.IntegrityError("bulk_factory_runtime_changed")
        if existing and client.block(existing[-1]["to_block"])["hash"] != existing[-1]["to_hash"]:
            raise core.IntegrityError("bulk_checkpoint_reorg")
        with connection:
            connection.execute("INSERT OR REPLACE INTO bulk_meta VALUES ('runtime_code_sha256',?)", (proof["runtime_code_sha256"],))
            connection.execute("INSERT OR REPLACE INTO bulk_meta VALUES ('factory_provenance',?)", (core.canonical(proof).decode(),))
        changed, failure = 0, None
        while start <= target and changed < max_pages:
            end = min(target, start + page_blocks - 1)
            try:
                page, records = primary_page(client, start, end)
            except core.SourceUnavailable as error:
                failure = str(error)
                break
            commit_page(connection, page, records)
            changed += 1
            start = end + 1
        return {**summary(connection), "new_pages": changed, "rpc_calls": budget.calls,
                "next_block": start, "requested_to_block": target, "failure": failure}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path.home() / ".cache" / "variational-dashboard" / "factory-primary-bulk.sqlite")
    parser.add_argument("--from-block", type=int)
    parser.add_argument("--to-block", type=int)
    parser.add_argument("--page-blocks", type=int, default=100000)
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--max-calls", type=int, default=80)
    args = parser.parse_args()
    try:
        result = run(args.state, args.from_block, args.to_block, args.page_blocks, args.max_pages, args.max_calls)
        print(json.dumps(result))
        return 0 if not result["failure"] else 2
    except (core.IndexErrorBase, OSError, sqlite3.Error) as error:
        print(json.dumps({"error": str(error) if isinstance(error, core.IndexErrorBase) else "local_state_error", "public_export": False, "completed_pages_retained": True}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
