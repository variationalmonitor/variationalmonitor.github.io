// The generic card renderer. A card is: title, ⓘ, view controls, one headline figure,
// plot, legend. Source and as-of time live in the ⓘ. Everything is driven by the catalog entry.

import { load, frame, windowOf, buckets, aggregate, barGran, sumRange, lastAt, firstIndex, snapshotBlock } from './data.js';
import { fmt, fmtDate, fmtDay, fmtPoint, fmtSpan, fmtMonth, fmtStamp, RANGE_LABEL, RANGE_DAYS, dayNum, isoDay } from './format.js';
import { Chart, colorOf, ramp, rampAt, theme, lockup, MARK_PATH } from './chart.js';
import { VENUE_COLOR, CLASSES } from './catalog.js';
import { h, icon, infoPop, menu, toast, download, copyText, toCsv } from './ui.js';
import { renderTable } from './table.js';

const pick = (obj, path) => path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj);
const hasValue = v => (Array.isArray(v) ? v.some(x => x != null) : v != null);
const lastIndex = arr => { for (let i = arr.length - 1; i >= 0; i--) if (arr[i] != null) return i; return -1; };
const bare = v => (v == null ? '—' : v.toFixed(1)); // a figure whose unit is in its column header
const RANGE_KEY = { '7d': '7D', '30d': '30D', '90d': '90D', '180d': '180D', '1y': '1Y', all: 'All' };
const TOGGLES = { share: 'Share', cum: 'Cumulative', venues: 'Venues' };
// headline rules that add up the selected range
const RANGE_SUMS = ['sum', 'sumStack', 'net'];
const refOf = app => ((app.meta && app.meta.data_as_of) || new Date().toISOString()).slice(0, 10);

/** Report periods that end inside the selected range, and the same number of periods before them. */
export function periodsIn(ends, range, ref) {
  const all = ends.map((_, i) => i);
  const days = RANGE_DAYS[range];
  if (!Number.isFinite(days)) return { idx: all, prev: [] };
  const from = isoDay(dayNum(ref) - days);
  const idx = all.filter(i => ends[i] > from && ends[i] <= ref);
  if (!idx.length) return { idx, prev: [] };
  const k = idx.length, a = idx[0];
  return { idx, prev: a - k >= 0 ? all.slice(a - k, a) : [] };
}

// ---------- per-card view state (kept for the session, restored from deep links) ----------
const states = new Map();
export function stateOf(entry) {
  if (!states.has(entry.id)) {
    const st = { share: false, cum: false, venues: false, table: false, iso: null, page: 0, events: entry.span === 12 };
    (entry.controls || []).forEach(c => { st[c.key] = c.default || c.options[0].v; });
    Object.assign(st, entry.defaults || {});
    states.set(entry.id, st);
  }
  return states.get(entry.id);
}
export function applyParams(entry, params) {
  const st = stateOf(entry);
  (entry.controls || []).forEach(c => { const v = params.get(c.key); if (v && (c.options.some(o => o.v === v) || (c.periods && /^\d{4}-\d{2}-\d{2}$/.test(v)))) st[c.key] = v; });
  (entry.toggles || []).forEach(k => { if (params.has(k)) st[k] = params.get(k) === '1'; });
  st.iso = null;
}
export function paramsOf(entry) {
  const st = stateOf(entry), out = [], d = entry.defaults || {};
  (entry.controls || []).forEach(c => { if (st[c.key] !== (c.default || c.options[0].v)) out.push([c.key, st[c.key]]); });
  (entry.toggles || []).forEach(k => { if (!!st[k] !== !!d[k]) out.push([k, st[k] ? '1' : '0']); });
  return out;
}

function resolve(entry, st, has) {
  let cfg = { ...entry };
  const shown = [];
  (entry.controls || []).forEach(c => {
    const opts = c.options.filter(o => !o.needs || has(o.needs, o.min || 1));
    const cur = opts.find(o => o.v === st[c.key]) || opts.find(o => o.v === (c.default || c.options[0].v)) || opts[0];
    if (cur && cur.set) cfg = { ...cfg, ...cur.set };
    cfg[c.key] = cur ? cur.v : null;
    if (opts.length > 1) shown.push({ ctl: c, opts, cur });
  });
  return { cfg, shown };
}

// ---------- series that come from the file rather than the catalog ----------
const PAIR_COLOR = { XAU: 'honey', XAG: 'blue', CL: 'pink', US500: 'teal' };
const CYCLE = ['blue', 'honey', 'teal', 'violet', 'pink'];
function dynamicSeries(cfg, file) {
  if (cfg.dynamic === 'classes') {
    // asset classes: the keys come from the file, in the catalog's base-to-top order; names and colours from CLASSES
    const root = cfg.shape && cfg.shape.startsWith('sub:') ? file[cfg.shape.slice(4)] || {} : file;
    const obj = cfg.classes ? pick(root, cfg.classes) || {} : root;
    return CLASSES.filter(([k]) => Array.isArray(obj[k])).map(([k, label, color]) => ({ col: cfg.classes ? `${cfg.classes}.${k}` : k, label, color }));
  }
  if (cfg.dynamic === 'tickers') {
    // the largest markets in the selection, named by the file. A swap scope carries two rates per ticker:
    // the rate longs pay (solid) and the rate shorts receive (same hue at 50%).
    const blk = pick(file, cfg.tickers) || {}, ts = (blk.tickers || []).slice(0, 5).map(String).filter(t => !t.includes('.'));
    if (blk.series) return ts.map((t, i) => ({ col: `${cfg.tickers}.series.${t}`, label: t, color: CYCLE[i] }));
    return ts.flatMap((t, i) => [
      { col: `${cfg.tickers}.long.${t}`, label: `${t}, longs pay`, color: CYCLE[i], lg: t, lgLabel: t, side: 'Longs pay', endLabel: false },
      { col: `${cfg.tickers}.short.${t}`, label: `${t}, shorts receive`, color: CYCLE[i], lg: t, lgLabel: t, side: 'Shorts receive', faint: true, endLabel: false },
    ]);
  }
  if (cfg.dynamic === 'peers') {
    return [{ col: 'variational', label: 'Omni', color: 'blue', strong: true, endLabel: true, lg: 'variational' }]
      .concat((file.venues || []).map((v, i) => ({ col: `venues.${i}`, label: String(v.name), color: 'grey', dim: true, endLabel: true, lg: 'others', lgLabel: 'Other venues' })));
  }
  if (cfg.dynamic === 'pairs') {
    return (file.pairs || []).map((p, i) => ({ col: `${cfg.prefix}.${p.key}`, label: String(p.label), color: PAIR_COLOR[p.key] || CYCLE[i % CYCLE.length] }));
  }
  if (cfg.dynamic === 'launch') {
    return (file.launch || []).slice(0, 5).map((l, i) => ({ col: `launch.${i}`, label: `By ${l.label}`, color: CYCLE[i] }));
  }
  return [];
}

// The bands of a scoped stack: market groups or asset classes, whichever the scope carries.
const BANDS = [
  ['crypto_majors', 'Crypto majors', 'blue'], ['other_crypto', 'Other crypto', 'violet'],
  ['tradfi_perp', 'TradFi perpetuals', 'pink'], ['tradfi_swap', 'Swaps', 'honey'], ['swap', 'Swaps', 'honey'], ['tradfi', 'TradFi', 'honey'],
].concat(CLASSES);
function bandSeries(cfg, file) {
  const obj = pick(file, `${cfg.bands}.bands`) || {};
  const known = BANDS.filter(([k]) => Array.isArray(obj[k])).map(([k, label, color]) => ({ col: `${cfg.bands}.bands.${k}`, label, color }));
  const rest = Object.keys(obj).filter(k => Array.isArray(obj[k]) && !BANDS.some(b => b[0] === k) && !k.includes('.')).map(k => ({ col: `${cfg.bands}.bands.${k}`, label: k, color: 'grey' }));
  return known.concat(rest);
}

// A total whose parts are known on some days only: the scope's bands (`bands: 'volume'`) or a named split
// (`splitBy: { total, parts }`). Days where the total is known and no part is draw as one grey `Not split` bar.
const NOT_SPLIT = 'notsplit';
function splitOf(cfg, file) {
  if (cfg.bands) return { total: `${cfg.bands}.total`, parts: bandSeries(cfg, file), min: 2 };
  if (cfg.splitBy && typeof cfg.splitBy === 'object') return { total: cfg.splitBy.total, parts: cfg.splitBy.parts, min: 1 };
  return null;
}

const running = vals => { let acc = 0, on = false; return vals.map(v => { if (v != null) { acc += v; on = true; } return on ? acc : null; }); };

function bucketLabel(b, gran, fr) {
  if (gran === 'd') return fmtDate(b.key);
  if (gran === 'i') return fmtPoint(b.key);
  if (gran === 'w' || gran === 'p') return fmtSpan(b.from, b.to);
  if (gran === 'wk') return `Week ${fr.weeks[b.i0]}, ${fmtDate(b.key)}`;
  if (fr.grain === 'm') return fmtMonth(b.key);
  const y = +b.key.slice(0, 4), mo = +b.key.slice(5, 7);
  const whole = b.from.slice(8) === '01' && +b.to.slice(8) === new Date(Date.UTC(y, mo, 0)).getUTCDate();
  return whole ? fmtMonth(b.key) : fmtSpan(b.from, b.to);
}

// What one bar is: `USD per day`, `per week`, `Share at day end`, `USD per two weeks`.
function unitLabelOf(unit, gran, { share, flow, cum }) {
  const period = { d: 'day', w: 'week', m: 'month', p: 'two weeks', wk: 'week' }[gran];
  const word = share ? 'Share' : unit === 'usd' ? 'USD' : '';
  if (cum) return `Cumulative${word ? ' ' + word : ''}`;
  if (!share && ['bps', 'pct', 'rate', 'times'].includes(unit)) return gran === 'p' ? 'per two-week period' : `per ${period}`;
  return `${word}${word ? ' ' : ''}${flow ? `per ${period}` : `at ${period} end`}`;
}

