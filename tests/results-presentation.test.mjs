import test from 'node:test';
import assert from 'node:assert/strict';
import {buildOperatingResults,OperatingResultsError} from '../public/assets/operating-results.js';
import {waterfallBody,waterfallExportRows,allocationBody} from '../public/assets/results-chart.js';
import {frozenOperatingReports} from './fixtures/operating-reports.mjs';

function report(id = 'test:1', overrides = {}) {
  const period_amounts = {gross_revenue:'100',market_making_costs:'-30',net_revenue:'70',rewards:'-20',net_profit:'50',treasury_inflow:'20',olp_pnl:'30',...overrides};
  return {id,published_at:'2026-11-13T18:51:03Z',period_amounts,
    units:Object.fromEntries(Object.keys(period_amounts).map(key => [key,'$'])),
    source_urls:['https://x.com/variational_io/status/2121317860740063501']};
}
const exported = (model,label) => waterfallExportRows(model).find(row => row[0] === label);
function noShares(html) {
  assert.doesNotMatch(html,/class="results-allocation-strip"/);
  assert.doesNotMatch(html,/<small>\d+(?:\.\d+)?%<\/small>/);
}

test('signed net loss retains its below-zero anchor and exact exported amount', () => {
  const model = buildOperatingResults([report('loss',{gross_revenue:'30',market_making_costs:'-40',net_revenue:'-10',rewards:'-20',net_profit:'-30'})]);
  const html = waterfallBody(model);
  assert.match(html,/−\$30/);
  assert.match(html,/is-zero/); // zero axis remains visible above the loss anchor.
  assert.deepEqual(exported(model,'Net Profit').slice(2,5),['-30','0','-30']);
  assert.deepEqual(exported(model,'Rewards').slice(2,5),['-20','-10','-30']);
  assert.doesNotMatch(html,/No complete bridge/);
});

