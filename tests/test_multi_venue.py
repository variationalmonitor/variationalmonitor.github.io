import copy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from multi_venue import costs, normalize_side, validate


class MultiVenueTest(unittest.TestCase):
    def setUp(self):
        self.dataset=json.loads((Path(__file__).resolve().parents[1]/"public/data/multi-venue.json").read_text())
        self.time=datetime(2026,11,21,15,12,8,tzinfo=timezone.utc)
        self.quote={"updated_at":"2026-11-21T15:12:08Z","size_1k":{"bid":"99","ask":"101"}}
        self.book=("BTC",self.time,[["99","20"]],[["101","20"]],Decimal(1))

    def test_depth_math_and_stale_or_inadequate_books(self):
        rows,status=costs("hyperliquid","BTC",self.quote,self.book,self.time)
        self.assertEqual(status,"ok")
        self.assertEqual(len(rows),1)
        self.assertEqual((rows[0]["omni_bps"],rows[0]["book_bps"],rows[0]["fee_bps"],rows[0]["total_bps"]),(200,200,9,209))
        old=dict(self.quote,updated_at="2026-11-21T15:11:01Z")
        self.assertEqual(costs("hyperliquid","BTC",old,self.book,self.time),([],"stale_source"))
        thin=("BTC",self.time,[["99","1"]],[["101","20"]],Decimal(1))
        self.assertEqual(costs("hyperliquid","BTC",self.quote,thin,self.time),([],"insufficient_depth"))

    def test_duplicate_levels_or_wrong_side_are_invalid(self):
        for levels in ([["101","1"],["101","2"]],[["101","1"],["100","2"]]):
            with self.assertRaises(ValueError):
                normalize_side(levels,Decimal(1),False)

    def test_stored_fee_derivation_and_embargo(self):
        validate(self.dataset)
        wrong=copy.deepcopy(self.dataset)
        row=next(item for item in wrong["rows"] if item["venue"]=="hyperliquid")
        row["fee_bps"]=0
        row["total_bps"]=row["book_bps"]
        with self.assertRaisesRegex(ValueError,"pricing inputs"):
            validate(wrong)
        wrong=copy.deepcopy(self.dataset)
        wrong["rows"][0].update(venue="okx",symbol="BTC-USDT-SWAP",currency="USDT")
        with self.assertRaisesRegex(ValueError,"Unknown venue"):
            validate(wrong)
