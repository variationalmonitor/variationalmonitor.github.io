import assert from 'node:assert/strict';
import test from 'node:test';
import {axisNumber, cash, date, number, price, shortDate, timestamp} from '../public/assets/utils.js';

test('dollar deductions use the sign before the currency symbol', () => {
  assert.equal(cash('-3456245'), '−$3.46M');
  assert.equal(cash('0'), '$0');
  assert.equal(cash(null), '—');
});

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

test('low-priced markets retain meaningful precision on screen', () => {
  assert.equal(price('0.0000000345'), '0.0000000345');
  assert.equal(price('0.000000000345'), '3.4500e-10');
  assert.equal(price(null), '—');
  assert.equal(price('1234.1234'), '1,234.12');
});
