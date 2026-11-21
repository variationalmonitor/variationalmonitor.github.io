// Pure aggregation of native snapshots using independently dated official metadata.
// Decimal amounts remain exact strings. Ratios are display-only floating values.
export class ProtocolMetricsError extends Error {}
const LABELS = {equity:'Equities',etf:'ETF',commodity:'Commodities',index:'Indices',currency:'Currencies',pre_ipo:'Pre-IPO',crypto:'Crypto',unclassified:'Unclassified',perpetual:'Perpetuals',swap:'Swaps',all:'All markets',tradfi:'TradFi'};
const AMOUNTS = ['volume24hUsdc','traderOpenInterestUsdc','longOpenInterestUsdc','shortOpenInterestUsdc'];
const INPUTS = ['volume_24h_usdc','trader_open_interest_usdc','long_open_interest_usdc','short_open_interest_usdc'];
const fail = (code) => { throw new ProtocolMetricsError(code); };
function time(value) { const n=Date.parse(value); if(typeof value!=='string'||!Number.isFinite(n)) fail('invalid_timestamp'); return n; }
function decimal(value) {
  if(typeof value!=='string'||value.length>256||!/^\d+(?:\.\d+)?$/.test(value)) fail('invalid_amount');
  const [whole,frac='']=value.split('.');
  if(whole.length>30||frac.length>128) fail('amount_out_of_bounds');
  return {n:BigInt(whole+frac),s:frac.length};
}
function add(a,b) { const s=Math.max(a.s,b.s); return {n:a.n*10n**BigInt(s-a.s)+b.n*10n**BigInt(s-b.s),s}; }
function formatted({n,s}) {
  const v=n.toString().padStart(s+1,'0');
  return s ? `${v.slice(0,-s)}.${v.slice(-s)}`.replace(/\.?0+$/,'') : v;
}
function sum(values) { return formatted(values.reduce((a,v)=>add(a,decimal(v)),{n:0n,s:0})); }
function ratio(a,b,multiplier=1) { return Number(b)===0 ? null : Number(a)/Number(b)*multiplier; }
function bucket(id, rows) {
  const out={id,label:LABELS[id]||id,marketCount:rows.length};
  for(const key of AMOUNTS) out[key]=sum(rows.map(r=>r[key]));
  out.longShortRatio=ratio(out.longOpenInterestUsdc,out.shortOpenInterestUsdc);
  out.turnover24h=ratio(out.volume24hUsdc,out.traderOpenInterestUsdc);
  return out;
}

export function buildProtocolMetrics(snapshot, classifications) {
  if(!snapshot||!Array.isArray(snapshot.markets)||snapshot.markets.length>2000||!classifications||classifications.schema_version!==1||!Array.isArray(classifications.mappings)||classifications.mappings.length>2000) fail('invalid_schema');
  const observed=time(snapshot.observed_at), classified=time(classifications.observed_at);
  const map=new Map();
  for(const row of classifications.mappings) {
    if(typeof row.ticker!=='string'||map.has(row.ticker)||!['tradfi','crypto','unclassified'].includes(row.scope)||!Object.hasOwn(LABELS,row.asset_class)) fail('invalid_classification');
    map.set(row.ticker,row);
  }
  const seen=new Set();
  const byMarket=snapshot.markets.map(m=>{
    if(typeof m.ticker!=='string'||m.ticker.length>120||seen.has(m.ticker)||!['perpetual','swap'].includes(m.contract_type)) fail('invalid_market_identity');
    seen.add(m.ticker);
    let known=map.get(m.ticker);
    // Full rows also require the name. Compact historical rows require the
    // exact contract ID and a verified listing timestamp before observation.
    if(known&&(known.contract_type!==m.contract_type||(m.name!=null&&known.name!==m.name)||(known.listed_at&&time(known.listed_at)>observed)||(m.name==null&&!known.listed_at))) known=null;
    const row={ticker:m.ticker,name:m.name??known?.name??m.ticker,contractType:m.contract_type,scope:known?.scope??'unclassified',assetClass:known?.asset_class??'unclassified',identityBasis:known?(m.name==null?'exact contract ID after documented listing time; historical name unavailable':'exact contract ID, type and name; dated curated taxonomy'):'unmatched or outside verified listing identity'};
    for(let i=0;i<AMOUNTS.length;i++) { decimal(m[INPUTS[i]]); row[AMOUNTS[i]]=m[INPUTS[i]]; }
    // A contradictory trader-side aggregate cannot silently alter class totals.
    if(formatted(decimal(row.traderOpenInterestUsdc))!==sum([row.longOpenInterestUsdc,row.shortOpenInterestUsdc])) fail('market_oi_identity');
    row.longShortRatio=ratio(row.longOpenInterestUsdc,row.shortOpenInterestUsdc);
    row.turnover24h=ratio(row.volume24hUsdc,row.traderOpenInterestUsdc);
    return row;
  });
  const groups={all:byMarket,tradfi:byMarket.filter(r=>r.scope==='tradfi'),crypto:byMarket.filter(r=>r.scope==='crypto'),unclassified:byMarket.filter(r=>r.scope==='unclassified')};
  const totals=Object.fromEntries(Object.entries(groups).map(([k,rows])=>[k,bucket(k,rows)]));
  const reported=snapshot.totals?.num_markets_reported;
  if(reported!=null&&(!Number.isSafeInteger(reported)||reported<byMarket.length||reported>10000)) fail('invalid_reported_market_count');
  const coverage={observedMarkets:byMarket.length,reportedMarkets:reported??null,classifiedMarkets:byMarket.length-groups.unclassified.length,unclassifiedMarkets:groups.unclassified.length,listingCoverageComplete:reported===byMarket.length,classificationCoverageComplete:groups.unclassified.length===0};
  coverage.shareCoverageComplete=coverage.listingCoverageComplete&&coverage.classificationCoverageComplete;
  const platformVolume=snapshot.totals?.volume_24h_usdc,platformOi=snapshot.totals?.trader_open_interest_usdc;
  if(platformVolume!=null) decimal(platformVolume);
  if(platformOi!=null) decimal(platformOi);
  return {observedAt:snapshot.observed_at,classificationObservedAt:classifications.observed_at,classificationBasis:observed<classified?'Current dated classification applied to an earlier native snapshot; not historical taxonomy.':'Dated official classification joined by exact market identity.',coverage,totals,
    byAssetClass:['equity','etf','commodity','index','currency','pre_ipo','unclassified'].map(id=>bucket(id,byMarket.filter(r=>r.assetClass===id))),
    byInstrumentType:['perpetual','swap'].map(id=>bucket(id,groups.tradfi.filter(r=>r.contractType===id))),
    byMarket,
    shares:{tradfiVolumePct:coverage.shareCoverageComplete&&platformVolume!=null?ratio(totals.tradfi.volume24hUsdc,platformVolume,100):null,tradfiTraderOiPct:coverage.shareCoverageComplete&&platformOi!=null?ratio(totals.tradfi.traderOpenInterestUsdc,platformOi,100):null}};
}