test('reported zero has real zero geometry while missing profit stays null in export', () => {
  const zero = buildOperatingResults([report('zero',{gross_revenue:'0',market_making_costs:'0',net_revenue:'0',rewards:'0',net_profit:'0'})]);
  assert.deepEqual(exported(zero,'Net Profit').slice(2,5),['0','0','0']);
  assert.match(waterfallBody(zero),/results-wf-bar[^\"]* is-zero/);
  assert.match(waterfallBody(zero),/\$0/);
  const missing = buildOperatingResults([report('missing',{net_profit:null})]);
  assert.deepEqual(exported(missing,'Net Profit').slice(2,7),[null,null,null,0,1]);
  assert.match(waterfallBody(missing),/No complete bridge/);
  assert.match(waterfallBody(missing),/Unavailable/);
});

test('offsetting source exceptions remain disclosed even when their aggregate difference is zero', () => {
  const model = buildOperatingResults([report('a',{net_profit:'49'}),report('b',{net_profit:'51'})]);
  const html = waterfallBody(model);
  const difference = waterfallExportRows(model).find(row => row[1] === 'source_difference');
  assert.deepEqual(difference.slice(2,5),['0','100','100']);
  assert.match(html,/2 source differences/);
  assert.match(html,/Individual exceptions remain visible even when they offset/);
  assert.match(html,/Reported − calculated/);
  assert.match(html,/not additional income/);
  assert.match(html,/\+\$1/);
  assert.match(html,/−\$1/);
});

test('partial itemized costs are separate from the calculated gross-to-net bridge in HTML and export', () => {
  const model = buildOperatingResults([report('full'),report('partial',{market_making_costs:null})]);
  const html = waterfallBody(model);
  const rows = waterfallExportRows(model);
  const derived = rows.find(row => row[1] === 'calculated');
  const disclosed = rows.find(row => row[0] === 'Market Making Costs (disclosed subtotal)');
  assert.deepEqual(derived.slice(0,7),['Gross-to-net change','calculated','-60','200','140',2,2]);
  assert.deepEqual(disclosed.slice(1,7),['reported','-30',null,null,1,2]);
  assert.match(html,/Calculated change/);
  assert.match(html,/1 of 2 reports/);
  assert.match(html,/Itemized Market Making Costs: −\$30 across 1 of 2 updates/);
});

test('an explicit source-cost mismatch retains the reported net anchor and a separately labeled difference', () => {
  const model = buildOperatingResults([report('cost-mismatch',{net_revenue:'75',rewards:'-5',net_profit:'70'})]);
  const rows = waterfallExportRows(model);
  assert.deepEqual(exported(model,'Market Making Costs (Cost of Revenue)').slice(2,5),['-30','100','70']);
  assert.deepEqual(exported(model,'Net Revenue').slice(2,5),['75','0','75']);
  assert.deepEqual(rows.find(row => row[1] === 'source_difference').slice(2,5),['5','70','75']);
  assert.match(waterfallBody(model),/Source difference/);
});

test('negative allocation components and losses never receive normalized percentage shares', () => {
  for (const input of [
    report('negative-component',{treasury_inflow:'60',olp_pnl:'-10',net_profit:'50'}),
    report('loss',{treasury_inflow:'-5',olp_pnl:'-15',net_profit:'-20'}),
    structuredClone(frozenOperatingReports.find(item => item.published_at.startsWith('2026-05-01'))),
  ]) {
    const html = allocationBody([input]);
    noShares(html);
    assert.match(html,/Signed reported allocations reconcile/);
    assert.match(html,/Periods with a negative amount/);
    assert.match(html,/−\$/);
  }
});

test('zero allocation and missing amounts remain distinct and neither gets shares', () => {
  const zero = allocationBody([report('zero',{treasury_inflow:'0',olp_pnl:'0',net_profit:'0'})]);
  noShares(zero);
  assert.match(zero,/reconcile to zero/);
  assert.match(zero,/\$0/);
  const missing = allocationBody([report('missing',{olp_pnl:null})]);
  noShares(missing);
  assert.match(missing,/Allocation incomplete/);
  assert.match(missing,/0 of 1 reports, omitted/);
  assert.match(missing,/Unavailable/);
});

test('allocation mismatches withhold shares including offsetting per-period discrepancies', () => {
  for (const selected of [[report('bad',{net_profit:'51'})],[report('under',{net_profit:'49'}),report('over',{net_profit:'51'})]]) {
    const html = allocationBody(selected);
    noShares(html);
    assert.match(html,/source allocation difference/);
    assert.match(html,/Percentages withheld/);
    assert.match(html,/Profit − allocations/);
  }
});

test('valid exact fractions beyond18 places and large aggregate totals survive presentation and CSV', () => {
  const precise = '0.1234567890123456789012345678';
  const model = buildOperatingResults([report('precise',{gross_revenue:precise,market_making_costs:'0',net_revenue:precise,rewards:'0',net_profit:precise})]);
  assert.equal(exported(model,'Net Profit')[2],precise);
  assert.ok(waterfallBody(model).includes('$'+precise));
  const large = {gross_revenue:'9007199254740993',market_making_costs:'-1',net_revenue:'9007199254740992',rewards:'-1',net_profit:'9007199254740991'};
  const aggregate = buildOperatingResults([report('a',large),report('b',large)]);
  assert.equal(exported(aggregate,'Net Profit')[2],'18014398509481982');
  assert.match(waterfallBody(aggregate),/\$18,014,398,509,481,982/);
});

test('mixed magnitude sums retain maximum source precision and aggregate whole digits', () => {
  const tiny = '0.' + '0'.repeat(39997) + '1';
  assert.equal(tiny.length,40000);
  const amounts = value => ({gross_revenue:value,market_making_costs:'0',net_revenue:value,rewards:'0',net_profit:value,treasury_inflow:value,olp_pnl:'0'});
  const reports = [report('large',amounts('10000000000000000')),report('tiny',amounts(tiny))];
  const model = buildOperatingResults(reports);
  const exact = '10000000000000000.' + '0'.repeat(39997) + '1';
  assert.equal(exact.length,40016);
  assert.equal(exported(model,'Net Profit')[2],exact);
  assert.equal(exported(model,'Spreads Paid (Gross Revenue)')[2],exact);
  const displayed = '$10,000,000,000,000,000.' + '0'.repeat(39997) + '1';
  assert.ok(waterfallBody(model).includes(displayed));
  const allocation = allocationBody(reports);
  assert.ok(allocation.includes(displayed));
  assert.match(allocation,/100\.0%/);
  assert.doesNotMatch(allocation,/(?:NaN|Infinity)%/);
  const oversized = '0.' + '0'.repeat(39998) + '1';
  assert.throws(() => buildOperatingResults([report('oversized',amounts(oversized))]),error => error.code === 'invalid_dollar_string');
  noShares(allocationBody([report('oversized',amounts(oversized))]));
});

test('untrusted labels are escaped and invalid source values fail with controlled model codes', () => {
  const model = buildOperatingResults([report()]);
  model.rows.find(item => item.key === 'rewards').label = '<img src=x onerror=alert(1)>';
  const html = waterfallBody(model);
  assert.doesNotMatch(html,/<img\b/);
  assert.match(html,/&lt;img src=x onerror=alert\(1\)&gt;/);
  const invalid = report('invalid',{net_profit:'<script>private</script>'});
  assert.throws(() => buildOperatingResults([invalid]),error => error instanceof OperatingResultsError && error.code === 'invalid_dollar_string' && !error.message.includes('private'));
});

test('unsafe source links and malformed allocation amounts do not become executable markup or zero', () => {
  const source = report('link',{olp_pnl:'-10',treasury_inflow:'60'});
  source.source_urls = ['javascript:alert(1)','data:text/html,<script>alert(1)</script>'];
  const html = allocationBody([source]);
  assert.doesNotMatch(html,/href="(?:javascript|data):/);
  assert.doesNotMatch(html,/<script\b/);
  const malformed = allocationBody([report('amount',{olp_pnl:'<img src=x>'})]);
  assert.doesNotMatch(malformed,/<img\b/);
  assert.match(malformed,/Unavailable/);
  noShares(malformed);
});