const THIN = 14;
// Several lines with too few dates to read as lines: the latest date, one horizontal bar per series. Two
// series under one name (the two financing rates of a swap) are two bars beside one label.
function latestBars(cfg, fr, defs, cols, li) {
  const at = k => (cols[k][li] == null ? null : cols[k][li] * (defs[k].times || 1));
  const groups = [];
  defs.forEach((d, k) => {
    const id = d.lg || d.col;
    let g = groups.find(x => x.id === id);
    if (!g) groups.push(g = { id, label: d.lgLabel || d.label, parts: [] });
    g.parts.push({ label: d.label, side: d.side, v: at(k) });
  });
  const two = groups.length > 0 && groups.every(g => g.parts.length === 2 && g.parts[0].side && g.parts[1].side);
  const rows = (two ? groups : groups.flatMap(g => g.parts.map(p => ({ label: g.parts.length > 1 ? p.label : g.label, parts: [p] })))).filter(g => g.parts.some(p => p.v != null));
  if (!rows.length) return null;
  const name = cfg.valueName || 'Value', names = two ? rows[0].parts.map(p => p.side) : null, blue = colorOf('blue'), date = fr.dates[li];
  return {
    type: 'hbar', cats: rows.map(r => r.label), values: rows.map(r => r.parts[0].v), values2: two ? rows.map(r => r.parts[1].v) : undefined, names,
    colors: rows.map(() => blue), unit: cfg.unit, name, extra: [], unitLabel: fmtPoint(date),
    legend: two ? names.map((n, k) => ({ id: `side${k}`, label: n, color: 'blue', opacity: k ? 0.5 : undefined, key: 'rect', fixed: true })) : [],
    plotH: rows.length * (two ? 46 : 36) + 26,
    table: () => ({
      cols: [{ label: 'Market' }].concat((names || [name]).map(n => ({ label: n, num: true }))),
      rows: rows.map(r => [r.label].concat(r.parts.map(p => fmt(p.v, cfg.unit)))),
      raw: [['date', 'market'].concat((names || [name]).map(n => n.toLowerCase().replace(/ /g, '_')))].concat(rows.map(r => [date, r.label].concat(r.parts.map(p => p.v)))),
    }),
  };
}

