/** Build compact, reproducible group time series from verified Omni partitions. */
import {readFile,rename,writeFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {join} from 'node:path';
import {buildProtocolMetrics} from '../public/assets/protocol-metrics.js';

const directory = process.argv.slice(2).find(arg=>arg!=='--check') || 'public/data';
const read = async name => JSON.parse(await readFile(join(directory,name),'utf8'));
const [index,latest,classifications] = await Promise.all(
  ['market-history.json','markets.json','market-classifications.json'].map(read));
const rows = [];
for (const entry of index.snapshots) {
  const filename = entry.partition_url;
  if (!filename) continue;
  let snapshot;
  if (filename === 'data/markets.json') {
    snapshot = latest.latest;
  } else {
    if (!/^data\/market-history\/\d{4}-\d{2}-\d{2}\.json$/.test(filename)) throw new Error('Unexpected market partition');
    const bytes = await readFile(join(directory,filename.slice(5)));
    if (createHash('sha256').update(bytes).digest('hex') !== entry.partition_sha256) throw new Error('Partition checksum mismatch');
    snapshot = JSON.parse(bytes).snapshot;
  }
  if (snapshot.observed_at !== entry.observed_at) throw new Error('Partition observation mismatch');
  const {observedAt,coverage,totals,byAssetClass,byInstrumentType,shares} = buildProtocolMetrics(snapshot,classifications);
  const benchmark = ['BTC-PERP','ETH-PERP','SOL-PERP','HYPE-PERP'];
  const halfSpreads = Object.fromEntries(benchmark.map(ticker => {
    const value = snapshot.markets.find(row=>row.ticker===ticker)?.quotes?.size_100k?.full_spread_bps;
    return [ticker, value === null || value === undefined ? null : Number(value)/2];
  }));
  rows.push({observedAt,coverage,totals,byAssetClass,byInstrumentType,shares,halfSpreads});
}
const output = {schema_version:1,classificationObservedAt:classifications.observed_at,rows};
const target = join(directory,'protocol-metrics-history.json');
const text = JSON.stringify(output,null,2)+'\n';
if (process.argv.includes('--check')) {
  if (await readFile(target,'utf8') !== text) throw new Error('Grouped metric history differs from verified source partitions');
} else {
  await writeFile(target+'.tmp',text);
  await rename(target+'.tmp',target);
}
console.log('Prepared protocol metrics for '+rows.length+' verified Omni observations');
