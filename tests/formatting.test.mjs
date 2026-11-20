import assert from 'node:assert/strict';
import test from 'node:test';
import {axisNumber, date, number, shortDate, timestamp} from '../public/assets/utils.js';

test('financial axes preserve the actual interval at thousands, millions and billions', () => {
  assert.equal(axisNumber(1.5e9, 5e8), '1.5B');
  assert.equal(axisNumber(2.5e6, 2.5e6), '2.5M');
  assert.equal(axisNumber(1e9, 5e8), '1B');
  assert.equal(axisNumber(-2500, 500), '-2.5K');
  assert.equal(axisNumber(0.0025, 0.0005), '0.0025');
  assert.equal(axisNumber(0, 1e9), '0');
});

test('missing dates and amounts have no synthetic timestamp or financial value', () => {
  for (const missing of [null, undefined, '', false, {}]) {
    assert.equal(date(missing), 'Unavailable');
    assert.equal(timestamp(missing), 'Unavailable');
    assert.equal(shortDate(missing), '—');
    assert.equal(number(missing), null);
  }
  assert.equal(date('2026-11-20T00:15:00+09:00'), '19 Nov 2026');
  assert.equal(number('0'), 0);
});
