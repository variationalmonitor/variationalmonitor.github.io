import copy
from contextlib import closing
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import arbitrum_index as index

LOWER = 417159094
UPPER = LOWER + 999
PARTICIPANT = "0x" + "1" * 40
POOL = "0x" + "2" * 40


def word(value):
    return f"{value:064x}"


def address_word(value):
    return value[2:].zfill(64)


def block(number):
    return {"number": number, "hash": "0x" + word(number), "parent_hash": "0x" + word(number - 1),
            "timestamp": 1767366000 + number - LOWER}


def raw_log(number=LOWER + 49, uuid=7, pool=POOL):
    return {"address": index.FACTORY, "removed": False,
            "topics": [index.TOPIC, "0x" + address_word(index.OLP), "0x" + word(uuid)],
            "data": "0x" + "".join([word(192), address_word(pool), word(0), word(0), address_word(PARTICIPANT), word(0), word(1), address_word(PARTICIPANT)]),
            "blockNumber": hex(number), "blockHash": block(number)["hash"], "blockTimestamp": "0x0",
            "transactionHash": "0x" + word(uuid), "transactionIndex": "0x0", "logIndex": "0x1"}


def source():
    return {"chainId": "42161", "address": index.FACTORY, "creationMatch": "exact_match", "runtimeMatch": "exact_match",
            "proxyResolution": {"isProxy": False}, "deployment": {"blockNumber": str(index.DEPLOYMENT_BLOCK), "transactionHash": index.DEPLOYMENT_TX},
            "abi": [{"name": "PoolCreated", "type": "event", "anonymous": False,
                     "inputs": [{"name": name, "type": kind, "indexed": indexed} for name, kind, indexed in index.EVENT_INPUTS]}],
            "signatures": {"event": [{"signature": index.SIGNATURE, "signatureHash32": index.TOPIC}]},
            "runtimeBytecode": {"onchainBytecode": "0x6000"}}


class FakeRPC:
    def __init__(self, logs, witness=False, reorg=False):
        self.logs, self.witness, self.reorg, self.queries = logs, witness, reorg, []

    def block(self, number, fresh=False):
        value = block(number)
        if fresh and self.reorg:
            value["hash"] = "0x" + "f" * 64
        return value

    def call(self, method, params):
        if method == "eth_getLogs":
            query = params[0]
            start, end = int(query["fromBlock"], 16), int(query["toBlock"], 16)
            self.queries.append((start, end))
            if self.witness and end - start + 1 > 50:
                raise AssertionError("Witness request exceeds documented observed limit")
            return [item for item in self.logs if start <= int(item["blockNumber"], 16) <= end]
        if method == "eth_getTransactionReceipt":
            item = next(item for item in self.logs if item["transactionHash"] == params[0])
            return {"status": "0x1", "transactionHash": item["transactionHash"], "blockNumber": item["blockNumber"],
                    "blockHash": item["blockHash"], "logs": [item]}
        raise AssertionError(method)


