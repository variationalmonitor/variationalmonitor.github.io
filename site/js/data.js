// Fetch and cache of data/*.json, plus the time helpers every card shares:
// frames (dates + named columns), range windows and Granularity buckets.
// The browser never derives a metric here; it only reshapes along time.

import { dayNum, isoDay, weekStart, RANGE_DAYS } from './format.js';

const cache = new Map();
const ready = new Map();
const fixtures = new Set();
const listeners = new Set();

export const onFixture = fn => listeners.add(fn);
export const hasFixture = () => fixtures.size > 0;

async function fetchJson(name) {
  try {
    const res = await fetch(`data/${name}.json`, { cache: 'no-cache' });
    if (!res.ok) return null;
    const json = await res.json();
    if (json && json.fixture === true) {
      fixtures.add(name);
      listeners.forEach(fn => fn(true));
    }
    return json;
  } catch {
    return null; // a missing or broken file degrades to "No data yet"
  }
}

export function load(name) {
  if (!cache.has(name)) cache.set(name, fetchJson(name).then(v => { ready.set(name, v); return v; }));
  return cache.get(name);
}
/** A file that has already arrived, without waiting; undefined until then. */
export const peek = name => ready.get(name);

export function reset() {
  cache.clear();
  ready.clear();
  fixtures.clear();
  listeners.forEach(fn => fn(false));
}

// ---------- frames ----------
const pick = (obj, path) => path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj);
const hasValue = arr => Array.isArray(arr) && arr.some(v => v !== null && v !== undefined);

/** A frame: { dates: [ISO day], col(path) -> array | null, grain }. Daily frames are re-indexed to a gap-free calendar. */
export function frame(file, shape = 'columnar') {
  if (!file) return null;
  let dates, get;
  switch (shape) {
    case 'periods': {
      const p = file.periods || [];
      dates = p.map(r => r.end);
      get = path => p.map(r => (r[path] === undefined ? null : r[path]));
      return fixed(dates, get, 'p', { starts: p.map(r => r.start) });
    }
    case 'monthly': {
      const m = file.monthly || {};
      dates = (m.months || []).map(s => s + '-01');
      return fixed(dates, path => m[path] || null, 'm');
    }
    case 'points': {
      const p = file.points || [];
      return fixed(p.map(r => r.date), path => p.map(r => (r[path] === undefined ? null : r[path])), 'wk', { weeks: p.map(r => r.week) });
    }
    case 'launch': {
      const all = new Set();
      (file.launch || []).forEach(l => (l.dates || []).forEach(d => all.add(d)));
      dates = [...all].sort();
      const ix = new Map(dates.map((d, i) => [d, i]));
      get = path => {
        const l = (file.launch || [])[+path.split('.')[1]];
        if (!l) return null;
        const out = new Array(dates.length).fill(null);
        (l.dates || []).forEach((d, i) => { out[ix.get(d)] = l.history[i]; });
        return out;
      };
      return daily(dates, get);
    }
    case 'peers':
      return daily(file.dates || [], path => (path === 'variational' ? file.variational : (file.venues[+path.split('.')[1]] || {}).values) || null);
    case 'fdvhist': {
      const h = (file.fdv && file.fdv.history) || {};
      return daily(h.dates || [], path => h[path] || null);
    }
    default: {
      // 'ends:<key>' reads a block of running totals aligned to report period ends (financials.cumulative)
      if (shape.startsWith('ends:')) {
        const r = file[shape.slice(5)] || {};
        return fixed(Array.isArray(r.ends) ? r.ends : [], path => (Array.isArray(r[path]) ? r[path] : null), 'p');
      }
      // 'sub:<key>' reads a block that carries its own dates (tradfi.listed)
      const root = shape.startsWith('sub:') ? file[shape.slice(4)] || {} : file;
      // a block of half-hourly snapshots carries `ts` in place of `dates`
      if (!root.dates && Array.isArray(root.ts)) return stamped(root.ts, path => { const v = pick(root, path); return Array.isArray(v) ? v : null; });
      return daily(root.dates || [], path => { const v = pick(root, path); return Array.isArray(v) ? v : null; });
    }
  }
}

function fixed(dates, get, grain, extra = {}) {
  const memo = new Map();
  return {
    dates, grain, ...extra,
    col(path) { if (!memo.has(path)) { const a = get(path); memo.set(path, hasValue(a) ? a : null); } return memo.get(path); },
  };
}

// Half-hourly snapshots (`2026-11-22T07:30Z`), grain 'i'. The frame is laid on a gap-free half-hour grid so the
// x-axis is true to time: each snapshot takes its nearest slot (the later one when two share a slot) and the
// slot keeps the snapshot's own stamp. `absent[i]` marks a slot no snapshot fell in, as distinct from a snapshot
// that holds a null (a closed market): a line may run across the first, never across the second.
function stamped(ts, get) {
  const STEP = 30, mins = ts.map(s => Date.parse(s) / 60000);
  const ok = mins.length > 0 && mins.every((m, i) => Number.isFinite(m) && (i === 0 || m >= mins[i - 1]));
  const first = ok ? Math.round(mins[0] / STEP) : 0, n = ok ? Math.round(mins[mins.length - 1] / STEP) - first + 1 : 0;
  if (!ok || n > 4000) return fixed(ts, get, 'i');
  const pos = mins.map(m => Math.round(m / STEP) - first);
  const keys = Array.from({ length: n }, (_, i) => new Date((first + i) * STEP * 60000).toISOString().slice(0, 16) + 'Z');
  const absent = new Array(n).fill(true);
  pos.forEach((p, i) => { keys[p] = ts[i]; absent[p] = false; });
  return fixed(keys, path => {
    const a = get(path);
    if (!a) return a;
    const out = new Array(n).fill(null);
    for (let i = 0; i < pos.length; i++) out[pos[i]] = a[i] === undefined ? null : a[i];
    return out;
  }, 'i', { absent });
}

