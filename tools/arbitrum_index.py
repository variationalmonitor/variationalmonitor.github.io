"""Bounded, resumable index of the documented settlement factory's creation events.

Private SQLite retains raw public-chain evidence. The published file contains
only scoped aggregates and coverage proofs, never a claim of protocol-wide users,
trading activity, referral claims, collateral or cash-flow completeness.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
FACTORY = "0x0f820b9afc270d658a9fd7d16b1bdc45b70f074c"
OLP = "0x74bbbb0e7f0bad6938509dd4b556a39a4db1f2cd"
USDC = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
IMPLEMENTATION = "0x8db6c8b7a085c3839e93eb8dad45b93fb1ef5836"
DEPLOYMENT_BLOCK = 300692000
DEPLOYMENT_TX = "0x080b1709bcb228f8031e6043a22d5396c124d7b2f7602198bb546455ea3482ca"
TOPIC = "0x0b33722ea9d97f05fb7128e0987e4060c182350308a3d0a19f8ec53512fe1493"
SIGNATURE = "PoolCreated(address,address[],uint128,address,uint128,uint128,address,uint256)"
OFFICIAL = "https://arb1.arbitrum.io/rpc"
WITNESS = "https://arb-pokt.nodies.app"
REGISTRY_URL = "https://docs.variational.io/technical-documentation/mainnet-contracts"
SOURCE_URL = f"https://sourcify.dev/server/v2/contract/42161/{FACTORY}?fields=all"
RPC_DOCS = ["https://docs.arbitrum.io/chain-info", "https://docs.nodies.app/rpc-services/pokt-public-rpc-endpoints"]
EVENT_INPUTS = [
    ("creatorAddress", "address", True), ("otherAddresses", "address[]", False),
    ("poolUuid", "uint128", True), ("pool", "address", False),
    ("rfqUuid", "uint128", False), ("parentQuoteUuid", "uint128", False),
    ("feePaidBy", "address", False), ("feeAmount", "uint256", False),
]
HEX = re.compile(r"0x[0-9a-fA-F]*\Z")
HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
ZERO = "0x" + "0" * 40
MAX_CALLS = 80
MAX_LOGS = 10000
MAX_RESPONSE = 4_000_000


class IndexErrorBase(Exception):
    """Fixed codes deliberately exclude untrusted response contents."""


class IntegrityError(IndexErrorBase):
    pass


class SourceUnavailable(IndexErrorBase):
    pass


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def quantity(value, bits=64):
    if not isinstance(value, str) or not re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)", value):
        raise IntegrityError("invalid_hex_quantity")
    number = int(value, 16)
    if number >= 1 << bits:
        raise IntegrityError("quantity_out_of_bounds")
    return number


def address(value, allow_zero=False):
    if not isinstance(value, str) or not ADDRESS.fullmatch(value):
        raise IntegrityError("invalid_address")
    result = value.lower()
    if not allow_zero and result == ZERO:
        raise IntegrityError("zero_participant_or_pool")
    return result


def hash_value(value):
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise IntegrityError("invalid_hash")
    return value.lower()


def header(value):
    if not isinstance(value, dict):
        raise IntegrityError("missing_block_header")
    return {"number": quantity(value.get("number")), "hash": hash_value(value.get("hash")),
            "parent_hash": hash_value(value.get("parentHash")), "timestamp": quantity(value.get("timestamp"))}


def same_header(left, right):
    if left != right:
        raise IntegrityError("canonical_header_disagreement")


class Budget:
    def __init__(self, maximum=MAX_CALLS, interval=.8, clock=time.monotonic, sleeper=time.sleep):
        if not 1 <= maximum <= MAX_CALLS or interval < .75:
            raise IntegrityError("invalid_rpc_budget")
        self.maximum, self.interval, self.clock, self.sleeper = maximum, interval, clock, sleeper
        self.calls, self.last = 0, None

    def take(self):
        if self.calls >= self.maximum:
            raise SourceUnavailable("rpc_budget_exhausted")
        if self.last is not None:
            self.sleeper(max(0, self.interval - (self.clock() - self.last)))
        self.calls += 1
        self.last = self.clock()
        return self.calls


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def http_json(url, payload=None, limit=MAX_RESPONSE):
    if url not in {OFFICIAL, WITNESS, SOURCE_URL}:
        raise IntegrityError("unapproved_http_source")
    request = Request(url, data=canonical(payload) if payload is not None else None,
                      headers={"Content-Type": "application/json", "User-Agent": "VariationalDashboard/1.0 bounded read-only factory index"})
    try:
        with build_opener(NoRedirect()).open(request, timeout=18) as response:
            if response.geturl().split("?", 1)[0] != url.split("?", 1)[0]:
                raise SourceUnavailable("unexpected_source_redirect")
            raw = response.read(limit + 1)
    except HTTPError as error:
        code = "http_rate_limited" if error.code == 429 else "http_source_unavailable"
        error.close()
        raise SourceUnavailable(code) from error
    except (URLError, TimeoutError) as error:
        raise SourceUnavailable("network_source_unavailable") from error
    if len(raw) > limit:
        raise SourceUnavailable("response_size_cap")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise IntegrityError("invalid_source_json") from error


class RPC:
    def __init__(self, url, budget, transport=http_json):
        if url not in {OFFICIAL, WITNESS}:
            raise IntegrityError("unapproved_rpc")
        self.url, self.budget, self.transport, self.headers = url, budget, transport, {}

    def call(self, method, params):
        if method not in {"eth_chainId", "eth_getBlockByNumber", "eth_getLogs", "eth_getCode", "eth_getTransactionReceipt"}:
            raise IntegrityError("rpc_method_not_read_only_allowlisted")
        request_id = self.budget.take()
        value = self.transport(self.url, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        if not isinstance(value, dict) or value.get("id") != request_id or value.get("jsonrpc") != "2.0":
            raise IntegrityError("rpc_response_identity")
        if "error" in value:
            message = str((value.get("error") or {}).get("message", "")).lower()
            code = "log_range_or_result_cap" if any(part in message for part in ("range too large", "exceeds limit", "too many results")) else "rpc_source_unavailable"
            raise SourceUnavailable(code)
        if value.get("result") is None:
            raise SourceUnavailable("rpc_result_unavailable")
        return value["result"]

    def block(self, number, fresh=False):
        if fresh or number not in self.headers:
            value = header(self.call("eth_getBlockByNumber", [hex(number), False]))
            if value["number"] != number:
                raise IntegrityError("wrong_block_number")
            self.headers[number] = value
        return self.headers[number]


def source_proof(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("abi"), list) or not all(isinstance(item, dict) for item in payload["abi"]):
        raise IntegrityError("invalid_factory_source_document")
    if (payload.get("chainId") != "42161" or address(payload.get("address")) != FACTORY
            or payload.get("creationMatch") != "exact_match" or payload.get("runtimeMatch") != "exact_match"
            or payload.get("proxyResolution", {}).get("isProxy") is not False):
        raise IntegrityError("factory_source_match_failed")
    deployment = payload.get("deployment", {})
    if str(deployment.get("blockNumber")) != str(DEPLOYMENT_BLOCK) or deployment.get("transactionHash", "").lower() != DEPLOYMENT_TX:
        raise IntegrityError("factory_deployment_changed")
    events = [entry for entry in payload.get("abi", []) if entry.get("type") == "event" and entry.get("name") == "PoolCreated"]
    if len(events) != 1 or events[0].get("anonymous") is not False:
        raise IntegrityError("factory_event_abi_missing")
    inputs = [(item.get("name"), item.get("type"), item.get("indexed")) for item in events[0].get("inputs", [])]
    signatures = payload.get("signatures", {}).get("event", [])
    if inputs != EVENT_INPUTS or not any(item.get("signature") == SIGNATURE and item.get("signatureHash32") == TOPIC for item in signatures):
        raise IntegrityError("factory_event_abi_changed")
    code = payload.get("runtimeBytecode", {}).get("onchainBytecode")
    if not isinstance(code, str) or not HEX.fullmatch(code) or len(code) < 4 or len(code) > 100_000 or len(code) % 2:
        raise IntegrityError("factory_runtime_missing")
    return {"source_url": SOURCE_URL, "source_sha256": digest(payload), "runtime_code_sha256": hashlib.sha256(bytes.fromhex(code[2:])).hexdigest(),
            "deployment_block": DEPLOYMENT_BLOCK, "deployment_transaction": DEPLOYMENT_TX,
            "event_signature": SIGNATURE, "event_topic": TOPIC, "event_abi": events[0]}, code.lower()


def verify_registry(primary, witness, proof, expected_code):
    for client in (primary, witness):
        if quantity(client.call("eth_chainId", [])) != 42161:
            raise IntegrityError("wrong_chain")
    stable = header(primary.call("eth_getBlockByNumber", ["finalized", False]))
    same_header(stable, witness.block(stable["number"]))
    for client in (primary, witness):
        code = client.call("eth_getCode", [FACTORY, hex(stable["number"])])
        if not isinstance(code, str) or code.lower() != expected_code:
            raise IntegrityError("factory_runtime_changed")
    receipt = primary.call("eth_getTransactionReceipt", [DEPLOYMENT_TX])
    if not isinstance(receipt, dict):
        raise IntegrityError("factory_deployment_receipt_mismatch")
    if (receipt.get("transactionHash", "").lower() != DEPLOYMENT_TX or quantity(receipt.get("status")) != 1
            or quantity(receipt.get("blockNumber")) != DEPLOYMENT_BLOCK or address(receipt.get("contractAddress")) != FACTORY):
        raise IntegrityError("factory_deployment_receipt_mismatch")
    deployment = primary.block(DEPLOYMENT_BLOCK)
    same_header(deployment, witness.block(DEPLOYMENT_BLOCK))
    if hash_value(receipt.get("blockHash")) != deployment["hash"]:
        raise IntegrityError("deployment_not_canonical")
    proof.update(deployment_block_hash=deployment["hash"], deployment_timestamp=iso(deployment["timestamp"]),
                 code_verified_at_block=stable["number"], code_verified_at_hash=stable["hash"])
    return stable


def normalized_log(raw, lower, upper):
    if not isinstance(raw, dict) or raw.get("removed") is not False or address(raw.get("address")) != FACTORY:
        raise IntegrityError("wrong_factory_or_removed_log")
    topics = raw.get("topics")
    if not isinstance(topics, list) or len(topics) != 3 or hash_value(topics[0]) != TOPIC:
        raise IntegrityError("unexpected_event_topic")
    block = quantity(raw.get("blockNumber"))
    if not lower <= block <= upper:
        raise IntegrityError("out_of_range_log")
    data = raw.get("data")
    if not isinstance(data, str) or not HEX.fullmatch(data) or len(data) > 270_000 or (len(data) - 2) % 64:
        raise IntegrityError("invalid_event_data")
    return {"block_number": block, "block_hash": hash_value(raw.get("blockHash")),
            "transaction_hash": hash_value(raw.get("transactionHash")), "transaction_index": quantity(raw.get("transactionIndex")),
            "log_index": quantity(raw.get("logIndex")), "topics": [hash_value(item) for item in topics], "data": data.lower()}


def normalized_logs(raw, lower, upper):
    if not isinstance(raw, list) or len(raw) >= MAX_LOGS:
        raise SourceUnavailable("log_result_cap_or_invalid_list")
    values = [normalized_log(item, lower, upper) for item in raw]
    values.sort(key=lambda item: (item["block_number"], item["transaction_index"], item["log_index"]))
    keys = [(item["transaction_hash"], item["log_index"]) for item in values]
    if len(set(keys)) != len(keys):
        raise IntegrityError("duplicate_log_identity")
    return values


def event(raw):
    words = [raw["data"][offset:offset + 64] for offset in range(2, len(raw["data"]), 64)]
    if len(words) < 8 or int(words[0], 16) != 192:
        raise IntegrityError("event_dynamic_offset")
    count = int(words[6], 16)
    if count < 1 or count > 4096 or len(words) != 7 + count:
        raise IntegrityError("event_participant_length")
    def word_address(word, allow_zero=False):
        if len(word) != 64 or word[:24] != "0" * 24:
            raise IntegrityError("address_padding")
        return address("0x" + word[24:], allow_zero)
    creator = word_address(raw["topics"][1][2:])
    participants = [creator] + [word_address(word) for word in words[7:]]
    if len(set(participants)) != len(participants):
        raise IntegrityError("duplicate_pool_participant")
    uuid, rfq, quote = int(raw["topics"][2], 16), int(words[2], 16), int(words[3], 16)
    if uuid == 0 or max(uuid, rfq, quote) >= 1 << 128:
        raise IntegrityError("uuid_bounds")
    fee = int(words[5], 16)
    paid_by = word_address(words[4], allow_zero=fee == 0)
    return {**raw, "creator": creator, "participants_at_creation": participants, "pool": word_address(words[1]),
            "pool_uuid": str(uuid), "rfq_uuid": str(rfq), "parent_quote_uuid": str(quote),
            "fee_paid_by": paid_by, "fee_amount_raw": str(fee)}


def logs_for(client, lower, upper):
    return normalized_logs(client.call("eth_getLogs", [{"address": FACTORY, "topics": [TOPIC],
                                                         "fromBlock": hex(lower), "toBlock": hex(upper)}]), lower, upper)


def verified_chunk(primary, witness, lower, upper):
    if lower < DEPLOYMENT_BLOCK or upper < lower or upper - lower >= 1000:
        raise IntegrityError("chunk_bounds")
    left, right = primary.block(lower), primary.block(upper)
    same_header(left, witness.block(lower)); same_header(right, witness.block(upper))
    observed = logs_for(primary, lower, upper)
    independent = []
    for start in range(lower, upper + 1, 50):
        independent.extend(logs_for(witness, start, min(start + 49, upper)))
    independent.sort(key=lambda item: (item["block_number"], item["transaction_index"], item["log_index"]))
    if observed != independent:
        raise IntegrityError("independent_log_set_disagreement")
    records = []
    for raw in observed:
        block = primary.block(raw["block_number"])
        same_header(block, witness.block(raw["block_number"]))
        if block["hash"] != raw["block_hash"]:
            raise IntegrityError("event_block_hash_mismatch")
        receipt = primary.call("eth_getTransactionReceipt", [raw["transaction_hash"]])
        if not isinstance(receipt, dict) or not isinstance(receipt.get("logs"), list):
            raise IntegrityError("event_receipt_mismatch")
        if (quantity(receipt.get("status")) != 1 or quantity(receipt.get("blockNumber")) != raw["block_number"]
                or hash_value(receipt.get("blockHash")) != raw["block_hash"]
                or hash_value(receipt.get("transactionHash")) != raw["transaction_hash"]):
            raise IntegrityError("event_receipt_mismatch")
        candidates = [item for item in receipt.get("logs", []) if str(item.get("address", "")).lower() == FACTORY
                      and item.get("topics", [None])[0] == TOPIC]
        if raw not in normalized_logs(candidates, lower, upper):
            raise IntegrityError("event_absent_from_receipt")
        records.append({**event(raw), "timestamp": iso(block["timestamp"])})
    # Numeric-range RPCs cannot pin a block hash. Recheck the boundary after the
    # complete two-provider scan; mismatch aborts the whole chunk transaction.
    same_header(right, primary.block(upper, fresh=True)); same_header(right, witness.block(upper, fresh=True))
    return {"from_block": lower, "to_block": upper, "from_hash": left["hash"], "to_hash": right["hash"],
            "from_timestamp": iso(left["timestamp"]), "to_timestamp": iso(right["timestamp"]),
            "log_count": len(records), "normalized_logs_sha256": digest(observed),
            "primary_rpc": OFFICIAL, "witness_rpc": WITNESS, "witness_page_blocks": 50,
            "verification": "two_provider_complete_range_log_set_agreement_plus_canonical_headers_and_receipts",
            "verified_at": now()}, records


def private_state_path(path):
    path = Path(path).expanduser().resolve()
    if path.is_relative_to((ROOT / "public").resolve()):
        raise IntegrityError("private_checkpoint_cannot_be_in_public")
    return path


def open_state(path):
    path = private_state_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    os.chmod(path, 0o600)
    connection.executescript("""
      CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS chunks (from_block INTEGER PRIMARY KEY, to_block INTEGER NOT NULL UNIQUE, proof TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS events (transaction_hash TEXT NOT NULL, log_index INTEGER NOT NULL,
        pool TEXT NOT NULL UNIQUE, pool_uuid TEXT NOT NULL UNIQUE, data TEXT NOT NULL,
        PRIMARY KEY(transaction_hash, log_index));
    """)
    return connection


def stored_chunks(connection):
    chunks = [json.loads(row[0]) for row in connection.execute("SELECT proof FROM chunks ORDER BY from_block")]
    for left, right in zip(chunks, chunks[1:]):
        if left["to_block"] + 1 != right["from_block"]:
            raise IntegrityError("checkpoint_coverage_gap")
    return chunks


def commit_chunk(connection, proof, records):
    chunks = stored_chunks(connection)
    if chunks and chunks[-1]["to_block"] + 1 != proof["from_block"]:
        raise IntegrityError("noncontiguous_chunk")
    if proof["log_count"] != len(records):
        raise IntegrityError("chunk_count_mismatch")
    try:
        with connection:
            for record in records:
                connection.execute("INSERT INTO events VALUES (?,?,?,?,?)", (record["transaction_hash"], record["log_index"],
                                   record["pool"], record["pool_uuid"], canonical(record).decode()))
            connection.execute("INSERT INTO chunks VALUES (?,?,?)", (proof["from_block"], proof["to_block"], canonical(proof).decode()))
    except sqlite3.IntegrityError as error:
        raise IntegrityError("conflicting_checkpoint_event") from error


def aggregate(connection, factory_proof, target):
    chunks = stored_chunks(connection)
    if not chunks:
        raise SourceUnavailable("no_verified_complete_chunk")
    records = [json.loads(row[0]) for row in connection.execute("SELECT data FROM events")]
    if sum(chunk["log_count"] for chunk in chunks) != len(records):
        raise IntegrityError("checkpoint_event_count_mismatch")
    def counts(values):
        participants = {person for value in values for person in value["participants_at_creation"]}
        return {"created_pools": len({value["pool"] for value in values}),
                "distinct_creator_accounts": len({value["creator"] for value in values}),
                "distinct_creation_participant_accounts": len(participants),
                "creation_participant_accounts_excluding_core_olp": len(participants - {OLP})}
    days = sorted({record["timestamp"][:10] for record in records})
    daily = [{"date": day, **counts([record for record in records if record["timestamp"].startswith(day)]),
              "coverage": "indexed_blocks_only", "complete_utc_day": False} for day in days]
    return {"schema_version": 1, "chain_id": 42161, "generated_at": now(),
            "factory": {"address": FACTORY, "registry_url": REGISTRY_URL, **factory_proof},
            "coverage": {"status": "complete_for_declared_blocks", "from_block": chunks[0]["from_block"],
                         "to_block": chunks[-1]["to_block"], "from_timestamp": chunks[0]["from_timestamp"],
                         "to_timestamp": chunks[-1]["to_timestamp"], "next_block": chunks[-1]["to_block"] + 1,
                         "requested_to_block": target, "requested_window_complete": chunks[-1]["to_block"] >= target,
                         "registry_complete_since_deployment": chunks[0]["from_block"] == DEPLOYMENT_BLOCK,
                         "current_factory_only": True, "ranges": chunks},
            "summary": counts(records), "daily": daily,
            "methodology": {"scope": "PoolCreated events from the currently documented factory within the declared block range only.",
                            "participants": "Distinct participant addresses named at creation. Excluding Core OLP does not establish a unique person, trader, or new account. OtherPartyAdded can change membership later.",
                            "cash_flows": "Pool deposit/withdraw summaries alone cannot establish a complete cash ledger. Deployed pool implementations and native-USDC Transfer coverage must be verified against a complete block-aware pool registry; internal pool transfers cancel at the chosen perimeter.",
                            "source_timestamps": "Canonical block headers, not optional log blockTimestamp fields, determine dates.",
                            "empty_blocks": "Every block in each declared range was queried on two RPC endpoints, including blocks with no matched events. Days outside those ranges are not zero.",
                            "verification_limit": "Two-provider agreement and canonical headers/receipts validate this bounded pilot; this is not an independent audit or evidence of complete historical factory migrations.",
                            "rpc_documentation": RPC_DOCS},
            "availability": {
                "trading_active_accounts": {"status": "unavailable", "reason": "Factory creation is not trading activity; complete trade/settlement semantics and coverage are unverified."},
                "protocol_net_flows": {"status": "unavailable", "reason": "Complete historical pool registry and native-USDC Transfer coverage are not yet established."},
                "referral_claims": {"status": "unverified", "reason": "Current official registry and referral documentation do not establish a canonical claims contract/event. Factory creation fees are not referral claims.", "source_url": "https://docs.variational.io/omni/rewards/referrals"}}}


def validate_public(payload):
    """Validate a published aggregate without RPC access or the private database.

    This independently checks schema/provenance/coverage and aggregate identities;
    it cannot recreate private log-set agreement from counts alone.
    """
    def integer(value, minimum=0):
        if type(value) is not int or not minimum <= value <= 2**53 - 1:
            raise IntegrityError("invalid_public_integer")
        return value

    def timestamp(value):
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
            raise IntegrityError("invalid_public_timestamp")
        try:
            return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError as error:
            raise IntegrityError("invalid_public_timestamp") from error

    def sha(value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise IntegrityError("invalid_public_digest")

    def counts(value):
        if not isinstance(value, dict):
            raise IntegrityError("invalid_public_counts")
        pools, creators, participants, excluding = [integer(value.get(key)) for key in (
            "created_pools", "distinct_creator_accounts", "distinct_creation_participant_accounts", "creation_participant_accounts_excluding_core_olp")]
        if (creators > pools or participants > pools * 4097 or participants < creators
                or participants - excluding not in (0, 1)
                or (pools > 0 and (creators == 0 or participants < 2))
                or (pools == 0 and (creators or participants or excluding))):
            raise IntegrityError("inconsistent_public_counts")
        return pools

    def fields(value, allowed):
        if not isinstance(value, dict) or not set(value) <= set(allowed):
            raise IntegrityError("unexpected_public_fields")

    if not isinstance(payload, dict) or payload.get("schema_version") != 1 or payload.get("chain_id") != 42161:
        raise IntegrityError("invalid_public_chain_or_schema")
    fields(payload, ("schema_version", "chain_id", "generated_at", "factory", "coverage", "summary", "daily", "methodology", "availability"))
    timestamp(payload.get("generated_at"))
    factory, coverage = payload.get("factory", {}), payload.get("coverage", {})
    if not isinstance(factory, dict) or not isinstance(coverage, dict):
        raise IntegrityError("invalid_public_registry_or_coverage")
    fields(factory, ("address", "registry_url", "source_url", "source_sha256", "runtime_code_sha256", "deployment_block", "deployment_transaction", "event_signature", "event_topic", "event_abi", "deployment_block_hash", "deployment_timestamp", "code_verified_at_block", "code_verified_at_hash"))
    fields(coverage, ("status", "from_block", "to_block", "from_timestamp", "to_timestamp", "next_block", "requested_to_block", "requested_window_complete", "registry_complete_since_deployment", "current_factory_only", "ranges"))
    if (factory.get("address") != FACTORY or factory.get("registry_url") != REGISTRY_URL or factory.get("source_url") != SOURCE_URL
            or factory.get("deployment_block") != DEPLOYMENT_BLOCK or factory.get("deployment_transaction") != DEPLOYMENT_TX
            or factory.get("event_signature") != SIGNATURE or factory.get("event_topic") != TOPIC):
        raise IntegrityError("invalid_public_factory_registry")
    event_abi = factory.get("event_abi", {})
    if (not isinstance(event_abi, dict) or not isinstance(event_abi.get("inputs"), list)
            or not all(isinstance(item, dict) for item in event_abi["inputs"])):
        raise IntegrityError("invalid_public_event_abi")
    fields(event_abi, ("name", "type", "inputs", "anonymous"))
    for item in event_abi["inputs"]:
        fields(item, ("name", "type", "indexed", "internalType"))
    if (event_abi.get("type") != "event" or event_abi.get("name") != "PoolCreated" or event_abi.get("anonymous") is not False
            or [(item.get("name"), item.get("type"), item.get("indexed")) for item in event_abi.get("inputs", [])] != EVENT_INPUTS):
        raise IntegrityError("invalid_public_event_abi")
    for key in ("source_sha256", "runtime_code_sha256"):
        sha(factory.get(key))
    hash_value(factory.get("deployment_block_hash")); hash_value(factory.get("code_verified_at_hash"))
    timestamp(factory.get("deployment_timestamp"))
    integer(factory.get("code_verified_at_block"), DEPLOYMENT_BLOCK)
    lower, upper = integer(coverage.get("from_block"), DEPLOYMENT_BLOCK), integer(coverage.get("to_block"), DEPLOYMENT_BLOCK)
    target = integer(coverage.get("requested_to_block"), DEPLOYMENT_BLOCK)
    if (upper < lower or target < upper or coverage.get("next_block") != upper + 1
            or coverage.get("status") != "complete_for_declared_blocks"
            or coverage.get("current_factory_only") is not True
            or coverage.get("requested_window_complete") is not (upper == target)
            or coverage.get("registry_complete_since_deployment") is not (lower == DEPLOYMENT_BLOCK)
            or factory["code_verified_at_block"] < upper):
        raise IntegrityError("invalid_public_coverage")
    first_time, last_time = timestamp(coverage.get("from_timestamp")), timestamp(coverage.get("to_timestamp"))
    ranges = coverage.get("ranges")
    if not isinstance(ranges, list) or not ranges:
        raise IntegrityError("missing_public_ranges")
    cursor, total_logs, previous_time = lower, 0, first_time
    for item in ranges:
        if not isinstance(item, dict):
            raise IntegrityError("invalid_public_range_proof")
        fields(item, ("from_block", "to_block", "from_hash", "to_hash", "from_timestamp", "to_timestamp", "log_count", "normalized_logs_sha256", "primary_rpc", "witness_rpc", "witness_page_blocks", "verification", "verified_at"))
        start, end = integer(item.get("from_block"), DEPLOYMENT_BLOCK), integer(item.get("to_block"), DEPLOYMENT_BLOCK)
        start_time, end_time = timestamp(item.get("from_timestamp")), timestamp(item.get("to_timestamp"))
        count = integer(item.get("log_count"))
        if (start != cursor or end < start or end - start >= 1000 or count >= MAX_LOGS
                or start_time < previous_time or end_time < start_time or end_time > last_time
                or item.get("primary_rpc") != OFFICIAL or item.get("witness_rpc") != WITNESS
                or item.get("witness_page_blocks") != 50
                or item.get("verification") != "two_provider_complete_range_log_set_agreement_plus_canonical_headers_and_receipts"):
            raise IntegrityError("invalid_public_range_proof")
        hash_value(item.get("from_hash")); hash_value(item.get("to_hash")); sha(item.get("normalized_logs_sha256"))
        timestamp(item.get("verified_at"))
        cursor, total_logs, previous_time = end + 1, total_logs + count, end_time
    count_fields = ("created_pools", "distinct_creator_accounts", "distinct_creation_participant_accounts", "creation_participant_accounts_excluding_core_olp")
    fields(payload.get("summary"), count_fields)
    if (cursor != upper + 1 or first_time > last_time
            or ranges[0]["from_timestamp"] != coverage["from_timestamp"] or ranges[-1]["to_timestamp"] != coverage["to_timestamp"]
            or counts(payload.get("summary")) != total_logs):
        raise IntegrityError("public_range_summary_mismatch")
    daily = payload.get("daily")
    if not isinstance(daily, list):
        raise IntegrityError("invalid_daily_aggregate")
    days, daily_total = [], 0
    for item in daily:
        if not isinstance(item, dict):
            raise IntegrityError("invalid_daily_aggregate")
        fields(item, (*count_fields, "date", "coverage", "complete_utc_day"))
        day = item.get("date")
        if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise IntegrityError("invalid_daily_date")
        timestamp(day + "T00:00:00Z")
        if (day < coverage["from_timestamp"][:10] or day > coverage["to_timestamp"][:10]
                or item.get("coverage") != "indexed_blocks_only" or item.get("complete_utc_day") is not False):
            raise IntegrityError("unsupported_daily_coverage")
        days.append(day); daily_total += counts(item)
    if days != sorted(set(days)) or daily_total != total_logs:
        raise IntegrityError("daily_summary_mismatch")
    available = payload.get("availability", {})
    if not isinstance(available, dict):
        raise IntegrityError("unsupported_metric_availability")
    fields(available, ("trading_active_accounts", "protocol_net_flows", "referral_claims"))
    for key, status in (("trading_active_accounts", "unavailable"), ("protocol_net_flows", "unavailable"), ("referral_claims", "unverified")):
        if not isinstance(available.get(key), dict) or available[key].get("status") != status or not isinstance(available[key].get("reason"), str):
            raise IntegrityError("unsupported_metric_availability")
        fields(available[key], ("status", "reason", "source_url"))
    fields(payload.get("methodology"), ("scope", "participants", "cash_flows", "source_timestamps", "empty_blocks", "verification_limit", "rpc_documentation"))
    return payload


def save_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as handle:
            name = handle.name
            json.dump(value, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
        os.chmod(name, 0o644)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def run(state, output, from_block=None, to_block=None, chunk_size=1000, max_calls=MAX_CALLS, transport=http_json):
    state = private_state_path(state)
    if not 1 <= chunk_size <= 1000:
        raise IntegrityError("invalid_chunk_size")
    budget = Budget(max_calls)
    primary, witness = RPC(OFFICIAL, budget, transport), RPC(WITNESS, budget, transport)
    proof, code = source_proof(transport(SOURCE_URL, limit=8_000_000))
    stable = verify_registry(primary, witness, proof, code)
    target = stable["number"] if to_block is None else to_block
    if not isinstance(target, int) or target < DEPLOYMENT_BLOCK or target > stable["number"]:
        raise IntegrityError("target_not_finalized")
    connection = open_state(state)
    changed = 0
    failure = None
    try:
        chunks = stored_chunks(connection)
        start = chunks[-1]["to_block"] + 1 if chunks else DEPLOYMENT_BLOCK if from_block is None else from_block
        if (from_block is not None and from_block != start) or start < DEPLOYMENT_BLOCK:
            raise IntegrityError("resume_start_mismatch")
        prior = connection.execute("SELECT value FROM metadata WHERE key='factory_runtime'").fetchone()
        if prior and prior[0] != proof["runtime_code_sha256"]:
            raise IntegrityError("checkpoint_factory_runtime_changed")
        if chunks:
            previous = primary.block(chunks[-1]["to_block"])
            same_header(previous, witness.block(previous["number"]))
            if previous["hash"] != chunks[-1]["to_hash"]:
                raise IntegrityError("checkpoint_reorg")
        with connection:
            connection.execute("INSERT OR REPLACE INTO metadata VALUES ('factory_runtime',?)", (proof["runtime_code_sha256"],))
        while start <= target:
            end = min(target, start + chunk_size - 1)
            try:
                chunk, records = verified_chunk(primary, witness, start, end)
            except SourceUnavailable as error:
                failure = str(error)
                break
            commit_chunk(connection, chunk, records)
            changed += 1
            start = end + 1
        if changed:
            save_atomic(output, validate_public(aggregate(connection, proof, target)))
        elif not Path(output).exists() and stored_chunks(connection):
            save_atomic(output, validate_public(aggregate(connection, proof, target)))
        return {"new_complete_chunks": changed, "rpc_calls": budget.calls, "next_block": start,
                "failure": failure, "published": Path(output).exists()}
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path.home() / ".cache" / "variational-dashboard" / "arbitrum-index.sqlite")
    parser.add_argument("--output", type=Path, default=ROOT / "public" / "data" / "arbitrum-index.json")
    parser.add_argument("--from-block", type=int)
    parser.add_argument("--to-block", type=int)
    parser.add_argument("--chunk-size", type=int, default=1000)
    parser.add_argument("--max-calls", type=int, default=MAX_CALLS)
    parser.add_argument("--verify", action="store_true", help="Validate existing public aggregates without network access")
    args = parser.parse_args()
    try:
        if args.verify:
            payload = validate_public(json.loads(args.output.read_text(encoding="utf-8")))
            print(json.dumps({"verified": True, "created_pools": payload["summary"]["created_pools"],
                              "from_block": payload["coverage"]["from_block"], "to_block": payload["coverage"]["to_block"]}))
            return 0
        result = run(args.state, args.output, args.from_block, args.to_block, args.chunk_size, args.max_calls)
        print(json.dumps(result))
        return 0 if result["published"] else 2
    except (IndexErrorBase, OSError, sqlite3.Error) as error:
        print(json.dumps({"error": str(error) if isinstance(error, IndexErrorBase) else "local_state_error", "last_good_retained": True}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
