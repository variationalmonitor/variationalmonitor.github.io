import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tools import financials as f, reported_rewards as r
from tests.test_financials import REPORT, NOW, source, message, dataset

TEXT=REPORT+"Total Rewards Claimed: $9.31M\nTotal Losses Refunded (Sunset): $4.60M\nTotal Referral Rewards Claimed: $4.71M\n"


class RewardsTests(unittest.TestCase):
    def record(self,text=TEXT): return r.parse(text,source(),dataset())
    def test_exact_source_units_and_rounding(self):
        row=self.record();field=row['fields']['total_referral_rewards_claimed']
        self.assertEqual(field['value'],'4710000.00');self.assertEqual(field['raw'],'$4.71M')
        self.assertTrue(field['rounded']);self.assertEqual(field['precision_increment'],'10000')
        self.assertEqual(row['fields']['referral_rewards']['value'],'-217813')
        self.assertIsNone(row['fields']['loss_refunds']);self.assertIsNone(row['period_end'])
    def test_missing_is_not_zero_and_real_zero_retained(self):
        self.assertIsNone(self.record()['fields']['loss_refunds'])
        self.assertEqual(self.record(TEXT+'Loss Refunds: $0\n')['fields']['loss_refunds']['value'],'0')
    def test_native_sunset_loss_refund_label_is_preserved(self):
        row=self.record(TEXT+'Loss Refunds (Sunset): ($47,134)\n')
        self.assertEqual(row['fields']['loss_refunds']['value'],'-47134')
        self.assertEqual(row['fields']['loss_refunds']['label'],'Loss Refunds (Sunset)')
    def test_cumulative_counters_never_summed(self):
        data=r.finalize([self.record()],NOW)
        self.assertNotIn('totals',data)
        self.assertEqual(data['reports'][0]['fields']['total_losses_refunded']['kind'],'cumulative_reported')
    def test_bad_source_forward_and_financial_mismatch_fail(self):
        for change in [{'author':'fake'},{'forwarded':True},{'url':'https://example.com/459'}]:
            s=source();s.update(change)
            with self.assertRaises(f.SourceError):r.parse(TEXT,s,dataset())
        with self.assertRaises(f.SourceError):self.record(TEXT.replace('$2,991,123','$2,991,124'))
    def test_wrong_sign_duplicate_and_all_missing_reject(self):
        for text in [TEXT+'Total Rewards Claimed: $1\n',TEXT.replace('($217,813)\n2w Net Profit','$217,813\n2w Net Profit'),TEXT.replace('$9.31M','($9.31M)')]:
            with self.assertRaises(f.SourceError):self.record(text)
        self.assertIsNone(r.parse('ordinary announcement',source(),dataset()))
    def test_feed_ignores_forwarded_and_link_previews(self):
        self.assertEqual(r.feed_records(message(TEXT,forwarded=True),NOW,dataset()),[])
        rows=r.feed_records(message(TEXT,preview='Total Rewards Claimed: $999999'),NOW,dataset())
        self.assertEqual(rows[0]['fields']['total_rewards_claimed']['raw'],'$9.31M')
    def test_unchanged_does_not_mutate_and_revision_retains_previous(self):
        rows=[self.record()];before=copy.deepcopy(rows)
        self.assertFalse(r.merge(rows,self.record(),NOW));self.assertEqual(rows,before)
        changed=self.record(TEXT.replace('$9.31M','$9.32M'))
        self.assertTrue(r.merge(rows,changed,NOW));self.assertEqual(rows[0]['revisions'][0]['fields']['total_rewards_claimed']['raw'],'$9.31M')
    def test_mirror_conflict_fails_without_overwriting(self):
        row=self.record();incoming=copy.deepcopy(row)
        incoming['sources'][0].update(platform='x',id='999',url='https://x.com/variational_io/status/999')
        incoming['fields']['total_rewards_claimed']['raw']='$9.32M'
        before=copy.deepcopy(row)
        with self.assertRaises(f.SourceError):r.merge([row],incoming,NOW)
        self.assertEqual(row,before)
    def test_rounded_components_do_not_create_fake_precision_warning(self):
        data=r.finalize([self.record(TEXT.replace('$9.31M','$9.32M'))],NOW)
        self.assertEqual(data['reports'][0]['warnings'],[])
    def test_exact_source_residual_preserved_as_warning(self):
        text=TEXT.replace('$9.31M','$9310001').replace('$4.60M','$4600000').replace('$4.71M','$4710000')
        data=r.finalize([self.record(text)],NOW)
        self.assertEqual(data['reports'][0]['warnings'][0]['residual'],'1')
    def test_precision_tampering_fails_validation(self):
        data=r.finalize([self.record()],NOW);data['reports'][0]['fields']['total_rewards_claimed']['rounded']=False
        with self.assertRaises(f.SourceError):r.validate(data)
        for key,value in [('kind','flow'),('precision_increment','1')]:
            data=r.finalize([self.record()],NOW);data['reports'][0]['fields']['total_rewards_claimed'][key]=value
            with self.assertRaises(f.SourceError):r.validate(data)
    def test_invalid_reference_date_and_unsorted_history_fail(self):
        data=r.finalize([self.record()],NOW);data['reports'][0]['financial_report_published_at']='no date'
        with self.assertRaises(f.SourceError):r.validate(data)
        data=json.loads(Path('public/data/reported-rewards.json').read_text());data['reports'].reverse()
        with self.assertRaises(f.SourceError):r.validate(data)
    def test_refresh_unchanged_and_network_failure_retains_input(self):
        original=r.finalize([self.record()],NOW);before=copy.deepcopy(original)
        data,changed=r.refresh(original,dataset(),lambda before=None:message(TEXT),NOW)
        self.assertFalse(changed);self.assertIs(data,original)
        def unavailable(before=None):raise f.SourceError('unavailable')
        with self.assertRaises(f.SourceError):r.refresh(original,dataset(),unavailable,NOW)
        self.assertEqual(original,before)
    def test_atomic_write_failure_preserves_previous(self):
        data=r.finalize([self.record()],NOW)
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'rewards.json';p.write_text('previous')
            with patch('tools.reported_rewards.os.replace',side_effect=OSError('fail')):
                with self.assertRaises(OSError):r.atomic_write(p,data)
            self.assertEqual(p.read_text(),'previous');self.assertEqual(len(list(Path(folder).iterdir())),1)
    def test_live_public_dataset_has_consistent_transcriptions(self):
        data=json.loads(Path('public/data/reported-rewards.json').read_text());r.validate(data)
        for row in data['reports']:
            self.assertNotIn('text',row);self.assertNotIn('body',row)


if __name__=='__main__':unittest.main()
