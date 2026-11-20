import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {buildOperatingResults, OperatingResultsError} from '../public/assets/operating-results.js';
import {frozenOperatingReports as reports} from './fixtures/operating-reports.mjs';

const dataset = JSON.parse(await readFile(new URL('../public/data/financials.json', import.meta.url), 'utf8'));
const expectedTotals = {gross_revenue:'64653135',market_making_costs:'-15488037',net_revenue:'30819751',rewards:'-3456245',net_profit:'27673543'};
const row = (model, key) => model.rows.find(item => item.key === key);
const check = (model, id) => model.reconciliations.find(item => item.id === id);
const on = day => reports.find(report => report.published_at.startsWith(day));

function fixture(id = 'test:1', overrides = {}) {
  const values = {gross_revenue:'100',market_making_costs:'-30',net_revenue:'70',rewards:'-20',net_profit:'50', ...overrides};
  return {id,published_at:'2026-11-13T18:51:03Z',period_amounts:values,
    units:Object.fromEntries(Object.keys(values).map(key => [key,'$']))};
}

test('latest source report retains missing costs and shows independent endpoint difference', () => {
  const model = buildOperatingResults([reports.at(-1)]);
  assert.equal(model.reportCount,1);
  assert.equal(model.status,'incomplete');
  assert.equal(model.grossToNetChange,'-1699994');
  assert.equal(row(model,'gross_revenue').value,'4908930');
  assert.equal(row(model,'net_revenue').value,'3208936');
  assert.equal(row(model,'rewards').value,'-217813');
  assert.equal(row(model,'net_profit').value,'2991123');
  assert.equal(row(model,'market_making_costs').value,null);
  assert.deepEqual(row(model,'market_making_costs').chart,{start:null,end:null});
  assert.equal(check(model,'gross_to_net').expected,null);
  assert.equal(check(model,'net_to_profit').status,'reconciled');
  assert.equal(check(model,'net_to_profit').residual,'0');
});

test('All matches all19 exact source totals without making a partial cost sum complete', () => {
  const model = buildOperatingResults(reports);
  assert.equal(model.reportCount,19);
  assert.equal(model.grossToNetChange,'-33833384');
  assert.equal(model.status,'mismatch');
  assert.equal(model.hasIncomplete,true);
  for (const item of model.rows) assert.equal(item.value,expectedTotals[item.key]);
  assert.equal(row(model,'market_making_costs').coverage.reportedCount,12);
  assert.equal(row(model,'market_making_costs').coverage.reportCount,19);
  assert.equal(row(model,'market_making_costs').coverage.status,'partial');
  assert.deepEqual(row(model,'market_making_costs').chart,{start:null,end:null});
  assert.equal(check(model,'gross_to_net').status,'incomplete');
  assert.equal(check(model,'gross_to_net').residual,null);
  const profit = check(model,'net_to_profit');
  assert.equal(profit.expected,'27363506');
  assert.equal(profit.reported,'27673543');
  assert.equal(profit.residual,'-310037');
  assert.equal(profit.reportedMinusCalculated,'310037');
  assert.equal(profit.exceptions.length,2);
});

test('historical source mismatches keep reported profit anchors and expected geometry distinct', () => {
  for (const [day, residual, profit] of [['2026-08-07','-5','1239387'],['2026-08-21','-310032','1322137']]) {
    const model = buildOperatingResults([on(day)]);
    assert.equal(check(model,'net_to_profit').residual,residual);
    assert.equal(check(model,'net_to_profit').residualDefinition,'expected_minus_reported');
    assert.equal(row(model,'net_profit').value,profit);
    assert.equal(row(model,'net_profit').chart.end,profit);
    assert.notEqual(row(model,'rewards').chart.end,profit);
    assert.equal(model.status,'mismatch');
  }
});

test('every native report is reconciled independently and unrelated lifetime warnings do not contaminate the model', () => {
  assert.equal(reports.length,19);
  for (const report of reports) {
    const model = buildOperatingResults([report]);
    const sourceWarning = report.warnings.find(warning => warning.code === 'net_profit_identity');
    assert.equal(check(model,'net_to_profit').residual,sourceWarning?.residual ?? '0');
    if (report.period_amounts.market_making_costs !== null) assert.equal(check(model,'gross_to_net').residual,'0');
  }
  const october = buildOperatingResults([on('2026-10-16')]);
  assert.equal(october.hasMismatch,false);
  assert.equal(check(october,'net_to_profit').status,'reconciled');
});

