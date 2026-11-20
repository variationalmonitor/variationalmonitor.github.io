import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const asset=path=>readFile(new URL('../public/assets/'+path,import.meta.url),'utf8');
const [utils,ranges,app]=await Promise.all([asset('utils.js'),asset('ranges.js'),asset('app.js')]);
const fixture=async name=>JSON.parse(await readFile(new URL('../public/data/'+name,import.meta.url),'utf8'));
const [financials,markets,history,supplement]=await Promise.all(['financials.json','markets.json','market-history.json','supplement.json'].map(fixture));
const archivedFinancials={...financials,reports:financials.reports.filter(report=>Date.parse(report.published_at)<=Date.parse('2026-11-13T23:59:59Z'))};

function harness(hash='#/financials?range=all'){
  const events={document:{},window:{}},nodes=new Map();
  const node=id=>{if(!nodes.has(id))nodes.set(id,{id,innerHTML:'',textContent:'',hidden:false,dataset:{},classList:{toggle(){},contains(){return false;}},setAttribute(){},removeAttribute(){},getAttribute(){return null;},querySelectorAll(){return [];},querySelector(){return node('link');},contains(){return false;},focus(){},addEventListener(){}});return nodes.get(id);};
  let currentHash=hash;
  const location={search:'',href:'https://example.com/',get hash(){return currentHash;},set hash(value){currentHash='#'+value.replace(/^#/,'');}};
  const listen=target=>(type,handler)=>(events[target][type]??=[]).push(handler);
  const document={getElementById:node,querySelectorAll(){return [];},addEventListener:listen('document'),documentElement:{dataset:{}},activeElement:null};
  const context=vm.createContext({document,location,window:{addEventListener:listen('window'),scrollTo(){},innerWidth:1200},URL,URLSearchParams,Intl,Date,Map,Set,console,setTimeout,clearTimeout,requestAnimationFrame:fn=>fn(),mountChart:()=>()=>{},fixtures:{financials:archivedFinancials,markets,history,supplement}});
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
  assert.match(html,/No bi-weekly report in this period/);
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
