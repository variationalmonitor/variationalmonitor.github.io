import test from 'node:test';
import assert from 'node:assert/strict';
import { snapshotBlock, snapshotsOnly, frame } from '../site/js/data.js';

const RANGES = ['7d', '30d', '90d', '180d', '1y', 'all'];
const intraday = { ts: ['2026-11-24T09:30Z', '2026-11-24T10:00Z'], vol24h: [0.16, 0.16], slip100k_bps: [219.02, 226.42] };
const daily = { dates: ['2026-11-22', '2026-11-23'], vol24h: [5, 6], intraday };
const firstDay = { dates: [], vol24h: [], intraday };

test('a file with daily dates reads its half-hourly block at 7D only, as before', () => {
  assert.equal(snapshotsOnly(daily), false);
  assert.deepEqual(snapshotBlock(daily, '7d'), { block: intraday, range: '7d' });
  for (const r of RANGES.slice(1)) assert.equal(snapshotBlock(daily, r), null, r);
  // no half-hourly points: the daily file at every range, 7D included
  for (const r of RANGES) assert.equal(snapshotBlock({ ...daily, intraday: { ts: [] } }, r), null, r);
});

test('a listing on its first day reads its half-hourly block at every range, as at 7D', () => {
  assert.equal(snapshotsOnly(firstDay), true);
  for (const r of RANGES) assert.deepEqual(snapshotBlock(firstDay, r), { block: intraday, range: '7d' }, r);
  const fr = frame({ ...snapshotBlock(firstDay, '90d').block });
  assert.equal(fr.grain, 'i');
  assert.deepEqual(fr.col('vol24h'), [0.16, 0.16]);
});

test('a file with neither daily dates nor half-hourly points has nothing to read', () => {
  for (const f of [null, undefined, { dates: [] }, { dates: [], intraday: { ts: [] } }, { dates: [], intraday: null }]) {
    assert.equal(snapshotsOnly(f), false);
    for (const r of RANGES) assert.equal(snapshotBlock(f, r), null);
  }
  // a scope block with no `dates` key at all is not a first-day file
  assert.equal(snapshotsOnly({ intraday }), false);
  assert.equal(snapshotBlock({ intraday }, '90d'), null);
});
