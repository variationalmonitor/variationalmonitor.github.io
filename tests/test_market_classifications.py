import copy
import json
from pathlib import Path
import unittest
from tools import market_classifications as c

class ClassificationTests(unittest.TestCase):
    def data(self):return json.loads(Path('public/data/market-classifications.json').read_text())
    def test_curated_registry_and_no_unapproved_metadata_source(self):
        data=self.data();c.validate(data)
        self.assertNotIn('supported_assets',json.dumps(data));self.assertTrue(data['coverage']['unclassified_count']>0)
    def test_ticker_collision_rejects_duplicates(self):
        data=self.data();data['mappings'].append(copy.deepcopy(data['mappings'][0]))
        with self.assertRaises(ValueError):c.validate(data)
    def test_wrong_scope_and_untrusted_source_reject(self):
        for key,value in [('source_urls',['https://example.com']),('scope','crypto')]:
            data=self.data();row=next(r for r in data['mappings'] if r['scope']=='tradfi');row[key]=value
            with self.assertRaises(ValueError):c.validate(data)
    def test_no_invented_listing_dates(self):
        self.assertTrue(all(row['listed_at'] is None for row in self.data()['mappings']))

if __name__=='__main__':unittest.main()