test('live reports remain accepted without imposing frozen historical amounts', () => {
  const model = buildOperatingResults(dataset.reports);
  assert.equal(model.reportCount,dataset.reports.length);
  for (const source of dataset.reports) {
    const single = buildOperatingResults([source]);
    for (const item of single.rows) {
      const original = source.period_amounts[item.key];
      if (original === null) assert.equal(item.value,null);
      else {
        const [whole, fraction = ''] = original.split('.');
        let end = fraction.length;
        while (end > 0 && fraction[end - 1] === '0') end -= 1;
        const normalized = whole + (end ? '.' + fraction.slice(0,end) : '');
        assert.equal(item.value,normalized === '-0' ? '0' : normalized);
      }
    }
  }
});

test('the complete12-report cost cohort reconciles gross-to-net while preserving the five-dollar profit exception', () => {
  const model = buildOperatingResults(reports.filter(report => report.period_amounts.market_making_costs !== null));
  assert.equal(model.reportCount,12);
  assert.equal(check(model,'gross_to_net').status,'reconciled');
  assert.equal(check(model,'gross_to_net').residual,'0');
  assert.equal(check(model,'net_to_profit').residual,'-5');
  assert.equal(model.grossToNetChange,'-15488037');
});

test('empty selection never falls back to latest or invents zero', () => {
  const model = buildOperatingResults([]);
  assert.equal(model.status,'empty');
  assert.equal(model.reportCount,0);
  assert.equal(model.firstPublishedAt,null);
  assert.equal(model.grossToNetChange,null);
  assert.ok(model.rows.every(item => item.value === null && item.coverage.status === 'empty'));
  assert.ok(model.reconciliations.every(item => item.status === 'empty' && item.residual === null));
});

test('zero, signed loss and exact fractional dollars remain distinct', () => {
  const zero = buildOperatingResults([fixture('zero',{gross_revenue:'0',market_making_costs:'-0.00',net_revenue:'0',rewards:'0',net_profit:'0'})]);
  assert.equal(zero.status,'reconciled');
  assert.ok(zero.rows.every(item => item.value === '0' && item.chart.end === '0'));
  const loss = buildOperatingResults([fixture('loss',{gross_revenue:'0.30',market_making_costs:'-0.40',net_revenue:'-0.10',rewards:'-0.20',net_profit:'-0.30'})]);
  assert.equal(loss.status,'reconciled');
  assert.equal(row(loss,'net_profit').value,'-0.3');
  assert.deepEqual(row(loss,'rewards').chart,{start:'-0.1',end:'-0.3'});
  assert.deepEqual(row(loss,'net_profit').chart,{start:'0',end:'-0.3'});
});

test('integers above safe Number precision retain exact string arithmetic', () => {
  const large = fixture('large',{gross_revenue:'9007199254740993',market_making_costs:'-1',net_revenue:'9007199254740992',rewards:'-1',net_profit:'9007199254740991'});
  const model = buildOperatingResults([large]);
  assert.equal(model.status,'reconciled');
  assert.equal(row(model,'gross_revenue').value,'9007199254740993');
  assert.equal(model.grossToNetChange,'-1');
  assert.doesNotThrow(() => JSON.stringify(model));
});

test('collector-compatible fractions beyond18 places and tiny canonical decimals remain exact', () => {
  const gross = '0.1234567890123456789012345678';
  const net = '0.1234567890123456789012345677';
  const profit = '0.1234567890123456789012345676';
  const cost = '-0.0000000000000000000000000001';
  const precise = buildOperatingResults([fixture('precise',{gross_revenue:gross,market_making_costs:cost,net_revenue:net,rewards:cost,net_profit:profit})]);
  assert.equal(precise.status,'reconciled');
  assert.equal(row(precise,'net_profit').value,profit);
  assert.equal(precise.grossToNetChange,cost);
  const tiny = '0.'+'0'.repeat(199)+'1';
  const model = buildOperatingResults([fixture('tiny',{gross_revenue:tiny,market_making_costs:'0',net_revenue:tiny,rewards:'0',net_profit:tiny})]);
  assert.equal(row(model,'net_profit').value,tiny);
  assert.equal(model.status,'reconciled');
});

