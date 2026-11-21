import {escapeHTML as e,number as n,compact,fixed,price,cash,percent,ratio,date,shortDate,timestamp,safeURL,sourceLink,downloadCSV,sum,cumulative} from './utils.js';
import {mountChart} from './charts.js';
import {buildProtocolMetrics} from './protocol-metrics.js';
import {buildOperatingResults,OperatingResultsError} from './operating-results.js';
import {waterfallBody,waterfallExportRows} from './results-chart.js';
import {warningsForMetrics,RANGE_OPTIONS,normalizeRange,rangeBounds,filterRange,chartDomain,financialSeries,periodTotals,latestTimestamp,rangeHref} from './ranges.js';

const BLUE='#4c9af8',TEAL='#80b8bb',GREY='#6c829d',LIGHT='#b8c6d8';
const API_DOCS='https://docs.variational.io/technical-documentation/api';
const DATA={financials:null,markets:null,history:null,supplement:null,baselines:null,chainHistory:null,multiVenue:null,officialActivity:null,arbitrumIndex:null,executionStudy:null,classifications:null,protocolHistory:null,rewards:null,errors:{}};
const state={financialKey:'net_profit',financialMode:'cumulative',allocationMode:'period',olpMode:'lifetime',marketSearch:'',marketSort:'volume_24h_usdc',marketDir:-1,marketPage:1,explorerSearch:'',explorerCategory:'All'};
const titles={overview:'Overview',omni:'Omni',tradfi:'TradFi',accounts:'Accounts',execution:'Execution',financials:'Financials',rewards:'Rewards',olp:'OLP',var:'VAR',pro:'Variational Pro',research:'Research',explorer:'Data Explorer'};
const financialLabels={gross_revenue:'Spreads Paid (Gross Revenue)',market_making_costs:'Market Making Costs (Cost of Revenue)',net_revenue:'Net Revenue',rewards:'Rewards',net_profit:'Net Profit',treasury_inflow:'Protocol Treasury Inflow',olp_pnl:'OLP PnL'};
const charts=new Map();let chartCleanups=[];let route={page:'overview',params:new URLSearchParams()};let lastFocus=null;
const historyCache=new Map(),historyPending=new Set(),historyFailed=new Set();
const main=document.getElementById('main');
const reports=()=>DATA.financials?.reports||[];
const latestReport=()=>reports().at(-1);
const latest=()=>DATA.markets?.latest;
const latestMarkets=()=>latest()?.markets||[];
const totals=()=>DATA.financials?.totals?.period_amounts||{};
const snapshots=()=>DATA.history?.snapshots||[];
const coverageText=()=>reports().length?`${reports().length} reports / ${shortDate(reports()[0].published_at)}–${date(latestReport().published_at)} UTC`:'Financial reports unavailable';
const marketKey=m=>m.ticker+'|'+m.contract_type;
function marketCoverageLabel(){const t=latest()?.totals||{};const observed=n(t.num_markets_observed),reported=n(t.num_markets_reported);return observed!==null&&reported!==null&&observed!==reported?`${fixed(observed,0)} observed / ${fixed(reported,0)} reported markets`:`${fixed(observed,0)} observed markets`;}
function completeMarketCoverage(){const t=latest()?.totals||{};return n(t.num_markets_observed)!==null&&n(t.num_markets_reported)!==null&&n(t.num_markets_observed)===n(t.num_markets_reported);}
const selectedMarket=()=>latestMarkets().find(m=>marketKey(m)===route.params.get('market'));

function selectedRange(){return normalizeRange(route.params.get('range'));}
function rangeAnchor(){return latestTimestamp([DATA.markets?.generated_at,latest()?.observed_at,DATA.financials?.generated_at,latestReport()?.published_at,DATA.supplement?.chain?.observed_at,DATA.supplement?.token?.checked_at,DATA.supplement?.execution?.last_attempt_at,DATA.chainHistory?.generated_at]);}
function rangeRows(rows){return filterRange(rows,selectedRange(),rangeAnchor());}
function selectedChartDomain(rows){return chartDomain(rows,selectedRange(),rangeAnchor());}
function selectedReports(){return filterRange(reports(),selectedRange(),rangeAnchor(),r=>r.published_at);}
function selectedFinancialTotals(){return periodTotals(selectedReports(),Object.keys(financialLabels));}
function pageRangeToolbar(){return `<div class="range-toolbar"><span class="range-label">Period</span><div class="range-buttons" role="group" aria-label="Historical period">${RANGE_OPTIONS.map(([key,label])=>`<button type="button" data-range="${key}" aria-pressed="${selectedRange()===key}">${label}</button>`).join('')}</div>${infoButton('range','Date-range methodology')}</div>`;}
let pendingRangeFocus=false;
let pendingResultsFocus=false;
function syncRangeLinks(){document.querySelectorAll('a[href^="#/"]:not([data-fixed-range])').forEach(a=>a.setAttribute('href',rangeHref(a.getAttribute('href'),selectedRange())));if(pendingRangeFocus){pendingRangeFocus=false;requestAnimationFrame(()=>main.querySelector('button[data-range="'+selectedRange()+'"]')?.focus({preventScroll:true}));}}
function selectedFlowDetail(){return selectedReports().length?'Reports published in selected period':'No reports published in selected period';}
function rangeWindowLabel(){const bounds=rangeBounds(selectedRange(),rangeAnchor());return bounds.endExclusive===null?'Capture date unavailable':(bounds.start===null?'Archive start':date(bounds.start))+' through '+date(bounds.endExclusive-1)+' UTC';}

function infoButton(key,label='Metric information',context='') {
  const text = String(context).replace(/<[^>]+>/g,'').trim();
  return `<button class="info" type="button" data-method="${e(key)}"${text ? ' data-context="'+e(text)+'"' : ''} aria-label="${e(label)}">i</button>`;
}
function heading(title,context='',_eyebrow='',actions='',method='coverage') {
  return `<div class="page-heading"><h1>${e(title)}</h1><div class="page-actions">${actions}
    ${context ? infoButton(method,title+' information',context) : ''}</div></div>`;
}
function stat(label,value,detail,method) {
  return `<div class="stat"><div class="stat-label"><span>${e(label)}</span>${method ? infoButton(method,label+' methodology',detail) : ''}</div>
    <div class="stat-value">${e(value)}</div></div>`;
}
function panel(title,subtitle,content,method,extra='') {
  return `<section class="panel"><div class="panel-head"><h2>${e(title)}</h2><div class="panel-tools">${extra}
    ${method ? infoButton(method,title+' methodology',subtitle) : ''}</div></div>${content}</section>`;
}
function dataUnavailable(name,key){return `<section class="error-card"><h2>${e(name)} unavailable</h2><p>${e(DATA.errors[key]||'No source data is available for this view.')}</p></section>`;}
function warningCount(){return reports().reduce((count,r)=>count+(r.warnings?.length||0),0);}
function sourceFooter(url,label,detail=''){return `<div class="source-line"><span>${detail ? infoButton('coverage',label+' information',detail) : ''}</span>${sourceLink(url,label)}</div>`;}
function financialRows(keys,{mode='period',snapshot=false}={}){return financialSeries(reports(),keys,{mode,snapshot,range:selectedRange(),anchor:rangeAnchor()}).map(r=>({...r,label:date(r.date)+' UTC'}));}
function marketHistoryEntries(){return filterRange(snapshots(),selectedRange(),rangeAnchor(),s=>s.observed_at);}
function historyRows(key,market=null) {
  return marketHistoryEntries().map(snapshot => {
    const data = snapshot.markets ? snapshot : historyCache.get(snapshot.date) ||
      (snapshot.observed_at === latest()?.observed_at ? latest() : null);
    const record = market ? data?.markets?.find(row => marketKey(row) === marketKey(market)) : snapshot.totals;
    const values = typeof key === 'function' ? key(record) : {value:record?.[key] ?? null};
    return {date:snapshot.observed_at,label:timestamp(snapshot.observed_at),values,source:snapshot.provenance?.source_url};
  });
}
function marketHistoryPanels(market) {
  const swap = market.contract_type === 'swap';
  const standard = {method:'market-history',source:'data/market-history.json',sourceLabel:'Data'};
  const series = (key,label,color=BLUE) => ({key,label,color});
  const specs = [
    {id:'market-volume',title:'24-Hour Volume',unit:'USDC',type:'bar',key:'volume_24h_usdc',series:[series('value','24-Hour volume')]},
    {id:'market-oi',title:'Long and Short Open Interest',unit:'USDC',type:'stacked',
      key:m=>({long:n(m?.long_open_interest_usdc),short:n(m?.short_open_interest_usdc)===null ? null : -n(m.short_open_interest_usdc)}),
      series:[series('long','Long'),series('short','Short',TEAL)]},
    {id:'market-skew',title:'Long/Short Ratio',unit:'x',key:m=>({value:ratio(m?.long_open_interest_usdc,m?.short_open_interest_usdc)}),
      series:[series('value','Long/short ratio')]},
    {id:'market-turnover',title:'Open Interest Turnover',unit:'x',key:m=>({value:ratio(m?.volume_24h_usdc,m?.trader_open_interest_usdc)}),
      series:[series('value','Volume / trader open interest',TEAL)]},
    {id:'market-funding',title:swap?'Annualized Financing':'Annualized Funding',unit:'%',
      key:m=>({value:n(m?.funding?.last?.[swap?'annualized_long_rate':'annualized_rate'])===null ? null : n(m.funding.last[swap?'annualized_long_rate':'annualized_rate'])*100,
        short:n(m?.funding?.last?.annualized_short_rate)===null ? null : n(m.funding.last.annualized_short_rate)*100}),
      series:swap?[series('value','Long'),series('short','Short',TEAL)]:[series('value','Annualized funding')]},
    {id:'market-slippage',title:'Quoted Half-Spread at $100K',unit:'bps',key:m=>{
      const full = n(m?.quotes?.size_100k?.full_spread_bps);
      return {value:full===null ? null : full/2};
    },series:[series('value','Quoted half-spread',TEAL)]}
  ];
  const window = marketHistoryEntries();
  const pending = window.some(snapshot => historyPending.has(snapshot.date) ||
    (snapshot.partition_url && snapshot.observed_at!==latest()?.observed_at &&
      !snapshot.markets && !historyCache.has(snapshot.date) && !historyFailed.has(snapshot.date)));
  const failed = window.some(snapshot => historyFailed.has(snapshot.date));
  const status = pending ? '<p class="data-state" role="status">Loading market history…</p>' :
    failed ? '<p class="data-state" role="status">Some observations could not load.</p>' : '';
  return status+'<div class="grid two">'+specs.map(({key,...spec})=>chartPanel({...standard,...spec,
    rows:historyRows(key,market),subtitle:spec.id==='market-slippage' ?
      'Half of the indicative full spread at $100,000. One saved quote per observation.' :
      spec.id==='market-turnover'?'Rolling 24-hour volume divided by trader open interest.':
      spec.id==='market-skew'?'Long trader open interest divided by short trader open interest.':
      spec.id==='market-oi'?'Trader positions only. The short side is drawn below zero.':'Saved Omni API observations.'})).join('')+'</div>';
}
function executionObservation() {
  const eligible = marketHistoryEntries().filter(entry=>entry.partition_url || entry.markets);
  const coverage = new Map((DATA.protocolHistory?.rows || []).map(row=>[row.observedAt,row.swapQuotes]));
  const quoted = [...eligible].reverse().find(entry=>coverage.get(entry.observed_at)>0);
  const requested = route.params.get('quote-date');
  const snapshot = requested ? eligible.find(entry=>entry.date===requested) : quoted || eligible.at(-1);
  const data = snapshot?.observed_at===latest()?.observed_at ? latest() : snapshot?.markets ? snapshot : historyCache.get(snapshot?.date);
  return {eligible,snapshot,data,requested,prior:!requested && Boolean(snapshot && snapshot!==eligible.at(-1))};
}
async function ensureMarketHistory() {
  const market = route.page==='omni' && selectedMarket();
  const execution = route.page==='execution' || (route.page==='omni' && route.params.get('view')==='execution');
  if (!market && !execution) return;
  const requested = location.hash;
  const entries = market ? marketHistoryEntries() : [executionObservation().snapshot].filter(Boolean);
  const jobs = entries.filter(entry => !entry.markets && entry.observed_at!==latest()?.observed_at &&
    entry.partition_url && !historyCache.has(entry.date) && !historyPending.has(entry.date) && !historyFailed.has(entry.date));
  if (!jobs.length) return;
  for (const entry of jobs) historyPending.add(entry.date);
  const queue = [...jobs];
  await Promise.all(Array.from({length:Math.min(3,queue.length)},async()=>{
    while (queue.length) {
      const entry = queue.shift();
      try {
        if (!/^data\/market-history\/\d{4}-\d{2}-\d{2}\.json$/.test(entry.partition_url)) throw new Error('Unexpected history path');
        const response = await fetch(new URL('../'+entry.partition_url,import.meta.url));
        if (!response.ok) throw new Error('History HTTP '+response.status);
        const payload = await response.json();
        const snapshot = payload.snapshot;
        if (payload.schema_version!==2 || payload.format!==entry.partition_format ||
            !Array.isArray(snapshot?.markets) || snapshot.date!==entry.date || snapshot.observed_at!==entry.observed_at) {
          throw new Error('History metadata mismatch');
        }
        historyCache.set(entry.date,snapshot);
      } catch { historyFailed.add(entry.date); }
      finally { historyPending.delete(entry.date); }
    }
  }));
  if (location.hash===requested || (route.page==='omni' && selectedMarket()) ||
      route.page==='execution' || (route.page==='omni' && route.params.get('view')==='execution')) render();
}
function chartPanel({id, title, subtitle, rows, series, type='line', unit='$', method='financials',
                     toolbar='', note='', source, sourceLabel='Source data', maxGapDays, interval=false}) {
  const hasData = rows.some(row => series.some(item => n(row.values[item.key]) !== null));
  charts.set(id, {rows, series, type, unit, label:title, xDomain:selectedChartDomain(rows), maxGapDays, interval});
  const legend = series.length > 1 ? '<div class="chart-legend">' + series.map(item =>
    `<span><i style="background:${e(item.color)}"></i>${e(item.label)}</span>`).join('') + '</div>' : '';
  const controls = toolbar ? `<div class="chart-toolbar">${toolbar}</div>` : '';
  const exception = rows.some(row => row.warnings?.length) ?
    '<button type="button" class="text-button" style="color:var(--amber)" data-method="exceptions">Source exception</button>' : '';
  const headers = series.map(item => `<th class="number">${e(item.label)} (${e(unit)})</th>`).join('');
  const details = `<details class="table-disclosure" data-chart-table="${e(id)}">
    <summary>Chart data</summary><div class="table-wrap"><table class="data-table">
    <thead><tr>${interval ? '<th>From (UTC)</th><th>To (UTC)</th>' : '<th>Date (UTC)</th>'}${headers}<th>Source</th></tr></thead><tbody></tbody></table></div></details>`;
  const measured = rows.filter(row=>series.some(item=>n(row.values[item.key])!==null)).length;
  const sparse = measured>0 && measured<=8 ? `<span class="chart-coverage">${measured} observation${measured===1?'':'s'}</span>` : '';
  const context = [subtitle,note].filter(Boolean).join('. ');
  const historyURL = rangeHref(location.hash,'all');
  const chart = hasData ? `${legend}<div class="chart-shell" data-chart="${e(id)}"></div>` :
    `<div class="chart-empty">No observations in this period${selectedRange()!=='all' && historyURL.startsWith('#/') ?
      ' <a class="inline-link" data-fixed-range href="'+e(historyURL)+'">View all history</a>' : ''}</div>`;
  const footer = hasData ? `<div class="chart-bottom">${sparse}<div>${exception}
    <button type="button" class="text-button" data-export-chart="${e(id)}">CSV</button>
    ${source ? sourceLink(source,sourceLabel) : ''}</div></div>${details}` : '';
  return panel(title, context, controls+chart+footer, method);
}

