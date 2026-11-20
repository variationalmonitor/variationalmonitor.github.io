import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
import {buildOperatingResults,OperatingResultsError} from '../public/assets/operating-results.js';
import {waterfallBody,waterfallExportRows,allocationBody} from '../public/assets/results-chart.js';
import {frozenOperatingReports} from './fixtures/operating-reports.mjs';

const asset=path=>readFile(new URL('../public/assets/'+path,import.meta.url),'utf8');
const [utils,ranges,app]=await Promise.all([asset('utils.js'),asset('ranges.js'),asset('app.js')]);
const fixture=async name=>JSON.parse(await readFile(new URL('../public/data/'+name,import.meta.url),'utf8'));
const [financials,markets,history,supplement]=await Promise.all(['financials.json','markets.json','market-history.json','supplement.json'].map(fixture));
const archivedFinancials={...financials,reports:financials.reports.filter(report=>Date.parse(report.published_at)<=Date.parse('2026-11-13T23:59:59Z'))};

function harness(hash='#/financials?range=all',financialFixture=archivedFinancials){
  const events={document:{},window:{}},nodes=new Map();
  const node=id=>{if(!nodes.has(id))nodes.set(id,{id,innerHTML:'',textContent:'',hidden:false,dataset:{},classList:{toggle(){},contains(){return false;}},setAttribute(){},removeAttribute(){},getAttribute(){return null;},querySelectorAll(){return [];},querySelector(){return node('link');},contains(){return false;},focus(){},addEventListener(){}});return nodes.get(id);};
  let currentHash=hash;
  const location={search:'',href:'https://example.com/',get hash(){return currentHash;},set hash(value){currentHash='#'+value.replace(/^#/,'');}};
  const listen=target=>(type,handler)=>(events[target][type]??=[]).push(handler);
  const document={getElementById:node,querySelectorAll(){return [];},addEventListener:listen('document'),documentElement:{dataset:{}},activeElement:null};
  const context=vm.createContext({document,location,window:{addEventListener:listen('window'),scrollTo(){},innerWidth:1200},URL,URLSearchParams,Intl,Date,Map,Set,console,setTimeout,clearTimeout,requestAnimationFrame:fn=>fn(),mountChart:()=>()=>{},buildOperatingResults,OperatingResultsError,waterfallBody,waterfallExportRows,allocationBody,fixtures:{financials:financialFixture,markets,history,supplement}});
  const compiled=utils.replace(/^export /gm,'')+'\nconst e=escapeHTML,n=number;\n'+ranges.replace(/^export /gm,'')+'\n'+app.replace(/^import .*$/gm,'').replaceAll('import.meta.url',"'https://example.com/assets/app.js'").replace(/^load\(\)\.catch.*$/m,'');
  vm.runInContext(compiled,context);
  vm.runInContext("DATA.financials=fixtures.financials;DATA.markets=fixtures.markets;DATA.history=fixtures.history;DATA.supplement=fixtures.supplement;rangeAnchor=()=> '2026-11-20T09:31:41Z';readRoute();",context);
  return {context,location,events,run:code=>vm.runInContext(code,context),clickRange(value){const button={dataset:{range:value},hasAttribute(){return false;}};for(const handler of events.document.click||[])handler({target:{closest(){return button;}}});for(const handler of events.window.hashchange||[])handler();}};
}

test('all historical pages render exactly one six-option period toolbar',()=>{
  const h=harness();
  for(const expression of ['overview()','omni()','marketDetail(latestMarkets()[0])','financials()','olp()']){
    const html=h.run(expression);
    assert.equal((html.match(/class="range-toolbar"/g)||[]).length,1,expression);
    assert.equal((html.match(/data-range=/g)||[]).length,6,expression);
    assert.equal(html.includes('data-history-range'),false,expression);
  }
  h.run("route.params.set('view','treasury')");
  assert.equal((h.run('financials()').match(/class="range-toolbar"/g)||[]).length,1);
});

test('7D real-data financial charts and CSV contain no older reports',()=>{
  const h=harness('#/financials?range=7d');
  const html=h.run('financials()');
  assert.equal(h.run('selectedReports().length'),0);
  assert.equal(h.run("charts.get('financial-performance').rows.length"),0);
  assert.equal(h.run('selectedFinancialTotals().net_profit'),null);
  assert.match(html,/No reports published in the selected period/);
  h.run('globalThis.exported=null;downloadCSV=(name,columns,rows)=>globalThis.exported={name,rows};exportFinancials();');
  assert.equal(h.context.exported.rows.length,0);
  assert.match(h.context.exported.name,/7d/);
});

test('30D cumulative chart retains prior archive amounts while period cards sum only selected reports',()=>{
  const h=harness('#/financials?range=30d');h.run('financials()');
  const first=h.run("charts.get('financial-performance').rows[0]");
  const expected=financials.reports.filter(r=>Date.parse(r.published_at)<=Date.parse(first.date)).reduce((total,r)=>total+Number(r.period_amounts.net_profit),0);
  assert.equal(first.values.net_profit,expected);
  const selectedTotal=h.run('selectedReports().reduce((total,r)=>total+Number(r.period_amounts.net_profit),0)');
  assert.equal(h.run('selectedFinancialTotals().net_profit'),selectedTotal);
  assert.ok(first.values.net_profit>Number(h.run('selectedReports()[0].period_amounts.net_profit')));
});

test('range buttons preserve Treasury route and restore through history and reload',()=>{
  const h=harness('#/financials?view=treasury&range=30d');
  h.clickRange('7d');
  assert.match(h.location.hash,/view=treasury/);assert.match(h.location.hash,/range=7d/);
  assert.equal(h.run('selectedRange()'),'7d');
  h.location.hash='#/financials?view=treasury&range=30d';
  for(const handler of h.events.window.hashchange||[])handler();
  assert.equal(h.run('selectedRange()'),'30d');
  assert.equal(harness(h.location.hash).run('selectedRange()'),'30d');
  h.run("route.params.set('market',marketKey(latestMarkets()[0]))");
  assert.match(h.run("rangeHref('#/omni?market='+encodeURIComponent(route.params.get('market')),selectedRange())"),/range=30d/);
});

test('a week without a report still shows observed native API counters with their original units',()=>{
  const h=harness('#/overview?range=7d');
  const html=h.run('overview()');
  for (const id of ['overview-volume','overview-oi']) {
    const chart=h.run(`charts.get('${id}')`);
    assert.equal(chart.unit,'USDC');
    assert.ok(chart.rows.length>0);
    assert.ok(chart.rows.every(row=>Date.parse(row.date)>=Date.parse('2026-11-14T00:00:00Z')));
  }
  assert.match(html,/No financial update in this period/);
  assert.equal(h.run('selectedFinancialTotals().net_profit'),null);
});

test('between-report volume carries both publication timestamps after filtering',()=>{
  const h=harness('#/overview?range=30d');
  h.run("activityModes.volume='interval'; protocolActivityChart('volume-proof')");
  const chart=h.run("charts.get('volume-proof')");
  assert.equal(chart.interval,true);
  assert.ok(chart.rows.length>0);
  for (const row of chart.rows) {
    const first=archivedFinancials.reports.find(r=>r.published_at===row.startDate);
    const last=archivedFinancials.reports.find(r=>r.published_at===row.date);
    assert.ok(Date.parse(first.published_at)<Date.parse(last.published_at));
    assert.equal(row.values.value,Number(last.snapshot.cumulative_volume)-Number(first.snapshot.cumulative_volume));
  }
});

test('the swap filter changes both market charts and the drilldown table to the same product',()=>{
  const h=harness('#/omni?contract=swap&range=all');
  const html=h.run('omni()');
  assert.match(html,/Swaps · 24-Hour Trading Volume/);
  assert.match(html,/Swaps · Trader Open Interest/);
  assert.ok(h.run("marketRows().every(market=>market.contract_type==='swap')"));
  const chart=h.run("charts.get('omni-volume')");
  for (const row of chart.rows) {
    const snap=history.snapshots.find(s=>s.observed_at===row.date);
    assert.equal(row.values.volume,snap.by_contract_type.find(i=>i.contract_type==='swap').volume_24h_usdc);
  }
});

test('individual quote exceptions carry through to the market view and quote CSV',()=>{
  const h=harness('#/omni?range=7d');
  h.run(`const testMarket=latestMarkets()[0];
    testMarket.source_warnings=[{field:'quotes.size_100k',code:'zero_indicative_quote_price',source_fields:['quotes.size_100k.bid']}];
    testMarket.quotes.size_100k=null;
    globalThis.savedExport=null;downloadCSV=(name,columns,rows)=>globalThis.savedExport={columns,rows};
    exportQuotes(marketKey(testMarket));`);
  assert.match(h.run('marketDetail(testMarket)'),/Source reported zero bid/);
  const report=h.context.savedExport;
  assert.equal(report.columns.at(-1),'quote_status');
  assert.equal(report.rows.find(row=>row[1]==='100000').at(-1),'Source reported zero bid');
  assert.equal(report.rows.find(row=>row[1]==='100000')[4],undefined);
});

function operatingHarness(hash) {
  return harness(hash,{...archivedFinancials,reports:frozenOperatingReports});
}

function exportOperatingRows(view) {
  view.run('globalThis.downloaded=null; downloadCSV=(filename,columns,rows)=>{globalThis.downloaded={filename,columns,rows}}; exportWaterfall();');
  return view.context.downloaded;
}

test('Operating Results shows the waterfall before allocation and exports the selected totals with provenance',()=>{
  const view=operatingHarness();
  const html=view.run('financials()');
  assert.match(html,/<h1>Operating Results<\/h1>/);
  assert.ok(html.indexOf('<h2>Gross Revenue to Net Profit</h2>') < html.indexOf('<h2>Net Profit Allocation</h2>'));
  assert.match(html,/<h2>Results by Period/);
  assert.doesNotMatch(html,/Bi-weekly Financial Reports/);
  const exported=exportOperatingRows(view);
  assert.equal(exported.filename,'variational-operating-waterfall-all.csv');
  const lines=[...exported.rows];
  assert.equal(lines.find(row=>row[0]==='Net Profit')[2],'27673543');
  assert.ok(lines.some(row=>row[2]==='-33833384'));
  assert.ok(lines.some(row=>row[2]==='-15488037' && row[5]===12 && row[6]===19));
  assert.ok(lines.some(row=>row[2]==='310037'));
  for(const row of lines){
    assert.equal(row.length,exported.columns.length);
    assert.equal(row[7],frozenOperatingReports[0].published_at);
    assert.equal(row[8],frozenOperatingReports.at(-1).published_at);
    assert.equal(row[9].split(' ').length,19);
  }
});

test('individual breakdown links select the same publication in both panels, its source and the CSV',()=>{
  const latest=frozenOperatingReports.at(-1);
  const view=operatingHarness('#/financials?range=all&report='+encodeURIComponent(latest.id));
  const selection=view.run('resultsSelection()');
  assert.equal(selection.records.length,1);
  assert.equal(selection.selected.id,latest.id);
  const html=view.run('financials()');
  assert.ok(html.includes('value="'+latest.id+'" selected'));
  assert.equal((html.match(/13 Nov 2026 update · USD/g)||[]).length,2);
  assert.ok(html.includes(view.run('officialUpdate(resultsSelection().selected)')));
  const exported=exportOperatingRows(view);
  assert.equal(exported.filename,'variational-operating-waterfall-2026-11-13.csv');
  assert.deepEqual([...exported.rows.filter(row=>row[2]!==null).map(row=>row[2])],
    ['4908930','-1699994','3208936','-217813','2991123']);
  assert.ok(exported.rows.every(row=>row[7]===row[8] && row[7]===latest.published_at));
  assert.equal(view.run('selectedReports().length'),19);
  view.clickRange('7d');
  assert.ok(!view.location.hash.includes('report='));
  assert.equal(view.run('resultsSelection().records.length'),0);
  assert.equal(exportOperatingRows(view),null);
});

test('an out-of-window report URL cannot bring later or earlier earnings into the window',()=>{
  const view=operatingHarness('#/financials?range=7d&report='+encodeURIComponent(frozenOperatingReports.at(-1).id));
  assert.equal(view.run('resultsSelection().records.length'),0);
  assert.match(view.run('financials()'),/data-results-report aria-label="Breakdown period" disabled/);
  assert.doesNotMatch(view.run('financials()'),/data-export-waterfall/);
  const stale=operatingHarness('#/financials?range=30d&report='+encodeURIComponent(frozenOperatingReports[0].id));
  assert.ok(stale.run('selectedReports().length')>0);
  assert.equal(stale.run('resultsSelection().records.length'),0);
  assert.match(stale.run('financials()'),/The linked update is unavailable in this period/);
  assert.equal(exportOperatingRows(stale),null);
});

test('incompatible published units isolate the breakdown and keep the source table accessible',()=>{
  const view=operatingHarness();
  const valid=view.context.fixtures.financials.reports.at(-1);
  view.context.bad={...valid,units:{...valid.units,net_profit:'USDC'}};
  view.run('DATA.financials={...DATA.financials,reports:[bad]}');
  const html=view.run('financials()');
  assert.match(html,/Source fields could not be verified/);
  assert.match(html,/Results by Period/);
  assert.doesNotMatch(html,/data-export-waterfall/);
  assert.equal(exportOperatingRows(view),null);
});

test('table values preserve fractional source dollars, signed sub-dollar amounts and omissions',()=>{
  const view=operatingHarness();
  assert.equal(view.run('exactReportAmount("9007199254740993.125")'),'9,007,199,254,740,993.125');
  assert.equal(view.run('exactReportAmount("-0.005")'),'−0.005');
  assert.equal(view.run('exactReportAmount("0")'),'0');
  assert.equal(view.run('exactReportAmount(null)'),'—');
});