// ---------- the time-series model ----------
export function buildTime(cfg, file, st, app) {
  const fr = frame(file, cfg.shape);
  if (!fr || !fr.dates.length) return null;
  const range = app.G.range;
  const canCum = (cfg.toggles || []).includes('cum'), canShare = (cfg.toggles || []).includes('share');
  let cumOn = st.cum && canCum;
  let defs = cfg.series || (cfg.dynamic ? dynamicSeries(cfg, file) : []);
  defs = (defs || []).filter(d => fr.col(d.col));
  // a series' values: a column of the file, or one derived here (Not split)
  const colOf = d => d.arr || fr.col(d.col);

  // A total with parts (scope bands, the crypto and TradFi split): drawn as a stack of the parts on the days
  // that have them and one grey Not split bar on the days that do not. With no part known inside the range,
  // the total is drawn alone. A split never falls back to a cross-section.
  let stackOn = !!cfg.stack, totalCol = null, single = false;
  const sp = splitOf(cfg, file);
  if (sp) {
    const tc = fr.col(sp.total);
    if (!tc && cfg.bands) return null;
    if (tc) {
      const w0 = windowOf(fr, range, cfg.minPoints), a = Math.max(w0.i0, firstIndex(tc)), z = Math.min(w0.i1, lastIndex(tc));
      const parts = sp.parts.filter(d => fr.col(d.col)), pc = parts.map(d => fr.col(d.col));
      let seen = false;
      for (let i = a; i <= z && !seen; i++) if (tc[i] != null && pc.some(c => c[i] != null)) seen = true;
      if (parts.length >= sp.min && seen) {
        const rest = tc.map((t, i) => (t != null && !pc.some(c => c[i] != null) ? t : null));
        defs = parts.concat(rest.some(v => v != null) ? [{ col: NOT_SPLIT, label: 'Not split', color: 'grey', arr: rest }] : []);
        stackOn = true; totalCol = tc;
      } else if (cfg.bands) {
        defs = [{ col: sp.total, label: cfg.valueName || 'Total', color: 'blue' }]; single = true;
      }
    }
  }
  if (!defs.length) return null;

  // window: the global range, clamped to where these series have data
  const win = windowOf(fr, range, cfg.minPoints);
  // paired bars compare two sources period by period, so they need both
  if (cfg.paired && defs.length < (cfg.series || []).length) return null;
  const cols = defs.map(colOf);
  // the columns that decide whether a date holds a value: the total behind a split, Omni alone in its share view
  const keyCols = totalCol ? [totalCol] : cfg.dynamic === 'peers' && !st.venues ? cols.slice(0, 1) : cols;
  const has = i => (cfg.paired ? keyCols.every(c => c[i] != null) : keyCols.some(c => c[i] != null));
  const first = (cfg.paired ? Math.max : Math.min)(...keyCols.map(firstIndex)), last = Math.max(...keyCols.map(lastIndex));
  // a week of snapshots runs to the latest snapshot, so a market that is closed now shows as closed
  const i1 = fr.grain === 'i' ? win.i1 : Math.min(win.i1, last);
  let i0 = Math.max(win.i0, first);
  if (i0 > i1) i0 = Math.max(first, i1 - (win.i1 - win.i0));

  // The days inside the range that hold a value, and where their trailing run of consecutive days starts. Days that
  // skip a day, with a trailing run shorter than 14, are sampled days: a running total over them means nothing, so
  // the card then has no Cumulative view and draws its values as they are, whatever the toggle says.
  const days = [];
  if (fr.grain === 'd') for (let i = i0; i <= i1; i++) if (has(i)) days.push(i);
  let run = days.length - 1;
  while (run > 0 && days[run - 1] === days[run] - 1) run--;
  const scattered = run > 0 && days.length - run < THIN;
  if (scattered) cumOn = false;

  // Thin series. One series with fewer than three points is its headline figure and date, over the bars the
  // entry names for that case (the two sides behind a ratio) or with no plot; several lines with fewer than 14
  // points draw their latest point as one bar per series. Both become lines as points arrive. Daily series
  // count the dates the file holds; half-hourly snapshots count the points in view.
  if (cfg.thin !== false && (fr.grain === 'd' || fr.grain === 'i') && !cumOn && !stackOn && !sp && !cfg.mirror && !cfg.paired && !cfg.markers && cfg.dynamic !== 'peers' && (cfg.mark || 'line') === 'line') {
    const cs = defs.map(colOf);
    const from = fr.grain === 'i' ? windowOf(fr, range, 0).i0 : 0;
    let dated = 0, li = -1;
    for (let i = from; i < fr.dates.length; i++) if (cs.some(c => c[i] != null)) { dated++; li = i; }
    if (defs.length === 1 && li >= 0 && dated < 3) {
      const v = cs[0][li] * (defs[0].times || 1), date = fr.dates[li];
      const headline = { value: v, unit: cfg.unit, label: fmtPoint(date), delta: null };
      const tb = cfg.thinBars, sides = tb ? tb.series.map(s => ({ ...s, v: (fr.col(s.col) || [])[li] })).filter(s => s.v != null) : [];
      if (sides.length > 1) {
        return {
          type: 'hbar', cats: sides.map(s => s.label), values: sides.map(s => s.v), colors: sides.map(s => colorOf(s.color)), unit: tb.unit, name: tb.name,
          extra: [], legend: [], headline, plotH: sides.length * 36 + 12, fit: true,
          table: () => ({
            cols: [{ label: 'Side' }, { label: tb.name, num: true }], rows: sides.map(s => [s.label, fmt(s.v, tb.unit)]),
            raw: [['date', defs[0].label].concat(sides.map(s => s.label.toLowerCase()))].concat([[date, v].concat(sides.map(s => s.v))]),
          }),
        };
      }
      return { type: 'figure', legend: [], headline, table: () => ({ cols: [], rows: [], raw: [['date', defs[0].label], [date, v]] }) };
    }
    if (defs.length > 1 && li >= 0 && dated < THIN) return latestBars(cfg, fr, defs, cs, li);
  }

  let baseMark = cfg.mark || 'line';
  // a banded card with no bands to draw may name another mark for its one series (open interest: a line)
  if (single && cfg.singleMark) baseMark = cfg.singleMark;
  // half-hourly snapshots are levels read through the day: a line, whatever the daily mark
  if (fr.grain === 'i' && baseMark === 'bar') baseMark = 'line';
  // bars take their period from the range; lines (and cumulative views) stay daily
  const barLike = baseMark === 'bar' && !cumOn;
  let gran = fr.grain === 'd' ? (barLike ? barGran(range) : 'd') : fr.grain;

  // A chart never shows an empty slot. Of the dates that hold a value inside the range:
  //  1. when they cover every period from the first to the last (a day, or the week or month one bar stands
  //     for; a zero is a value), the time axis is drawn as usual;
  //  2. otherwise the trailing run of consecutive dates ending at the latest, if it holds 14 or more, is drawn
  //     alone on a time axis that starts at its first date;
  //  3. otherwise every dated value takes its own evenly spaced slot on a category axis, in date order, unbinned.
  // Report periods, months and weeks of a fixed-period file draw the periods that hold a value; half-hourly
  // snapshots are drawn as they are.
  let bks, sampled = 0;
  if (fr.grain === 'd') {
    if (!days.length) return null;
    const z = days[days.length - 1];
    // a young series is never drawn as one or two coarse buckets: it steps down to a finer period
    const fit = a => {
      let g = gran, b = buckets(fr, { i0: a, i1: z }, g);
      while (g !== 'd' && b.length < 3) { g = g === 'm' ? 'w' : 'd'; b = buckets(fr, { i0: a, i1: z }, g); }
      return { g, b };
    };
    const filled = b => { for (let i = b.i0; i <= b.i1; i++) if (has(i)) return true; return false; };
    let f = fit(days[0]);
    if (!f.b.every(filled)) f = days.length - run >= THIN ? fit(days[run]) : null;
    if (f) { gran = f.g; bks = f.b; }
    else {
      gran = 'd'; sampled = days.length;
      bks = days.map(i => ({ key: fr.dates[i], from: fr.dates[i], to: fr.dates[i], i0: i, i1: i }));
    }
  } else {
    bks = buckets(fr, { i0, i1 }, gran);
    if (fr.grain !== 'i') bks = bks.filter(b => has(b.i0));
    if (!bks.length) return null;
  }
  const agg = cfg.agg || 'last';
  // a stack read at period ends takes every part from one day: the last day in the period that holds a value
  const at = agg === 'last' && stackOn && bks.some(b => b.i1 > b.i0)
    ? bks.map(b => { for (let i = b.i1; i >= b.i0; i--) if (cols.some(c => c[i] != null)) return i; return -1; }) : null;
  const agged = c => (at ? at.map(i => (i < 0 || c[i] == null ? null : c[i])) : aggregate(c, bks, agg));

  let series = defs.map((d, k) => {
    const v = agged(cols[k]);
    return {
      id: d.col, label: d.label, color: d.color, neg: !!d.neg, mark: d.mark || baseMark, step: !!cfg.step,
      lg: d.lg || d.col, lgLabel: d.lgLabel || d.label, dim: d.dim, strong: d.strong, endLabel: d.endLabel,
      faint: !!d.faint,
      values: d.times ? v.map(x => (x == null ? null : x * d.times)) : v,
    };
  });
  // a part with no value in view has no segment and no legend entry
  if (stackOn) series = series.filter(s => s.values.some(v => v != null));
  if (!series.length) return null;
  let unit = cfg.unit, stacked = stackOn, mirror = !!cfg.mirror, total = null, yMin = cfg.yMin, yMax = cfg.yMax;
  let tipExtra = null;

  if (cfg.tip) {
    // figures behind the plotted one, for the tooltip only (the two sides behind a ratio, and their difference)
    const of = c => aggregate(fr.col(c), bks, agg);
    tipExtra = cfg.tip.map(x => {
      const a = of(x.minus ? x.minus[0] : x.col), b = x.minus ? of(x.minus[1]) : null;
      return { label: x.label, unit: x.unit || 'usd', values: b ? a.map((v, k) => (v == null || b[k] == null ? null : v - b[k])) : a };
    });
  }
  let shareAxis = false, cumBars = false;

  // Period averages rather than true daily values (a reported two-week total averaged per day): the bar series
  // that holds those days (the one series, or Not split beneath a stack) is drawn at 55% under its own legend entry.
  if (cfg.averaged && !cumOn) {
    const base = series.find(s => s.id === NOT_SPLIT) || (!stacked && series.length === 1 ? series[0] : null);
    const src = fr.col(cfg.averaged.col), code = cfg.averaged.code != null ? cfg.averaged.code : (file.sources || []).indexOf(cfg.averaged.source);
    if (base && base.mark === 'bar' && src && code >= 0) {
      const isAvg = bks.map(b => { let any = false; for (let i = b.i0; i <= b.i1; i++) { if (src[i] == null) continue; if (src[i] !== code) return false; any = true; } return any; });
      if (isAvg.some(Boolean)) {
        series.splice(series.indexOf(base), 1, ...[
          { ...base, values: base.values.map((v, b) => (isAvg[b] ? null : v)) },
          { ...base, id: base.id + ':avg', lg: 'avg', label: cfg.averaged.label, lgLabel: cfg.averaged.label, opacity: 0.55, values: base.values.map((v, b) => (isAvg[b] ? v : null)) },
        ].filter(s => s.values.some(v => v != null)));
      }
    }
  }

  if (cumOn) {
    if (cfg.cumulative) {
      const c = cfg.cumulative;
      series = [{ id: c.col, label: c.label, lgLabel: c.label, lg: 'cum', color: 'blue', mark: 'area', values: aggregate(fr.col(c.col), bks, 'last') }];
      stacked = false; mirror = false;
    } else if (cfg.net) {
      const p = aggregate(fr.col(cfg.net.plus), bks, 'sum'), q = aggregate(fr.col(cfg.net.minus), bks, 'sum');
      series = [{ id: 'net', label: cfg.net.label, lgLabel: cfg.net.label, lg: 'net', color: 'blue', mark: 'area', values: running(p.map((v, b) => (v == null && q[b] == null ? null : (v || 0) - (q[b] || 0)))) }];
      stacked = false; mirror = false;
    } else {
      series.forEach(s => { s.values = running(s.values); });
      const nonneg = series.every(s => s.values.every(v => v == null || v >= 0));
      cumBars = stacked && !nonneg;
      series.forEach(s => { s.mark = cumBars ? 'bar' : 'area'; s.step = false; });
    }
  }

  if (cfg.dynamic === 'peers' && !st.venues) {
    // the default view is Variational's share as one line; the venue levels are the alternate
    const den = bks.map((_, b) => series.reduce((a, s) => a + (s.values[b] || 0), 0));
    const v = series[0].values.map((x, b) => (x == null || !den[b] ? null : x / den[b]));
    series = [{ id: 'share', label: "Omni's share", lgLabel: "Omni's share", lg: 'share', color: 'blue', mark: 'line', values: v }];
    unit = 'pct';
  } else if (st.share && canShare && (stacked || mirror)) {
    const den = bks.map((_, b) => series.reduce((a, s) => a + (s.values[b] || 0), 0));
    series.forEach(s => { s.values = s.values.map((v, b) => (v == null || !den[b] ? null : v / den[b])); });
    unit = 'pct'; shareAxis = true; // shares of a whole tick in quarters
    if (stacked && series.every(s => s.values.every(v => v == null || v >= 0))) { yMin = 0; yMax = 1; }
  } else if (stacked) {
    total = { label: cfg.totalLabel || 'Total', values: bks.map((_, b) => { const p = series.map(s => s.values[b]).filter(v => v != null); return p.length ? p.reduce((a, v) => a + v, 0) : null; }) };
  } else if (mirror && cfg.net && series.length === 2) {
    total = { label: cfg.net.label, values: bks.map((_, b) => { const a = series[0].values[b], c = series[1].values[b]; return a == null && c == null ? null : (a || 0) - (c || 0); }) };
  }

  // hollow markers for a second, sparser source (reported figures)
  if (cfg.markers && !cumOn) {
    const vals = bks.map(() => null);
    (file[cfg.markers.list] || []).forEach(r => {
      const d = r[cfg.markers.date];
      const b = bks.findIndex(k => d >= k.from && d <= k.to);
      if (b >= 0) vals[b] = r[cfg.markers.value];
    });
    if (vals.some(v => v != null)) series.push({ id: 'markers', label: cfg.markers.label, lgLabel: cfg.markers.label, lg: 'markers', color: cfg.markers.color, mark: 'marker', values: vals });
  }

  // drawing values: below-zero series are negated; sparse lines show their observations
  const lines = series.filter(s => s.mark === 'line' && !s.dim);
  series.forEach(s => {
    s.stack = stacked && s.mark !== 'marker' && !cfg.paired;
    s.draw = s.values.map(v => (v == null ? null : s.neg ? -v : v));
    if (s.mark === 'line' || s.mark === 'area') {
      const a = s.draw.findIndex(v => v != null), z = lastIndex(s.draw);
      // observations are marked while they are few; a long sparse history reads as a line
      const seen = s.draw.filter(v => v != null).length;
      s.gappy = fr.grain === 'i' ? seen <= 20 : a >= 0 && (a === z || seen < THIN || (seen <= 24 && s.draw.slice(a, z + 1).some(v => v == null)));
      // Snapshots are not joined across a stretch without quotes (a closed swap). Across slots where no snapshot
      // was taken the line runs straight from one reading to the next, as it would on a time axis: `path` holds
      // those in-between positions for drawing only; values, tooltips and markers stay on the readings.
      if (fr.grain === 'i') {
        s.breaks = true;
        if (fr.absent) {
          const path = s.draw.slice();
          let p = -1; // the last reading before the current run of absent slots
          for (let k = 0; k < path.length; k++) {
            if (s.draw[k] != null) {
              if (p >= 0 && k - p > 1) for (let j = p + 1; j < k; j++) path[j] = s.draw[p] + (s.draw[k] - s.draw[p]) * (j - p) / (k - p);
              p = k;
            } else if (!fr.absent[bks[k].i0]) p = -1;
          }
          s.path = path;
        }
      }
      if (s.endLabel === undefined && !stacked && s.mark === 'line' && lines.length > 1 && lines.length <= 4) s.endLabel = true;
    }
    if (mirror && s.mark === 'area') s.wash = 0.16;
  });

  // legend: one entry per legend group; click isolates
  const legend = [];
  series.forEach(s => {
    if (!legend.some(l => l.id === s.lg)) legend.push({ id: s.lg, label: s.lgLabel || s.label, color: s.color, opacity: s.opacity, key: s.mark === 'marker' ? 'ring' : s.mark === 'line' ? 'line' : 'rect' });
  });
  const iso = legend.some(l => l.id === st.iso) ? st.iso : null;
  series.forEach(s => { s.hidden = !!iso && s.lg !== iso; });

  // events inside the window; on a category axis only an event on one of the sampled dates
  let events = [];
  if (st.events && app.events && bks.length > 1 && fr.grain !== 'i') {
    events = app.events.map(e => {
      let idx = -1;
      if (sampled) idx = bks.findIndex(b => b.key === e.date);
      else {
        for (let b = 0; b < bks.length; b++) if (bks[b].from <= e.date) idx = b;
        if (e.date < bks[0].from || e.date > bks[bks.length - 1].to) idx = -1;
      }
      return idx < 0 ? null : { index: idx, label: String(e.label) };
    }).filter(Boolean);
  }

  const hasBars = series.some(s => s.mark === 'bar');
  let unitLabel = cfg.unitLabel === false ? null : hasBars ? unitLabelOf(cfg.unit, gran, { share: shareAxis, flow: agg === 'sum' || fr.grain !== 'd', cum: cumBars }) : cfg.unitLabel || null;
  // a category axis of sampled dates says how many it holds: `USD per day, 13 sampled days`
  if (sampled) unitLabel = [unitLabel, `${sampled} sampled day${sampled === 1 ? '' : 's'}`].filter(Boolean).join(', ');
  // ticks name the day, with its year when the dates run across calendar years; a tooltip names the full date,
  // and a day whose day-end reading was not taken at midnight says so
  const years = sampled && bks[0].key.slice(0, 4) !== bks[bks.length - 1].key.slice(0, 4);
  const approx = fr.grain === 'd' ? fr.col('approx') : null;
  return {
    type: 'time', grain: gran, unit, stacked, total, share: shareAxis, absAxis: mirror && !stacked, yMin, yMax,
    paired: !!cfg.paired, single, tipExtra, range, cat: !!sampled, sampled, scattered, unitLabel,
    x: bks.map(b => ({
      key: b.key, from: b.from, to: b.to,
      label: bucketLabel(b, gran, fr) + (approx && b.i0 === b.i1 && approx[b.i0] === true ? ', approximate' : ''),
      tick: sampled ? (years ? fmtDate(b.key) : fmtDay(b.key)) : undefined,
    })),
    // a legend is not drawn when every series already carries its name at the line's end
    series, legend: legend.length > 1 && !series.every(s => s.endLabel) ? legend : [], iso, events,
    table: () => timeTable({ gran, unit, series, total, x: bks }),
  };
}

