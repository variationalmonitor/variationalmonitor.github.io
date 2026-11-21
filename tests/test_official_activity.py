"""Native report windows, exact transcriptions, source identity and failures."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tools import official_activity as a, financials as f
from tests.test_financials import REPORT, NOW, source, message, dataset

OLD="""Welcome back to our biweekly update!
**Metrics**
* Total Volume Traded: $1,000,000.25
* Current Dual-Sided OI: $39.8M
* Total Trades Placed: 500,612
* Total Tokens Currently Listed: 520
* Daily Active Users (avg. over past week): 346
* Weekly Active Users: 890
* 2w Revenue: $143,070
* Revenue Directed to OLP: $101,127
* Revenue Directed to Loss Refund Pool: $41,943 (plus $95,000 in manual deposits for boost campaign)
* Total OLP PnL: $971,409
"""
MODERN=REPORT+"Daily Active Users (avg. over past week): 10,005\nWeekly Active Users: 16,206\n"


class OfficialActivityTests(unittest.TestCase):
    def record(self,text=OLD,**kwargs):return a.parse_report(text,source(post_id='210',published='2025-09-20T09:21:06Z',**kwargs))
    def test_exact_amounts_source_qualifiers_and_legacy_boundary(self):
        row=self.record();self.assertEqual(row['metrics']['cumulative_volume']['value'],'1000000.25')
        self.assertEqual(row['metrics']['dual_sided_oi']['value'],'39800000.0')
        self.assertEqual(row['metrics']['dual_sided_oi']['precision_increment'],'100000')
        self.assertEqual(row['legacy_financials']['revenue_2w']['value'],'143070')
        self.assertIn('plus $95,000',row['legacy_financials']['revenue_directed_to_loss_refund_pool']['qualifier'])
        self.assertNotIn('net_revenue',row['legacy_financials']);self.assertNotIn('net_profit',row['legacy_financials'])
        self.assertIsNone(row['period_start']);self.assertIsNone(row['period_end'])
    def test_users_are_source_window_counts_not_daily_points(self):
        row=self.record();self.assertEqual(row['metrics']['daily_active_users_week_average']['kind'],'reported_daily_average_over_past_week')
        self.assertEqual(row['metrics']['weekly_active_users']['kind'],'reported_weekly_users')
        self.assertEqual(row['metrics']['total_trades_placed']['value'],'500612')
    def test_empty_and_missing_counts_are_not_zero(self):
        text=OLD.replace('Total Tokens Currently Listed: 520','Total Tokens Currently Listed: ')
        self.assertIsNone(self.record(text)['metrics']['tokens_listed'])
        self.assertEqual(self.record(OLD.replace('Weekly Active Users: 890','Weekly Active Users: 0'))['metrics']['weekly_active_users']['value'],'0')
    def test_original_phase_and_no_yield_invention(self):
        row=self.record(OLD+'Omni Mainnet Private Beta is live.\nOLP Yield (90d): ~118%\n')
        self.assertEqual(row['reported_phase'],'mainnet_private_beta');self.assertNotIn('apy',json.dumps(row).lower())
    def test_wrong_origin_forward_url_and_future_rejected(self):
        for change in [{'author':'fake'},{'forwarded':True},{'id':210},{'url':'https://example.com/210'},{'published_at':'2027-01-01T00:00:00Z'}]:
            s=source(post_id='210');s.update(change)
            with self.assertRaises(f.SourceError):a.parse_report(OLD,s)
    def test_duplicate_bad_units_negative_and_malformed_amounts_reject(self):
        for text in [OLD+'Total Volume Traded: $1\n',OLD.replace('$39.8M','39.8M USDC'),OLD.replace('$39.8M','($39.8M)'),OLD.replace('$39.8M','$NaN'),OLD.replace('$39.8M','$1e3')]:
            with self.assertRaises(f.SourceError):self.record(text)
    def test_nonreport_and_exact_reviewed_heading_exception(self):
        self.assertIsNone(self.record('Milestone: Total Volume Traded: $1B'))
        text=OLD.replace('Welcome back to our biweekly update!','Product, Community and Metrics')
        self.assertIsNone(self.record(text))
        self.assertIsNotNone(a.parse_report(text,source(post_id='217',published='2025-10-03T12:00:36Z')))
    def test_financial_id_and_dates_are_linked_without_expanding_financials(self):
        financial=dataset();before=copy.deepcopy(financial)
        row=a.parse_report(MODERN,source(),financial)
        self.assertEqual(row['id'],financial['reports'][0]['id']);self.assertEqual(row['financial_report_id'],row['id'])
        self.assertEqual(row['published_at'],financial['reports'][0]['published_at']);self.assertEqual(financial,before)
        with self.assertRaises(f.SourceError):a.parse_report(MODERN.replace('$475.77B','$475.78B'),source(),financial)
    def test_canonical_cohort_date_is_not_earlier_source_availability(self):
        financial=dataset();s=source(post_id='999',platform='x',published='2026-11-13T19:02:44Z')
        row=a.parse_report(MODERN,s,financial);data=a.finalize([row],NOW)
        self.assertEqual(data['reports'][0]['published_at'],'2026-11-13T18:51:03Z')
        self.assertEqual(data['reports'][0]['source_published_at'],'2026-11-13T19:02:44Z')
        self.assertEqual(data['reports'][0]['source_available_at'],NOW)
        data['reports'][0]['source_available_at']=data['reports'][0]['published_at']
        with self.assertRaises(f.SourceError):a.validate(data)
    def test_mirror_dedup_and_conflicting_mirrors_do_not_inflate(self):
        row=self.record();reports=[row];s=source(post_id='999',platform='x',published='2025-09-20T09:30:00Z')
        incoming=a.parse_report(OLD,s)
        self.assertTrue(a.merge(reports,incoming,NOW));self.assertEqual(len(reports),1);self.assertEqual(len(reports[0]['sources']),2)
        conflict=a.parse_report(OLD.replace('890','891'),source(post_id='998',platform='x',published='2025-09-20T10:00:00Z'))
        before=copy.deepcopy(reports)
        with self.assertRaises(f.SourceError):a.merge(reports,conflict,NOW)
        self.assertEqual(reports,before)
    def test_original_source_revision_preserves_old_data(self):
        reports=[self.record()];changed=self.record(OLD.replace('890','891'))
        self.assertTrue(a.merge(reports,changed,NOW));self.assertEqual(reports[0]['metrics']['weekly_active_users']['value'],'891')
        self.assertEqual(reports[0]['revisions'][0]['metrics']['weekly_active_users']['value'],'890')
        a.validate(a.finalize(reports,NOW))
    def test_label_precision_metadata_tamper_is_rejected(self):
        for key,value in [('precision_increment','1'),('unit','USDC'),('kind','daily'),('qualifier','none')]:
            data=a.finalize([self.record()],NOW);data['reports'][0]['metrics']['dual_sided_oi'][key]=value
            with self.assertRaises(f.SourceError):a.validate(data)
    def test_chronology_source_dates_and_coverage_are_verified(self):
        data=a.finalize([self.record()],NOW);data['reports'][0]['published_at']='2020-01-01T00:00:00Z'
        with self.assertRaises(f.SourceError):a.validate(data)
        data=a.finalize([self.record()],NOW);data['coverage']['field_counts']['dual_sided_oi']=99
        with self.assertRaises(f.SourceError):a.validate(data)
    def test_refresh_no_change_and_failure_preserve_input(self):
        financial=dataset();row=a.parse_report(MODERN,source(),financial);existing=a.finalize([row],NOW);before=copy.deepcopy(existing)
        out,changed=a.refresh(existing,financial,lambda before=None:message(MODERN),NOW)
        self.assertFalse(changed);self.assertIs(out,existing)
        def failure(before=None):raise f.SourceError('unavailable')
        with self.assertRaises(f.SourceError):a.refresh(existing,financial,failure,NOW)
        self.assertEqual(existing,before)
    def test_forwarded_feed_and_link_preview_cannot_supply_facts(self):
        self.assertEqual(a.feed_reports(message(MODERN,forwarded=True),NOW,dataset()),[])
        result=a.feed_reports(message(MODERN,preview='Weekly Active Users: 999999'),NOW,dataset())
        self.assertEqual(result[0]['metrics']['weekly_active_users']['value'],'16206')
    def test_atomic_write_failure_keeps_previous_file(self):
        data=a.finalize([self.record()],NOW)
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'activity.json';p.write_text('previous')
            with patch('tools.official_activity.os.replace',side_effect=OSError('fail')):
                with self.assertRaises(OSError):a.atomic_write(p,data)
            self.assertEqual(p.read_text(),'previous');self.assertEqual(len(list(Path(folder).iterdir())),1)
    def test_import_manifest_rejects_path_or_origin_tampering(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'manifest.json'
            for entry in [{'file':'../source.html','url':f.FEED_URL},{'file':'source.html','url':'https://example.com'}]:
                p.write_text(json.dumps([entry]))
                with self.assertRaises(f.SourceError):a.load_captures(p,dataset())
    def test_live_artifact_consistent_and_public_only(self):
        data=json.loads(Path('public/data/official-activity.json').read_text());a.validate(data)
        for row in data['reports']:
            self.assertNotIn('text',row);self.assertNotIn('body',row);self.assertNotIn('draft',row)
        self.assertNotIn('totals',data)

if __name__=='__main__':unittest.main()