class FactoryIndexTests(unittest.TestCase):
    def test_exact_source_event_and_deployment_are_pinned(self):
        proof, code = index.source_proof(source())
        self.assertEqual(code, "0x6000")
        self.assertEqual(proof["deployment_block"], 300692000)
        for mutate in [lambda value: value.update(runtimeMatch="match"),
                       lambda value: value["abi"][0]["inputs"][1].update(indexed=True),
                       lambda value: value["deployment"].update(blockNumber="1")]:
            value = source(); mutate(value)
            with self.assertRaises(index.IntegrityError): index.source_proof(value)

    def test_decode_dynamic_addresses_and_uuid_without_number_rounding(self):
        identifier = (1 << 128) - 1
        value = index.event(index.normalized_log(raw_log(uuid=identifier), LOWER, UPPER))
        self.assertEqual(value["pool_uuid"], str(identifier))
        self.assertEqual(value["participants_at_creation"], [index.OLP, PARTICIPANT])
        self.assertEqual(value["fee_amount_raw"], "0")
        self.assertEqual(value["pool"], POOL)

    def test_rejects_removed_wrong_contract_wrong_topic_outside_range_and_invalid_abi(self):
        for field, value in [("removed", True), ("address", PARTICIPANT), ("blockNumber", hex(LOWER - 1)),
                             ("topics", [index.TOPIC]), ("data", "0x00")]:
            raw = raw_log(); raw[field] = value
            with self.assertRaises(index.IntegrityError): index.normalized_log(raw, LOWER, UPPER)
        raw = index.normalized_log(raw_log(), LOWER, UPPER)
        raw["data"] = "0x" + word(160) + raw["data"][66:]
        with self.assertRaisesRegex(index.IntegrityError, "dynamic_offset"): index.event(raw)
        with self.assertRaises(index.SourceUnavailable): index.normalized_logs([raw_log()] * 10000, LOWER, UPPER)
        with self.assertRaises(index.IntegrityError): index.normalized_logs([raw_log(), raw_log()], LOWER, UPPER)

    def test_entire_interval_and_empty_pages_are_compared_before_accepting_canonical_logs(self):
        logs = [raw_log(LOWER + 49), raw_log(LOWER + 900, 8, "0x" + "3" * 40)]
        first, witness = FakeRPC(logs), FakeRPC(logs, witness=True)
        proof, records = index.verified_chunk(first, witness, LOWER, UPPER)
        self.assertEqual(first.queries, [(LOWER, UPPER)])
        self.assertEqual(witness.queries, [(value, value + 49) for value in range(LOWER, UPPER + 1, 50)])
        self.assertEqual(proof["log_count"], 2)
        self.assertTrue(all(not record["timestamp"].startswith("1970") for record in records))
        with self.assertRaisesRegex(index.IntegrityError, "log_set_disagreement"):
            index.verified_chunk(FakeRPC(logs), FakeRPC(logs[:1], witness=True), LOWER, UPPER)

    def test_reorg_after_scan_aborts_the_chunk(self):
        with self.assertRaisesRegex(index.IntegrityError, "header_disagreement"):
            index.verified_chunk(FakeRPC([raw_log()], reorg=True), FakeRPC([raw_log()], witness=True), LOWER, UPPER)

    def test_private_checkpoint_is_atomic_contiguous_and_publishes_only_aggregate_scope(self):
        proof, records = index.verified_chunk(FakeRPC([raw_log()]), FakeRPC([raw_log()], witness=True), LOWER, UPPER)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            connection = index.open_state(path)
            index.commit_chunk(connection, proof, records)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            public = index.aggregate(connection, {"event_topic": index.TOPIC}, UPPER)
            self.assertEqual(public["summary"]["created_pools"], 1)
            self.assertEqual(public["summary"]["distinct_creation_participant_accounts"], 2)
            self.assertEqual(public["summary"]["creation_participant_accounts_excluding_core_olp"], 1)
            self.assertFalse(public["coverage"]["registry_complete_since_deployment"])
            self.assertFalse(public["daily"][0]["complete_utc_day"])
            self.assertNotIn(PARTICIPANT, json.dumps(public))
            self.assertNotIn(POOL, json.dumps(public))
            self.assertEqual(public["availability"]["trading_active_accounts"]["status"], "unavailable")
            bad = dict(proof, from_block=UPPER + 2, to_block=UPPER + 100)
            with self.assertRaisesRegex(index.IntegrityError, "noncontiguous"):
                index.commit_chunk(connection, bad, records)
            conflict = dict(proof, from_block=UPPER + 1, to_block=UPPER + 1000)
            with self.assertRaisesRegex(index.IntegrityError, "conflicting_checkpoint_event"):
                index.commit_chunk(connection, conflict, records)
            self.assertEqual(len(index.stored_chunks(connection)), 1)
            connection.close()

    def test_source_failure_keeps_last_good_output_and_unverified_chunk_out_of_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            state, output = Path(directory) / "state.sqlite", Path(directory) / "data.json"
            output.write_bytes(b'{"last_good":true}\n')
            with patch.object(index, "source_proof", return_value=({"runtime_code_sha256": "abc"}, "0x6000")), \
                 patch.object(index, "verify_registry", return_value={"number": UPPER + 1000}), \
                 patch.object(index, "verified_chunk", side_effect=index.SourceUnavailable("http_rate_limited")):
                result = index.run(state, output, LOWER, UPPER, transport=lambda *args, **kwargs: {})
            self.assertEqual(result["new_complete_chunks"], 0)
            self.assertEqual(output.read_bytes(), b'{"last_good":true}\n')
            with closing(sqlite3.connect(state)) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 0)

    def test_rpc_budget_and_read_only_allowlist(self):
        budget = index.Budget(1, sleeper=lambda value: None)
        client = index.RPC(index.OFFICIAL, budget, transport=lambda url, payload: {"jsonrpc": "2.0", "id": payload["id"], "result": "0xa4b1"})
        self.assertEqual(client.call("eth_chainId", []), "0xa4b1")
        with self.assertRaises(index.SourceUnavailable): client.call("eth_chainId", [])
        with self.assertRaises(index.IntegrityError): client.call("eth_sendRawTransaction", ["private"])
        with self.assertRaises(index.IntegrityError): index.RPC("https://example.com", budget)

    def test_public_artifact_validation_rejects_scope_count_and_registry_corruption(self):
        chunk, records = index.verified_chunk(FakeRPC([raw_log()]), FakeRPC([raw_log()], witness=True), LOWER, UPPER)
        factory, _ = index.source_proof(source())
        factory.update(deployment_block_hash="0x" + word(index.DEPLOYMENT_BLOCK), deployment_timestamp="2025-01-01T00:00:00Z",
                       code_verified_at_block=UPPER + 1000, code_verified_at_hash="0x" + word(UPPER + 1000))
        with tempfile.TemporaryDirectory() as directory:
            connection = index.open_state(Path(directory) / "state.sqlite")
            index.commit_chunk(connection, chunk, records)
            public = index.aggregate(connection, factory, UPPER)
            self.assertIs(index.validate_public(public), public)
            mutations = [lambda item: item.update(chain_id=1),
                         lambda item: item["factory"].update(address=PARTICIPANT),
                         lambda item: item["coverage"].update(registry_complete_since_deployment=True),
                         lambda item: item["coverage"].update(next_block=UPPER + 10),
                         lambda item: item["coverage"]["ranges"][0].update(log_count=2),
                         lambda item: item["daily"][0].update(complete_utc_day=True),
                         lambda item: item["summary"].update(created_pools=100),
                         lambda item: item["availability"]["trading_active_accounts"].update(status="available"),
                         lambda item: item.update(events=[{"participant": PARTICIPANT}]),
                         lambda item: item["summary"].update(participant_addresses=[PARTICIPANT]),
                         lambda item: item.update(factory=[])]
            for mutate in mutations:
                bad = copy.deepcopy(public); mutate(bad)
                with self.assertRaises(index.IntegrityError): index.validate_public(bad)
            connection.close()

    def test_raw_checkpoint_paths_cannot_resolve_inside_public_before_any_network_call(self):
        transport = unittest.mock.Mock(side_effect=AssertionError("Network must not be called"))
        with self.assertRaisesRegex(index.IntegrityError, "cannot_be_in_public"):
            index.run(index.ROOT / "public" / "raw.sqlite", Path("unused.json"), transport=transport)
        transport.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            linked = Path(directory) / "linked-public"
            linked.symlink_to(index.ROOT / "public", target_is_directory=True)
            with self.assertRaisesRegex(index.IntegrityError, "cannot_be_in_public"):
                index.open_state(linked / "raw.sqlite")

    def test_http_redirect_is_not_followed_even_to_another_local_path(self):
        requested = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requested.append(self.path)
                if self.path == "/start":
                    self.send_response(302)
                    self.send_header("Location", "/must-not-be-contacted")
                    self.end_headers()
                else:
                    self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
            def log_message(self, *args):
                pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            endpoint = "http://127.0.0.1:" + str(server.server_port) + "/start"
            with patch.object(index, "OFFICIAL", endpoint):
                with self.assertRaises(index.SourceUnavailable): index.http_json(endpoint)
            self.assertEqual(requested, ["/start"])
        finally:
            server.shutdown(); thread.join(); server.server_close()


if __name__ == "__main__":
    unittest.main()
