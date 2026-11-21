import copy
from contextlib import closing
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import arbitrum_bulk as bulk
import arbitrum_index as core
from test_arbitrum_index import FakeRPC, raw_log, LOWER, UPPER


class PrimaryBulkTests(unittest.TestCase):
    def test_pages_remain_explicitly_primary_only_and_preserve_exact_source_fields(self):
        client = FakeRPC([raw_log()])
        proof, records = bulk.primary_page(client, LOWER, LOWER + 99999)
        self.assertEqual(proof["verification"], "primary_only_unreconciled")
        self.assertEqual(proof["log_count"], 1)
        self.assertNotIn("timestamp", records[0])
        self.assertEqual(client.queries, [(LOWER, LOWER + 99999)])

    def test_private_state_aggregates_and_duplicate_rollback(self):
        proof, records = bulk.primary_page(FakeRPC([raw_log()]), LOWER, UPPER)
        with tempfile.TemporaryDirectory() as directory, closing(bulk.open_state(Path(directory) / "bulk.sqlite")) as connection:
            bulk.commit_page(connection, proof, records)
            result = bulk.summary(connection)
            self.assertEqual(result["created_pools"], 1)
            self.assertEqual(result["distinct_creation_participants"], 2)
            self.assertEqual(result["creation_participants_excluding_core_olp"], 1)
            self.assertFalse(result["public_export"])
            bad = copy.deepcopy(proof)
            bad.update(from_block=UPPER + 1, to_block=UPPER + 1000, from_parent_hash=proof["to_hash"])
            with self.assertRaises(core.IntegrityError): bulk.commit_page(connection, bad, records)
            self.assertEqual(bulk.summary(connection)["completed_pages"], 1)
            self.assertEqual(bulk.summary(connection)["created_pools"], 1)

    def test_bulk_cannot_write_within_repository_or_reuse_verified_pilot_database(self):
        transport = Mock(side_effect=AssertionError("No network expected"))
        for path in [core.ROOT / "public" / "raw.sqlite", core.ROOT / "raw.sqlite"]:
            with self.assertRaises(core.IntegrityError): bulk.run(path, transport=transport)
        transport.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pilot.sqlite"
            core.open_state(path).close()
            with self.assertRaisesRegex(core.IntegrityError, "not_a_primary_bulk_checkpoint"):
                bulk.open_state(path)

    def test_cap_or_post_read_reorg_never_creates_a_page(self):
        with self.assertRaises(core.IntegrityError):
            bulk.primary_page(FakeRPC([raw_log()], reorg=True), LOWER, UPPER)
        with self.assertRaises(core.SourceUnavailable):
            bulk.primary_page(FakeRPC([raw_log()] * 10000), LOWER, UPPER)


if __name__ == "__main__":
    unittest.main()