function chartValueForTable(value) {
  if (typeof value==='string' && value && /^-?\d+(?:\.\d+)?$/.test(value)) return exactReportAmount(value);
  return n(value)===null ? '—' : new Intl.NumberFormat('en-US',{maximumSignificantDigits:12}).format(n(value));
}
document.addEventListener('toggle', event => {
  const details = event.target;
  if (!details.matches?.('details[data-chart-table]') || !details.open || details.dataset.loaded) return;
  const chart = charts.get(details.dataset.chartTable);
  if (!chart) return;
  const body = details.querySelector('tbody');
  body.innerHTML = chart.rows.length ? chart.rows.map(row => {
    const values = chart.series.map(item => `<td class="number">${e(chartValueForTable(row.values[item.key]))}</td>`).join('');
    return `<tr>${chart.interval ? '<td>'+e(row.startDate ? timestamp(row.startDate) : '—')+'</td>' : ''}<td>${e(timestamp(row.date))}</td>${values}<td>${row.source ? sourceLink(row.source,'Source') : '—'}</td></tr>`;
  }).join('') : `<tr><td colspan="${chart.series.length+(chart.interval?3:2)}">No observations in this period.</td></tr>`;
  details.dataset.loaded = 'true';
}, true);

function financialToolbar(){return `<div class="field-select"><select aria-label="Financial metric" data-financial-key>${['net_profit','gross_revenue','net_revenue','treasury_inflow','olp_pnl'].map(k=>`<option value="${k}"${state.financialKey===k?' selected':''}>${e(financialLabels[k])}</option>`).join('')}</select></div><div class="segmented" aria-label="Financial aggregation"><button type="button" data-financial-mode="period" aria-pressed="${state.financialMode==='period'}">Per report</button><button type="button" data-financial-mode="cumulative" aria-pressed="${state.financialMode==='cumulative'}">Cumulative</button></div>`;}
function primaryFinancialChart(id='performance'){return chartPanel({id,title:state.financialMode==='cumulative'?'Cumulative '+financialLabels[state.financialKey]:financialLabels[state.financialKey]+' by Report',subtitle:'Reported / $ / publication dates in UTC',rows:financialRows([state.financialKey],{mode:state.financialMode}),series:[{key:state.financialKey,label:financialLabels[state.financialKey],color:BLUE}],type:state.financialMode==='cumulative'?'area':'bar',toolbar:financialToolbar(),note:state.financialMode==='cumulative'?'Cumulative from archive start; displayed period is cropped':'Native report amounts',source:'data/financials.json',sourceLabel:'JSON'});}
function apiNotice() {
  const observed = latest()?.observed_at;
  if (!observed) return '';
  const age = Date.now() - Date.parse(observed);
  if (age <= 36 * 3600 * 1000) return '';
  return `<div class="notice"><span>Latest Omni observation: ${e(timestamp(observed))}.</span>
    <button class="text-button nowrap" data-method="freshness" type="button">Source status</button></div>`;
}
const activityModes = {volume:'cumulative', interest:'dual'};
function activityToolbar(metric, modes) {
  const buttons = modes.map(([key,label]) => `<button type="button" data-activity-metric="${e(metric)}"
    data-activity-mode="${e(key)}" aria-pressed="${activityModes[metric] === key}">${e(label)}</button>`).join('');
  return `<div class="segmented" role="group" aria-label="${metric === 'volume' ? 'Trading volume measure' : 'Open interest measure'}">${buttons}</div>`;
}

function publishedMetricRecords(field) {
  const series = new Map();
  for (const report of DATA.officialActivity?.reports || []) {
    const metric=report.metrics?.[field];
    if (!metric) continue;
    series.set(report.id,{date:report.published_at,value:metric.value,unit:metric.unit,
      source:report.source_urls?.[0],detail:metric.precision_increment && n(metric.precision_increment)>1 ? 'Rounded in the source update' : ''});
  }
  for (const report of reports()) {
    const value = report.snapshot?.[field];
    if (value!==null && value!==undefined) series.set(report.id,
      {date:report.published_at,value,unit:report.units?.[field],source:report.source_urls?.[0]});
  }
  return [...series.values()].sort((a,b)=>a.date.localeCompare(b.date));
}
function publishedMetricRows(field,label='value') {
  return rangeRows(publishedMetricRecords(field).map(row=>({...row,values:{[label]:row.value}})));
}
function accountsView() {
  const index = DATA.arbitrumIndex;
  const title = heading('Accounts')+pageRangeToolbar();
  const reported = officialActivityPanels();
  const userStats = [['Daily Active Users','daily_active_users_week_average'],['Weekly Active Users','weekly_active_users']].map(([label,field])=>{
    const item = publishedMetricRecords(field).at(-1);
    return item ? stat(label+' ('+shortDate(item.date)+')',fixed(item.value,0),date(item.date),'reported-activity') : '';
  }).join('');
  const activeCards = userStats ? `<div class="stats user-activity-stats">${userStats}</div>` : '';
  if (!index?.summary || !index.coverage) return title+activeCards+reported+dataUnavailable('Onchain pool index','arbitrumIndex');
  const {coverage,summary} = index;
  const window = rangeBounds(selectedRange(),rangeAnchor());
  const indexStart = Date.parse(coverage.from_timestamp), indexEnd = Date.parse(coverage.to_timestamp);
  const wholeIndex = selectedRange()==='all' || (window.start!==null && window.endExclusive!==null &&
    indexStart>=window.start && indexEnd<window.endExclusive);
  if (!wholeIndex) {
    const overlapping = window.start!==null && window.endExclusive!==null &&
      indexStart<window.endExclusive && indexEnd>=window.start;
    const message = overlapping ? 'Only part of the indexed interval falls in this period.' : 'No indexed blocks in this period.';
    return title+activeCards+panel('Verified Pool Index Coverage','',`<div class="chart-empty">${message}
      <a class="inline-link" href="#/accounts?range=all" data-fixed-range>View all indexed blocks</a></div>`,'pool-index')+reported;
  }
  const indexed = `Block ${fixed(coverage.from_block,0)} through ${fixed(coverage.to_block,0)}. ${timestamp(coverage.from_timestamp)} to ${timestamp(coverage.to_timestamp)}.`;
  const stats = `<div class="stats account-index-stats">
    ${stat('Indexed Pool Creations',fixed(summary.created_pools,0),indexed,'pool-index')}
    ${stat('Distinct Pool Creators',fixed(summary.distinct_creator_accounts,0),indexed,'pool-index')}
    ${stat('Creation Participants Excluding Core OLP',fixed(summary.creation_participant_accounts_excluding_core_olp,0),indexed,'pool-index')}</div>`;
  const indexRows = (coverage.ranges || []).map(chunk=>`<tr><td>${e(timestamp(chunk.from_timestamp))}</td><td>${e(timestamp(chunk.to_timestamp))}</td>
    <td class="number">${fixed(chunk.from_block,0)}</td><td class="number">${fixed(chunk.to_block,0)}</td>
    <td class="number">${fixed(chunk.log_count,0)}</td></tr>`).join('');
  const availability = [['trading_active_accounts','Onchain Trading Activity'],['protocol_net_flows','Settlement Pool Net Flows'],['referral_claims','Onchain Referral Claims']];
  const statusRows=availability.map(([key,label])=>`<tr><th scope="row">${e(label)}</th><td class="wrap muted">${e(index.availability?.[key]?.reason || 'Coverage unavailable')}</td></tr>`).join('');
  const firstDate = date(coverage.from_timestamp), lastDate = date(coverage.to_timestamp);
  const indexDates = firstDate === lastDate ? firstDate : firstDate+' to '+lastDate;
  return title+activeCards+'<p class="data-state">Indexed '+e(indexDates)+' UTC</p>'+stats+panel('Verified Pool Index Coverage',indexed,`<div class="table-wrap"><table class="data-table">
    <thead><tr><th>From (UTC)</th><th>To (UTC)</th><th class="number">First block</th><th class="number">Last block</th>
    <th class="number">Pool creations</th></tr></thead><tbody>${indexRows}</tbody></table></div>
    <div class="source-line">${sourceLink(index.factory?.registry_url,'Contract registry')}<a class="inline-link" href="data/arbitrum-index.json" download>Index data</a></div>`,'pool-index')+
    reported+`<div class="section-space">${panel('Onchain Coverage Gaps','',`<div class="table-wrap"><table class="data-table"><tbody>${statusRows}</tbody></table></div>`,'pool-index')}</div>`;
}
function officialActivityPanels() {
  if (!DATA.officialActivity?.reports.length) return '';
  const common={source:'data/official-activity.json',sourceLabel:'Data',method:'reported-activity',type:'line'};
  const charts = [
    ['daily_active_users_week_average','Reported Daily Active Users','count','Daily users (seven-day average)',BLUE],
    ['weekly_active_users','Reported Weekly Active Users','count','Weekly active users',TEAL],
    ['total_trades_placed','Reported Cumulative Trade Count','count','Total trades placed',BLUE],
    ['tokens_listed','Reported Listed Tokens','count','Listed tokens',TEAL],
  ].map(([field,title,unit,label,color])=>chartPanel({...common,id:'activity-'+field,title,unit,
    rows:publishedMetricRows(field),series:[{key:'value',label,color}]}));
  return `<div class="grid two section-space">${charts.join('')}</div>`;
}

function protocolActivityChart(id='overview-volume') {
  const mode = activityModes.volume;
  const toolbar = activityToolbar('volume', [['cumulative','Cumulative'],['interval','Between reports'],['rolling','24H']]);
  if (mode === 'rolling') {
    return chartPanel({id, title:'24-Hour Trading Volume', subtitle:'Omni API / USDC', rows:historyRows('volume_24h_usdc'),
      series:[{key:'value',label:'24H volume',color:BLUE}], toolbar, type:'bar', unit:'USDC',
      method:'market-history', source:'data/market-history.json',sourceLabel:'JSON'});
  }
  const cumulativeMode = mode === 'cumulative';
  const fullRows = [];
  let previous = null;
  for (const report of publishedMetricRecords('cumulative_volume')) {
    const amount = n(report.value);
    const available = amount !== null && report.unit === '$';
    const comparable = available && previous && amount >= previous.amount;
    const value = cumulativeMode ? (available ? amount : null) : (comparable ? amount - previous.amount : null);
    fullRows.push({date:report.date,detail:report.detail,startDate:comparable ? previous.date : null,values:{value},source:report.source});
    previous = available ? {amount,date:report.date} : null;
  }
  const title = cumulativeMode ? 'Cumulative Trading Volume' : 'Trading Volume Between Reports';
  const visible = rangeRows(fullRows);
  if (cumulativeMode && !visible.some(row => row.values.value !== null)) {
    const apiRows = historyRows('cumulative_total_volume_usdc');
    if (apiRows.some(row => n(row.values.value) !== null)) {
      return chartPanel({id,title,subtitle:'Omni API / USDC',rows:apiRows,
        series:[{key:'value',label:'Lifetime trading volume',color:BLUE}],toolbar,type:'bar',unit:'USDC',
        method:'api-volume-window',source:'data/market-history.json',sourceLabel:'API data',
        note:'No financial update in this period'});
    }
  }
  return chartPanel({id, title, subtitle:'Variational updates / USD', rows:visible,
    series:[{key:'value',label:cumulativeMode?'Total Volume Traded':'Change in Total Volume Traded',color:BLUE}],
    toolbar, type:cumulativeMode?'area':'bar',unit:'$',method:'reported-volume',interval:!cumulativeMode,
    note:visible.length?'':'Latest report / '+date(latestReport()?.published_at),
    source:DATA.officialActivity?'data/official-activity.json':'data/financials.json',sourceLabel:'Reports'});
}

function protocolOpenInterestChart(id='overview-oi') {
  const toolbar = activityToolbar('interest',[['dual','Dual-sided OI'],['trader','Trader OI']]);
  if (activityModes.interest === 'dual') {
    const visible = publishedMetricRows('dual_sided_oi','dual_sided_oi');
    if (!visible.some(row => n(row.values.dual_sided_oi) !== null)) {
      const apiRows = historyRows('open_interest_total_usdc');
      if (apiRows.some(row => n(row.values.value) !== null)) {
        return chartPanel({id,title:'Dual-Sided Open Interest',subtitle:'Omni API / USDC',rows:apiRows,
          series:[{key:'value',label:'Double-sided open interest',color:TEAL}],toolbar,type:'bar',unit:'USDC',
          method:'api-dual-window',source:'data/market-history.json',sourceLabel:'API data',
          note:'No financial update in this period'});
      }
    }
    return chartPanel({id,title:'Dual-Sided Open Interest',subtitle:'Variational updates / USD', toolbar, unit:'$',
      rows:visible,series:[{key:'dual_sided_oi',label:'Dual-sided OI',color:TEAL}],
      type:'line',method:'reported-oi',note:selectedReports().length?'':'Latest report / '+date(latestReport()?.published_at),source:DATA.officialActivity?'data/official-activity.json':'data/financials.json',sourceLabel:'Reports'});
  }
  return chartPanel({id,title:'Trader Open Interest',subtitle:'Omni API in USDC',toolbar,unit:'USDC',
    rows:historyRows('trader_open_interest_usdc'),series:[{key:'value',label:'Trader open interest',color:TEAL}],
    type:'bar',method:'oi',source:'data/market-history.json',sourceLabel:'JSON'});
}

document.addEventListener('click', event => {
  const button = event.target.closest('button[data-activity-mode]');
  if (!button) return;
  const metric = button.dataset.activityMetric;
  const mode = button.dataset.activityMode;
  activityModes[metric] = mode;
  render();
  main.querySelector(`button[data-activity-metric="${metric}"][data-activity-mode="${mode}"]`)?.focus({preventScroll:true});
});

function overview() {
  const m = latest()?.totals || {};
  const t = selectedFinancialTotals();
  const marketCards = DATA.markets ? `<div class="stats">
    ${stat('24-Hour Trading Volume',compact(m.volume_24h_usdc),'USDC / rolling 24 hours','volume')}
    ${stat('Lifetime Trading Volume',compact(m.cumulative_total_volume_usdc),'USDC / cumulative','volume')}
    ${stat('Trader Open Interest',compact(m.trader_open_interest_usdc),'USDC / trader positions','oi')}
    ${stat('Total Value Locked',compact(m.tvl_usdc),'USDC / Omni API','tvl')}</div>` : dataUnavailable('Omni activity','markets');
  const marketRanks = !DATA.markets ? dataUnavailable('Market rankings','markets') : panel('Markets by 24-Hour Volume','Omni / USDC',
    `<div id="overview-markets">${marketTable({limit:6,compact:true})}</div>
    <div class="source-line"><span>${e(timestamp(latest()?.observed_at))}</span><a class="inline-link" href="#/omni">All markets</a></div>`,'markets');
  const financeCards = selectedReports().length ? `<div class="stats section-space">
    ${stat('Spreads Paid (Gross Revenue)',cash(t.gross_revenue),selectedFlowDetail(),'selected-flow')}
    ${stat('Net Profit',cash(t.net_profit),selectedFlowDetail(),'selected-flow')}
    ${stat('Net Profit / Gross Revenue',percent(ratio(t.net_profit,t.gross_revenue)),'Selected reports','profit-ratio')}
    ${stat('Protocol Treasury Inflow',cash(t.treasury_inflow),selectedFlowDetail(),'selected-flow')}</div>` : '';
  return heading('Overview','','') + pageRangeToolbar() + apiNotice() + marketCards +
    `<div class="grid two">${protocolActivityChart()}${protocolOpenInterestChart()}</div>` + financeCards +
    `<div class="grid two section-space">${primaryFinancialChart()}${marketRanks}</div>
     <div class="section-space">${latestReportSummary()}</div>${protocolDepthPanels()}${officialActivityPanels()}`;
}
function officialUpdate(report) {
  return report?.sources?.find(source => source.matches_current)?.url || report?.source_urls?.[0];
}
function latestReportSummary() {
  const r = latestReport();
  if (!r) return dataUnavailable('Latest results','financials');
  const rows = ['gross_revenue','net_revenue','net_profit'].map(key =>
    `<tr><td>${e(financialLabels[key])}</td><td class="number">${e(cash(r.period_amounts[key]))}</td></tr>`).join('');
  return panel('Latest Results',date(r.published_at)+' / USD',
    `<div class="table-wrap"><table class="data-table"><tbody>${rows}</tbody></table></div>
     <div class="source-line"><button class="text-button" data-report="${e(r.id)}" type="button">Details</button>
     ${sourceLink(officialUpdate(r),'View official update')}</div>`,'financials');
}