function timeTable({ gran, unit, series, total, x }) {
  const first = gran === 'p' ? 'Period end' : gran === 'w' || gran === 'wk' ? 'Week' : gran === 'm' ? 'Month' : gran === 'i' ? 'Time, UTC' : 'Date';
  const cols = [{ label: first }].concat(series.map(s => ({ label: s.label, num: true })), total ? [{ label: total.label, num: true }] : []);
  const rows = [], raw = [];
  for (let b = x.length - 1; b >= 0; b--) {
    const vals = series.map(s => s.values[b]).concat(total ? [total.values[b]] : []);
    if (vals.every(v => v == null)) continue; // days without an observation are not rows
    rows.push([gran === 'm' ? fmtMonth(x[b].key) : gran === 'i' ? fmtPoint(x[b].key).replace(' UTC', '') : fmtDate(x[b].key)].concat(vals.map(v => fmt(v, unit))));
    raw.push([x[b].key].concat(vals));
  }
  return { cols, rows, raw: [cols.map(c => c.label)].concat(raw.reverse()) };
}

// ---------- category forms ----------
// Ordered buckets as horizontal bars with the value at the tip (pool tiers, FDV strikes, launch dates).
function buildHbar(cfg, file) {
  const list = (pick(file, cfg.list) || []).slice(0, 12);
  if (!list.length) return null;
  const cats = list.map(r => `${cfg.prefix || ''}${r.label}`);
  const values = list.map(r => r[cfg.field]);
  const other = cfg.other ? { label: cfg.other.name, unit: cfg.other.unit, values: list.map(r => r[cfg.other.field]) } : null;
  const model = {
    type: 'hbar', cats, values, unit: cfg.unit, name: cfg.name,
    colors: cfg.oneHue ? list.map(() => colorOf('blue')) : ramp(list.length), // date buckets are one series, one hue
    max: cfg.unit === 'pct' ? 1 : undefined, extra: other ? [other] : [],
    plotH: list.length * (list.length > 7 ? 30 : 40) + 12, fit: list.length < 5, legend: [],
  };
  model.table = () => {
    const cols = [{ label: cfg.category || 'Bucket' }, { label: cfg.name, num: true }].concat(other ? [{ label: other.label, num: true }] : []);
    return {
      cols,
      rows: cats.map((c, i) => [c, fmt(values[i], cfg.unit)].concat(other ? [fmt(other.values[i], other.unit)] : [])),
      raw: [cols.map(c => c.label)].concat(cats.map((c, i) => [c, values[i]].concat(other ? [other.values[i]] : []))),
    };
  };
  return model;
}

// Round-trip cost per market: one dot per venue, joined by a hairline. The pipeline pools the matched
// samples per range; the browser only picks the block for the global range. Venue keys come from the file.
const venuesOf = file => (Array.isArray(file.venues) && file.venues.length ? file.venues : [{ key: 'hl', label: 'Hyperliquid' }]);
function buildDots(cfg, file, st, app) {
  const markets = file.markets || [];
  const block = (file.range || {})[RANGE_KEY[app.G.range]];
  if (!markets.length || !block) return null;
  const venues = venuesOf(file), fees = cfg.fees === '1';
  const rows = markets.map(m => {
    const d = (block[m.key] || {})[cfg.size] || {};
    const other = venues.map(v => {
      const book = d[`${v.key}_book`], fee = d[`${v.key}_fee`];
      const withFee = fees && fee != null;
      return { book: book ?? null, fee: fee ?? null, value: book == null ? null : withFee ? book + fee : book, label: fees && fee == null ? `${v.label}, fee not included` : String(v.label) };
    });
    const ok = d.omni != null && other.some(o => o.value != null);
    return { label: String(m.omni || m.label), omni: ok ? d.omni : null, other, ok, n: d.n ?? null };
  });
  const good = rows.filter(r => r.ok);
  const cheapest = good.filter(r => r.other.every(o => o.value == null || r.omni < o.value)).length;
  const model = {
    type: 'dots', unit: 'bps', cats: rows.map(r => r.label),
    tipTitles: markets.map(m => `${m.omni}, ${m.label}`),
    rowOk: rows.map(r => r.ok), missing: 'No matched quotes',
    series: [{ label: 'Omni', color: 'blue', values: rows.map(r => r.omni) }].concat(venues.map((v, k) => ({
      label: String(v.label), labels: rows.map(r => r.other[k].label), color: VENUE_COLOR[v.key] || 'pink',
      values: rows.map(r => (r.ok ? r.other[k].value : null)),
    }))),
    extra: [{ label: 'Matched samples', unit: 'count', values: rows.map(r => r.n) }],
    plotH: rows.length * 34 + 30,
    headline: good.length ? { value: cheapest, text: `${cheapest} of ${good.length}`, unit: 'count', label: cfg.headline.label } : null,
    // fee notes appear only inside the ⓘ: one per venue when the file lists venues, else the file's single note
    notes: venues.some(v => v.fee_note) ? venues.map(v => v.fee_note).filter(Boolean) : [file.fee_note].filter(Boolean),
  };
  // a venue with no value in this range and size has no dots, so it has no legend entry either
  model.series = model.series.filter((s, i) => i === 0 || s.values.some(v => v != null));
  model.legend = model.series.map((s, i) => ({ id: 's' + i, label: s.label, color: s.color, key: 'dot', fixed: true }));
  model.table = () => {
    const cols = [{ label: 'Market' }, { label: 'Omni, bps', num: true }].concat(venues.map(v => ({ label: `${v.label}, bps`, num: true })), [{ label: 'Matched samples', num: true }]);
    return {
      cols,
      rows: rows.map(r => [r.label, bare(r.omni)].concat(r.other.map(o => bare(r.ok ? o.value : null)), [fmt(r.n, 'count')])),
      raw: [['market', 'omni_market', 'range', 'size', 'omni_bps'].concat(venues.flatMap(v => [`${v.key}_book_bps`, `${v.key}_fees_bps`]), ['matched_samples'])]
        .concat(markets.map((m, i) => [m.label, m.omni, RANGE_KEY[app.G.range], cfg.size, rows[i].omni].concat(rows[i].other.flatMap(o => [rows[i].ok ? o.book : null, o.fee]), [rows[i].n]))),
    };
  };
  return model;
}

function buildSegmented(cfg, file) {
  const list = file.allocation || [];
  if (!list.length) return null;
  const segs = list.map((r, i) => ({ label: String(r.label), pct: r.pct, color: (cfg.colors || [])[i] || 'grey' }));
  return {
    type: 'segmented', segs, fit: true,
    legend: segs.map((s, i) => ({ id: 's' + i, label: s.label, color: s.color, key: 'rect', fixed: true })),
    table: () => ({
      cols: [{ label: 'Allocation' }, { label: 'Share of supply', num: true }],
      rows: segs.map(s => [s.label, fmt(s.pct, 'pct')]),
      raw: [['allocation', 'share']].concat(segs.map(s => [s.label, s.pct])),
    }),
  };
}

// A stat tile: one figure, with the title above and a label beneath. A card that is a bare figure is always a tile.
function buildStat(cfg, file) {
  if (!cfg.figure) return { type: 'stat', legend: [], fit: true }; // the figure comes from the entry's headline rule
  const f = cfg.figure, v = pick(file, f.path);
  if (v == null) return null;
  const n = f.of ? pick(file, f.of) : null;
  if (f.of && n == null) return null;
  const when = f.date ? pick(file, f.date) : null;
  const label = (f.label || '').replace('{date}', when ? fmtDate(String(when).slice(0, 10)) : '');
  return {
    type: 'stat', legend: [], fit: true,
    headline: { value: v, text: f.of ? `${fmt(v, 'count')} of ${fmt(n, 'count')}` : null, unit: f.unit, label, delta: null },
    table: () => ({ cols: [], rows: [], raw: [[cfg.title, 'label'], [f.of ? `${v} of ${n}` : v, label]] }),
  };
}

// Small multiples: one time panel per entry in cfg.panels, each on its own y-scale, sharing the card's range.
function buildMultiples(cfg, file, st, app) {
  const sub = { ...st, share: false, cum: false, events: false, iso: null };
  const base = { ...cfg, form: 'time', panels: null };
  const panels = cfg.panels.map(p => ({
    title: p.title, color: p.series[0].color,
    model: buildTime({ ...base, ...p, series: p.series.map(s => ({ ...s, endLabel: false })) }, file, sub, app),
  })).filter(p => p.model);
  if (!panels.length) return null;
  panels.forEach(p => { p.model.unitLabel = null; }); // the panel title already says what is plotted
  const all = buildTime({ ...base, series: cfg.panels.flatMap(p => p.series) }, file, sub, app);
  return { type: 'multiples', panels, legend: [], table: all ? all.table : null };
}

// Cohort heatmap: rows are first-funded months, columns months since, cells the share still active.
function buildHeatmap(cfg, file) {
  const rows = (file.cohorts || []).filter(c => c && Array.isArray(c.active));
  if (!rows.length) return null;
  const width = Math.max(...rows.map(c => c.active.length));
  return {
    type: 'heatmap', rows, width, legend: [], fit: true,
    table: () => ({
      cols: [], rows: [],
      raw: [['first_funded', 'accounts'].concat(Array.from({ length: width }, (_, i) => `month_${i}`))].concat(rows.map(c => [c.month, c.size].concat(Array.from({ length: width }, (_, i) => c.active[i] ?? null)))),
    }),
  };
}

// A ranked list as a paged table (largest pools, referral claimants).
function buildRows(cfg, file) {
  let list = pick(file, cfg.list);
  if (!Array.isArray(list) || !list.length) return null;
  if (cfg.latestFirst) list = [...list].reverse();
  return {
    type: 'rows', list, legend: [], fit: true,
    table: () => ({
      cols: [], rows: [],
      raw: [cfg.columns.map(c => c.label)].concat(list.map((r, i) => cfg.columns.map(c => (c.kind === 'rank' ? i + 1 : r[c.field])))),
    }),
  };
}

// Two dots per row with no figures on the plot (values live in the tooltip and the table view).
function pairModel(cfg, labels, titles, points, values, extra) {
  const ok = labels.map((_, i) => values.every(v => v[i] != null));
  const model = {
    type: 'dots', unit: cfg.unit, cats: labels, tipTitles: titles, rowOk: ok, missing: 'No matched quotes',
    series: points.map((p, k) => ({ label: p.label, color: p.color, values: values[k].map((v, i) => (ok[i] ? v : null)) })),
    extra: extra || [], plotH: labels.length * 34 + 30, headline: null,
  };
  model.legend = model.series.map((s, i) => ({ id: 's' + i, label: s.label, color: s.color, key: 'dot', fixed: true }));
  const u = cfg.unit === 'bps' ? ', bps' : '';
  model.table = () => ({
    cols: [{ label: 'Market' }].concat(points.map(p => ({ label: p.label + u, num: true })), (extra || []).map(x => ({ label: x.label, num: true }))),
    rows: labels.map((l, i) => [l].concat(values.map(v => (cfg.unit === 'bps' ? bare(v[i]) : fmt(v[i], cfg.unit))), (extra || []).map(x => fmt(x.values[i], x.unit)))),
    raw: [['market'].concat(points.map(p => p.label.toLowerCase().replace(/ /g, '_')), (extra || []).map(x => x.label.toLowerCase().replace(/ /g, '_')))]
      .concat(labels.map((l, i) => [l].concat(values.map(v => v[i]), (extra || []).map(x => x.values[i])))),
  });
  return model;
}