test('source-aligned amount-length and report-count bounds reject before scale allocation', () => {
  const maximum = '0.'+'0'.repeat(39997)+'1';
  const model = buildOperatingResults([fixture('bound',{gross_revenue:maximum,market_making_costs:'0',net_revenue:maximum,rewards:'0',net_profit:maximum})]);
  assert.equal(row(model,'net_profit').value,maximum);
  assert.throws(() => buildOperatingResults([fixture('oversized',{net_profit:'0.'+'0'.repeat(39998)+'1'})]),/invalid_dollar_string/);
  assert.throws(() => buildOperatingResults(Array.from({length:2001},(_,i)=>fixture('n:'+i))),/invalid_report_selection/);
});

test('missing required values produce partial totals and unavailable geometry, never inherit dataset totals', () => {
  const missing = fixture('missing',{gross_revenue:null,market_making_costs:undefined,net_revenue:'',rewards:null,net_profit:null});
  missing.totals = dataset.totals;
  const single = buildOperatingResults([missing]);
  assert.ok(single.rows.every(item => item.value === null && item.coverage.status === 'missing'));
  const combined = buildOperatingResults([fixture(),missing]);
  assert.equal(row(combined,'gross_revenue').value,'100');
  assert.equal(row(combined,'gross_revenue').coverage.status,'partial');
  assert.equal(row(combined,'gross_revenue').chart.end,null);
  assert.equal(combined.grossToNetChange,null);
  assert.equal(check(combined,'net_to_profit').residual,null);
});

test('aggregate cancellation cannot hide two source exceptions', () => {
  const model = buildOperatingResults([fixture('under',{net_profit:'49'}),fixture('over',{net_profit:'51'})]);
  const profit = check(model,'net_to_profit');
  assert.equal(profit.residual,'0');
  assert.equal(profit.reportedMinusCalculated,'0');
  assert.equal(profit.status,'mismatch');
  assert.equal(profit.exceptions.length,2);
  assert.equal(model.hasMismatch,true);
});

test('cost and reward sublines, allocation and stock fields are not counted again', () => {
  const report = fixture();
  report.period_amounts.loss_refunds = '-100';
  report.period_amounts.referral_rewards = '-200';
  report.period_amounts.treasury_inflow = '10';
  report.snapshot = {lifetime_olp_pnl:'1000000',treasury_holdings:'2000000'};
  report.privateDraft = 'DO_NOT_FORWARD';
  report.warnings = [{code:'unrelated',message:'DO_NOT_FORWARD'}];
  const model = buildOperatingResults([report]);
  assert.equal(model.rows.length,5);
  assert.equal(row(model,'rewards').value,'-20');
  assert.equal(model.status,'reconciled');
  assert.equal(JSON.stringify(model).includes('DO_NOT_FORWARD'),false);
});

test('source label qualifiers are retained without assigning old allocation labels new meanings', () => {
  const model = buildOperatingResults(reports);
  assert.deepEqual(row(model,'rewards').sourceLabels,['2w Rewards']);
  assert.equal(model.rows.some(item => item.key === 'treasury_inflow'),false);
  assert.equal(row(model,'market_making_costs').label,'Market Making Costs (Cost of Revenue)');
});

test('selection ordering is deterministic and caller objects are never mutated', () => {
  const selected = structuredClone(reports.slice(-2).reverse());
  const before = JSON.stringify(selected);
  const model = buildOperatingResults(selected);
  assert.deepEqual(model.reportIds,reports.slice(-2).map(report => report.id));
  assert.equal(JSON.stringify(selected),before);
  assert.equal(model.firstPublishedAt,reports.at(-2).published_at);
  assert.equal(model.lastPublishedAt,reports.at(-1).published_at);
});

test('invalid, overprecise, exponent and unsafe-source inputs fail with fixed codes', () => {
  for (const value of [1,true,'NaN','Infinity','1e-100000','10000000000000001',' 1','<script>']) {
    assert.throws(() => buildOperatingResults([fixture('bad',{net_profit:value})]),OperatingResultsError);
  }
  assert.throws(() => buildOperatingResults([fixture(),fixture()]),/duplicate_report_id/);
  assert.throws(() => buildOperatingResults([fixture('positive',{rewards:'1'})]),/deduction_sign_changed/);
  const unit = fixture(); unit.units.net_profit = 'USDC';
  assert.throws(() => buildOperatingResults([unit]),/incompatible_dollar_unit/);
  const label = fixture(); label.labels = {rewards:'<img src=x onerror=alert(1)>'};
  assert.throws(() => buildOperatingResults([label]),/source_label_changed/);
});
