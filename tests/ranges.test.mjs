import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source=await readFile(new URL('../public/assets/ranges.js',import.meta.url),'utf8');
const {rangeBounds,filterRange,financialSeries,periodTotals,rangeHref,chartDomain,normalizeRange,latestTimestamp}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const anchor='2026-11-20T09:31:41Z';
test('7D includes seven UTC calendar dates with an exclusive next-day boundary',()=>{
  const bounds=rangeBounds('7d',anchor);
  assert.equal(new Date(bounds.start).toISOString(),'2026-11-14T00:00:00.000Z');
  assert.equal(new Date(bounds.endExclusive).toISOString(),'2026-11-21T00:00:00.000Z');
  const rows=['2026-11-13T23:59:59.999Z','2026-11-14T00:00:00Z','2026-11-20T23:59:59.999Z','2026-11-21T00:00:00Z'].map(date=>({date}));
  assert.deepEqual(filterRange(rows,'7d',anchor),rows.slice(1,3));
});
test('UTC conversion applies before calendar membership, independent of local timezone',()=>{
  const rows=[{date:'2026-11-14T00:30:00+09:00'},{date:'2026-11-14T09:00:00+09:00'}];
  assert.deepEqual(filterRange(rows,'7d',anchor),[rows[1]]);
});
test('sparse points are filtered by dates rather than observation count',()=>{
  const rows=['2026-11-02','2026-11-04','2026-11-11','2026-11-20'].map(date=>({date,values:{value:1}}));
  assert.deepEqual(filterRange(rows,'7d',anchor),[rows[3]]);
  assert.equal(filterRange(rows,'30d',anchor).length,4);
  assert.equal(chartDomain(filterRange(rows,'7d',anchor),'7d',anchor).start,Date.parse('2026-11-14'));
});
test('7D without a report is empty, and selected flow totals stay null',()=>{
  const reports=[{published_at:'2026-11-13T19:02:44Z',period_amounts:{net_profit:'300'}}];
  const rows=financialSeries(reports,['net_profit'],{range:'7d',anchor});
  assert.deepEqual(rows,[]);assert.equal(periodTotals([],['net_profit']).net_profit,null);
});
test('cumulative totals start at the archive beginning before a 30D crop',()=>{
  const reports=[['2026-03-06',100],['2026-10-16',200],['2026-10-30',300],['2026-11-13',400]].map(([published_at,net_profit])=>({published_at,period_amounts:{net_profit}}));
  const rows=financialSeries(reports,['net_profit'],{mode:'cumulative',range:'30d',anchor});
  assert.deepEqual(rows.map(row=>row.values.net_profit),[600,1000]);
  assert.equal(periodTotals(reports.slice(2),['net_profit']).net_profit,700);
});
test('missing source amounts remain gaps rather than zero or a carried observation',()=>{
  const reports=[1,null,3].map((value,i)=>({published_at:`2026-11-${14+i}`,period_amounts:{pnl:value}}));
  assert.deepEqual(financialSeries(reports,['pnl'],{mode:'cumulative',range:'7d',anchor}).map(row=>row.values.pnl),[1,null,4]);
});
test('1Y uses a calendar anniversary and all preserves archive dates',()=>{
  assert.equal(new Date(rangeBounds('1y','2024-02-29').start).toISOString(),'2023-03-01T00:00:00.000Z');
  assert.equal(new Date(rangeBounds('180d',anchor).start).toISOString(),'2026-05-25T00:00:00.000Z');
  assert.equal(normalizeRange('bogus'),'all');
  assert.equal(filterRange([{date:'2020-01-01'},{date:null}],'all',anchor).length,1);
});
test('route links retain existing view parameters and explicit range for reload/back',()=>{
  assert.equal(rangeHref('#/financials?view=treasury','30d'),'#/financials?view=treasury&range=30d');
  assert.equal(rangeHref('#/omni?range=7d&market=BTC','all'),'#/omni?range=all&market=BTC');
  assert.equal(rangeHref('https://example.com','7d'),'https://example.com');
  assert.equal(latestTimestamp([null,'invalid','2026-11-20T09:00:00Z','2026-11-19']),'2026-11-20T09:00:00.000Z');
});

test('chart exception markers reflect only the displayed financial measure', async () => {
  const {warningsForMetrics} = await import('../public/assets/ranges.js');
  const report = {warnings:[{code:'net_profit_identity'},{code:'lifetime_olp_change'}]};
  assert.deepEqual(warningsForMetrics(report,['dual_sided_oi']),[]);
  assert.deepEqual(warningsForMetrics(report,['net_profit']),[{code:'net_profit_identity'}]);
  assert.deepEqual(warningsForMetrics(report,['olp_pnl']),[{code:'lifetime_olp_change'}]);
  assert.equal(warningsForMetrics({warnings:[{code:'possible_report_gap'}]},['dual_sided_oi']).length,1);
});