// Perpetual and swap on the same underlying: range medians from the pipeline, per metric.
function buildPairDots(cfg, file, st, app) {
  const pairs = file.pairs || [], block = (file.range || {})[RANGE_KEY[app.G.range]];
  if (!pairs.length || !block) return null;
  const values = cfg.points.map(p => pairs.map(x => { const v = (block[x.key] || {})[`${p.side}_${cfg.field}`]; return v == null ? null : v; }));
  if (!values.some(v => v.some(x => x != null))) return null;
  return pairModel(cfg, pairs.map(x => String(x.label)), pairs.map(x => `${x.label}, ${x.perp} and ${x.swap}`), cfg.points, values,
    [{ label: 'Snapshots', unit: 'count', values: pairs.map(x => (block[x.key] || {}).n ?? null) }]);
}

const buildDotForm = (cfg, file, st, app) => (cfg.dots === 'pairs' ? buildPairDots : buildDots)(cfg, file, st, app);

// Spreads paid to net profit as a horizontal waterfall. Totals are full bars from zero; deductions float from
// one total down to the next; the two parts of net profit sit beneath it (a negative part runs left of zero).
function buildWaterfall(cfg, file, st, app) {
  const P = file.periods || [];
  if (!P.length) return null;
  const ends = P.map(p => p.end);
  let idx = [P.length - 1];
  if (cfg.period === 'range') idx = periodsIn(ends, app.G.range, refOf(app)).idx;
  if (!idx.length) return null;
  else if (cfg.period && cfg.period !== 'latest' && ends.includes(cfg.period)) idx = [ends.indexOf(cfg.period)];
  const sum = f => idx.reduce((a, i) => a + (P[i][f] || 0), 0);
  const steps = cfg.steps.filter(sp => idx.some(i => P[i][sp.field] != null));
  if (!steps.length) return null;
  let level = 0, at = 0;
  const bars = steps.map((sp, k) => {
    const v = sum(sp.field), next = steps[k + 1];
    if (sp.kind === 'total') { level = v; at = 0; return { x0: 0, x1: v, color: colorOf('blue'), text: fmt(v, cfg.unit), link: next && next.kind === 'minus' ? v : null, raw: v }; }
    if (sp.kind === 'minus') { const b = { x0: level - v, x1: level, color: colorOf('grey'), text: fmt(-v, cfg.unit), link: level - v, raw: -v }; return b; }
    if (v < 0) return { x0: 0, x1: v, color: colorOf(sp.color), text: fmt(v, cfg.unit), link: null, raw: v };
    const b = { x0: at, x1: at + v, color: colorOf(sp.color), text: fmt(v, cfg.unit), link: next && next.kind === 'part' && sum(next.field) >= 0 ? at + v : null, raw: v };
    at += v;
    return b;
  });
  const a = P[idx[0]], z = P[idx[idx.length - 1]];
  const model = {
    type: 'waterfall', unit: cfg.unit, name: 'Reported', cats: steps.map(sp => sp.label), bars, legend: [], headline: null,
    unitLabel: idx.length === 1 ? `USD, ${fmtSpan(z.start || z.end, z.end)}` : `USD, ${idx.length} periods, ${fmtSpan(a.start || a.end, z.end)}`,
    plotH: steps.length * 40 + 26,
  };
  model.table = () => ({
    cols: [{ label: 'Line' }, { label: 'USD', num: true }],
    rows: steps.map((sp, k) => [sp.label, bars[k].text]),
    raw: [['from', 'to', 'line', 'usd']].concat(steps.map((sp, k) => [a.start || a.end, z.end, sp.label, bars[k].raw])),
  });
  return model;
}