function marketRows(){let rows=latestMarkets();
if (route.page==='tradfi') {
  const tickers=new Set((currentProtocolMetrics()?.byMarket || []).filter(item=>item.scope==='tradfi').map(item=>item.ticker+'|'+item.contractType));
  rows=rows.filter(m=>tickers.has(marketKey(m)));
}
const contract=route.params.get('contract');if(contract==='perpetual'||contract==='swap')rows=rows.filter(m=>m.contract_type===contract);const q=state.marketSearch.trim().toLowerCase();if(q)rows=rows.filter(m=>[m.ticker,m.name,m.contract_type].join(' ').toLowerCase().includes(q));return [...rows].sort((a,b)=>{const av=marketSortValue(a),bv=marketSortValue(b);if(av===null)return bv===null?0:1;if(bv===null)return -1;return typeof av==='string'?av.localeCompare(bv)*state.marketDir:(av-bv)*state.marketDir;});}
function marketSortValue(m){if(state.marketSort==='ticker')return m.ticker;if(state.marketSort==='spread')return n(m.quotes?.size_100k?.full_spread_bps);return n(m[state.marketSort]);}
function marketTable({limit=null,compact:small=false}={}){const filtered=small?[...latestMarkets()].sort((a,b)=>n(b.volume_24h_usdc)-n(a.volume_24h_usdc)):marketRows();const start=limit?0:(state.marketPage-1)*40;const page=filtered.slice(start,limit||start+40);const sort=(key,label)=>`<button type="button" class="sort-button ${state.marketSort===key?'active':''}" data-market-sort="${key}">${label}${state.marketSort===key?` <span aria-hidden="true">${state.marketDir===1?'⌃':'⌄'}</span>`:''}</button>`;return `<div class="table-wrap"><table class="data-table"><thead><tr><th>${small?'Market':sort('ticker','Market')}</th>${small?'':'<th class="hide-mobile">Contract</th>'}<th class="number">${small?'24H volume':sort('volume_24h_usdc','24H volume')}</th><th class="number">${small?'Trader OI':sort('trader_open_interest_usdc','Trader OI')}</th>${small?'':`<th class="number hide-mobile">${sort('mark_price','Mark price')}</th><th class="number hide-mobile">${sort('skew','Long/Short')}</th><th class="number hide-mobile">${sort('funding','Funding / financing')}</th><th class="number">${sort('spread','Spread (bps)')}</th>`}</tr></thead><tbody>${page.length?page.map(m=>`<tr><td><button type="button" class="market-button" data-market="${e(marketKey(m))}">${e(m.ticker.replace(/-(PERP|SWAP)$/,''))}</button><span class="market-name">${e(m.name)}</span></td>${small?'':`<td class="hide-mobile"><span class="badge">${m.contract_type==='swap'?'Swap':'Perpetual'}</span></td>`}<td class="number">${compact(m.volume_24h_usdc)}</td><td class="number">${compact(m.trader_open_interest_usdc)}</td>${small?'':`<td class="number hide-mobile">${price(m.mark_price)}</td><td class="number hide-mobile">${fixed(ratio(m.long_open_interest_usdc,m.short_open_interest_usdc),2)}</td><td class="number hide-mobile">${percent(m.funding?.last?.[m.contract_type==='swap'?'annualized_long_rate':'annualized_rate'],1)}</td><td class="number">${fixed(m.quotes?.size_100k?.full_spread_bps,2)}</td>`}</tr>`).join(''):`<tr><td colspan="8" class="wrap">No markets match this search.</td></tr>`}</tbody></table></div>${small?'':`<div class="pagination"><span>${filtered.length?start+1:0}–${Math.min(start+40,filtered.length)} of ${filtered.length} markets</span><div><button class="button compact" type="button" data-market-page="prev"${state.marketPage<=1?' disabled':''}>Previous</button><button class="button compact" type="button" data-market-page="next"${start+40>=filtered.length?' disabled':''}>Next</button></div></div>`}`;}
function contractActivityPanels(contract) {
  if (!['swap','perpetual'].includes(contract)) {
    return protocolActivityChart('omni-volume') + protocolOpenInterestChart('omni-oi');
  }
  const label = contract === 'swap' ? 'Swap' : 'Perpetual';
  const rows = rangeRows(snapshots().map(snapshot => {
    const group = snapshot.by_contract_type?.find(item => item.contract_type === contract);
    return {date:snapshot.observed_at,label:timestamp(snapshot.observed_at),source:snapshot.provenance?.source_url,
      values:{volume:group?.volume_24h_usdc ?? null,interest:group?.trader_open_interest_usdc ?? null}};
  }));
  const common = {rows,subtitle:'Returned Omni listings / USDC',unit:'USDC',method:'product-activity',
    type:'bar',source:'data/market-history.json',sourceLabel:'JSON'};
  return chartPanel({...common,id:'omni-volume',title:'24-Hour '+label+' Volume',
    series:[{key:'volume',label:'24H volume',color:BLUE}]}) +
    chartPanel({...common,id:'omni-oi',title:label+' Trader Open Interest',
    series:[{key:'interest',label:'Trader open interest',color:TEAL}]});
}
function productMix() {
  const groups = latest()?.by_contract_type || [];
  if (!groups.length) return '';
  const observed = sum(groups.map(item => item.volume_24h_usdc));
  const rows = groups.map(group => `<tr><td>${group.contract_type==='swap'?'Swaps':'Perpetuals'}</td>
    <td class="number">${fixed(group.markets,0)}</td><td class="number">${compact(group.volume_24h_usdc)}</td>
    <td class="number">${percent(ratio(group.volume_24h_usdc,observed))}</td><td class="number">${compact(group.trader_open_interest_usdc)}</td></tr>`).join('');
  return `<div class="section-space">${panel('Volume and Open Interest by Contract Type','Omni / USDC',
    '<div class="table-wrap"><table class="data-table"><thead><tr><th>Contract</th><th class="number">Markets</th>'+
    '<th class="number">24H volume</th><th class="number">Volume share</th><th class="number">Trader OI</th></tr></thead><tbody>'+
    rows+'</tbody></table></div>','product-activity')}</div>`;
}
function marketListPanel(title='Market Listings') {
  return `<section class="panel section-space"><div class="panel-head"><h2>${e(title)}</h2>${infoButton('markets',title+' methodology')}</div>
    <div class="table-toolbar"><label class="search-field"><input type="search" placeholder="Search markets" aria-label="Search markets"
      data-market-search value="${e(state.marketSearch)}"></label></div><div id="market-table">${marketTable()}</div></section>`;
}
function omni(){if(route.params.get('view')==='execution')return executionView();if(!DATA.markets)return heading('Omni Markets')+pageRangeToolbar()+dataUnavailable('Omni','markets');const market=selectedMarket();if(market)return marketDetail(market);const contract=route.params.get('contract')||'all';return heading('Omni Markets',`${marketCoverageLabel()}. ${timestamp(latest().observed_at)}`,'',`<button class="button" type="button" data-export-markets>Export markets</button>`,'markets')+pageRangeToolbar()+apiNotice()+`<div class="subnav"><a class="${contract==='all'?'active':''}" href="#/omni">Markets</a><a class="${contract==='perpetual'?'active':''}" href="#/omni?contract=perpetual">Perpetuals</a><a class="${contract==='swap'?'active':''}" href="#/omni?contract=swap">Swaps</a></div><div class="grid two">${contractActivityPanels(contract)}</div>${contract==='all'?productMix():''}${marketListPanel()}`;}
function quoteIssue(market, key) {
  const issue = market.source_warnings?.find(item => item.field === 'quotes.'+key && item.code === 'zero_indicative_quote_price');
  if (!issue) return '';
  const sides = issue.source_fields.map(field => field.split('.').at(-1));
  return 'Source reported zero ' + sides.join(' and ');
}
function marketDetail(m){const q=m.quotes||{},funding=m.funding||{};const quoteRows=[['base','Base'],['size_1k','$1,000'],['size_100k','$100,000'],['size_1m','$1,000,000']];const l=n(m.long_open_interest_usdc),s=n(m.short_open_interest_usdc);return heading(m.ticker,`${m.name} / ${m.contract_type==='swap'?'Swap':'Perpetual'} / ${timestamp(latest().observed_at)}`,'OMNI MARKET DETAIL',`<a class="button" href="#/omni">All markets</a><button class="button" type="button" data-export-quotes="${e(marketKey(m))}">Export quotes</button>`)+pageRangeToolbar()+`<div class="stats">${stat('Mark Price',price(m.mark_price),'<span class="unit">USDC</span>','prices')}${stat('24-Hour Volume',compact(m.volume_24h_usdc),'<span class="unit">USDC</span> / rolling window','volume')}${stat('Trader Open Interest',compact(m.trader_open_interest_usdc),'<span class="unit">USDC</span> / OLP excluded','oi')}${stat('Long Share',percent(ratio(l,(l||0)+(s||0))),'Long / total trader open interest','oi')}</div>${marketHistoryPanels(m)}<div class="grid two section-space">${panel('Indicative Quotes by Size','Prices in USDC / source notional convention',`<div class="table-wrap"><table class="data-table"><thead><tr><th>Notional</th><th class="number">Bid</th><th class="number">Ask</th><th class="number">Full spread, bps</th></tr></thead><tbody>${quoteRows.map(([key,label])=>`<tr><td>${label}${quoteIssue(m,key) ? '<span class="market-name">'+e(quoteIssue(m,key))+'</span>' : ''}</td><td class="number">${price(q[key]?.bid)}</td><td class="number">${price(q[key]?.ask)}</td><td class="number">${fixed(q[key]?.full_spread_bps,2)}</td></tr>`).join('')}</tbody></table></div>${sourceFooter(DATA.markets.source.url,'Omni API',timestamp(q.updated_at))}`,'quotes')}${fundingPanel(m,funding)}</div>`;}
function fundingPanel(m,f){const isSwap=m.contract_type==='swap';const keys=isSwap?[['annualized_long_rate','Annualized long rate'],['annualized_short_rate','Annualized short rate'],['interval_long_rate','Interval long rate'],['interval_short_rate','Interval short rate']]:[['annualized_rate','Annualized rate'],['interval_rate','Interval rate']];return panel(isSwap?'Swap Financing':'Perpetual Funding',`Source interval: ${f.interval_seconds?fixed(n(f.interval_seconds)/3600,0)+' hours':'unavailable'}`,`<div class="table-wrap"><table class="data-table"><thead><tr><th>Rate</th><th class="number">Last</th><th class="number">Next</th></tr></thead><tbody>${keys.map(([key,label])=>`<tr><td>${label}</td><td class="number">${percent(f.last?.[key],key.includes('annualized')?2:4)}</td><td class="number">${percent(f.next?.[key],key.includes('annualized')?2:4)}</td></tr>`).join('')}</tbody></table></div><div class="source-line"><span>Next timestamp / ${f.next_timestamp?e(timestamp(n(f.next_timestamp)*1000)):'Unavailable'}</span>${sourceLink('https://docs.variational.io/omni/trading/funding-rates','Definitions')}</div>`,'funding');}
const groupColors = [BLUE,TEAL,'#b78ae8','#e4b96a','#9ab2df','#e68c9f','#7392ad'];
let protocolCache;
function currentProtocolMetrics() {
  if (!latest() || !DATA.classifications) return null;
  if (protocolCache?.snapshot !== latest() || protocolCache?.source !== DATA.classifications) {
    let value = null;
    try { value = buildProtocolMetrics(latest(),DATA.classifications); }
    catch { DATA.errors.classifications = 'The market catalogue could not be verified.'; }
    protocolCache = {snapshot:latest(),source:DATA.classifications,value};
  }
  return protocolCache.value;
}
function protocolHistoryRows(value) {
  return rangeRows((DATA.protocolHistory?.rows || []).map(row => ({
    date:row.observedAt,label:timestamp(row.observedAt),values:value(row),source:API_DOCS
  })));
}
function groupHistoryChart({id,title,field,groups,bucket,test=()=>true,type='stacked',unit='USDC'}) {
  const series = groups.map(([key,label],index)=>({key,label,color:groupColors[index%groupColors.length]}));
  const rows = protocolHistoryRows(row=>Object.fromEntries(groups.map(([key])=>
    [key,test(row) ? bucket(row,key)?.[field] ?? null : null])));
  return chartPanel({id,title,rows,series,type,unit,method:'classified-markets',source:'data/protocol-metrics-history.json',sourceLabel:'Data'});
}
function volumeDistribution() {
  const observations = latestMarkets();
  const denominator = sum(observations.map(m=>m.volume_24h_usdc));
  const top = [...observations].sort((a,b)=>n(b.volume_24h_usdc)-n(a.volume_24h_usdc)).slice(0,8);
  const rows = top.map(m=>`<tr><th scope="row"><button class="market-button" type="button" data-market="${e(marketKey(m))}">
    ${e(m.ticker)}</button></th><td class="number">${compact(m.volume_24h_usdc)}</td>
    <td class="number">${percent(ratio(m.volume_24h_usdc,denominator),1)}</td></tr>`).join('');
  const other = top.length && denominator>0 ? Math.max(0,denominator-sum(top.map(m=>m.volume_24h_usdc))) : null;
  const remainder = other===null ? '' : `<tr><th scope="row">Other</th><td class="number">${compact(other)}</td>
    <td class="number">${percent(ratio(other,denominator),1)}</td></tr>`;
  return panel('24-Hour Volume by Market',timestamp(latest()?.observed_at),rows ?
    `<div class="table-wrap"><table class="data-table"><thead><tr><th>Market</th><th class="number">Volume (USDC)</th>
    <th class="number">Share</th></tr></thead><tbody>${rows}${remainder}</tbody></table></div>` :
    '<div class="chart-empty">No market data</div>','market-volume-distribution');
}
function protocolDepthPanels() {
  if (!DATA.protocolHistory) return '';
  const groups = [['tradfi','TradFi'],['crypto','Crypto'],['unclassified','Unclassified']];
  const bucket = (row,key)=>row.totals[key];
  const volume = groupHistoryChart({id:'protocol-volume-groups',title:'24-Hour Volume by Market Group',field:'volume24hUsdc',groups,bucket});
  const interest = groupHistoryChart({id:'protocol-oi-groups',title:'Open Interest by Market Group',field:'traderOpenInterestUsdc',
    groups:[...groups,['olp','OLP']],bucket:(row,key)=>key==='olp' ? (row.coverage.listingCoverageComplete ? row.totals.all : null) : bucket(row,key)});
  const skew = chartPanel({id:'protocol-skew',title:'Long/Short Ratio',
    rows:protocolHistoryRows(row=>({value:row.coverage.listingCoverageComplete ? row.totals.all.longShortRatio : null})),
    series:[{key:'value',label:'Long/short ratio',color:TEAL}],unit:'x',method:'classified-markets'});
  const quotes = chartPanel({id:'majors-half-spread',title:'Quoted Half-Spread at $100K',
    rows:protocolHistoryRows(row=>row.halfSpreads),series:['BTC','ETH','SOL','HYPE'].map((ticker,index)=>({key:ticker+'-PERP',label:ticker,color:groupColors[index]})),
    unit:'bps',method:'chart-half-spread'});
  const tvl = chartPanel({id:'omni-tvl',title:'Omni Total Value Locked',rows:historyRows('tvl_usdc'),
    series:[{key:'value',label:'TVL',color:BLUE}],unit:'USDC',method:'tvl',source:'data/market-history.json',sourceLabel:'Data'});
  return `<div class="grid two section-space">${volume}${interest}${skew}${quotes}${tvl}${volumeDistribution()}</div>`;
}
function tradfiView() {
  const current = currentProtocolMetrics();
  const top = heading('TradFi')+pageRangeToolbar();
  if (!current) return top+dataUnavailable('TradFi classification','classifications');
  const {totals,coverage} = current;
  const scope = `${coverage.classifiedMarkets} of ${coverage.observedMarkets} markets classified. ${timestamp(current.observedAt)}.`;
  const stats = `<div class="stats">
    ${stat('Verified TradFi Volume',compact(totals.tradfi.volume24hUsdc),scope,'classified-markets')}
    ${stat('Verified TradFi Open Interest',compact(totals.tradfi.traderOpenInterestUsdc),scope,'classified-markets')}
    ${stat('Verified TradFi Markets',fixed(totals.tradfi.marketCount,0),scope,'classified-markets')}
    ${stat('Classification Coverage',percent(ratio(coverage.classifiedMarkets,coverage.observedMarkets),1),scope,'classified-markets')}</div>`;
  const instruments = [['perpetual','Perpetuals'],['swap','Swaps']];
  const classes = [['equity','Equities'],['etf','ETFs'],['commodity','Commodities'],['index','Indices'],['currency','Currencies'],['pre_ipo','Pre-IPO']];
  const instrument = (row,key)=>row.byInstrumentType.find(group=>group.id===key);
  const assetClass = (row,key)=>row.byAssetClass.find(group=>group.id===key);
  const verifiedShare = (row,field) => {
    const claimed = row.coverage.listingCoverageComplete ? ratio(row.totals.tradfi[field],row.totals.all[field]) : null;
    return claimed===null ? null : claimed*100;
  };
  const charts = [groupHistoryChart({id:'tradfi-volume-type',title:'TradFi Volume by Instrument',field:'volume24hUsdc',groups:instruments,bucket:instrument}),
    groupHistoryChart({id:'tradfi-oi-type',title:'Open Interest by Instrument',field:'traderOpenInterestUsdc',groups:instruments,bucket:instrument}),
    groupHistoryChart({id:'tradfi-volume-class',title:'24-Hour Volume by Asset Class',field:'volume24hUsdc',groups:classes,bucket:assetClass}),
    groupHistoryChart({id:'tradfi-oi-class',title:'Open Interest by Asset Class',field:'traderOpenInterestUsdc',groups:classes,bucket:assetClass}),
    chartPanel({id:'tradfi-share',title:'TradFi Share of Omni (Lower Bound)',
      rows:protocolHistoryRows(row=>({volume:verifiedShare(row,'volume24hUsdc'),oi:verifiedShare(row,'traderOpenInterestUsdc')})),
      subtitle:'Verified TradFi listings divided by all returned listings. A partial class catalogue yields a lower bound.' ,
      series:[{key:'volume',label:'24-Hour Volume',color:BLUE},{key:'oi',label:'Trader Open Interest',color:TEAL}],unit:'%',method:'classified-markets'})];
  const leaders = current.byMarket.filter(item=>item.scope==='tradfi').sort((a,b)=>n(b.volume24hUsdc)-n(a.volume24hUsdc)).slice(0,10);
  const leaderRows=leaders.map(item=>`<tr><td>${e(item.ticker)}</td><td class="number">${compact(item.volume24hUsdc)}</td>
    <td class="number">${percent(ratio(item.volume24hUsdc,totals.tradfi.volume24hUsdc),1)}</td></tr>`).join('');
  charts.push(panel('Top TradFi Markets by 24-Hour Volume',scope,`<div class="table-wrap"><table class="data-table"><thead><tr><th>Market</th>
    <th class="number">24-Hour Volume</th><th class="number">Share of Verified Volume</th></tr></thead><tbody>${leaderRows}</tbody></table></div>`,'classified-markets'));
  return top+stats+`<div class="grid two">${charts.join('')}</div>`+marketListPanel('TradFi Listings');
}