function daily(dates, get) {
  if (!dates.length) return fixed([], () => null, 'd');
  const first = dayNum(dates[0]), last = dayNum(dates[dates.length - 1]);
  const n = last - first + 1;
  const gapless = n === dates.length;
  const full = gapless ? dates : Array.from({ length: n }, (_, i) => isoDay(first + i));
  const pos = gapless ? null : dates.map(d => dayNum(d) - first);
  return fixed(full, path => {
    const a = get(path);
    if (!a || gapless) return a;
    const out = new Array(n).fill(null);
    for (let i = 0; i < pos.length; i++) out[pos[i]] = a[i] === undefined ? null : a[i];
    return out;
  }, 'd');
}

// ---------- half-hourly blocks ----------
const hasPoints = it => !!it && Array.isArray(it.ts) && it.ts.length > 0;

/** A file with half-hourly points and no daily date yet: a listing on its first UTC day. */
export const snapshotsOnly = file => !!file && Array.isArray(file.dates) && !file.dates.length && hasPoints(file.intraday);

/** The half-hourly block a snapshot-native card reads instead of the daily file, and the range it is read at:
 *  the file's `intraday` at 7D, and at every range, read as at 7D, when the file has no daily date yet.
 *  Null when the card reads the daily file. */
export function snapshotBlock(file, range) {
  if (!file || !hasPoints(file.intraday)) return null;
  if (range === '7d') return { block: file.intraday, range };
  return snapshotsOnly(file) ? { block: file.intraday, range: '7d' } : null;
}

// ---------- range windows ----------
/** Index window [i0, i1] for a range on a frame; fixed-grain frames keep at least minPoints. */
export function windowOf(fr, range, minPoints = 6) {
  const n = fr.dates.length;
  if (!n) return null;
  const i1 = n - 1;
  const days = RANGE_DAYS[range] ?? 90;
  if (days === Infinity) return { i0: 0, i1, clipped: false, want: null };
  const want = isoDay(dayNum(fr.dates[i1]) - days + 1);
  let i0 = fr.dates.findIndex(d => d >= want);
  if (i0 < 0) i0 = i1;
  if (fr.grain !== 'd') i0 = Math.min(i0, Math.max(0, n - minPoints));
  return { i0, i1, want, clipped: fr.dates[0] > want };
}

// Bars choose their period from the range: daily up to 90D, weekly at 180D and 1Y, monthly at All. Lines stay daily.
export const barGran = range => ({ '180d': 'w', '1y': 'w', all: 'm' }[range] || 'd');

/** Buckets for a window: [{ key, from, to, i0, i1 }] in frame indices. */
export function buckets(fr, win, gran) {
  const out = [];
  if (fr.grain !== 'd' || gran === 'd') {
    for (let i = win.i0; i <= win.i1; i++) {
      out.push({ key: fr.dates[i], from: fr.starts ? fr.starts[i] : fr.dates[i], to: fr.dates[i], i0: i, i1: i });
    }
    return out;
  }
  const keyOf = gran === 'w' ? weekStart : d => d.slice(0, 7) + '-01';
  let cur = null;
  for (let i = win.i0; i <= win.i1; i++) {
    const k = keyOf(fr.dates[i]);
    if (!cur || cur.key !== k) { cur = { key: k, from: fr.dates[i], to: fr.dates[i], i0: i, i1: i }; out.push(cur); }
    else { cur.to = fr.dates[i]; cur.i1 = i; }
  }
  return out;
}

/** Aggregate one column over buckets: 'sum' for flows, 'last' for stocks. */
export function aggregate(arr, bks, how) {
  if (!arr) return bks.map(() => null);
  return bks.map(b => {
    if (b.i0 === b.i1) { const v = arr[b.i0]; return v === undefined ? null : v; }
    if (how === 'sum') {
      let s = 0, any = false;
      for (let i = b.i0; i <= b.i1; i++) if (arr[i] != null) { s += arr[i]; any = true; }
      return any ? s : null;
    }
    for (let i = b.i1; i >= b.i0; i--) if (arr[i] != null) return arr[i];
    return null;
  });
}

export const sumRange = (arr, i0, i1) => {
  let s = 0, any = false;
  for (let i = Math.max(0, i0); i <= i1; i++) if (arr[i] != null) { s += arr[i]; any = true; }
  return any ? s : null;
};
export const lastAt = (arr, i) => { for (let j = i; j >= 0; j--) if (arr[j] != null) return { v: arr[j], i: j }; return null; };
export const firstIndex = arr => (arr ? arr.findIndex(v => v != null) : -1);