// The statement: line items as rows, periods as columns (latest first), with `Selected range` and `Latest × 26`.
function buildStatement(cfg, file, st, app) {
  const P = file.periods || [];
  if (!P.length) return null;
  const w = periodsIn(P.map(p => p.end), app.G.range, refOf(app)), last = P[P.length - 1], ann = file.annualised || {};
  const rev = [...P].reverse();
  const add = (f, ix) => { const xs = ix.map(i => P[i][f]).filter(v => v != null); return xs.length ? xs.reduce((a, v) => a + v, 0) : null; };
  const ratio = (l, x, y) => (x != null && y ? x / y * l.ratio[2] : null);
  const rows = cfg.lines.map(l => {
    const of = p => (p[l.field] != null ? p[l.field] : l.ratio ? ratio(l, p[l.ratio[0]], p[l.ratio[1]]) : null);
    const inRange = l.ratio ? ratio(l, add(l.ratio[0], w.idx), add(l.ratio[1], w.idx)) : add(l.field, w.idx);
    const annual = l.ratio ? of(last) : ann[l.field] != null ? ann[l.field] : last[l.field] != null ? last[l.field] * 26 : null;
    const raw = [inRange, annual].concat(rev.map(of));
    return { label: l.label, strong: !!l.strong, rule: !!l.rule, raw, cells: raw.map(v => (l.unit === 'bare' ? bare(v) : fmt(v, l.unit))) };
  });
  const cols = [{ label: 'Selected range' }, { label: 'Latest \u00d7 26' }].concat(rev.map(p => ({ label: fmtDate(p.end), href: typeof p.source_url === 'string' && /^https:\/\//.test(p.source_url) ? p.source_url : null })));
  return {
    type: 'statement', cols, rows, legend: [], fit: true, headline: null,
    table: () => ({ cols: [], rows: [], raw: [['line', 'selected_range', 'latest_x26'].concat(rev.map(p => p.end))].concat(rows.map(r => [r.label].concat(r.raw))) }),
  };
}

const BUILD = { time: buildTime, hbar: buildHbar, dots: buildDotForm, segmented: buildSegmented, stat: buildStat, multiples: buildMultiples, heatmap: buildHeatmap, rows: buildRows, waterfall: buildWaterfall, statement: buildStatement };

// ---------- headline figures ----------
function change(v, p, kind) {
  if (v == null || p == null || !kind) return null;
  // a percent of a zero or negative base, or of 500% and more, says less than the change in the series' own unit
  if (kind === 'pct') return p <= 0 || Math.abs((v - p) / p) >= 5 ? { v: v - p, unit: null } : { v: (v - p) / p, unit: 'pct' };
  if (kind === 'pp') return { v: v - p, unit: 'pp' };
  return { v: v - p, unit: null };
}

function headlineOf(entry, cfg, file, app, opts = {}) {
  let hd = entry.headline;
  if (!hd) return null;
  const range = app.G.range, label = hd.label.replace('{range}', RANGE_LABEL[range]);
  const unit = hd.unit || cfg.unit;
  const out = (value, prev, lab = label) => {
    if (value == null) return null;
    const d = change(value, prev, hd.delta);
    return { value, unit, label: lab, delta: d && { ...d, unit: d.unit || unit } };
  };
  if (opts.cumulative) {
    const fr = frame(file, entry.shape), arr = fr && fr.col(entry.cumulative.col);
    const L = arr && lastAt(arr, arr.length - 1);
    return L ? { value: L.v, unit: entry.unit, label: entry.cumulative.headline, delta: null } : null;
  }
  if (hd.rule === 'value') return out(pick(file, hd.path));
  if (hd.rule === 'listLast') {
    const list = file[hd.list], it = Array.isArray(list) && list.length ? list[list.length - 1] : null;
    return it ? out(it[hd.value], null, label.replace('{date}', it[hd.date] ? fmtDate(String(it[hd.date]).slice(0, 10)) : '')) : null;
  }
  if (hd.rule === 'launchP') {
    const list = file.launch || [];
    const l = list.find(x => String(x.label).includes(hd.match)) || list[0];
    if (!l) return null;
    let prev = null;
    if (range !== 'all' && l.dates && l.dates.length) {
      const i = l.dates.indexOf(isoDay(dayNum(l.dates[l.dates.length - 1]) - RANGE_DAYS[range]));
      if (i >= 0) prev = l.history[i];
    }
    return out(l.p, prev, String(l.label).includes(hd.match) ? label : `By ${l.label}`);
  }
  const fr = frame(file, entry.shape);
  // a headline may name the rule to use until the file carries its column
  if (hd.or && fr && !fr.col(hd.col)) hd = { ...hd, rule: hd.or };
  if (!fr || !fr.dates.length) return null;
  const n = fr.dates.length, win = windowOf(fr, range, entry.minPoints), len = win.i1 - win.i0 + 1;
  const hasPrev = range !== 'all' && fr.grain === 'd';
  const stack = (cfg.series || []).map(s => fr.col(s.col)).filter(Boolean);
  if (hd.rule === 'periodSum') {
    // the report periods that end inside the selected range, against the same number before them
    const c = fr.col(hd.col);
    if (!c) return null;
    const w = periodsIn(fr.dates, range, refOf(app)), add = ix => ix.reduce((a, i) => a + (c[i] || 0), 0);
    return w.idx.length ? out(add(w.idx), w.prev.length ? add(w.prev) : null) : null;
  }
  if (hd.rule === 'sumAll') { const c = fr.col(hd.col); return c ? out(sumRange(c, 0, c.length - 1)) : null; }
  if (hd.rule === 'annual30') {
    // the last 30 days, scaled to a year
    const c = fr.col(hd.col), z = c ? lastIndex(c) : -1;
    const s30 = z >= 29 ? sumRange(c, z - 29, z) : null;
    return s30 == null ? null : out(s30 * 365 / 30);
  }
  if (hd.rule === 'mean') {
    // the average per day over the range (days that have a value)
    const c = fr.col(hd.col) || [];
    const mean = (i0, i1) => { let t = 0, k = 0; for (let i = Math.max(0, i0); i <= i1; i++) if (c[i] != null) { t += c[i]; k++; } return k ? t / k : null; };
    return out(mean(win.i0, win.i1), hasPrev && win.i0 - len >= firstIndex(c) ? mean(win.i0 - len, win.i0 - 1) : null);
  }
  if (hd.rule === 'sum' || hd.rule === 'net' || hd.rule === 'sumStack') {
    const cols = hd.rule === 'sum' ? [fr.col(hd.col)].filter(Boolean) : hd.rule === 'sumStack' ? stack : null;
    const tot = (i0, i1) => {
      if (cols) { const parts = cols.map(c => sumRange(c, i0, i1)).filter(v => v != null); return parts.length ? parts.reduce((a, v) => a + v, 0) : null; }
      const p = sumRange(fr.col(entry.net.plus) || [], i0, i1), q = sumRange(fr.col(entry.net.minus) || [], i0, i1);
      return p == null && q == null ? null : (p || 0) - (q || 0);
    };
    const lead = cols ? Math.min(...cols.map(firstIndex).filter(i => i >= 0), Infinity) : firstIndex(fr.col(entry.net.plus));
    const prev = hasPrev && lead >= 0 && win.i0 - len >= lead ? tot(win.i0 - len, win.i0 - 1) : null;
    return out(tot(win.i0, win.i1), prev);
  }
  if (hd.rule === 'stackShare' || hd.rule === 'stackShareLast') {
    // part of a stack as a share of the whole: over the range, or at the latest date or period
    const part = hd.of.map(c => fr.col(c)).filter(Boolean);
    if (!part.length || !stack.length) return null;
    const add = (cs, i0, i1) => cs.reduce((a, c) => a + (sumRange(c, i0, i1) || 0), 0);
    const share = (i0, i1) => { const den = add(stack, i0, i1); return den ? add(part, i0, i1) / den : null; };
    if (hd.rule === 'stackShare') return out(share(win.i0, win.i1));
    const li = Math.max(-1, ...stack.map(lastIndex));
    if (li < 0) return null;
    let pi = li - 1;
    while (pi >= 0 && !stack.some(c => c[pi] != null)) pi--;
    return out(share(li, li), fr.grain !== 'd' && pi >= 0 ? share(pi, pi) : null);
  }
  if (hd.rule === 'lastStack') {
    const at = i => { const parts = stack.map(c => c[i]).filter(v => v != null); return parts.length ? parts.reduce((a, v) => a + v, 0) : null; };
    const li = Math.max(-1, ...stack.map(lastIndex));
    if (li < 0) return null;
    let pi = hasPrev ? win.i0 - 1 : -1;
    while (pi >= 0 && at(pi) == null) pi--;
    return out(at(li), pi >= 0 && win.i0 - 1 - pi <= len ? at(pi) : null, label.replace('{date}', fmtPoint(fr.dates[li])));
  }
  if (hd.rule === 'rangeShare') {
    // the numerator's stack over the range, divided by another file's total on the same days
    const den = opts.extra && frame(opts.extra), denCol = den && den.col(hd.denCol);
    const num = Object.keys(pick(file, hd.num) || {}).map(k => fr.col(`${hd.num}.${k}`)).filter(Boolean);
    if (!denCol || !num.length) return null;
    const at = new Map(den.dates.map((d, i) => [d, i]));
    let a = 0, b = 0;
    for (let i = win.i0; i <= win.i1; i++) {
      const parts = num.map(c => c[i]).filter(v => v != null), j = at.get(fr.dates[i]);
      if (!parts.length || j === undefined || denCol[j] == null) continue;
      a += parts.reduce((s, v) => s + v, 0); b += denCol[j];
    }
    return b > 0 ? out(a / b) : null;
  }
  if (hd.rule === 'peersShare') {
    const cols = ['variational'].concat((file.venues || []).map((_, i) => `venues.${i}`)).map(c => fr.col(c)).filter(Boolean);
    if (!cols.length) return null;
    const at = i => { if (i < 0 || cols[0][i] == null) return null; const den = cols.reduce((a, c) => a + (c[i] || 0), 0); return den ? cols[0][i] / den : null; };
    return out(at(lastIndex(cols[0])), hasPrev ? at(win.i0 - 1) : null);
  }
  // 'last'
  const arr = fr.col(hd.col || (cfg.series && cfg.series[0] && cfg.series[0].col) || '');
  if (!arr) return null;
  const L = lastAt(arr, n - 1);
  if (!L) return null;
  let P = null;
  if (fr.grain === 'i') P = null; // a week of snapshots has no preceding week to compare with
  else if (fr.grain !== 'd') P = lastAt(arr, L.i - 1);
  else if (hasPrev) { P = lastAt(arr, win.i0 - 1); if (P && win.i0 - 1 - P.i > len) P = null; }
  return out(L.v, P && P.v, label.replace('{window}', cfg.windowLabel || '').replace('{basis}', cfg.basisLabel || '').replace('{n}', fr.weeks ? fr.weeks[L.i] : '').replace('{date}', fmtPoint(String(fr.dates[L.i]))));
}

// ---------- the card ----------
export class Card {
  constructor(entry, app) {
    this.entry = entry; this.app = app; this.st = stateOf(entry);
    this.seq = 0; this.charts = [];
    this.titleEl = h('h2', { class: 'card-title', id: `t-${entry.id}`, text: entry.title });
    this.infoBtn = h('button', { class: 'icon-btn info', type: 'button', 'aria-label': `About: ${entry.title}`, 'aria-expanded': 'false', onclick: () => this.openInfo() }, icon('info'));
    this.ctlEl = h('div', { class: 'card-ctl' });
    this.hlEl = h('div', { class: 'hl' });
    this.bodyEl = h('div', { class: 'card-body' });
    this.legendEl = h('div', { class: 'legend' });
    const size = (entry.height ? ` is-${entry.height}` : '') + (entry.rows === 2 ? ' rows-2' : '');
    this.el = h('section', { class: `card span-${entry.span}${size}`, id: `card-${entry.id}`, 'data-card': entry.id, 'aria-labelledby': `t-${entry.id}` },
      h('header', { class: 'card-head' }, h('div', { class: 'card-t' }, this.titleEl, this.infoBtn), this.ctlEl),
      this.hlEl, this.bodyEl, this.legendEl);
  }

  title() { return this.titleEl.textContent || this.entry.title; }
  source() { return (this.cfg || this.entry).source; }
  asOf() { return this.file && this.file.as_of ? fmtStamp(this.file.as_of) : ''; }

  // the ⓘ: catalog text, any note the file carries for this card, then Source and As of beneath a hairline
  openInfo() {
    infoPop(this.infoBtn, [this.entry.info].concat((this.view && this.view.notes) || []), [['Source', this.source()], ['As of', this.asOf()]]);
  }

  async update() {
    const seq = ++this.seq, e = this.entry, st = this.st;
    const [raw, extra] = await Promise.all([load(e.file), e.headline && e.headline.denFile ? load(e.headline.denFile) : null]);
    if (this.dead || seq !== this.seq) return;
    this.el.classList.remove('is-loading');
    // a scoped card reads the block the page filters select; its title may follow the scope
    const sc = e.scoped && raw && raw.scopes ? raw.scopes[this.app.scopeKey()] : null;
    let file = e.scoped ? (sc ? { ...sc, as_of: raw.as_of, sources: raw.sources } : null) : raw;
    // at 7D a snapshot-native card reads the file's half-hourly block, which carries the same names as the daily one;
    // a file with no daily date yet is read that way at every range, drawn as at 7D
    const snaps = e.intraday ? snapshotBlock(file, this.app.G.range) : null;
    let app = this.app;
    if (snaps) {
      file = { ...snaps.block, as_of: file.as_of, sources: file.sources };
      if (snaps.range !== app.G.range) app = Object.create(app, { G: { value: { ...app.G, range: snaps.range } } });
    }
    const by = e.titleBy && Object.values(this.app.G.f).find(v => e.titleBy[v]);
    this.titleEl.textContent = by ? e.titleBy[by] : e.title;
    if (!file) { this.file = null; this.empty(); return; }
    this.file = file;
    const has = (path, min) => (path === 'fee'
      ? Object.values(file.range || {}).some(r => Object.values(r).some(m => Object.values(m).some(s => s && Object.keys(s).some(k => k.endsWith('_fee') && s[k] != null))))
      : min > 1 ? (pick(file, path) || []).filter(x => x != null).length >= min : hasValue(pick(file, path)));
    // a card whose Cumulative view comes from the file offers it only once that series exists
    this.cumLock = !!(e.cumulative && st.cum && has(e.cumulative.col));
    this.cumOff = !!(e.cumulative && !has(e.cumulative.col));
    // a Period control lists the latest two weeks, the selected range, then each report date (latest first)
    const dyn = (e.controls || []).some(c => c.periods)
      ? { ...e, controls: e.controls.map(c => (c.periods ? { ...c, options: c.options.concat([...(file.periods || [])].reverse().map(p => ({ v: p.end, label: fmtDate(p.end) }))) } : c)) }
      : e;
    const res = resolve(dyn, st, has);
    let cfg = res.cfg;
    if (cfg.snapshotUntil && !(file.launch || []).some(l => (l.history || []).filter(v => v != null).length >= cfg.snapshotUntil)) cfg = { ...cfg, ...cfg.snapshot };
    if (cfg.dynamic && !cfg.series) cfg = { ...cfg, series: dynamicSeries(cfg, file) };
    this.cfg = cfg;
    const view = (BUILD[cfg.form || 'time'] || buildTime)(cfg, file, this.cumOff ? { ...st, cum: false } : st, app);
    this.view = view;
    // sampled days are drawn as they are (buildTime), so the file's running total does not stand in for them either
    if (view && view.scattered) this.cumLock = false;
    this.renderControls(cfg, res.shown, view);
    if (!view) { this.empty(true); return; }
    this.el.classList.remove('is-empty');
    this.el.hidden = false;
    this.el.classList.toggle('is-stat', view.type === 'stat');   // a bare figure is a tile: title, value, label
    this.el.classList.toggle('is-fit', !!view.fit);              // content height; never stretched to match a neighbour
    this.headline = view.headline !== undefined ? view.headline : headlineOf(e, cfg, file, app, { cumulative: this.cumLock, extra });
    const hd = e.headline;
    if (hd && hd.noCum && st.cum && (e.toggles || []).includes('cum') && !view.scattered) this.headline = null;
    // a sum over the range drawn on sampled days is the sum of those days: the label counts them, and there is no
    // preceding window to compare it with
    if (this.headline && view.sampled && hd && RANGE_SUMS.includes(hd.rule)) {
      this.headline = { ...this.headline, label: `${view.sampled} sampled day${view.sampled === 1 ? '' : 's'}`, delta: null };
    }
    if (view.type === 'stat' && !this.headline) { this.empty(true); return; }
    this.renderHeadline(view);
    this.renderBody(cfg, view);
    this.renderLegend(view);
  }

  empty(keepControls) {
    this.el.classList.add('is-empty');
    // a card may stay off the page until its series exists (`hideEmpty`), instead of showing an empty frame
    this.el.hidden = !!this.entry.hideEmpty;
    this.el.classList.remove('is-stat', 'is-fit');
    this.dropCharts();
    if (!keepControls) this.ctlEl.replaceChildren();
    this.view = null;
    this.hlEl.replaceChildren(); this.hlEl.hidden = true;
    this.legendEl.replaceChildren(); this.legendEl.hidden = true;
    this.bodyEl.hidden = false;
    this.bodyEl.replaceChildren(h('div', { class: 'nodata', text: 'No data yet' }));
  }

  set(patch) { Object.assign(this.st, patch); this.app.onState(this.entry); this.update(); }

  renderControls(cfg, shown, view) {
    const e = this.entry, st = this.st, out = [];
    shown.forEach(({ ctl, opts, cur }) => {
      const b = h('button', { class: 'ctl', type: 'button', 'aria-haspopup': 'menu', 'aria-expanded': 'false', 'aria-label': `${ctl.label}: ${cur.label}` },
        h('span', { class: 'ctl-k', text: ctl.label }), h('span', { class: 'ctl-v', text: cur.label }), icon('chev'));
      b.addEventListener('click', () => menu(b, opts.map(o => ({ label: o.label, value: o.v, kind: 'radio', checked: o.v === cur.v })), it => this.set({ [ctl.key]: it.value, iso: null }), { align: 'right' }));
      out.push(b);
    });
    const tog = (key, enabled) => {
      if (!(e.toggles || []).includes(key) || !enabled) return;
      out.push(h('button', { class: 'ctl tog', type: 'button', 'aria-pressed': String(!!st[key]), text: TOGGLES[key], onclick: () => this.set({ [key]: !st[key], iso: null }) }));
    };
    tog('share', !this.cumLock && (cfg.stack || cfg.mirror || (cfg.bands && !(view && view.single))));
    tog('cum', !this.cumOff && !(view && view.scattered));
    tog('venues', true);
    if (view) {
      const more = h('button', { class: 'icon-btn more', type: 'button', 'aria-label': `More: ${this.title()}`, 'aria-haspopup': 'menu', 'aria-expanded': 'false' }, icon('more'));
      more.addEventListener('click', () => this.openMenu(more));
      out.push(more);
    }
    this.ctlEl.replaceChildren(...out);
  }

  openMenu(anchor) {
    const v = this.view, st = this.st;
    const plotted = ['time', 'hbar', 'dots', 'waterfall', 'segmented', 'multiples'].includes(v.type);
    const items = [];
    if (plotted && v.table) items.push({ label: st.table ? 'View as chart' : 'View as table', value: 'table' });
    if (v.table) items.push({ label: 'Download CSV', value: 'csv' });
    if (plotted && !st.table) items.push({ label: 'Download PNG', value: 'png' });
    items.push({ label: 'Copy link', value: 'link' });
    if (v.type === 'time' && this.app.events && this.app.events.length) items.push({ sep: true }, { label: 'Events', value: 'events', kind: 'check', checked: st.events });
    menu(anchor, items, it => {
      if (it.value === 'table') this.set({ table: !st.table });
      else if (it.value === 'events') this.set({ events: !st.events });
      else if (it.value === 'csv') download(`variational-monitor_${this.entry.id}_${this.app.G.range}.csv`, new Blob([toCsv(v.table().raw)], { type: 'text/csv' }));
      else if (it.value === 'png') exportPng(this);
      else if (it.value === 'link') copyText(this.app.link(this.entry)).then(() => toast('Link copied'));
    }, { align: 'right' });
  }

  renderHeadline(view) {
    const hd = this.headline;
    if (!hd) { this.hlEl.replaceChildren(); this.hlEl.hidden = true; return; }
    this.hlEl.hidden = false;
    const kids = [h('span', { class: 'hl-v', text: hd.text || fmt(hd.value, hd.unit) })];
    if (hd.delta && Number.isFinite(hd.delta.v)) {
      const d = hd.delta, txt = fmt(d.v, d.unit, { sign: true });
      const dir = /[1-9]/.test(txt) ? (d.v > 0 ? 'up' : 'down') : 'flat';
      const days = RANGE_DAYS[this.app.G.range];
      kids.push(h('span', { class: `hl-d ${dir}`, title: Number.isFinite(days) && view.type === 'time' && view.grain !== 'p' && view.grain !== 'wk' ? `Change versus the preceding ${days} days` : 'Change versus the preceding period' },
        dir === 'flat' ? null : icon(dir), h('span', { text: txt })));
    }
    kids.push(h('span', { class: 'hl-l', text: hd.label }));
    if (view.type === 'stat') { this.hlEl.replaceChildren(kids[0], h('span', { class: 'hl-sub' }, kids.slice(1))); return; }
    this.hlEl.replaceChildren(...kids);
  }

  renderBody(cfg, view) {
    const st = this.st;
    const chartForm = ['time', 'hbar', 'dots', 'waterfall'].includes(view.type);
    const asTable = st.table && view.table && ['time', 'hbar', 'dots', 'waterfall', 'segmented', 'multiples'].includes(view.type);
    this.bodyEl.hidden = view.type === 'stat' || view.type === 'figure';
    if (!chartForm || asTable) this.dropCharts();
    if (asTable) {
      const t = view.table();
      const cols = t.cols.map((c, i) => ({ label: c.label, num: c.num, cell: r => r[i] }));
      this.bodyEl.replaceChildren(h('div', { class: 'tview', tabindex: '0', style: view.plotH || view.type === 'segmented' ? { height: 'auto', minHeight: '0', flex: '0 0 auto' } : null }, renderTable({ cols, rows: t.rows, caption: this.title() })));
      return;
    }
    if (chartForm) {
      if (!this.chart) {
        const plot = h('div', { class: 'plot' });
        this.bodyEl.replaceChildren(plot);
        this.chart = new Chart(plot, { label: `${this.title()}. Use the arrow keys to read values.`, onHover: this.onHover || null });
      }
      // the watermark is for large plots: 16px on a full-width card, 13px on a half-width one, none below that
      const cl = this.el.classList;
      this.chart.wm = cl.contains('span-12') ? 16 : cl.contains('span-6') || cl.contains('span-8') ? 13 : 0;
      // category plots have a natural height; a short one keeps it, a long one may grow with its row
      const s = this.chart.host.style;
      s.minHeight = view.plotH ? `${view.plotH}px` : '';
      s.flex = view.plotH && view.fit !== false && (view.fit || view.type === 'dots' || view.type === 'waterfall') ? '0 0 auto' : '';
      s.height = s.flex ? `${view.plotH}px` : '';
      this.chart.render(view);
      return;
    }
    if (view.type === 'multiples') {
      const made = view.panels.map(p => {
        const plot = h('div', { class: 'plot' });
        const key = h('span', { class: 'lg-key line' }); key.style.background = colorOf(p.color);
        return { p, plot, node: h('div', { class: 'panel' }, h('h3', { class: 'panel-title' }, key, h('span', { text: p.title })), plot) };
      });
      this.bodyEl.replaceChildren(h('div', { class: 'panels is-quad' }, made.map(x => x.node)));
      this.charts = made.map(x => {
        const c = new Chart(x.plot, { small: true, label: `${this.title()}, ${x.p.title}. Use the arrow keys to read values.`, onHover: (i, src) => this.linkHover(i, src) });
        c.title = x.p.title;
        c.render(x.p.model);
        return c;
      });
    } else if (view.type === 'segmented') {
      const bar = h('div', { class: 'segbar', role: 'img', 'aria-label': view.segs.map(s => `${s.label} ${fmt(s.pct, 'pct')}`).join(', ') },
        view.segs.filter(s => s.pct > 0).map(s => {
          const c = colorOf(s.color);
          return h('div', { class: 'seg-b', tabindex: '0', title: `${s.label}, ${fmt(s.pct, 'pct')}`, style: { flex: `${s.pct} 1 0`, background: c, color: ink(c) } },
            s.pct >= 0.06 ? h('span', { text: fmt(s.pct, 'pct', { axis: true }) }) : null);
        }));
      this.bodyEl.replaceChildren(h('div', { class: 'segwrap' }, bar));
    } else if (view.type === 'heatmap') {
      this.bodyEl.replaceChildren(heatTable(view, this.title()));
    } else if (view.type === 'statement') {
      this.bodyEl.replaceChildren(statementTable(view, this.title()));
    } else if (view.type === 'rows') {
      this.renderRows(cfg, view);
    } else {
      this.bodyEl.replaceChildren();
    }
  }

  renderRows(cfg, view) {
    const st = this.st, n = view.list.length, size = cfg.showFirst ? (st.all ? n : cfg.showFirst) : cfg.pageSize || 10;
    const pages = Math.max(1, Math.ceil(n / size));
    st.page = Math.min(st.page || 0, pages - 1);
    const from = st.page * size;
    const cols = cfg.columns.map(c => ({
      label: c.label, num: c.num, cls: c.kind === 'address' ? 'c-addr' : c.kind === 'rank' ? 'c-rank' : '',
      cell: r => {
        const v = r.row[c.field];
        if (c.kind === 'rank') return String(r.i + 1);
        if (c.kind === 'address') return address(v);
        if (c.kind === 'date') return v ? fmtDate(String(v).slice(0, 10)) : '—';
        if (c.kind === 'bare') return bare(v);
        return fmt(v, c.unit);
      },
    }));
    const rows = view.list.slice(from, from + size).map((row, k) => ({ row, i: from + k }));
    const pager = cfg.showFirst ? (n > cfg.showFirst ? h('div', { class: 'pager' },
      h('button', { class: 'ctl', type: 'button', text: st.all ? 'Show fewer' : 'Show all', onclick: () => { st.all = !st.all; this.renderRows(cfg, view); } })) : null)
      : n > size ? h('div', { class: 'pager' },
      h('span', { class: 'pager-t', text: `${fmt(from + 1, 'count')}–${fmt(Math.min(n, from + size), 'count')} of ${fmt(n, 'count')}` }),
      h('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Previous page', disabled: st.page === 0, onclick: () => { st.page--; this.renderRows(cfg, view); } }, icon('prev')),
      h('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Next page', disabled: st.page >= pages - 1, onclick: () => { st.page++; this.renderRows(cfg, view); } }, icon('next'))) : null;
    this.bodyEl.replaceChildren(h('div', { class: 'tscroll' }, renderTable({ cols, rows, caption: this.title() })), pager);
  }

  renderLegend(view) {
    const items = view.legend || [];
    if (!items.length || this.st.table) { this.legendEl.replaceChildren(); this.legendEl.hidden = true; return; }
    this.legendEl.hidden = false;
    this.legendEl.replaceChildren(...items.map(l => {
      const key = h('span', { class: `lg-key ${l.key}` });
      const c = colorOf(l.color);
      if (l.key === 'ring') key.style.borderColor = c; else key.style.background = c;
      if (l.opacity) key.style.opacity = String(l.opacity);
      if (l.fixed) return h('span', { class: 'lg' }, key, h('span', { text: l.label }));
      const off = view.iso && view.iso !== l.id;
      return h('button', {
        class: 'lg' + (off ? ' is-off' : ''), type: 'button', 'aria-pressed': String(view.iso === l.id),
        title: view.iso === l.id ? 'Show all series' : 'Show only this series',
        onclick: () => { this.st.iso = view.iso === l.id ? null : l.id; this.update(); },
      }, key, h('span', { text: l.label }));
    }));
  }

  dropCharts() {
    if (this.chart) { this.chart.dispose(); this.chart = null; }
    this.charts.forEach(c => c.dispose());
    this.charts = [];
  }

  // one crosshair across the panels of a small-multiples card, matched by date
  linkHover(i, src) {
    const key = i >= 0 && src.model.x[i] ? src.model.x[i].key : null;
    this.charts.forEach(c => {
      if (c === src) return;
      const j = key ? c.model.x.findIndex(p => p.key === key) : -1;
      if (j < 0) c.hide(); else c.setHover(j, null, null, false, true);
    });
  }

  flash() {
    this.el.classList.add('is-target');
    setTimeout(() => this.el.classList.remove('is-target'), 1600);
  }

  hold() { this.el.classList.add('is-loading'); }

  dispose() {
    this.dead = true;
    this.dropCharts();
  }
}

// Ink on a coloured cell: white or the page navy, whichever contrasts more (WCAG luminance; they cross at 0.183,
// where each gives 4.5:1).
const lin = v => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
const lum = c => [1, 3, 5].map(i => lin(parseInt(c.slice(i, i + 2), 16) / 255)).reduce((a, v, i) => a + v * [0.2126, 0.7152, 0.0722][i], 0);
const ink = c => (lum(c) > 0.183 ? '#010612' : '#fff');

// A wallet or pool address: shortened, linking to Arbiscan when it is a well-formed address.
function address(v) {
  const s = String(v || '');
  const short = s.length > 14 ? `${s.slice(0, 6)}…${s.slice(-4)}` : s;
  if (!/^0x[0-9a-fA-F]{40}$/.test(s)) return h('span', { class: 'addr', text: short || '—' });
  return h('a', { class: 'addr', href: `https://arbiscan.io/address/${s}`, target: '_blank', rel: 'noopener noreferrer', title: s, text: short });
}

function statementTable(view, caption) {
  const head = h('tr', null, h('th', { scope: 'col', class: 'st-l' }),
    view.cols.map((c, i) => h('th', { scope: 'col', class: 'num' + (i < 2 ? ' st-s' : '') + (i === 1 ? ' st-e' : '') },
      c.href ? h('a', { href: c.href, target: '_blank', rel: 'noopener noreferrer', text: c.label }) : c.label)));
  const body = view.rows.map(r => h('tr', { class: (r.strong ? 'is-strong' : '') + (r.rule ? ' is-rule' : '') },
    h('th', { scope: 'row', class: 'st-l', text: r.label }),
    r.cells.map((v, i) => h('td', { class: 'num' + (i < 2 ? ' st-s' : '') + (i === 1 ? ' st-e' : ''), text: v }))));
  return h('div', { class: 'tscroll hscroll' }, h('table', { class: 'tbl st' }, h('caption', { class: 'sr', text: caption }), h('thead', null, head), h('tbody', null, body)));
}

function heatTable(view, caption) {
  const head = h('tr', null,
    h('th', { scope: 'col', text: 'First funded' }), h('th', { scope: 'col', class: 'num', text: 'Accounts' }),
    Array.from({ length: view.width }, (_, i) => h('th', { scope: 'col', class: 'hm-c', text: i === 0 ? 'Month 0' : String(i) })));
  const body = view.rows.map(c => h('tr', null,
    h('th', { scope: 'row', text: fmtMonth(String(c.month) + '-01') }),
    h('td', { class: 'num', text: fmt(c.size, 'count') }),
    Array.from({ length: view.width }, (_, i) => {
      const v = c.active[i];
      if (v == null) return h('td', { class: 'hm-c is-none' });
      const bg = rampAt(v);
      return h('td', { class: 'hm-c', title: `${fmt(v, 'pct')} of ${fmt(c.size, 'count')} accounts first funded in ${fmtMonth(String(c.month) + '-01')}, month ${i}` },
        h('span', { class: 'hm-v', style: { background: bg, color: ink(bg) }, text: fmt(v, 'pct').replace(/\.\d%$/, '%') }));
    })));
  return h('div', { class: 'tscroll hscroll' }, h('table', { class: 'tbl hm' }, h('caption', { class: 'sr', text: caption }), h('thead', null, head), h('tbody', null, body)));
}

// ---------- PNG export: title, headline, plot, legend, source ----------
export async function exportPng(card) {
  const t = theme(), s = 2, pad = 24, v = card.view;
  let plot = card.chart ? card.chart.snapshot(s) : null;
  if (card.charts.length) {
    // small multiples: panel titles and plots side by side, as on the page
    const shots = card.charts.map(c => c.snapshot(s)), gap = 24 * s, top = 26 * s;
    plot = document.createElement('canvas');
    plot.width = shots.reduce((a, c) => a + c.width, 0) + gap * (shots.length - 1);
    plot.height = Math.max(...shots.map(c => c.height)) + top;
    const pc = plot.getContext('2d');
    let px = 0;
    shots.forEach((c, i) => {
      pc.fillStyle = t.text2; pc.font = `500 ${12 * s}px ${t.sans}`; pc.textBaseline = 'alphabetic';
      pc.fillText(card.charts[i].title || '', px, 14 * s);
      pc.drawImage(c, px, top);
      px += c.width + gap;
    });
  }
  const plotW = plot ? plot.width / s : Math.max(480, card.bodyEl.clientWidth || 480);
  const plotH = plot ? plot.height / s : v.type === 'segmented' ? 28 : 0;
  const hd = card.headline;
  const legend = v.legend || [];
  const W = plotW + pad * 2;
  const H = pad + 20 + (hd ? 44 : 0) + 16 + plotH + (legend.length ? 34 : 0) + 30 + pad;
  const cv = document.createElement('canvas');
  cv.width = W * s; cv.height = H * s;
  const c = cv.getContext('2d');
  c.scale(s, s);
  c.fillStyle = t.card; c.fillRect(0, 0, W, H);
  c.textBaseline = 'alphabetic';
  let y = pad + 15;
  c.fillStyle = t.text; c.font = `500 14px ${t.sans}`; c.fillText(card.title(), pad, y);
  if (hd) {
    y += 38;
    c.font = `500 30px ${t.sans}`;
    const txt = hd.text || fmt(hd.value, hd.unit);
    c.fillText(txt, pad, y);
    let x = pad + c.measureText(txt).width + 10;
    c.font = `500 12px ${t.sans}`;
    if (hd.delta && Number.isFinite(hd.delta.v)) {
      const d = fmt(hd.delta.v, hd.delta.unit, { sign: true }), up = hd.delta.v > 0;
      const cs = getComputedStyle(document.documentElement);
      c.fillStyle = cs.getPropertyValue(up ? '--up' : '--down');
      c.beginPath();
      if (up) { c.moveTo(x, y - 1); c.lineTo(x + 7, y - 1); c.lineTo(x + 3.5, y - 7); } else { c.moveTo(x, y - 7); c.lineTo(x + 7, y - 7); c.lineTo(x + 3.5, y - 1); }
      c.fill();
      c.fillText(d, x + 11, y);
      x += 11 + c.measureText(d).width + 10;
    }
    c.fillStyle = t.text3; c.fillText(hd.label, x, y);
  }
  y += 16;
  if (plot) c.drawImage(plot, pad, y, plotW, plotH);
  else if (v.type === 'segmented') {
    let x = pad;
    const tot = v.segs.reduce((a, g) => a + g.pct, 0), room = plotW - 2 * (v.segs.length - 1);
    v.segs.forEach(g => {
      const w = room * g.pct / tot;
      c.fillStyle = colorOf(g.color);
      c.beginPath(); c.roundRect(x, y, w, 28, 4); c.fill();
      c.fillStyle = ink(colorOf(g.color)); c.font = `600 12px ${t.sans}`; c.fillText(fmt(g.pct, 'pct', { axis: true }), x + 8, y + 18);
      x += w + 2;
    });
  }
  y += plotH;
  if (legend.length) {
    y += 24;
    c.font = `500 12px ${t.sans}`;
    // left-aligned at the image's padding, as on the card
    const widths = legend.map(l => 14 + c.measureText(l.label).width);
    let x = pad;
    legend.forEach((l, i) => {
      c.globalAlpha = l.opacity || 1;
      c.fillStyle = colorOf(l.color);
      if (l.key === 'line') c.fillRect(x, y - 5, 10, 2);
      else if (l.key === 'ring') { c.strokeStyle = colorOf(l.color); c.lineWidth = 2; c.beginPath(); c.arc(x + 4, y - 4, 3, 0, 7); c.stroke(); }
      else if (l.key === 'dot') { c.beginPath(); c.arc(x + 4, y - 4, 4, 0, 7); c.fill(); }
      else { c.beginPath(); c.roundRect(x, y - 8, 8, 8, 2); c.fill(); }
      c.globalAlpha = 1;
      c.fillStyle = t.text2; c.fillText(l.label, x + 14, y);
      x += widths[i] + 16;
    });
  }
  y += 26;
  // an image leaves the page, so it carries its own source and as-of time, set apart as two runs of text
  c.font = `400 11px ${t.sans}`; c.fillStyle = t.text3;
  const src = card.source() || '', when = card.asOf() ? `As of ${card.asOf()}` : '';
  c.fillText(src, pad, y);
  if (when) c.fillText(when, pad + (src ? c.measureText(src).width + 16 : 0), y);
  // the lockup, right-aligned: the mark with its foot on the baseline, then the two words
  const k = lockup(11), x0 = W - pad - k.width;
  c.save();
  c.translate(x0, y - k.mh); c.scale(k.mw / 80, k.mh / 58); c.translate(-212, -133);
  c.fillStyle = t.brand; c.fill(new Path2D(MARK_PATH), 'evenodd');
  c.restore();
  c.font = k.f1; c.fillStyle = t.text; c.fillText('Variational', x0 + k.mw + k.gap, y);
  c.font = k.f2; c.fillStyle = t.brand; c.fillText('Monitor', x0 + k.mw + k.gap + k.w1 + k.sp, y);
  cv.toBlob(b => { if (b) download(`variational-monitor_${card.entry.id}_${card.app.G.range}.png`, b); }, 'image/png');
}