const executionSizes = [['size_1k',1000,'$1K'],['size_100k',100000,'$100K'],['size_1m',1000000,'$1M']];
const executionBenchmarks = ['US100-SWAP','US30-SWAP','US500-SWAP','XAU-SWAP','BTC-PERP','ETH-PERP','SOL-PERP','HYPE-PERP'];
function median(values) {
  const ordered = values.map(n).filter(value => value !== null).sort((a,b) => a-b);
  const middle = Math.floor(ordered.length/2);
  return !ordered.length ? null : ordered.length%2 ? ordered[middle] : (ordered[middle-1]+ordered[middle])/2;
}
function spreadLabel(value) {
  const spread = n(value);
  return spread===null ? '—' : fixed(spread,2);
}
function quoteBreadth(observation) {
  const marketList = observation.data?.markets || [];
  if (!marketList.length) return panel('Omni Quote Coverage','','<div class="chart-empty">No quotes in this period</div>','quote-breadth');
  const rows = [['perpetual','Perpetuals'],['swap','Swaps']].flatMap(([contract,label]) => {
    const markets = marketList.filter(market => market.contract_type===contract);
    return executionSizes.map(([key,,size]) => {
      const quotes = markets.map(market=>market.quotes?.[key]?.full_spread_bps).filter(value=>n(value)!==null);
      return `<tr><td>${label}</td><td>${size}</td><td class="number">${quotes.length}</td>
        <td class="number">${markets.length}</td><td class="number">${spreadLabel(median(quotes))}</td></tr>`;
    });
  }).join('');
  return panel('Omni Quote Coverage',timestamp(observation.snapshot?.observed_at),
    `<div class="table-wrap"><table class="data-table"><thead><tr><th>Contract</th><th>Size</th>
      <th class="number">Quoted</th><th class="number">Listed</th><th class="number">Median spread (bps)</th></tr></thead><tbody>
      ${rows}</tbody></table></div>`,'quote-breadth');
}
function quoteSizePanel(observation) {
  const marketList = observation.data?.markets || [];
  const selected = executionBenchmarks.map(ticker => marketList.find(market=>market.ticker===ticker)).filter(Boolean);
  const values = selected.flatMap(market => executionSizes.map(([key])=>n(market.quotes?.[key]?.full_spread_bps))).filter(value=>value!==null);
  const max = Math.max(1,...values);
  const rows = selected.map(market => {
    const allMissing = executionSizes.every(([key])=>n(market.quotes?.[key]?.full_spread_bps)===null);
    const availability = allMissing ? '<td colspan="3" class="muted">No quote in this observation</td>' : executionSizes.map(([key])=>{
      const value = n(market.quotes?.[key]?.full_spread_bps);
      return `<td class="number spread-cell">${value===null ? '' : '<i aria-hidden="true" style="width:'+Math.max(0,Math.min(100,value/max*100)).toFixed(4)+'%"></i>'}
        <span>${value===null ? '<span aria-label="Quote unavailable">—</span>' : spreadLabel(value)}</span></td>`;
    }).join('');
    return `<tr><th scope="row"><button type="button" class="market-button" data-market="${e(marketKey(market))}">${e(market.ticker)}</button></th>
    ${availability}</tr>`;
  }).join('');
  return panel('Full Bid-Ask Spread by Order Size',timestamp(observation.snapshot?.observed_at),
    rows ? `<div class="table-wrap"><table class="data-table spread-table"><thead><tr><th>Market</th>
      ${executionSizes.map(([,,label])=>'<th class="number">'+label+' (bps)</th>').join('')}</tr></thead><tbody>${rows}</tbody></table></div>`
      : '<div class="chart-empty">No quotes in this period</div>','quote-breadth');
}
function executionMatches() {
  const rows = new Map();
  const add = row => rows.set([row.market,row.notional_usd,Date.parse(row.quote_time),Math.floor(Date.parse(row.peer_book_time)/1000)].join('|'),row);
  const live = DATA.supplement?.execution;
  for (const row of live?.rows || []) add({...row,notional_usd:live.notional_usd,observed_at:live.observed_at});
  for (const row of DATA.executionStudy?.rows || []) add(row);
  return [...rows.values()].filter(row=>rangeRows([{date:row.observed_at}]).length).sort((a,b)=>a.observed_at.localeCompare(b.observed_at));
}
function archivedExecutionPanel() {
  const sizes = executionSizes.map(([,size])=>size);
  const requested = Number(route.params.get('size'));
  const size = sizes.includes(requested) ? requested : 100000;
  const matches = executionMatches().filter(row=>row.notional_usd===size);
  const coins = executionBenchmarks.slice(4).map(ticker=>ticker.split('-')[0]);
  const summaries = coins.map(coin => {
    const subset = matches.filter(match=>match.market===coin);
    return {coin,count:subset.length,omni:median(subset.map(row=>row.omni_bps)),
      book:median(subset.map(row=>row.peer_book_bps)),fees:median(subset.map(row=>row.peer_fees_bps)),
      total:median(subset.map(row=>row.peer_total_bps)),
      edge:median(subset.map(row=>n(row.peer_total_bps)-n(row.omni_bps)))};
  });
  const sizeLinks = executionSizes.map(([,value,label])=>`<a class="button compact${value===size?' selected-size':''}"
    href="#/execution?size=${value}${route.params.has('quote-date') ? '&amp;quote-date='+encodeURIComponent(route.params.get('quote-date')) : ''}"${value===size?' aria-current="true"':''}>${label}</a>`).join('');
  const rows = summaries.map(row=>`<tr><th scope="row">${row.coin}</th>
    ${[row.omni,row.book,row.fees,row.total,row.edge].map(value=>'<td class="number">'+spreadLabel(value)+'</td>').join('')}
    <td class="number">${row.count}</td></tr>`).join('');
  const details = matches.map(row=>`<tr><td>${e(timestamp(row.observed_at))}</td><td>${e(row.market)}</td>
    <td class="number">${spreadLabel(row.omni_bps)}</td><td class="number">${spreadLabel(row.peer_total_bps)}</td></tr>`).join('');
  return panel('November 11 Hyperliquid Study','Indicative costs in basis points.',
    `<div class="chart-toolbar" aria-label="Order size">${sizeLinks}</div>`+
    (matches.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>Market</th><th class="number">Omni</th>
      <th class="number">HL book</th><th class="number">HL fees</th><th class="number">HL total</th>
      <th class="number">Omni savings</th><th class="number">Samples</th></tr></thead><tbody>${rows}</tbody></table></div>
      <details class="table-disclosure"><summary>Matched quotes</summary><div class="table-wrap"><table class="data-table">
      <thead><tr><th>Time (UTC)</th><th>Market</th><th class="number">Omni</th><th class="number">HL total</th></tr></thead>
      <tbody>${details}</tbody></table></div></details>` : '<div class="chart-empty">No matched quotes in this period</div>'),'execution');
}
const venueOrder = [['hyperliquid','Hyperliquid'],['lighter','Lighter'],['binance','Binance'],['okx','OKX'],['bybit','Bybit']];
const missingVenue = {
  permission_required:'Publication permission required',not_sampled:'No sample collected',stale_source:'Source times did not match', source_unavailable:'Public book unavailable', market_unavailable:'Market unavailable',
  no_omni_quote:'Omni quote unavailable', invalid_data:'Public book could not be verified', insufficient_depth:'Insufficient published depth'
};
function venueRoute(coin,size) {
  const params = new URLSearchParams(route.params);
  params.delete('view');
  params.set('coin',coin);
  params.set('size',String(size));
  return '#/execution?'+params.toString();
}
function multiVenuePanel() {
  const coins = ['BTC','ETH','SOL','HYPE'];
  const sizes = executionSizes.map(([,size])=>size);
  const choice = route.params.get('coin'), requested = Number(route.params.get('size'));
  const coin = coins.includes(choice) ? choice : 'BTC';
  const size = sizes.includes(requested) ? requested : 100000;
  const archive = DATA.multiVenue;
  const matches = (archive?.rows || []).filter(row=>row.market===coin && row.notional_usd===size);
  const visible = rangeRows(matches.map(row=>({...row,date:row.observed_at})));
  const tabs = (choices,option,label,path)=>`<div class="segmented" aria-label="${e(label)}">${choices.map(([value,name])=>
    `<a href="${e(path(value))}"${option===value?' aria-current="true"':''}>${e(name)}</a>`).join('')}</div>`;
  const controls = '<div class="chart-toolbar">'+tabs(coins.map(value=>[value,value]),coin,'Market',value=>venueRoute(value,size))+
    tabs(executionSizes.map(([,amount,label])=>[amount,label]),size,'Order size',value=>venueRoute(coin,value))+'</div>';
  const rows = venueOrder.map(([key,name])=>{
    const subset = visible.filter(row=>row.venue===key);
    const currency = archive?.venues?.[key]?.currency || (key==='hyperliquid'||key==='lighter'?'USDC':'USDT');
    const label = `<tr><th scope="row">${name}</th><td>${currency}</td>`;
    if (!subset.length) {
      const status = archive?.coverage?.find(row=>row.venue===key && row.market===coin)?.status;
      const detail = matches.some(row=>row.venue===key) ? 'No match in this period' :
        status==='ok' ? 'No matched depth at this size' : missingVenue[status] || 'No matched sample';
      return label+`<td colspan="5" class="muted">${e(detail)}</td><td class="number">0</td><td>—</td></tr>`;
    }
    const fields = ['omni_bps','book_bps','fee_bps','total_bps'];
    const values = fields.map(field=>median(subset.map(row=>row[field])));
    const edge = median(subset.map(row=>row.total_bps-row.omni_bps));
    return label + [...values,edge].map(value=>`<td class="number">${spreadLabel(value)}</td>`).join('')+`<td class="number">${subset.length}</td><td>${e(shortDate(subset.at(-1).observed_at))}</td></tr>`;
  }).join('');
  const table = `<div class="table-wrap"><table class="data-table"><thead><tr><th>Venue</th><th>Quote</th>
    <th class="number">Omni (bps)</th><th class="number">Depth (bps)</th><th class="number">Fees (bps)</th>
    <th class="number">Total (bps)</th><th class="number">Vs. Omni (bps)</th><th class="number">Pairs</th><th>Last match (UTC)</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
  return panel('Perpetual Trading Costs by Venue','Median matched round trips. A positive difference means the peer costs more.',
    controls+table+(archive?.rows.length ? '<div class="source-line"><a class="inline-link" href="data/multi-venue.json" download>Sample data</a></div>' : ''),
    'multi-venue');
}
function executionView() {
  const observation = executionObservation();
  const options = [...observation.eligible].reverse().map(entry=>
    `<option value="${e(entry.date)}"${observation.requested===entry.date?' selected':''}>${e(date(entry.observed_at))}</option>`).join('');
  const actions = options ? `<div class="field-select"><select data-execution-date aria-label="Omni quote date">
    <option value="">Latest with swap quotes</option>${options}</select></div>` : '';
  const pending = observation.snapshot && !observation.data && !historyFailed.has(observation.snapshot.date);
  const detail = pending ? '<p class="data-state" role="status">Loading quotes…</p>' :
    observation.prior ? '<p class="data-state">Last saved swap quotes: '+e(timestamp(observation.snapshot.observed_at))+'</p>' :
    observation.requested && !observation.snapshot ? '<p class="data-state">Selected quote date is outside this period.</p>' :
    observation.snapshot && !observation.data ? '<p class="data-state" role="status">Quote observation could not load.</p>' : '';
  return heading('Execution','','',actions)+pageRangeToolbar()+detail+
    `<div id="execution-observations" class="grid two">${quoteSizePanel(observation)}${quoteBreadth(observation)}</div>
    <div class="section-space">${multiVenuePanel()}</div>
    <details class="panel section-space table-disclosure"><summary>Archived Hyperliquid study</summary>${archivedExecutionPanel()}</details>`;
}

function resultsSelection(periodReports = selectedReports()) {
  const id = route.params.get('report');
  const selected = id ? periodReports.find(report => report.id === id) : null;
  const unavailable = Boolean(id && !selected);
  return {selected,unavailable,records:unavailable ? [] : selected ? [selected] : periodReports};
}
function resultPeriodPicker(periodReports, selection) {
  const updates = [...periodReports].reverse().map(report =>
    `<option value="${e(report.id)}"${report === selection.selected ? ' selected' : ''}>${e(date(report.published_at))}</option>`).join('');
  return `<label>Breakdown <span class="field-select"><select data-results-report aria-label="Breakdown period"${updates ? '' : ' disabled'}>
    <option value="">Period total</option>${selection.unavailable ? '<option disabled selected>Update unavailable</option>' : ''}${updates ? '<optgroup label="Published updates">'+updates+'</optgroup>' : ''}</select></span></label>`;
}
function financials() {
  const treasury = route.params.get('view') === 'treasury';
  const title = treasury ? 'Protocol Treasury' : 'Operating Results';
  if (!DATA.financials || (treasury && !latestReport())) {
    return heading(title,'','')+pageRangeToolbar()+dataUnavailable(title,'financials');
  }
  const periodReports = selectedReports();
  const selection = resultsSelection(periodReports);
  const exportButton = '<button type="button" class="button" data-export-financials>Export period table</button>';
  const tabs = `<div class="subnav"><a href="#/financials" class="${treasury ? '' : 'active'}">Operating Results</a>
    <a href="#/financials?view=treasury" class="${treasury ? 'active' : ''}">Protocol Treasury</a></div>`;
  const controls = treasury ? tabs : `<div class="results-controls">${tabs}${resultPeriodPicker(periodReports,selection)}</div>`;
  return heading(title,'','',exportButton)+pageRangeToolbar()+controls+
    (treasury ? treasuryView() : reportsView(periodReports,selection));
}
function resultPanels(selection) {
  const {selected, records} = selection;
  if (selection.unavailable) {
    const message = '<div class="empty-state">The linked update is unavailable in this period.</div>';
    return panel('Gross Revenue to Net Profit','USD',message,'waterfall');
  }
  if (!records.length) {
    const last = latestReport();
    const contextual = last ? ` <a class="inline-link" href="#/financials?report=${e(encodeURIComponent(last.id))}&amp;range=all" data-fixed-range>View ${e(date(last.published_at))} update</a>` : '';
    return panel('Gross Revenue to Net Profit','',`<div class="chart-empty">No updates in this period.${contextual}</div>`,'waterfall');
  }
  const scope = selected ? date(selected.published_at)+' update' : 'Period total';
  const subtitle = scope+' / USD';
  let model;
  try {
    model = buildOperatingResults(records);
  } catch (error) {
    if (!(error instanceof OperatingResultsError)) throw error;
    const message = '<div class="empty-state">Source fields could not be verified for this selection. See Results by Period for the published figures.</div>';
    return panel('Gross Revenue to Net Profit',subtitle,message,'waterfall');
  }
  const cards = model.rows.filter(row => row.key !== 'market_making_costs').map(row => {
    const detail = row.coverage.status === 'partial'
      ? `${row.coverage.reportedCount} of ${model.reportCount} updates` : scope;
    return stat(row.key === 'gross_revenue' ? 'Gross Revenue' : row.label,cash(row.value),e(detail),'waterfall');
  }).join('');
  const source = selected ? sourceLink(officialUpdate(selected),'View official update')
    : sourceLink('data/financials.json','Source data');
  const action = model.reportCount ? '<button type="button" class="text-button" data-export-waterfall>Export waterfall CSV</button>' : '';
  const body = waterfallBody(model)+`<div class="source-line">${action}${source}</div>`;
  return `<div class="stats">${cards}</div>`+panel('Gross Revenue to Net Profit',subtitle,body,'waterfall');
}
function resultsByPeriod(periodReports) {
  const warning = periodReports.reduce((count,report) => count+(report.warnings?.length||0),0);
  const keys = Object.keys(financialLabels);
  const labels = ['Gross Revenue','Market Making Costs','Net Revenue','Rewards','Net Profit','Treasury Inflow','OLP PnL'];
  const rows = [...periodReports].reverse().map(report => {
    const badge = report.warnings?.length ? ' <span class="badge warning" aria-label="Source exception">!</span>' : '';
    const amounts = keys.map(key => `<td class="number">${e(exactReportAmount(report.period_amounts[key]))}</td>`).join('');
    return `<tr><td>${e(date(report.published_at))}${badge}</td>${amounts}<td><button type="button" class="text-button"
      data-report="${e(report.id)}">Details</button></td></tr>`;
  }).join('');
  return `<section class="panel section-space"><div class="panel-head"><div><h2>Results by Period
    ${warning ? `<button class="badge warning" type="button" data-method="period-exceptions">${warning} source exceptions</button>` : ''}</h2>
    </div>${infoButton('financials','Results by Period methodology')}</div>
    <div class="table-wrap"><table class="data-table"><thead><tr><th>Published</th>
    ${labels.map(label => '<th class="number">'+e(label)+'</th>').join('')}<th>Update</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="9" class="wrap">No reports published in the selected period.</td></tr>'}</tbody></table></div></section>`;
}
const wholeDollars = new Intl.NumberFormat('en-US');
function exactReportAmount(value) {
  if (typeof value !== 'string' || !/^-?\d+(?:\.\d+)?$/.test(value)) return '—';
  const [integer, fraction] = value.split('.');
  const grouped = wholeDollars.format(BigInt(integer));
  const signed = integer === '-0' ? '−0' : grouped.replace('-', '−');
  return fraction ? signed+'.'+fraction : signed;
}
function reportsView(periodReports,selection) {
  const ratioRows = periodReports.map(report => {
    const value = ratio(report.period_amounts.net_profit,report.period_amounts.gross_revenue);
    return {date:report.published_at,label:date(report.published_at)+' UTC',values:{ratio:value===null ? null : value*100},
      warnings:warningsForMetrics(report,['net_profit','gross_revenue']),source:officialUpdate(report)};
  });
  const allocationHistory = chartPanel({id:'allocation-history',title:'Net Profit Allocation by Period',subtitle:'Reported / USD',
    rows:financialRows(['treasury_inflow','olp_pnl']),type:'stacked',method:'allocation',
    series:[{key:'treasury_inflow',label:'Protocol Treasury Inflow',color:BLUE},{key:'olp_pnl',label:'OLP PnL',color:TEAL}]});
  const marginHistory = chartPanel({id:'profit-ratio',title:'Net Profit / Gross Revenue',subtitle:'Reported amounts / %',
    rows:ratioRows,series:[{key:'ratio',label:'Net Profit / Gross Revenue',color:BLUE}],unit:'%',method:'profit-ratio'});
  return resultPanels(selection)+`<div class="section-space">${primaryFinancialChart('financial-performance')}</div>
    <div class="grid two section-space">${allocationHistory}${marginHistory}</div>`+resultsByPeriod(periodReports);
}
function treasuryView(){const r=latestReport(),chain=DATA.supplement?.chain,account=chain?.accounts?.find(a=>a.id==='treasury');return `<div class="stats report-linked-stats">${stat('Reported Treasury Holdings',compact(r.snapshot.treasury_holdings),'<span class="unit">USDC</span> / '+e(date(r.published_at)),'treasury')}${stat('Observed Onchain Balance',compact(account?.balance_usdc),'<span class="unit">USDC</span> / '+e(chain?.observed_at?shortDate(chain.observed_at):'Unavailable'),'chain')}${selectedReports().length ? stat('Selected-period Treasury Inflow', cash(selectedFinancialTotals().treasury_inflow), selectedFlowDetail(), 'selected-flow') : ''}${stat('Latest Reported Inflow',cash(r.period_amounts.treasury_inflow),'2w / '+e(date(r.published_at)),'treasury')}</div><div class="grid two">${chartPanel({id:'treasury-holdings',title:'Reported Protocol Treasury Holdings',subtitle:'USDC / reported snapshots',rows:financialRows(['treasury_holdings'],{snapshot:true}),series:[{key:'treasury_holdings',label:'Treasury Holdings',color:BLUE}],type:'line',unit:'USDC',method:'treasury',note:'Missing observations remain gaps'})}${chartPanel({id:'treasury-inflow',title:'Cumulative Protocol Treasury Inflow',subtitle:'Reported / $ / sum of covered reports',rows:financialRows(['treasury_inflow'],{mode:'cumulative'}),series:[{key:'treasury_inflow',label:'Protocol Treasury Inflow',color:BLUE}],type:'area',method:'treasury',note:'Cumulative from archive start; displayed period is cropped'})}</div>${chainPanel('treasury')}`;}
function chainPanel(id) {
  const history = DATA.chainHistory?.points || [];
  const chain = DATA.supplement?.chain;
  const account = chain?.accounts?.find(item => item.id === id);
  const title = id === 'treasury' ? 'Protocol Treasury USDC Balance' : 'Core OLP Vault USDC Balance';
  const rows = history.map(point => ({
    date:point.block_timestamp, detail:'Block '+point.block_number,
    values:{value:point[id+'_usdc']}, source:point.source_url
  }));
  let output = '';
  if (rows.length) {
    output = chartPanel({id:id+'-chain-history',title,subtitle:'Arbitrum One / native USDC',
      rows:rangeRows(rows), series:[{key:'value',label:'Wallet balance',color:TEAL}],
      type:'line', maxGapDays:8, unit:'USDC', method:'chain-history', source:'data/chain-history.json',sourceLabel:'Blocks'});
  }
  if (id === 'treasury' && rows.length > 1) {
    const changes = rows.slice(1).map((row,index) => {
      const previous = rows[index];
      const before = n(previous.values.value), after = n(row.values.value);
      const change = before===null || after===null ? null : (Math.round(after*1e6)-Math.round(before*1e6))/1e6;
      return {...row,startDate:previous.date,values:{value:change}};
    });
    output += chartPanel({id:'treasury-balance-changes',title:'Treasury Balance Changes',subtitle:'Native USDC changes between dated Arbitrum blocks.',
      rows:rangeRows(changes),series:[{key:'value',label:'Net balance change',color:BLUE}],
      type:'bar',interval:true,unit:'USDC',method:'treasury-balance-changes',source:'data/chain-history.json',sourceLabel:'Blocks'});
  } else if (account) {
    const table = `<div class="table-wrap"><table class="data-table"><thead><tr>
      <th>Account</th><th>Address</th><th class="number">Balance, USDC</th></tr></thead><tbody>
      <tr><td>${e(account.name)}</td><td>${sourceLink(account.source_url,account.address.slice(0,8)+'…'+account.address.slice(-6))}</td>
      <td class="number">${fixed(account.balance_usdc,2)}</td></tr></tbody></table></div>`;
    output += panel('Current Arbitrum Balance','Block '+fixed(chain.block_number,0)+' / '+timestamp(chain.observed_at),table,'chain');
  }
  return output ? `<div class="grid two section-space">${output}</div>` : '';
}

function rewardsView() {
  const top = heading('Rewards')+pageRangeToolbar();
  const all = DATA.rewards?.reports || [];
  if (!all.length) return top+dataUnavailable('Reported rewards','rewards');
  const rows = filterRange(all,selectedRange(),rangeAnchor(),report=>report.published_at);
  const fields = [
    ['total_rewards_claimed','Total Rewards Claimed',BLUE],
    ['total_referral_rewards_claimed','Total Referral Rewards Claimed',TEAL],
    ['total_losses_refunded','Total Losses Refunded',groupColors[3]]
  ];
  const latestReward = all.at(-1);
  const display = value=>value ? value.raw : '—';
  const stats = fields.map(([key,label])=>stat(label,display(latestReward.fields[key]),timestamp(latestReward.published_at),'reported-rewards')).join('');
  const makeSeries = (id,title,fields,type='line') => chartPanel({id,title,rows:rows.map(report=>({
    date:report.published_at,source:report.source_url||report.source_urls?.[0],
    detail:fields.some(([key])=>report.fields[key]?.rounded) ? 'Source reports rounded values' : '',
    values:Object.fromEntries(fields.map(([key])=>[key,report.fields[key]?.value ?? null]))
  })),series:fields.map(([key,label,color])=>({key,label,color})),unit:'$',type,method:'reported-rewards',
  source:'data/reported-rewards.json',sourceLabel:'Data'});
  const referral = makeSeries('reported-referral-cost','Referral Rewards by Period',[['referral_rewards','Referral Rewards',TEAL]],'bar');
  return top+`<div class="stats rewards-stats">${stats}</div><div class="grid two">
    ${makeSeries('reported-rewards','Reported Rewards History',fields)}${referral}</div>`;
}
function olp(){if(!latestReport())return heading('Omni Liquidity Provider')+pageRangeToolbar()+dataUnavailable('OLP reports','financials');const r=latestReport(),account=DATA.supplement?.chain?.accounts?.find(a=>a.id==='olp');const lifetime=state.olpMode==='lifetime';const rows=lifetime?financialRows(['lifetime_olp_pnl'],{snapshot:true}):financialRows(['olp_pnl'],{mode:state.olpMode==='cumulative'?'cumulative':'period'});const key=lifetime?'lifetime_olp_pnl':'olp_pnl';const toolbar=`<div class="segmented" aria-label="OLP PnL series"><button type="button" data-olp-mode="lifetime" aria-pressed="${lifetime}">Reported lifetime</button><button type="button" data-olp-mode="period" aria-pressed="${state.olpMode==='period'}">Per report</button><button type="button" data-olp-mode="cumulative" aria-pressed="${state.olpMode==='cumulative'}">Covered cumulative</button></div>`;return heading('Omni Liquidity Provider','','OLP PERFORMANCE')+pageRangeToolbar()+`<div class="stats report-linked-stats">${stat('Lifetime OLP PnL',cash(r.snapshot.lifetime_olp_pnl),'Reported / '+e(date(r.published_at)),'olp')}${selectedReports().length ? stat('Selected-period OLP PnL', cash(selectedFinancialTotals().olp_pnl), selectedFlowDetail(), 'selected-flow') : ''}${stat('Latest 2W OLP PnL',cash(r.period_amounts.olp_pnl),'Reported / '+e(date(r.published_at)),'olp')}${stat('Observed OLP Balance',compact(account?.balance_usdc),'<span class="unit">USDC</span> / onchain cash','chain')}</div>${chartPanel({id:'olp-performance',title:lifetime?'Reported Lifetime OLP PnL':state.olpMode==='period'?'OLP PnL by Report':'Cumulative OLP PnL / Covered Reports',subtitle:'Reported / $ / publication dates in UTC',rows,series:[{key,label:lifetime?'Lifetime OLP PnL':'OLP PnL',color:BLUE}],type:state.olpMode==='period'?'bar':'area',method:'olp',toolbar,note:lifetime?'Source lifetime values / not a sum of this archive':state.olpMode==='cumulative'?'Cumulative from archive start; displayed period is cropped':'Native report amounts'})}${chainPanel('olp')}<div class="section-space">${panel('OLP Data Coverage','Source boundaries',`<div class="table-wrap"><table class="data-table status-table"><tbody><tr><td>Reported period and lifetime PnL</td><td>${reports().length} reports</td></tr><tr><td>Observed onchain USDC balance</td><td>${account?'Available':'Unavailable'}</td></tr><tr><td>Consolidated NAV and external hedges</td><td>Not publicly established</td></tr><tr><td>Return / APY</td><td>Insufficient accounting data</td></tr></tbody></table></div>`,'olp')}</div>`;}
function token(){const token=DATA.supplement?.token||{},verified=token.status==='verified';const allocations=token.allocations||[];return heading('VAR',token.checked_at?'Source checked '+timestamp(token.checked_at):'Source verification pending','TOKEN DATA',sourceLink(token.source_url,'CoinGecko'))+`<div class="stats">${stat('Token Price',verified?cash(token.price_usd):'Unavailable',verified?'USD':'Market identity not verified','var')}${stat('Market Capitalization',verified?cash(token.market_cap_usd):'Unavailable',verified?'USD':'Preview listing','var')}${stat('Circulating Supply',verified?compact(token.circulating_supply):'Unverified','VAR','var')}${stat('Token Contract',token.contract_address?'Verified':'Unverified',token.contract_address?e(token.contract_address.slice(0,10)+'…'):'Official identity required','var')}</div><div class="grid two">${allocations.length?panel('Planned Token Allocation','Announced allocation / not circulating supply',`<div class="panel-body"><div class="allocation-bar" style="height:18px" aria-label="Planned token allocation">${allocations.map((a,i)=>`<span style="width:${Math.max(0,Math.min(100,n(a.percent)||0))}%;background:${[BLUE,TEAL,GREY][i%3]}"></span>`).join('')}</div><div class="allocation-legend">${allocations.map((a,i)=>`<div class="allocation-row"><span><i style="background:${[BLUE,TEAL,GREY][i%3]}"></i>${e(a.label)}</span><strong class="number">${fixed(a.percent,0)}%</strong></div>`).join('')}</div></div>${sourceFooter(token.policy_url,'Official allocation',token.policy_as_of?'Source as of '+date(token.policy_as_of):'Documented policy')}`,'var'):panel('Token Allocation','Official documentation',`<div class="empty-state">Allocation data is not available in this release.</div>`,'var')}${panel('Token Data Availability','Activation follows source verification',`<div class="table-wrap"><table class="data-table status-table"><tbody><tr><td>CoinGecko listing</td><td>${e(token.status==='preview'?'Preview':'Identity review')}</td></tr><tr><td>Canonical contract</td><td>${token.contract_address?'Verified':'Unverified'}</td></tr><tr><td>Vesting / unlock execution</td><td>Unverified</td></tr><tr><td>Buyback purchases</td><td>No verified series</td></tr><tr><td>Token burns</td><td>No verified series</td></tr></tbody></table></div>`,'var')}</div>`;}
function pro(){const pro=DATA.supplement?.pro||{};return heading('Variational Pro',pro.checked_at?'Source checked '+timestamp(pro.checked_at):'Public documentation','INSTITUTIONAL OTC',sourceLink(pro.source_url||'https://docs.variational.io/pro/about-pro','Official documentation'))+panel('Variational Pro','Institutional OTC with multiple market makers',`<div class="table-wrap"><table class="data-table status-table"><tbody><tr><td>Public release</td><td>${pro.status==='prelaunch'?'Not live per documentation':'Under review'}</td></tr><tr><td>Public market data</td><td>No validated feed</td></tr><tr><td>RFQ and market maker participation</td><td>Authorized aggregates required</td></tr><tr><td>Quote response and acceptance</td><td>Authorized activity data required</td></tr><tr><td>Settlement outcomes</td><td>Authorized settlement data required</td></tr></tbody></table></div>`,'pro');}
function research(){const articles=(DATA.supplement?.research?.articles||[]).filter(a=>['published','scheduled'].includes(a.status));const selected=articles.find(a=>a.id===route.params.get('article'));if(selected)return articleReader(selected);return heading('Research','Articles and analysis by Jae (Ponyo)','RESEARCH LIBRARY')+(articles.length?`<div class="article-list">${[...articles].sort((a,b)=>new Date(b.published_at||b.scheduled_date)-new Date(a.published_at||a.scheduled_date)).map(a=>`<article class="article-row"><div class="article-date">${e(a.status==='published'?date(a.published_at):a.scheduled_date?date(a.scheduled_date):'Date pending')}<br><span class="badge ${a.status==='published'?'blue':''}" style="margin-top:8px">${e(a.status==='published'?'Published':'Scheduled')}</span></div><div><h2><a href="#/research?article=${encodeURIComponent(a.id)}">${e(a.title)}</a></h2><div class="article-meta"><span>${e(a.author||'Jae (Ponyo)')}</span><span>${e(a.language||'English')}</span>${(a.topics||[]).map(t=>`<span>${e(t)}</span>`).join('')}</div><a class="inline-link article-indicator-link" href="#/research?article=${encodeURIComponent(a.id)}&view=indicators">Research indicators</a>${a.status==='published'&&a.summary?`<p class="article-summary">${e(a.summary)}</p>`:''}</div><a class="button" href="#/research?article=${encodeURIComponent(a.id)}">${a.status==='published'?'Read article':'Details'}</a></article>`).join('')}</div>`:panel('Research Library','Published work',`<div class="empty-state"><h2>No published articles yet</h2><p>Published research and its related metrics will appear here.</p></div>`,'research'));}
function articleReader(a){const published=a.status==='published';const body=published&&typeof a.body==='string'?a.body:'';const related=(a.related||[]).filter(r=>typeof r==='string'&&titles[r]);return `<div class="reader"><a class="inline-link small" href="#/research">All research</a><h1>${e(a.title)}</h1><div class="article-meta"><span>${e(a.author||'Jae (Ponyo)')}</span><span>${e(a.language||'English')}</span><span>${published?'Published '+e(date(a.published_at)):'Scheduled / '+e(a.scheduled_date?date(a.scheduled_date):'date pending')+' KST'}</span></div>${published&&a.canonical_url?`<p class="section-space small">${sourceLink(a.canonical_url,'Canonical article')}</p>`:''}${researchIndicators(a)}${originalObservations(a)}<div class="reader-body">${body?body.split(/\n\s*\n/).map(p=>p.startsWith('## ')?`<h2>${e(p.slice(3))}</h2>`:`<p>${e(p).replace(/\n/g,'<br>')}</p>`).join(''):`<div class="empty-state"><p>${published?'Read the full article at its canonical source.':'Article text and public link will be available after publication.'}</p></div>`}</div>${related.length?`<div class="reader-related">${related.map(r=>`<a class="button" href="#/${r}">${e(titles[r])}</a>`).join('')}</div>`:''}</div>`;}

function originalObservations(article){
  if(article.status!=='published'||!Array.isArray(article.original_observations))return '';
  const rows=article.original_observations.filter(row=>row&&['metric','value','period','source_url'].every(key=>typeof row[key]==='string'));
  if(!rows.length)return '';
  return `<details class="original-observations section-space"><summary>Original research observations</summary><div class="table-wrap"><table class="data-table"><thead><tr><th>Metric</th><th>Article value</th><th>Observation period</th><th>Source</th></tr></thead><tbody>${rows.map(row=>`<tr><td class="wrap">${e(row.metric)}</td><td class="wrap">${e(row.value)}</td><td class="wrap">${e(row.period)}</td><td>${sourceLink(row.source_url,'Source')}</td></tr>`).join('')}</tbody></table></div></details>`;
}
function researchCoverageMatches(record,baseline){
  if(baseline?.coverage?.missing_known_versions_count!==0||baseline?.coverage?.first_report_published_at!==DATA.financials?.coverage?.first_report_published_at)return false;
  const ids=record?.coverage?.financial_report_ids;if(!Array.isArray(ids)||!ids.length)return false;
  const cutoff=Date.parse(baseline.source_observed_at);if(!Number.isFinite(cutoff))return false;
  const covered=reports().filter(report=>Date.parse(report.published_at)<=cutoff);
  if(covered.length!==ids.length||covered.some(report=>!ids.includes(report.id)||n(report.period_amounts?.net_profit)===null))return false;
  return Math.abs(covered.reduce((total,report)=>total+n(report.period_amounts.net_profit),0)-n(baseline.value))<0.005;
}
function seoulDay(value=new Date()){if(value===null||value===undefined||value==='')return null;const d=new Date(value);if(!Number.isFinite(d.getTime()))return null;const parts=Object.fromEntries(new Intl.DateTimeFormat('en-US',{year:'numeric',month:'2-digit',day:'2-digit',timeZone:'Asia/Seoul'}).formatToParts(d).map(p=>[p.type,p.value]));return `${parts.year}-${parts.month}-${parts.day}`;}
function indicatorValue(value,unit){if(n(value)===null)return 'Unavailable';return unit==='$'?cash(value):unit==='%'?fixed(value,2)+'%':unit==='count'?fixed(value,0):compact(value)+' '+unit;}
function indicatorChange(value,baseline,unit){if(n(value)===null||n(baseline)===null)return '—';const difference=n(value)-n(baseline),sign=difference>0?'+':difference<0?'−':'';const magnitude=Math.abs(difference);if(unit==='%')return sign+fixed(magnitude,2)+' pp';if(unit==='count')return sign+fixed(magnitude,0);if(unit==='$')return sign+cash(magnitude);return sign+compact(magnitude)+' '+unit;}
function researchIndicators(article){const m=latest()?.totals||{},report=latestReport(),swap=latest()?.by_contract_type?.find(row=>row.contract_type==='swap');const share=completeMarketCoverage()?ratio(swap?.volume_24h_usdc,m.volume_24h_usdc):null;const apiSource=DATA.markets?.source?.url;const reportSource=report?.source_urls?.[0];const apiDate=latest()?.observed_at;const reportDate=report?.published_at;const target=article.status==='published'&&article.published_at?seoulDay(article.published_at):article.scheduled_date;const record=DATA.baselines?.articles?.find(row=>row.article_id===article.id&&row.target_date===target);const absentLabel=DATA.errors.baselines?'Unavailable':!target?'Pending':target>=seoulDay()?'Pending':'Not recorded';const titleContext=record&&record.publication_status_at_capture!=='published'?'Scheduled-date snapshot / publication unconfirmed at capture':target?`Publication-day baseline / ${date(target)} KST${article.status==='scheduled'?' scheduled':''}`:'Publication-day baseline pending';const rows=[
  {id:'num_markets_observed',label:'Omni Market Breadth',value:m.num_markets_observed,unit:'count',observed:apiDate,dateLabel:'API observation',source:apiSource,href:'#/omni',context:marketCoverageLabel()},
  {id:'volume_24h_usdc',label:'24-Hour Trading Volume',value:m.volume_24h_usdc,unit:'USDC',observed:apiDate,dateLabel:'API observation',source:apiSource,href:'#/omni',context:'Rolling 24 hours'},
  {id:'trader_open_interest_usdc',label:'Trader Open Interest',value:m.trader_open_interest_usdc,unit:'USDC',observed:apiDate,dateLabel:'API observation',source:apiSource,href:'#/omni',context:'OLP excluded'},
  {id:'swap_volume_share_pct',label:'Swaps’ Volume Share',value:share===null?null:share*100,unit:'%',observed:apiDate,dateLabel:'API observation',source:apiSource,href:'#/omni?contract=swap',context:completeMarketCoverage()?'Swap / total 24H volume':'Unavailable / partial listing coverage'},
  {id:'reported_net_profit_cumulative',label:'Covered Net Profit',value:totals().net_profit,unit:'$',observed:reportDate,dateLabel:'Report publication',source:reportSource,href:'#/financials',context:`Sum of ${reports().length} covered reports`},
  {id:'lifetime_olp_pnl',label:'Lifetime OLP PnL',value:report?.snapshot?.lifetime_olp_pnl,unit:'$',observed:reportDate,dateLabel:'Report publication',source:reportSource,href:'#/olp',context:'Reported source lifetime'}
];return `<section class="panel research-indicators section-space" id="research-indicators"><div class="panel-head"><div><h2>Research Indicators</h2></div>${infoButton('research-indicators','Research indicator and baseline methodology')}</div><div class="table-wrap"><table class="data-table indicator-table"><thead><tr><th>Indicator</th><th>Latest source value</th><th>Baseline</th><th>Change</th><th>View</th></tr></thead><tbody>${rows.map(row=>{const baseline=record?.indicators?.[row.id];const comparable=baseline&&baseline.unit===row.unit&&n(baseline.value)!==null;const matchedCoverage=row.id!=='reported_net_profit_cumulative'||researchCoverageMatches(record,baseline);const sourceOrder=comparable&&Date.parse(row.observed)>=Date.parse(baseline.source_observed_at);const changeLabel=!comparable?'—':!matchedCoverage?'Coverage changed':!sourceOrder?'Current source older':indicatorChange(row.value,baseline.value,row.unit);return `<tr data-indicator="${row.id}"><td class="wrap" data-label="Indicator"><span class="indicator-name">${e(row.label)}</span><small class="indicator-context">${e(row.context)}</small></td><td data-label="Latest source value"><span class="indicator-value number">${e(indicatorValue(row.value,row.unit))}</span><small class="indicator-date">${e(row.dateLabel)}<br>${row.observed?sourceLink(row.source,timestamp(row.observed)):'Unavailable'}</small></td><td data-label="Baseline"><span class="indicator-value number">${comparable?e(indicatorValue(baseline.value,row.unit)):e(record?'Unavailable':absentLabel)}</span>${comparable&&baseline.source_observed_at?`<small class="indicator-date">Source date<br>${sourceLink(baseline.source_url,timestamp(baseline.source_observed_at))}</small>`:''}</td><td data-label="Change"><span class="indicator-value number">${e(changeLabel)}</span></td><td data-label="View"><a class="inline-link" href="${e(row.href)}">Open</a></td></tr>`;}).join('')}</tbody></table></div><div class="source-line"><span>Latest observations / separate from article observation values</span><a class="inline-link" href="#/omni?view=execution">Execution observations</a></div></section>`;}

function explorerEntries(){const m=latest()?.totals||{},r=latestReport(),entries=[];const add=(category,label,value,unit,coverage,href,method)=>entries.push({category,label,value,unit,coverage,href,method});
  [['24-Hour Trading Volume','volume_24h_usdc','volume'],['Cumulative Trading Volume','cumulative_total_volume_usdc','volume'],['Trader Open Interest','trader_open_interest_usdc','oi'],['Open Interest Including OLP','open_interest_total_usdc','oi'],['Total Value Locked','tvl_usdc','tvl']].forEach(([label,key,method])=>add('Omni',label,compact(m[key]),'USDC',`${snapshots().length} dated observations`,'#/omni',method));
  add('Omni','Reported Markets',fixed(m.num_markets_reported,0),'markets','API reported count','#/omni','markets');add('Omni','Observed Markets',fixed(m.num_markets_observed,0),'markets','Returned listing rows','#/omni','markets');
  (latest()?.by_contract_type||[]).forEach(row=>add('Omni',(row.contract_type==='swap'?'Swaps':'Perpetuals')+' 24-Hour Volume',compact(row.volume_24h_usdc),'USDC','Latest API response','#/omni?contract='+row.contract_type,'volume'));
  add('Omni','Indicative Quotes by Size',String(latestMarkets().filter(m=>m.quotes?.size_100k).length),'markets','$100K quotes available','#/omni','quotes');
  add('Omni','Perpetual Funding',String(latestMarkets().filter(m=>m.contract_type==='perpetual'&&n(m.funding?.next?.annualized_rate)!==null).length),'markets','Source interval and annualized rates','#/omni?contract=perpetual','funding');
  add('Omni','Swap Financing',String(latestMarkets().filter(m=>m.contract_type==='swap'&&n(m.funding?.next?.annualized_long_rate)!==null).length),'markets','Long / short terms','#/omni?contract=swap','funding');
  Object.entries(financialLabels).forEach(([key,label])=>{const coverage=DATA.financials?.totals?.field_coverage?.[key];add('Financials',label,cash(totals()[key]),'$ / covered sum',`${coverage?.reported_count||0}/${reports().length} reports${coverage&&!coverage.complete?' / partial':''}`,'#/financials',key==='market_making_costs'?'costs':'covered');});
  add('Financials','Net Profit / Gross Revenue',percent(ratio(totals().net_profit,totals().gross_revenue)),'ratio','Derived from covered reports','#/financials','profit-ratio');
  add('Financials','Protocol Treasury Holdings',compact(r?.snapshot?.treasury_holdings),'USDC','Latest reported snapshot','#/financials?view=treasury','treasury');
  add('OLP','Lifetime OLP PnL',cash(r?.snapshot?.lifetime_olp_pnl),'$ / source lifetime',`${reports().length} report observations`,'#/olp','olp');
  (DATA.supplement?.chain?.accounts||[]).forEach(a=>add(a.id==='olp'?'OLP':'Financials',a.name+' Onchain Balance',compact(a.balance_usdc),'USDC','Observed Arbitrum balance',a.id==='olp'?'#/olp':'#/financials?view=treasury','chain'));
  add('OLP','Consolidated OLP NAV','Unavailable','—','Additional accounting required','#/olp','olp');
  add('VAR','Token Price and Market Capitalization','Unavailable','USD','Identity and market verification required','#/var','var');
  add('VAR','Supply and Unlocks','Unverified','VAR','Contract and schedule verification required','#/var','var');
  add('VAR','Buyback Purchases and Burns','No verified series','VAR','Executed events required','#/var','var');
  add('Variational Pro','RFQ and Market Maker Participation','Unavailable','—','Authorized aggregate feed required','#/pro','pro');
  return entries;
}
function explorerRows(){const rows=explorerEntries().filter(r=>(state.explorerCategory==='All'||r.category===state.explorerCategory)&&[r.category,r.label,r.coverage].join(' ').toLowerCase().includes(state.explorerSearch.toLowerCase()));return `<div class="table-wrap"><table class="data-table"><thead><tr><th>Metric</th><th class="number">Value</th><th>Coverage</th><th>View</th></tr></thead><tbody>${rows.length?rows.map(r=>`<tr><td class="wrap">${e(r.label)}<span class="market-name">${e(r.category)}</span></td><td class="number">${e(r.value)}<span class="market-name">${e(r.unit)}</span></td><td class="wrap muted">${e(r.coverage)}</td><td><a class="inline-link" href="${e(r.href)}">Open</a> ${infoButton(r.method,r.label+' definition')}</td></tr>`).join(''):'<tr><td colspan="4">No matching metrics.</td></tr>'}</tbody></table></div>`;}
function explorer(){return heading('Data Explorer','Definitions, current values and source coverage','METRIC LIBRARY')+`<div class="coverage-grid"><section class="coverage-tile"><h2>Financial reports</h2><strong>${reports().length}</strong><p>${reports().length?e(shortDate(reports()[0].published_at)+'–'+date(latestReport().published_at)):'Unavailable'} / ${warningCount()} source exceptions</p></section><section class="coverage-tile"><h2>Omni market coverage</h2><strong>${latestMarkets().length}</strong><p>Observed listings in the latest response</p></section><section class="coverage-tile"><h2>Dated API observations</h2><strong>${snapshots().length}</strong><p>Actual collection dates / no invented backfill</p></section></div><section class="panel"><div class="panel-head"><div><h2>Metric Library</h2></div>${infoButton('coverage')}</div><div class="table-toolbar"><label class="search-field"><input type="search" placeholder="Find a metric" aria-label="Search metric library" value="${e(state.explorerSearch)}" data-explorer-search></label><div class="field-select"><select aria-label="Metric category" data-explorer-category>${['All','Omni','Financials','OLP','VAR','Variational Pro'].map(x=>`<option${state.explorerCategory===x?' selected':''}>${x}</option>`).join('')}</select></div></div><div id="explorer-table">${explorerRows()}</div></section>`;}

function breakdownMethod() {
  const {records,selected} = resultsSelection();
  const source = selected ? sourceLink(officialUpdate(selected),'View official update') :
    sourceLink('data/financials.json','Published financial figures');
  const updated = timestamp(records.at(-1)?.published_at);
  return {title:'Gross Revenue to Net Profit',
    definition:'Variational publishes trading results every two weeks. Period total sums updates published in the selected UTC window.',
    calculation:'Reported costs and rewards retain their signs. When itemized costs are missing, gross-to-net change equals Net Revenue minus Gross Revenue. Source differences show where the published subtotal differs from the calculation.',
    source,updated};
}
function definition(key){if(key==='waterfall')return breakdownMethod();const report=latestReport(),chain=DATA.supplement?.chain,api=DATA.markets?.source;const source=sourceLink(api?.url||API_DOCS,'Official Omni API');const official=sourceLink(report?.source_urls?.[0],'Official report');const methods={
  range:{title:'Historical period',definition:'UTC calendar windows end on the latest dashboard capture date. 1Y uses the calendar-year anniversary; All shows available history.',calculation:`${rangeWindowLabel()}. Cumulative values start at the archive beginning before cropping. Current snapshots retain their source dates.`,source:sourceLink('data/market-history.json','Observation dates')+' / '+sourceLink('data/financials.json','Report publication dates'),updated:timestamp(rangeAnchor())},
  'selected-flow':{title:'Selected-period reported flows',definition:`Sum of amounts in ${selectedReports().length} reports published in the selected UTC window; no publications means unavailable, not zero.`,calculation:'Only reported period amounts are summed. Publication dates do not establish accounting endpoints. Current holdings and source lifetime figures are not summed or reset.',source:sourceLink('data/financials.json','Complete source records'),updated:coverageText()},
  'api-volume-window':{title:'Cumulative volume from the Omni API',definition:'The chart uses source-dated Omni API lifetime counters in USDC when the period has no financial report.',calculation:'Each marker is a directly observed counter. The unit and source shown above the chart apply to the entire selected series.',source,updated:timestamp(latest()?.observed_at)},
  'api-dual-window':{title:'Double-sided open interest from the Omni API',definition:'The API reports open interest including both trader and OLP positions in USDC. This series is used when the period has no financial report.',calculation:'Each observation uses the published aggregate. The chart keeps its source and native unit consistent across the period.',source,updated:timestamp(latest()?.observed_at)},
  'product-activity':{title:'Trading activity by contract type',definition:'Perpetual and swap observations aggregate only the listings returned by the Omni API.',calculation:'The market count is the number of returned listings. Each group’s volume share uses total volume across those returned listings. Trader open interest sums long and short trader positions in the group.',source,updated:timestamp(latest()?.observed_at)},
  'reported-volume':{title:'Total Volume Traded',definition:'Each bi-weekly report supplies a cumulative volume counter in dollars.',calculation:'Between reports subtracts the prior report’s counter before the selected period is applied. The first observation, a missing counter or a decline has no interval value. Each report’s publication timestamp identifies the observation.',source:official,updated:timestamp(report?.published_at)},
  'reported-oi':{title:'Current Dual-Sided OI',definition:'Variational reports open interest including both trader and OLP positions.',calculation:'Each observation retains the dollar amount and publication date from its bi-weekly report.',source:official,updated:timestamp(report?.published_at)},
  volume:{title:'Trading volume',definition:'Omni notional volume in USDC. The 24-hour value is a rolling window; cumulative volume is the API counter.',calculation:'Repeated rolling windows are not summed. Contract totals aggregate the covered listing rows.',source,updated:timestamp(latest()?.observed_at)},
  oi:{title:'Open interest',definition:'The aggregate API OI is double-sided and includes traders and OLP. Individual markets and contract-type buckets cover returned trader positions.',calculation:'Aggregate trader OI equals half of the reported double-sided total. Market buckets sum the returned rows. The collector records the difference between the two measures. Long share equals long OI divided by long plus short OI, where the denominator is positive.',source,updated:timestamp(latest()?.observed_at)},
  tvl:{title:'Total value locked',definition:'Protocol-reported Omni TVL under the current API definition: settlement pools and OLP vault.',calculation:'Source value retained. A wallet balance or historical report with different hedging exclusions is a separate measure.',source,updated:timestamp(latest()?.observed_at)},
  markets:{title:'Market data',definition:'Returned Omni listings, separated into Perpetuals and Swaps. Observed and reported market counts are distinct; product buckets sum observed rows. Prices, volume and OI are USDC.',calculation:'Full spread shown at the source’s $100K notional: (ask − bid) / midpoint × 10,000. Quotes are indicative.',source,updated:timestamp(latest()?.observed_at)},
  prices:{title:'Mark price',definition:'API mark price in USDC, distinct from index price and indicative bid/ask quotes.',calculation:'Source value retained; not a realized execution price.',source:sourceLink('https://docs.variational.io/omni/trading/quoted-index-and-mark-prices','Price definitions'),updated:timestamp(latest()?.observed_at)},
  quotes:{title:'Indicative quotes by size',definition:'Bid/ask observations at the API’s named notional sizes. An available quote is not an executed trade.',calculation:'Full spread = (ask − bid) / ((ask + bid) / 2) × 10,000. A zero bid or ask makes the two-way quote at that size unavailable. The other sizes keep their own status.',source,updated:timestamp(selectedMarket()?.quotes?.updated_at||latest()?.observed_at)},
  funding:{title:'Funding and financing',definition:'Perpetual funding and swap long/short financing retain their own source intervals and next payment timestamps.',calculation:'Interval and already-annualized rates are displayed separately. No second annualization; missing values are not zero.',source:sourceLink('https://docs.variational.io/omni/trading/funding-rates','Funding definitions'),updated:timestamp(latest()?.observed_at)},
  'market-history':{title:'Dated market observations',definition:`${snapshots().length} actual saved API observations; rolling 24-hour volumes are not calendar-day totals.`,calculation:'Only actual dates are plotted. Missing dates/listings are unavailable; no interpolated history or summed rolling windows.',source:sourceLink('data/market-history.json','Snapshot data'),updated:timestamp(latest()?.observed_at)},
  financials:{title:'Reported operating results',definition:'Variational publishes these figures every two weeks. Dates identify when each update was published.',calculation:'Amounts retain their original reporting period and units. Source differences remain visible in the corresponding update.',source:official+' / '+sourceLink('data/financials.json','Complete source records'),updated:timestamp(report?.published_at)},
  covered:{title:'Covered-report totals',definition:`Sum of the ${reports().length} reports in this archive. This is not a protocol-lifetime financial total.`,calculation:'Only period flow amounts are summed. Cumulative volume, wallet balances and lifetime OLP PnL are not summed. Source exceptions remain.',source:sourceLink('data/financials.json','Financial data & provenance'),updated:coverageText()},
  'profit-ratio':{title:'Net Profit / Gross Revenue',definition:'A derived ratio using reported Net Profit and Spreads Paid (Gross Revenue).',calculation:'Reported Net Profit ÷ Gross Revenue. Cumulative mode uses the sums of the same covered reports, not an average of ratios.',source:sourceLink('data/financials.json','Source amounts'),updated:coverageText()},
  costs:{title:'Market Making Costs (Cost of Revenue)',definition:'Only the reports that explicitly disclose the cost line are included.',calculation:`Available in ${DATA.financials?.totals?.field_coverage?.market_making_costs?.reported_count||0}/${reports().length} reports. Missing amounts are not zero. Gross minus Net Revenue is not substituted as a reported cost.`,source:sourceLink('data/financials.json','Report coverage'),updated:coverageText()},
  allocation:{title:'Net Profit Allocation',definition:'Variational reports Protocol Treasury Inflow and OLP PnL as the components of Net Profit.',calculation:'Each amount retains its reported sign. Percentage shares are shown when the components are fully reported, nonnegative and reconcile to positive Net Profit.',source:sourceLink('data/financials.json','Published financial figures'),updated:coverageText()},
  treasury:{title:'Protocol Treasury',definition:'Reported Inflow uses the source dollar notation. Current Holdings are a USDC stock, not period profit.',calculation:'Covered Inflow sums reported period amounts. Holdings, independent chain balances and accounting profit are separate series.',source:official,updated:timestamp(report?.published_at)},
  olp:{title:'Omni Liquidity Provider (OLP)',definition:'Reported period PnL and source lifetime PnL use different baselines. Source lifetime includes activity outside the covered archive.',calculation:'Period PnL may be summed only over the covered reports. PnL and onchain cash do not establish consolidated NAV, external hedges or APY.',source:official+' / '+sourceLink('https://docs.variational.io/omni/the-omni-liquidity-provider-olp','OLP documentation'),updated:timestamp(report?.published_at)},
  'reported-rewards':{title:'Reported Rewards',definition:'Variational’s published cumulative claims and biweekly referral costs. Values retain the precision used in the original update.',calculation:'Cumulative claims are snapshots. Referral Rewards by Period retains the signed expense when reported.',source:sourceLink('data/reported-rewards.json','Published figures'),updated:timestamp(DATA.rewards?.reports?.at(-1)?.published_at)},
  'treasury-balance-changes':{title:'Treasury Balance Changes',definition:'Native USDC balance at each verified Arbitrum block less the balance at the prior recorded block.',calculation:'Each bar spans the two source timestamps. Observations may span multiple days. Total balance changes include any wallet transfers.',source:sourceLink('data/chain-history.json','Block observations'),updated:timestamp(DATA.chainHistory?.points?.at(-1)?.block_timestamp)},
  'chain-history':{title:'Historical Arbitrum wallet balances',definition:'Native USDC held by the published Protocol Treasury or Core OLP Vault address at each recorded Arbitrum One block.',calculation:'The token contract and block identity are checked. A point represents the wallet balance at that block. External hedges and liabilities require separate disclosures.',source:sourceLink('data/chain-history.json','Block timestamps and provenance')+' / '+sourceLink(chain?.source_url,'Contract registry'),updated:timestamp(DATA.chainHistory?.points?.at(-1)?.block_timestamp)},
  chain:{title:'Observed Arbitrum balances',definition:'Native USDC balanceOf observations at the stated Arbitrum block for the documented accounts.',calculation:'Wallet balances are stocks. They do not prove revenue, liabilities, external hedges or total OLP equity.',source:sourceLink(chain?.source_url,'Account documentation'),updated:timestamp(chain?.observed_at)+' / block '+(chain?.block_number||'unavailable')},
  var:{title:'VAR data activation',definition:'Announced allocation is a policy, not circulating supply. The preview listing does not establish live trading or a canonical contract.',calculation:'Price, supply and executed buyback/burn series activate only after identity and source verification. No missing price is rendered as zero.',source:sourceLink(DATA.supplement?.token?.policy_url||'https://docs.variational.io/token/usdvar','Official VAR policy')+' / '+sourceLink(DATA.supplement?.token?.source_url,'CoinGecko'),updated:timestamp(DATA.supplement?.token?.checked_at)},
  pro:{title:'Variational Pro',definition:'Institutional OTC with multiple market makers, separate from Omni and its OLP counterparty.',calculation:'A launch announcement alone does not establish a validated public feed. Institutional activity requires authorized aggregates.',source:sourceLink(DATA.supplement?.pro?.source_url||'https://docs.variational.io/pro/about-pro','Official Pro documentation'),updated:timestamp(DATA.supplement?.pro?.checked_at)},
  research:{title:'Research library',definition:'Author-approved article metadata and published content, each with its own publication state and date.',calculation:'Scheduled entries do not expose unpublished bodies or private URLs. Article observation dates remain distinct from current dashboard data.',source:'Canonical public links appear on published article pages.',updated:'Publication status is editorially maintained.'},
  'research-indicators':{title:'Research indicators & baseline',definition:'Latest public-source observations, separate from the article’s original observation values. API dates are observations; financial dates are report publications.',calculation:'Baseline is the frozen target-date snapshot in Asia/Seoul, never a later substitute. Changes are absolute; swaps’ share changes are percentage points. Covered Net Profit is not lifetime profit; its change requires matching covered report IDs and source amounts, plus complete known-version coverage. Coverage changes or revisions are labeled separately. Missing or incompatible baselines produce no change value.',source:sourceLink('data/research-baselines.json','Recorded baselines')+' / '+sourceLink('data/markets.json','Omni observations')+' / '+sourceLink('data/financials.json','Reported financials'),updated:'Recording a scheduled-date snapshot does not declare article publication. Source timestamps remain visible; no history is filled across gaps.'},
  'market-volume-distribution':{title:'24-Hour Volume by Market',definition:'The eight top markets and the rest of the returned listings at the latest observation.',calculation:'Each share uses the sum of 24-hour volume across returned listings.',source:sourceLink(API_DOCS,'Omni API'),updated:timestamp(latest()?.observed_at)},
  'classified-markets':{title:'Omni Market Groups',definition:'Trading volume and trader open interest from saved Omni observations. Volume covers the rolling 24 hours. Open interest represents trader positions except for the OLP series, which mirrors the matched trader side when listing coverage is complete.',calculation:'Groups match exact identities in the sourced market catalogue. TradFi totals cover classified listings. Total share requires complete listing and classification coverage. Trade Distribution uses the latest observation and ranks the ten highest-volume markets.',source:sourceLink('data/market-classifications.json','Classification evidence')+' / '+sourceLink(API_DOCS,'Omni API'),updated:timestamp(currentProtocolMetrics()?.classificationObservedAt)},
  'chart-half-spread':{title:'Quoted Half-Spread',definition:'One saved indicative quote per market and observation, at $100,000.',calculation:'Half of the full bid-ask spread in basis points.',source:sourceLink(API_DOCS,'Omni API'),updated:timestamp(latest()?.observed_at)},
  'quote-breadth':{title:'Omni Quotes',definition:'Full bid-ask spreads at $1K, $100K and $1M on the selected quote date. Quoted counts require two valid sides. A closed swap can have no quote.',calculation:'Spread in bps equals (ask − bid) / midpoint × 10,000. Medians cover markets with both quotes at that size.',source:sourceLink(API_DOCS,'Official Omni API'),updated:timestamp(executionObservation().snapshot?.observed_at)},
  'pool-index':{title:'Verified Settlement Pool Creations',definition:'Events from the documented Variational settlement pool factory inside the indexed block interval.',calculation:'PoolCreated identifies an event and its named participants. Participant and creator counts describe the indexed interval. Wallet cash movements require a separate native-USDC transfer ledger.',source:sourceLink('data/arbitrum-index.json','Block coverage and validation'),updated:timestamp(DATA.arbitrumIndex?.coverage?.to_timestamp)},
  'reported-activity':{title:'Variational Activity Updates',definition:'Actual observations published by Variational. Daily Active Users is an average for the prior week, and Weekly Active Users is the reported weekly count.',calculation:'The chart uses release dates. Source values retain their original reporting windows. These figures use Variational’s reported activity definition.',source:sourceLink('data/official-activity.json','Official disclosure archive'),updated:timestamp(DATA.officialActivity?.reports?.at(-1)?.published_at)},
  'multi-venue':{title:'Perpetual Trading Costs by Venue',definition:'Estimated cost of buying and selling the same amount at each order size. Omni indicative quotes are matched against public order books within 45 seconds.',calculation:'Each peer uses both sides of its own book and the fee assumption in the sample file. Binance uses 5 bps per side as an illustrative undiscounted rate. Lighter uses the zero standard fee reported by the market endpoint. USDC and USDT are treated at dollar par. Funding, rebates and account discounts are excluded. The matched Omni median may differ by peer because qualifying times differ.',source:sourceLink('data/multi-venue.json','Samples and venue documentation'),updated:timestamp(DATA.multiVenue?.attempted_at)},
  execution:{title:'November 11 Hyperliquid Study',definition:'Median indicative costs for BTC, ETH, SOL and HYPE. On November 11, 49 of 72 captured market-size pairs qualified. Archived pairs use the later source timestamp.',calculation:'Omni full spread versus depth-weighted Hyperliquid bids and asks plus 4.5 bps per side. Source times must be within 45 seconds. Both book sides must cover the full size. Savings is the median paired difference.',source:sourceLink('data/execution-study.json','Matched samples')+' / '+sourceLink('https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees','Hyperliquid fees'),updated:timestamp(executionMatches().at(-1)?.observed_at)},
  coverage:{title:'Data & methodology',definition:'Official Variational API observations, official reported financials, documented Arbitrum accounts and verified token reference data.',calculation:'Reported, observed and derived measures remain separate. Snapshot timestamps and report dates govern each view; missing values are never zero.',source:sourceLink('data/financials.json','Financial records')+' / '+sourceLink('data/markets.json','Market snapshot')+' / '+sourceLink('data/supplement.json','Supplemental data'),updated:`Financial reports: ${reports().length}. API snapshots: ${snapshots().length}. Daily collection is scheduled; source reports follow their native cadence.`},
  freshness:{title:'Source freshness',definition:'Market data refreshes daily. An observation older than 36 hours carries a visible delay notice.',calculation:DATA.markets?.status?.state==='last_known_good'?'The latest Omni request returned '+e(DATA.markets.status.error||'a source error')+' at '+timestamp(DATA.markets.status.last_attempt_at)+'. The dashboard retains the last successful snapshot.':'The source timestamp is retained independently of page load time.',source:sourceLink('data/markets.json','Market collection status')+' / '+sourceLink('data/financials.json','Report discovery'),updated:'Omni '+timestamp(latest()?.observed_at)+' / Financial report '+timestamp(report?.published_at)}
};return methods[key]||methods.coverage;}
function openDialog(title,content){const dialog=document.getElementById('info-dialog');lastFocus=document.activeElement;document.getElementById('info-title').textContent=title;document.getElementById('info-content').innerHTML=content;if(typeof dialog.showModal==='function')dialog.showModal();else dialog.setAttribute('open','');document.getElementById('close-dialog').focus();}
function warningText(w){if(typeof w==='string')return w;return (w.message||w.code||'Unresolved source discrepancy')+(w.residual!==undefined?' Residual: '+fixed(w.residual,0)+' '+(w.unit||'')+'.':'');}
function showMethod(key,context=''){if(key==='exceptions'||key==='period-exceptions'){const flagged=(key==='period-exceptions'?selectedReports():reports()).filter(r=>r.warnings?.length);openDialog('Reported source exceptions',`<p class="muted small">Original reported values are retained.</p><ul>${flagged.map(r=>`<li><strong>${e(date(r.published_at))} UTC</strong><br>${r.warnings.map(w=>e(warningText(w))).join('<br>')}<br>${sourceLink(r.source_urls?.[0],'Official report')}</li>`).join('')}</ul>`);return;}const d=definition(key);openDialog(d.title,`<dl><dt>Definition</dt><dd>${e(d.definition)}</dd><dt>Calculation</dt><dd>${e(d.calculation)}</dd>${context ? '<dt>Scope</dt><dd>'+e(context)+'</dd>' : ''}<dt>Source</dt><dd>${d.source}</dd><dt>Updated</dt><dd>${e(d.updated)}</dd></dl>`);}
function showReport(id){const r=reports().find(r=>r.id===id);if(!r)return;const rows=[...Object.entries(r.period_amounts||{}),...Object.entries(r.snapshot||{})].filter(([,v])=>v!==null);openDialog('Report / '+date(r.published_at)+' UTC',`<div class="table-wrap"><table class="data-table"><tbody>${rows.map(([k,v])=>`<tr><td class="wrap">${e(r.labels?.[k]||financialLabels[k]||k)}</td><td class="number">${e(r.raw_values?.[k]||(fixed(v,2)+' '+(r.units?.[k]||'')))}</td></tr>`).join('')}</tbody></table></div>${r.warnings?.length?`<div class="notice section-space">${r.warnings.map(w=>e(warningText(w))).join('<br>')}</div>`:''}<p class="small section-space">${(r.source_urls||[]).map((url,i)=>sourceLink(url,'Source '+(i+1))).join(' / ')}</p><p class="small muted section-space">Publication: ${e(timestamp(r.published_at))}. Exact accounting endpoints are not established.</p><p class="small muted">${e([...new Set((r.sources||[]).map(s=>s.retrieval_method))].join('; '))}</p>`);}
function closeDialog(){const dialog=document.getElementById('info-dialog');if(typeof dialog.close==='function')dialog.close();else dialog.removeAttribute('open');lastFocus?.focus();}
function syncNavigation(){
  const key=route.page==='financials'?'financials:'+(route.params.get('view')==='treasury'?'treasury':'reports'):route.page;
  let activeLabel=titles[route.page];
  document.querySelectorAll('[data-nav-route]').forEach(link=>{const active=link.dataset.navRoute===key;link.classList.toggle('active',active);if(active){link.setAttribute('aria-current','page');activeLabel=link.textContent.trim();}else link.removeAttribute('aria-current');});
  document.getElementById('breadcrumb-page').textContent=activeLabel;
  document.title=activeLabel+' / Variational Analytics';
}
function setMenu(open){
  const sidebar=document.getElementById('sidebar'),toggle=document.getElementById('menu-toggle');
  const restoreFocus=!open&&sidebar.classList.contains('is-open')&&sidebar.contains(document.activeElement);
  sidebar.classList.toggle('is-open',open);document.getElementById('mobile-scrim').hidden=!open;toggle.setAttribute('aria-expanded',String(open));toggle.setAttribute('aria-label',open?'Close navigation':'Open navigation');
  if(open)(sidebar.querySelector('[aria-current="page"]')||sidebar.querySelector('a')).focus();else if(restoreFocus)toggle.focus();
}
function readRoute(){const [path,query='']=(location.hash.replace(/^#\/?/,'')||'overview').split('?');route={page:titles[path]?path:'overview',params:new URLSearchParams(query)};if(!route.params.has('range'))route.params.set('range',normalizeRange(new URLSearchParams(location.search).get('range')));else route.params.set('range',normalizeRange(route.params.get('range')));}
function render(){chartCleanups.forEach(fn=>fn());chartCleanups=[];charts.clear();readRoute();syncNavigation();const views={overview,omni,tradfi:tradfiView,accounts:accountsView,execution:executionView,financials,rewards:rewardsView,olp,var:token,pro,research,explorer};main.innerHTML=views[route.page]();syncRangeLinks();main.querySelectorAll('[data-chart]').forEach(el=>{const config=charts.get(el.dataset.chart);if(config)chartCleanups.push(mountChart(el,config));});document.documentElement.dataset.appReady='true';setMenu(false);void ensureMarketHistory();if(route.page==='research'&&route.params.get('view')==='indicators'){requestAnimationFrame(()=>{const target=document.getElementById('research-indicators');if(typeof target?.scrollIntoView==='function')target.scrollIntoView({block:'start',behavior:'auto'});});}if(route.page==='omni'&&route.params.get('view')==='execution'){requestAnimationFrame(()=>{const target=document.getElementById('execution-observations');if(typeof target?.scrollIntoView==='function')target.scrollIntoView({block:'start',behavior:'auto'});});}}
function exportWaterfall() {
  const {records,selected} = resultsSelection();
  if (!records.length) return;
  let model;
  try { model = buildOperatingResults(records); }
  catch (error) { if (error instanceof OperatingResultsError) return; throw error; }
  const context = [model.firstPublishedAt,model.lastPublishedAt,records.map(officialUpdate).filter(Boolean).join(' ')];
  const fileScope = selected ? selected.published_at.slice(0,10) : selectedRange();
  downloadCSV('variational-operating-waterfall-'+fileScope+'.csv',
    ['line','basis','amount_usd','plot_start_usd','plot_end_usd','reported_count','report_count','first_publication_utc','last_publication_utc','sources'],
    waterfallExportRows(model).map(row => [...row,...context]));
}
function exportFinancials(){const keys=Object.keys(financialLabels);downloadCSV('variational-financial-reports-'+selectedRange()+'.csv',['publication_at_utc',...keys,'source_url','source_exception_count'],selectedReports().map(r=>[r.published_at,...keys.map(k=>r.period_amounts[k]),r.source_urls?.[0],r.warnings?.length||0]));}
function exportMarkets(){downloadCSV('variational-omni-markets.csv',['observed_at_utc','ticker','contract_type','mark_price_usdc','volume_24h_usdc','trader_open_interest_usdc','long_open_interest_usdc','short_open_interest_usdc','indicative_full_spread_100k_bps','quote_updated_at'],marketRows().map(m=>[latest().observed_at,m.ticker,m.contract_type,m.mark_price,m.volume_24h_usdc,m.trader_open_interest_usdc,m.long_open_interest_usdc,m.short_open_interest_usdc,m.quotes?.size_100k?.full_spread_bps,m.quotes?.updated_at]));}
function exportQuotes(key){const m=latestMarkets().find(m=>marketKey(m)===key);if(!m)return;downloadCSV('variational-'+m.ticker.replace(/[^a-z0-9_-]/gi,'_')+'-quotes.csv',['ticker','notional_source_convention','bid_usdc','ask_usdc','full_spread_bps','observed_at_utc','quote_status'],[['base','base'],['size_1k','1000'],['size_100k','100000'],['size_1m','1000000']].map(([k,label])=>[m.ticker,label,m.quotes?.[k]?.bid,m.quotes?.[k]?.ask,m.quotes?.[k]?.full_spread_bps,m.quotes?.updated_at,quoteIssue(m,k)||(m.quotes?.[k]?'available':'unavailable')]));}
document.addEventListener('click',event=>{const button=event.target.closest('button[data-range]');if(!button)return;const next=normalizeRange(button.dataset.range);if(next===selectedRange())return;const params=new URLSearchParams(route.params);params.set('range',next);if(route.page==='financials')params.delete('report');if(route.page==='execution')params.delete('quote-date');state.marketPage=1;pendingRangeFocus=true;location.hash='/'+route.page+'?'+params.toString();});
document.addEventListener('click',event=>{const b=event.target.closest('button');if(!b)return;if(b.dataset.method){showMethod(b.dataset.method,b.dataset.context);return;}if(b.dataset.report){showReport(b.dataset.report);return;}if(b.id==='close-dialog'){closeDialog();return;}if(b.id==='menu-toggle'){setMenu(b.getAttribute('aria-expanded')!=='true');return;}if(b.dataset.market){location.hash=rangeHref('#/omni?market='+encodeURIComponent(b.dataset.market),selectedRange()).slice(1);return;}if(b.dataset.financialMode){state.financialMode=b.dataset.financialMode;render();return;}if(b.dataset.olpMode){state.olpMode=b.dataset.olpMode;render();return;}if(b.dataset.marketSort){state.marketDir=state.marketSort===b.dataset.marketSort?-state.marketDir:-1;state.marketSort=b.dataset.marketSort;state.marketPage=1;document.getElementById('market-table').innerHTML=marketTable();return;}if(b.dataset.marketPage){state.marketPage=Math.max(1,state.marketPage+(b.dataset.marketPage==='next'?1:-1));document.getElementById('market-table').innerHTML=marketTable();return;}if(b.hasAttribute('data-export-waterfall')){exportWaterfall();return;}if(b.hasAttribute('data-export-financials')){exportFinancials();return;}if(b.hasAttribute('data-export-markets')){exportMarkets();return;}if(b.dataset.exportQuotes){exportQuotes(b.dataset.exportQuotes);return;}if(b.dataset.exportChart){const chart=charts.get(b.dataset.exportChart);if(chart)downloadCSV('variational-'+b.dataset.exportChart+'.csv',[...(chart.interval?['from_utc','to_utc']:['date_utc']),...chart.series.map(s=>s.label+' ('+chart.unit+')'),'source_url'],chart.rows.map(r=>[...(chart.interval?[r.startDate,r.date]:[r.date]),...chart.series.map(s=>r.values[s.key]),r.source]));}});
document.addEventListener('input',event=>{if(event.target.hasAttribute('data-market-search')){state.marketSearch=event.target.value;state.marketPage=1;document.getElementById('market-table').innerHTML=marketTable();}if(event.target.hasAttribute('data-explorer-search')){state.explorerSearch=event.target.value;document.getElementById('explorer-table').innerHTML=explorerRows();}});
document.addEventListener('change',event=>{
  if (event.target.hasAttribute('data-execution-date')) {
    const params = new URLSearchParams(route.params);
    if (event.target.value) params.set('quote-date',event.target.value);
    else params.delete('quote-date');
    location.hash = '/execution?'+params.toString();
    return;
  }
  if (!event.target.hasAttribute('data-results-report')) return;
  const id = event.target.value;
  const params = new URLSearchParams(route.params);
  if (id && selectedReports().some(report => report.id === id)) params.set('report',id);
  else params.delete('report');
  pendingResultsFocus = true;
  location.hash = '/financials?'+params.toString();
});
document.addEventListener('change',event=>{if(event.target.hasAttribute('data-financial-key')){state.financialKey=event.target.value;render();}if(event.target.hasAttribute('data-explorer-category')){state.explorerCategory=event.target.value;document.getElementById('explorer-table').innerHTML=explorerRows();}});
document.getElementById('mobile-scrim').addEventListener('click',()=>setMenu(false));
window.addEventListener('resize',()=>{const sidebar=document.getElementById('sidebar');if(window.innerWidth>720&&sidebar.classList.contains('is-open')){const focused=document.activeElement;setMenu(false);if(sidebar.contains(focused))focused.focus();}});
document.getElementById('info-dialog').addEventListener('click',event=>{if(event.target===event.currentTarget){const r=event.currentTarget.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)closeDialog();}});
document.getElementById('info-dialog').addEventListener('close',()=>lastFocus?.focus());
document.addEventListener('keydown',event=>{const sidebar=document.getElementById('sidebar');if(event.key==='Escape')setMenu(false);if(event.key==='Tab'&&sidebar.classList.contains('is-open')){const items=[...sidebar.querySelectorAll('a,button:not(:disabled)')],first=items[0],last=items.at(-1);if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}}});
window.addEventListener('hashchange',()=>{state.marketPage=1;render();
  if (pendingResultsFocus) {
    pendingResultsFocus = false;
    main.querySelector('[data-results-report]')?.focus({preventScroll:true});
  } else window.scrollTo({top:0,behavior:'auto'});
});
async function load(){const definitions=[['financials','financials.json'],['markets','markets.json'],['history','market-history.json'],['supplement','supplement.json'],['baselines','research-baselines.json'],['chainHistory','chain-history.json'],['executionStudy','execution-study.json'],['multiVenue','multi-venue.json'],['officialActivity','official-activity.json'],['arbitrumIndex','arbitrum-index.json'],['classifications','market-classifications.json'],['protocolHistory','protocol-metrics-history.json'],['rewards','reported-rewards.json']];await Promise.all(definitions.map(async([key,file])=>{try{const response=await fetch(new URL('../data/'+file,import.meta.url),{cache:'no-store'});if(!response.ok)throw new Error('HTTP '+response.status);const payload=await response.json();if(![1,2].includes(payload.schema_version))throw new Error('Unsupported data schema');if(key==='arbitrumIndex'&&(payload.chain_id!==42161||!Array.isArray(payload.coverage?.ranges)))throw new Error('Invalid pool index');if(key==='financials'&&!Array.isArray(payload.reports))throw new Error('Invalid financial records');if(key==='markets'&&!Array.isArray(payload.latest?.markets))throw new Error('Invalid market records');if(key==='history'&&!Array.isArray(payload.snapshots))throw new Error('Invalid history index');const requiredArrays={executionStudy:'rows',multiVenue:'rows',officialActivity:'reports',protocolHistory:'rows',rewards:'reports',classifications:'mappings'};if(requiredArrays[key] && (payload.schema_version!==1 || !Array.isArray(payload[requiredArrays[key]])))throw new Error('Invalid '+requiredArrays[key]+' data');if(key==='baselines'&&(payload.schema_version!==1||!Array.isArray(payload.articles)))throw new Error('Invalid research baseline data');if(key==='chainHistory' && (payload.chain_id!==42161 || !Array.isArray(payload.points))) throw new Error('Invalid onchain observation history');
      DATA[key]=payload;}catch(error){DATA.errors[key]=error.message;}}));document.getElementById('source-status').textContent=latest()?.observed_at?'Snapshot '+shortDate(latest().observed_at):'Source data unavailable';render();}
load().catch(()=>{main.innerHTML='<div class="error-card"><h1>Dashboard could not load</h1><p>Source files are unavailable. Please reload the page.</p></div>';});
