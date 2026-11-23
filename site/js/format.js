// The single number and date formatter. Every figure on the site goes through fmt().

export const MINUS = '−';
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const DAY = 86400000;

// ---------- dates (all UTC) ----------
export const dayNum = s => Math.round(Date.UTC(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10)) / DAY);
export const isoDay = n => new Date(n * DAY).toISOString().slice(0, 10);
export const weekStart = s => { const n = dayNum(s); return isoDay(n - ((new Date(n * DAY).getUTCDay() + 6) % 7)); };
export const fmtDate = s => `${+s.slice(8, 10)} ${MONTHS[+s.slice(5, 7) - 1]} ${s.slice(0, 4)}`;
export const fmtDay = s => `${+s.slice(8, 10)} ${MONTHS[+s.slice(5, 7) - 1]}`;
export const fmtMonth = (s, year = true) => MONTHS[+s.slice(5, 7) - 1] + (year ? ' ' + s.slice(0, 4) : '');
export const fmtTime = ts => `${ts.slice(11, 16)} UTC`;
export function fmtSpan(a, b) {
  if (a === b) return fmtDate(a);
  if (a.slice(0, 7) === b.slice(0, 7)) return `${+a.slice(8, 10)}–${fmtDate(b)}`;
  if (a.slice(0, 4) === b.slice(0, 4)) return `${fmtDay(a)} – ${fmtDate(b)}`;
  return `${fmtDate(a)} – ${fmtDate(b)}`;
}
/** A point in time on a chart: a day (`22 Nov 2026`), or a snapshot with its UTC time (`22 Nov 2026, 14:30 UTC`). */
export const fmtPoint = k => (k.length > 10 ? `${fmtDate(k)}, ${k.slice(11, 16)} UTC` : fmtDate(k));
// A time stamp for source lines: always the date, and the UTC time when the source gives one.
export function fmtStamp(ts) {
  if (!ts) return '';
  if (ts.length <= 10) return fmtDate(ts);
  return `${fmtDate(ts.slice(0, 10))} ${fmtTime(ts)}`;
}

// ---------- numbers ----------
const group = s => s.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
const trim = s => (s.includes('.') ? s.replace(/\.?0+$/, '') : s);
const sig3 = a => (a >= 100 ? a.toFixed(0) : a >= 10 ? a.toFixed(1) : a.toFixed(2));
const UNITS = [[1e12, 'T'], [1e9, 'B'], [1e6, 'M'], [1e3, 'K']];

function compact(a, axis) {
  for (let i = 0; i < UNITS.length; i++) {
    const [div, suf] = UNITS[i];
    if (a >= div || (i === UNITS.length - 1)) {
      let s = sig3(a / div);
      if (+s >= 1000 && i > 0) { s = sig3(a / UNITS[i - 1][0]); return (axis ? trim(s) : s) + UNITS[i - 1][1]; }
      return (axis ? trim(s) : s) + suf;
    }
  }
  return String(a);
}

function plain(a, unit, axis, tight) {
  switch (unit) {
    case 'usd':
      if (a >= 9999.5 || (tight && a >= 1000)) return '$' + compact(a, axis);
      if (a < 0.005) return '$0';
      if (axis) return '$' + (a < 10 && !Number.isInteger(a) ? a.toFixed(2) : group(trim(a.toFixed(2))));
      if (a >= 100) return '$' + group(a.toFixed(0));
      return '$' + a.toFixed(2);
    case 'count':
      if (axis && (a >= 9999.5 || (tight && a >= 1000))) return compact(a, true);
      return group(Number.isInteger(a) || a >= 100 ? a.toFixed(0) : trim(a.toFixed(1)));
    case 'bps':
      return (axis ? trim(a.toFixed(2)) : a.toFixed(1)) + ' bps';
    case 'pct':
    case 'rate': {
      const p = a * 100;
      return (axis ? trim(p.toFixed(2)) : p.toFixed(1)) + '%';
    }
    case 'pp':
      return (a * 100).toFixed(1) + ' pp';
    case 'ratio':
      return a.toFixed(2);
    case 'times':
      return (axis ? trim(a.toFixed(2)) : a.toFixed(1)) + '×';
    default:
      return group(trim(a.toFixed(2)));
  }
}

/**
 * fmt(value, unit, opts)
 *   unit: usd | count | bps | pct | rate | pp | times | ratio
 *   opts.axis: round tick style (trailing zeros dropped)
 *   opts.compact: with axis, abbreviate from 1,000 (an axis that reaches 10,000 uses one style)
 *   opts.sign: always print the sign (deltas)
 */
export function fmt(v, unit = 'count', opts = {}) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const a = Math.abs(v);
  const body = plain(a, unit, !!opts.axis, !!opts.compact);
  const zero = !/[1-9]/.test(body);
  if (zero) return body;
  return (v < 0 ? MINUS : opts.sign ? '+' : '') + body;
}

export const RANGE_LABEL = { '7d': '7D', '30d': '30D', '90d': '90D', '180d': '180D', '1y': '1Y', all: 'All-time' };
export const RANGE_DAYS = { '7d': 7, '30d': 30, '90d': 90, '180d': 180, '1y': 365, all: Infinity };
