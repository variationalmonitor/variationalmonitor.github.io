// ECharts theme and the chart forms in the design file. A chart is drawn from a
// plain model built by card.js; the same model feeds the tooltip, the table view and CSV.
//
// model.type 'time'     { x:[{key,label}], series:[…], unit, total?, absAxis?, yMin?, yMax?, scale?, events? }
// model.type 'hbar'     { cats:[label], values:[…], colors:[…], unit, name }
// model.type 'dots'     { cats:[label], series:[{label,color,values}], unit }   one dot per series per row, joined by a hairline
// model.type 'waterfall'{ cats:[label], bars:[{x0,x1,color,text,link}], unit }   floating horizontal bars on one zero baseline
// series: { id, label, color, mark:'bar'|'line'|'area'|'marker', stack, values, draw, step, gappy, endLabel, opacity, dim, hidden }

/* global echarts */
import { fmt, fmtDay, fmtMonth } from './format.js';

// ---------- theme (read once from tokens.css) ----------
let T = null;
export function theme() {
  if (T) return T;
  const cs = getComputedStyle(document.documentElement);
  const v = n => cs.getPropertyValue(n).trim();
  T = {
    card: v('--card'), pop: v('--pop'), line: v('--line'), lineStrong: v('--line-strong'),
    text: v('--text'), text2: v('--text-2'), text3: v('--text-3'), text4: v('--text-4'), brand: v('--brand'),
    colors: {
      blue: v('--s-blue'), honey: v('--s-honey'), teal: v('--s-teal'), violet: v('--s-violet'), pink: v('--s-pink'),
      grey: v('--s-grey'), grey2: v('--s-grey-2'), long: v('--s-long'), short: v('--s-short'), brand: v('--brand'), tint: v('--brand-tint'),
    },
    ramp: [1, 2, 3, 4, 5].map(i => v(`--s-ramp-${i}`)),
    sans: 'Inter, system-ui, sans-serif',
    mono: '"Geist Mono", ui-monospace, monospace',
  };
  return T;
}
export const colorOf = name => (name && name.startsWith('ramp:') ? theme().ramp[+name.slice(5) - 1] : theme().colors[name] || name);

/** n colours along the ordinal blue ramp, dark to light. */
export function ramp(n) {
  const r = theme().ramp.map(h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16)));
  if (n <= 1) return [theme().ramp[3]];
  return Array.from({ length: n }, (_, i) => {
    const p = (i / (n - 1)) * (r.length - 1), a = Math.floor(p), b = Math.min(r.length - 1, a + 1), f = p - a;
    return '#' + [0, 1, 2].map(k => Math.round(r[a][k] + (r[b][k] - r[a][k]) * f).toString(16).padStart(2, '0')).join('');
  });
}

const mctx = document.createElement('canvas').getContext('2d');
/** The ordinal ramp as a continuous scale, t in 0..1 (heatmap cells). */
export function rampAt(t) {
  const r = theme().ramp.map(h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16)));
  const p = Math.max(0, Math.min(1, t)) * (r.length - 1), a = Math.floor(p), b = Math.min(r.length - 1, a + 1), f = p - a;
  return '#' + [0, 1, 2].map(k => Math.round(r[a][k] + (r[b][k] - r[a][k]) * f).toString(16).padStart(2, '0')).join('');
}

export function textWidth(text, font) { mctx.font = font; return mctx.measureText(text).width; }

const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const isPhone = () => window.matchMedia('(max-width: 760px)').matches;

// ---------- x ticks ----------
// The tick unit comes from the Range, not from the card's width: days at 7D, Mondays at 30D, month starts from
// 90D up (each with its year at All, otherwise only January). Width only thins the ticks. A window too short
// to hold two ticks of its unit steps down to the next finer one. Report periods are named by their end dates.
function xTicks(x, grain, maxTicks, range) {
  const n = x.length, ticks = new Map();
  const day = k => fmtDay(k), d = i => x[i].key;
  // a very narrow plot (phone small multiples) names only its first and last point
  if (maxTicks < 2) { ticks.set(0, day(d(0))); if (n > 1) ticks.set(n - 1, day(d(n - 1))); return ticks; }
  const all = Array.from({ length: n }, (_, i) => i);
  // every k-th of a list of positions, counted back from the latest
  const thin = idx => {
    const step = Math.max(1, Math.ceil(idx.length / maxTicks));
    for (let j = idx.length - 1; j >= 0; j -= step) ticks.set(idx[j], day(d(idx[j])));
    return ticks;
  };
  if (grain === 'p' || grain === 'wk') return thin(all);
  if (grain === 'i') {
    // half-hourly snapshots tick on days; a stretch shorter than two days names round hours as well
    const days = all.filter(i => (i > 0 ? d(i).slice(0, 10) !== d(i - 1).slice(0, 10) : d(i).slice(11, 16) === '00:00'));
    if (days.length >= 2) return thin(days);
    const per = Math.max(1, Math.ceil(n / maxTicks / 6) * 6); // three-hour steps or coarser
    all.filter(i => d(i).slice(14, 16) === '00' && (+d(i).slice(11, 13) * 2) % per === 0).forEach(i => ticks.set(i, d(i).slice(11, 16) === '00:00' ? day(d(i)) : d(i).slice(11, 16)));
    if (ticks.size < 2) { ticks.clear(); ticks.set(0, n > 1 ? d(0).slice(11, 16) : day(d(0))); if (n > 1) ticks.set(n - 1, d(n - 1).slice(11, 16)); }
    return ticks;
  }
  const unit = grain === 'm' ? 'm' : range === '7d' ? 'd' : range === '30d' ? 'w' : 'm';
  if (unit === 'm') {
    // the first point of each month, thinned to every second, third, fourth, sixth or twelfth month from January
    const starts = all.filter(i => grain === 'm' || (i > 0 ? d(i).slice(0, 7) !== d(i - 1).slice(0, 7) : d(i).slice(8, 10) === '01'));
    if (starts.length >= 2 || (grain === 'm' && starts.length)) {
      const k = [1, 2, 3, 4, 6, 12].find(s => Math.ceil(starts.length / s) <= maxTicks) || 12;
      for (const i of starts) {
        const mon = +d(i).slice(5, 7) - 1;
        if (mon % k === 0) ticks.set(i, fmtMonth(d(i), range === 'all' || mon === 0));
      }
      if (ticks.size >= 2 || grain === 'm') { if (!ticks.size) ticks.set(starts[starts.length - 1], fmtMonth(d(starts[starts.length - 1]), true)); return ticks; }
      ticks.clear();
    }
  }
  if (unit !== 'd') {
    // weekly buckets are keyed by their Monday; daily points tick on Mondays
    const mondays = grain === 'w' ? all : all.filter(i => new Date(d(i) + 'T00:00:00Z').getUTCDay() === 1);
    if (mondays.length >= 2) return thin(mondays);
  }
  return thin(all);
}

// Sampled dates on a category axis: every slot may carry its date, thinned to what fits at an even step, and the
// first and last are always named. `ends` says how the first and last labels align ('left', 'right' or centred).
function catTicks(x, plotW, bars, ends) {
  const n = x.length, ticks = new Map();
  if (!n) return ticks;
  const font = `400 11px ${theme().mono}`, GAP = 16;
  const w = x.map(p => textWidth(p.tick, font));
  const slot = bars ? plotW / n : plotW / Math.max(1, n - 1);
  const pos = i => (bars ? (i + 0.5) * slot : n > 1 ? i * slot : plotW / 2);
  const span = i => (i === 0 && ends.min === 'left' ? [pos(i), pos(i) + w[i]] : i === n - 1 && ends.max === 'right' ? [pos(i) - w[i], pos(i)] : [pos(i) - w[i] / 2, pos(i) + w[i] / 2]);
  ticks.set(0, x[0].tick);
  if (n === 1) return ticks;
  const end = span(n - 1), step = Math.max(1, Math.ceil((Math.max(...w) + GAP) / slot));
  let prev = span(0);
  for (let i = step; i < n - 1; i += step) {
    const s = span(i);
    if (s[0] >= prev[1] + GAP && s[1] + GAP <= end[0]) { ticks.set(i, x[i].tick); prev = s; }
  }
  ticks.set(n - 1, x[n - 1].tick);
  return ticks;
}

// the Variational mark as an image, for the watermark drawn inside large plots
export const MARK_PATH = 'M251.938 133.933C258.799 133.933 265.014 138.331 267.581 144.844L280.926 180.771C281.637 182.473 283.234 183.566 284.944 183.566H292V190.067H284.944C280.534 190.067 276.493 187.2 274.933 182.937L271.167 172.809C270.493 171.014 268.849 169.851 267.085 169.851C265.309 169.851 263.676 170.984 263.005 172.804L263.003 172.809L259.237 182.937C257.676 187.2 253.636 190.067 249.226 190.067H212V183.566H218.976C220.704 183.566 222.383 182.398 223.056 180.608L236.218 145.156C238.705 138.476 244.964 133.934 251.938 133.933ZM251.938 140.434C247.647 140.434 243.668 143.234 242.108 147.481V147.485L228.684 183.566H234.081C235.811 183.565 237.492 182.395 238.163 180.604L246.626 157.839V157.834C248.709 152.292 253.753 148.642 259.513 148.642C260.464 148.642 261.403 148.751 262.314 148.956L261.695 147.29C260.046 143.141 256.156 140.434 251.938 140.434ZM259.471 155.184C256.397 155.184 253.669 157.089 252.514 160.17L243.79 183.566H249.184C250.915 183.566 252.597 182.396 253.269 180.604L257.032 170.478C258.603 166.258 262.419 163.432 266.807 163.389H267.016C267.222 163.39 267.424 163.397 267.622 163.41L266.426 160.163C265.309 157.164 262.539 155.184 259.471 155.184Z';
export const markUri = fill => 'data:image/svg+xml,' + encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="212 133 80 58" width="80" height="58"><path fill-rule="evenodd" clip-rule="evenodd" fill="${fill}" d="${MARK_PATH}"/></svg>`);
const WORDMARK = '"Zalando Sans Expanded", Inter, system-ui, sans-serif';
/** The lockup's geometry at a type size: the mark's box, the gap after it and the two words' widths. */
export function lockup(size) {
  const f1 = `600 ${size}px ${WORDMARK}`, f2 = `400 ${size}px ${WORDMARK}`;
  const mh = Math.round(size * 14 / 13), mw = mh * 80 / 58, gap = Math.round(size * 7 / 13);
  const w1 = textWidth('Variational', f1), sp = size * 0.3, w2 = textWidth('Monitor', f2);
  return { f1, f2, mh, mw, gap, w1, sp, w2, width: mw + gap + w1 + sp + w2 };
}

// ---------- option builders ----------
function baseAxisLabel(t) { return { color: t.text3, fontFamily: t.mono, fontSize: 11, fontWeight: 400 }; }

function timeOption(m, W, H, small) {
  const t = theme();
  const n = m.x.length;
  const vis = m.series.filter(s => !s.hidden);
  const bars = vis.filter(s => s.mark === 'bar');
  const hasBars = bars.length > 0;
  const ends = vis.filter(s => s.endLabel);
  const endFont = `500 11px ${t.sans}`;
  const right = ends.length ? Math.ceil(Math.max(...ends.map(s => textWidth(s.label, endFont)))) + 16 : hasBars ? 2 : 8;
  // one tick style per axis: compact throughout once the axis reaches 10,000
  const peak = m.stacked
    ? Math.max(0, ...m.x.map((_, i) => vis.reduce((a, s) => a + (s.mark === 'marker' ? 0 : Math.max(0, s.draw[i] || 0)), 0)))
    : Math.max(0, ...vis.flatMap(s => s.draw.map(v => Math.abs(v || 0))));
  const yFmt = v => fmt(m.absAxis ? Math.abs(v) : v, m.unit, { axis: true, compact: peak >= 9999.5 });
  const plotW = Math.max(40, W - 58 - right);
  const band = plotW / n;
  // first and last x labels: a bar chart centres them under their bars when there is room; a line chart, or a
  // category axis whose slots are narrower than a label, aligns them to the plot's edges
  const tickW = m.cat ? Math.max(...m.x.map(p => textWidth(p.tick, `400 11px ${t.mono}`))) : 0;
  const alignMin = m.cat ? (hasBars && band >= tickW ? null : 'left') : hasBars && plotW >= 150 ? null : 'left';
  const alignMax = m.cat ? (ends.length || (hasBars && band >= tickW) ? null : 'right') : (hasBars && plotW >= 150) || ends.length ? null : 'right';
  const ticks = m.cat ? catTicks(m.x, plotW, hasBars, { min: alignMin, max: alignMax })
    : xTicks(m.x, m.grain, plotW < 150 ? 1 : clamp(Math.floor(plotW / (small ? 64 : 84)), 2, 8), m.range);

  // bar geometry: capped width, 2px surface gap between neighbours and stacked segments
  const stackedBars = bars.length > 1 && !m.paired;
  const ring = stackedBars && band >= 6 ? 1 : 0;
  const gap = ring ? 0 : band >= 6 ? 2 : band >= 3 ? 1 : 0;
  // every bar caps at 24px; paired bars stand side by side, two to a slot. Only the data end is rounded.
  const barW = m.paired ? clamp((band - 8) / bars.length - 2, 1, 20) : clamp(band - gap, 0.5, 24);
  const rad = Math.min(2, barW / 4);
  const outerPos = new Array(n).fill(-1), outerNeg = new Array(n).fill(-1);
  bars.forEach((s, si) => s.draw.forEach((v, i) => { if (v > 0) outerPos[i] = si; else if (v < 0) outerNeg[i] = si; }));

  // A line chart's y-axis runs from a round value just below its minimum in the visible range to a round
  // value just above its maximum (about 10% of the range as padding). It is not forced to zero; zero is
  // included only when the series crosses it. Bars and areas always start at zero.
  let yLo = m.yMin, yHi = m.yMax, yStep;
  const lineOnly = vis.length > 0 && vis.every(s => s.mark === 'line' || s.mark === 'marker');
  if (lineOnly && yLo === undefined && yHi === undefined) {
    const vals = vis.flatMap(s => s.draw).filter(v => v != null);
    if (vals.length) {
      const lo = Math.min(...vals), hi = Math.max(...vals);
      // a flat series gets a band of a tenth of its level either side
      const flat = hi === lo, span = hi - lo;
      let a = flat ? lo - (Math.abs(lo) * 0.1 || 1) : lo - span * 0.1, b = flat ? hi + (Math.abs(hi) * 0.1 || 1) : hi + span * 0.1;
      if (lo >= 0 && a < 0) a = 0;
      if (hi <= 0 && b > 0) b = 0;
      const raw = (b - a) / (small ? 3 : 4), pow = 10 ** Math.floor(Math.log10(raw));
      yStep = [1, 2, 2.5, 5, 10].map(k => k * pow).find(v => v >= raw * 0.999) || 10 * pow;
      const fix = v => +v.toPrecision(12);
      yLo = fix(Math.floor(a / yStep + 1e-9) * yStep); yHi = fix(Math.ceil(b / yStep - 1e-9) * yStep);
      if (lo >= 0 && yLo < 0) yLo = 0;
      yStep = fix(yStep);
    }
  }

  // Stacked areas are stacked here, not by the library: its stacking drops to zero across missing days.
  // Each band is a 10% wash polygon between its own top and bottom, with the top edge drawn as a 2px line.
  const tops = new Map(), bands = [];
  const posAcc = new Array(n).fill(0), negAcc = new Array(n).fill(0);
  vis.filter(s => s.mark === 'area' && s.stack).forEach(s => {
    const top = [], bot = [];
    s.draw.forEach((v, i) => {
      if (v == null) { top.push(null); bot.push(null); return; }
      const acc = v >= 0 ? posAcc : negAcc;
      bot.push(acc[i]); acc[i] += v; top.push(acc[i]);
    });
    tops.set(s, top);
    const at = top.map((v, i) => (v == null ? -1 : i)).filter(i => i >= 0);
    if (at.length < 2) return;
    bands.push({
      type: 'custom', silent: true, z: 2, clip: true, id: s.id + ':band', name: s.label + ' band', data: [[at[0], 0]],
      renderItem(params, api) {
        const pts = [];
        at.forEach((i, k) => {
          if (k && s.step) pts.push(api.coord([i, top[at[k - 1]]]));
          pts.push(api.coord([i, top[i]]));
        });
        for (let k = at.length - 1; k >= 0; k--) {
          const i = at[k];
          pts.push(api.coord([i, bot[i]]));
          if (k && s.step) pts.push(api.coord([i, bot[at[k - 1]]]));
        }
        return { type: 'polygon', silent: true, shape: { points: pts }, style: { fill: colorOf(s.color), opacity: s.wash ?? 0.1 } };
      },
    });
  });

  const series = vis.map(s => {
    const color = colorOf(s.color);
    const common = { id: s.id, name: s.label, silent: true, z: s.dim ? 2 : 3, cursor: 'default' };
    if (s.mark === 'bar') {
      const si = bars.indexOf(s);
      return {
        ...common, type: 'bar', stack: m.paired ? `bar${si}` : 'bars', stackStrategy: 'samesign', barWidth: barW, barGap: m.paired ? '12%' : undefined, barMinHeight: 0,
        itemStyle: { color, opacity: s.opacity ?? 1, borderColor: t.card, borderWidth: ring },
        emphasis: { itemStyle: { color: lighten(color, 0.18) } },
        data: s.draw.map((v, i) => (v == null ? null : {
          value: v,
          itemStyle: { borderRadius: m.paired ? (v >= 0 ? [rad, rad, 0, 0] : [0, 0, rad, rad]) : outerPos[i] === si ? [rad, rad, 0, 0] : outerNeg[i] === si ? [0, 0, rad, rad] : 0 },
        })),
      };
    }
    if (s.mark === 'marker') {
      return {
        ...common, type: 'scatter', z: 5, symbol: 'circle', symbolSize: 9,
        itemStyle: { color: t.card, borderColor: color, borderWidth: 2, opacity: 1 },
        emphasis: { scale: false },
        data: s.draw,
      };
    }
    // in a stack the lower band's edge is drawn last, so a zero-height band above never hides it
    const banded = tops.has(s), area = s.mark === 'area' && !banded;
    return {
      ...common, type: 'line', z: s.dim ? 2 : area ? 3 : banded ? 4 + (vis.length - vis.indexOf(s)) : 4, connectNulls: !s.breaks, step: s.step ? 'end' : undefined,
      // a series drawn along a path between readings marks only the readings (all of them while they are few,
      // otherwise only one that stands alone between two gaps)
      showSymbol: !!s.gappy || !!s.path, showAllSymbol: true, symbol: 'circle',
      symbolSize: s.path ? (_, p) => { const i = p.dataIndex; return s.draw[i] != null && (s.gappy || (s.path[i - 1] == null && s.path[i + 1] == null)) ? 7 : 0; } : s.gappy ? 7 : 8,
      lineStyle: { color, width: s.dim ? 1.5 : 2, cap: 'round', join: 'round', opacity: s.faint ? 0.5 : 1 },
      itemStyle: { color, borderColor: t.card, borderWidth: s.gappy ? 1.5 : 2, opacity: s.faint ? 0.5 : 1 },
      areaStyle: area ? { color, opacity: s.wash ?? 0.1 } : undefined,
      emphasis: { scale: false, lineStyle: { width: s.dim ? 1.5 : 2 }, areaStyle: area ? { color, opacity: s.wash ?? 0.1 } : undefined },
      data: banded ? tops.get(s) : s.path || s.draw,
    };
  });
  series.unshift(...bands);
  const quarters = m.unit === 'pct' && (m.share || (m.yMin === 0 && m.yMax === 1));
  // event labels take a second row when two rules sit too close for one
  const evx = (m.events || []).map(e => e.index * band).sort((a, b) => a - b);
  const evRows = evx.some((x, i) => i && x - evx[i - 1] < 96) ? 2 : 1;
  m.evRows = evRows;

  return {
    animation: true, animationDuration: 0, animationDurationUpdate: 200, animationEasingUpdate: 'cubicOut',
    backgroundColor: 'transparent',
    textStyle: { fontFamily: t.sans },
    grid: { left: 0, right, top: 10 + (m.unitLabel ? 14 : 0) + (m.events && m.events.length ? 12 * evRows : 0), bottom: 0, containLabel: true },
    xAxis: {
      type: 'category', data: m.x.map(p => p.key), boundaryGap: hasBars,
      axisLine: { show: false }, axisTick: { show: false }, splitLine: { show: false },
      axisLabel: {
        // every position answers with text of full height (a figure space where there is no tick): the library
        // sizes the room beneath the plot from a sample of positions, and must not find them all empty
        ...baseAxisLabel(t), margin: 12, interval: i => ticks.has(i), formatter: (_, i) => ticks.get(i) || '\u2007',
        // sampled dates are spaced here, and their first and last labels are never dropped
        hideOverlap: !m.cat, alignMinLabel: alignMin, alignMaxLabel: alignMax,
      },
    },
    yAxis: {
      type: 'value', splitNumber: small ? 3 : 4, min: yLo, max: yHi, interval: quarters ? 0.25 : yStep,
      axisLine: { show: false }, axisTick: { show: false },
      splitLine: { lineStyle: { color: t.line, width: 1, type: 'solid' } },
      axisLabel: { ...baseAxisLabel(t), margin: 10, formatter: yFmt },
    },
    series,
  };
}

function hbarOption(m, W) {
  const t = theme();
  const font = `500 12px ${t.sans}`, vfont = `500 12px ${t.mono}`;
  const labW = Math.ceil(Math.max(...m.cats.map(c => textWidth(c, font)))) + 20;
  // a second value per category (values2) draws as a second, lighter bar under the same label
  const two = Array.isArray(m.values2), all = two ? m.values.concat(m.values2) : m.values;
  const valW = Math.ceil(Math.max(...all.map(v => textWidth(fmt(v, m.unit), vfont)))) + 14;
  // values below zero run left of a zero line, with their figure at the left tip
  const lo = Math.min(0, ...all.map(v => v || 0)), pad = lo < 0 ? valW : 0;
  const bars = (vals, k) => ({
    type: 'bar', silent: true, barWidth: 12, barGap: '25%', name: two ? m.names[k] : m.name,
    data: vals.map((v, i) => ({
      value: v,
      itemStyle: { color: m.colors[i], opacity: k ? 0.5 : 1, borderRadius: v < 0 ? [2, 0, 0, 2] : [0, 2, 2, 0] },
      label: { position: v < 0 ? 'left' : 'right' },
    })),
    emphasis: { disabled: true },
    label: { show: true, position: 'right', distance: 8, color: t.text, fontFamily: t.mono, fontSize: 12, fontWeight: 500, formatter: p => fmt(p.value, m.unit) },
    markLine: !k && lo < 0 ? { silent: true, symbol: 'none', animation: false, label: { show: false }, lineStyle: { color: t.lineStrong, width: 1, type: 'solid' }, data: [{ xAxis: 0 }] } : undefined,
  });
  return {
    animation: true, animationDuration: 0, animationDurationUpdate: 200, animationEasingUpdate: 'cubicOut',
    backgroundColor: 'transparent', textStyle: { fontFamily: t.sans },
    grid: { left: labW + pad, right: valW, top: m.unitLabel ? 20 : 6, bottom: 6, containLabel: false },
    yAxis: {
      type: 'category', inverse: true, data: m.cats, axisLine: { show: false }, axisTick: { show: false },
      axisLabel: { color: t.text2, fontFamily: t.sans, fontSize: 12, fontWeight: 500, align: 'left', margin: labW + pad },
    },
    xAxis: { type: 'value', show: false, min: lo < 0 ? lo : 0, max: m.max },
    series: two ? [bars(m.values, 0), bars(m.values2, 1)] : [bars(m.values, 0)],
  };
}

function dotsOption(m, W) {
  const t = theme();
  const font = `500 12px ${t.sans}`;
  const labW = Math.ceil(Math.max(...m.cats.map(c => textWidth(c, font)))) + 20;
  const hi = Math.max(0, ...m.series.flatMap(s => s.values).map(v => v || 0));
  const lo = Math.min(0, ...m.series.flatMap(s => s.values).map(v => v || 0));
  // on a phone the value axis takes three round steps, so its tick labels never touch
  let narrowStep;
  if (W < 520 && hi - lo > 0) {
    const raw = (hi - lo) / 3, pow = 10 ** Math.floor(Math.log10(raw));
    narrowStep = [1, 2, 5, 10].map(k => k * pow).find(v => v >= raw);
  }
  const dot = (s, z) => ({
    type: 'scatter', name: s.label, silent: true, z, symbol: 'circle', symbolSize: 11,
    itemStyle: { color: colorOf(s.color), borderColor: t.card, borderWidth: 2, opacity: 1 },
    emphasis: { scale: false },
    data: s.values.map((v, i) => (v == null ? null : [v, i])),
  });
  return {
    animation: true, animationDuration: 0, animationDurationUpdate: 200, animationEasingUpdate: 'cubicOut',
    backgroundColor: 'transparent', textStyle: { fontFamily: t.sans },
    grid: { left: labW, right: Math.ceil(textWidth(fmt(hi * 1.2, m.unit, { axis: true }), `400 11px ${t.mono}`) / 2) + 8, top: 6, bottom: 24, containLabel: false },
    yAxis: {
      type: 'category', inverse: true, data: m.cats, axisLine: { show: false }, axisTick: { show: false },
      axisLabel: { color: t.text2, fontFamily: t.sans, fontSize: 12, fontWeight: 500, align: 'left', margin: labW },
      splitLine: { show: false },
    },
    xAxis: {
      type: 'value', min: lo < 0 ? undefined : 0, splitNumber: W < 520 ? 3 : 5, interval: narrowStep, max: narrowStep && lo >= 0 ? Math.ceil(hi / narrowStep) * narrowStep : undefined,
      axisLine: { show: false }, axisTick: { show: false },
      splitLine: { lineStyle: { color: t.line, width: 1, type: 'solid' } },
      axisLabel: { ...baseAxisLabel(t), margin: 10, hideOverlap: true, formatter: v => fmt(v, m.unit, { axis: true }) },
    },
    series: [
      {
        // the hairline runs from the cheapest venue to the dearest on each row
        type: 'custom', silent: true, z: 2, name: 'link',
        data: m.cats.map((_, i) => {
          const v = m.series.map(s => s.values[i]).filter(x => x != null);
          return v.length > 1 ? [Math.min(...v), Math.max(...v), i] : [null, null, i];
        }),
        renderItem(params, api) {
          const a = api.value(0), b = api.value(1);
          if (a == null || b == null || Number.isNaN(a) || Number.isNaN(b)) return null;
          const p1 = api.coord([a, api.value(2)]), p2 = api.coord([b, api.value(2)]);
          return { type: 'line', shape: { x1: p1[0], y1: p1[1], x2: p2[0], y2: p2[1] }, style: { stroke: 'rgba(255,255,255,0.18)', lineWidth: 1 } };
        },
      },
      // the first series (Omni) is drawn last, on top
      ...m.series.map((s, k) => dot(s, 3 + m.series.length - k)),
    ],
  };
}

// Horizontal waterfall: totals as full bars from zero, deductions as floating bars that take one total down
// to the next, parts of the last total beneath it. The value sits at each bar's end; hairlines carry the level.
function waterfallOption(m, W) {
  const t = theme();
  const font = `500 12px ${t.sans}`, vfont = `500 12px ${t.mono}`;
  const labW = Math.ceil(Math.max(...m.cats.map(c => textWidth(c, font)))) + 20;
  const valW = Math.ceil(Math.max(...m.bars.map(b => textWidth(b.text, vfont)))) + 14;
  const lo = Math.min(0, ...m.bars.map(b => Math.min(b.x0, b.x1))), hi = Math.max(0, ...m.bars.map(b => Math.max(b.x0, b.x1)));
  const neg = lo < 0;
  return {
    animation: true, animationDuration: 0, animationDurationUpdate: 200, animationEasingUpdate: 'cubicOut',
    backgroundColor: 'transparent', textStyle: { fontFamily: t.sans },
    grid: { left: labW + (neg ? valW : 0), right: valW, top: m.unitLabel ? 20 : 6, bottom: 6, containLabel: false },
    yAxis: {
      type: 'category', inverse: true, data: m.cats, axisLine: { show: false }, axisTick: { show: false },
      axisLabel: { color: t.text2, fontFamily: t.sans, fontSize: 12, fontWeight: 500, align: 'left', margin: labW + (neg ? valW : 0) },
    },
    xAxis: { type: 'value', show: false, min: lo, max: hi },
    series: [{
      type: 'custom', silent: true, name: m.name, data: m.bars.map((b, i) => [b.x0, b.x1, i]),
      renderItem(params, api) {
        const i = api.value(2), b = m.bars[i];
        const p0 = api.coord([b.x0, i]), p1 = api.coord([b.x1, i]);
        const left = Math.min(p0[0], p1[0]), w = Math.max(1, Math.abs(p1[0] - p0[0])), y = p0[1], hgt = 12, r = Math.min(2, w / 2);
        const kids = [{
          type: 'rect', shape: { x: left, y: y - hgt / 2, width: w, height: hgt, r: b.x1 < 0 && b.x0 === 0 ? [r, 0, 0, r] : b.x0 === 0 ? [0, r, r, 0] : [r, r, r, r] },
          style: { fill: b.color },
        }];
        // the value at the bar's end: right of a bar, left of one that runs below zero
        const below = b.x1 < 0 && b.x0 === 0;
        kids.push({ type: 'text', x: below ? left - 8 : left + w + 8, y: y - 7, style: { text: b.text, fill: t.text, font: vfont, textAlign: below ? 'right' : 'left' } });
        // a hairline carries this row's level down to the next row
        if (b.link != null && i < m.bars.length - 1) {
          const x = Math.round(api.coord([b.link, i])[0]) + 0.5, y2 = api.coord([b.link, i + 1])[1];
          kids.push({ type: 'line', shape: { x1: x, y1: y + hgt / 2 + 2, x2: x, y2: y2 - hgt / 2 - 2 }, style: { stroke: t.lineStrong, lineWidth: 1 } });
        }
        return { type: 'group', children: kids };
      },
    }],
  };
}

function lighten(hex, f) {
  const c = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16));
  return '#' + c.map(v => Math.round(v + (255 - v) * f).toString(16).padStart(2, '0')).join('');
}

// ---------- chart instance ----------
const live = new Set();
const ro = new ResizeObserver(entries => {
  for (const e of entries) { const c = e.target.__chart; if (c) c.onResize(); }
});

export class Chart {
  constructor(host, opts = {}) {
    this.host = host;
    this.small = !!opts.small;
    this.onHover = opts.onHover || null;
    this.cv = el('div', 'plot-canvas');
    this.cv.tabIndex = 0;
    this.cv.setAttribute('role', 'img');
    if (opts.label) this.cv.setAttribute('aria-label', opts.label);
    this.xh = el('div', 'xhair');
    this.tip = el('div', 'tip');
    this.tip.setAttribute('role', 'status');
    host.replaceChildren(this.cv, this.xh, this.tip);
    host.__chart = this;
    this.ec = echarts.init(this.cv, null, { renderer: 'canvas' });
    this.idx = -1;
    this.w = host.clientWidth; this.h = host.clientHeight;
    const zr = this.ec.getZr();
    zr.on('mousemove', e => this.pointer(e.offsetX, e.offsetY));
    zr.on('globalout', () => this.leave());
    this.cv.addEventListener('keydown', e => this.key(e));
    this.cv.addEventListener('focus', () => { if (this.cv.matches(':focus-visible')) this.focusLast(); });
    this.cv.addEventListener('blur', () => this.leave());
    ro.observe(host);
    live.add(this);
  }

  static hideAll(except) { live.forEach(c => { if (c !== except) c.hide(); }); }

  render(model, instant) {
    this.model = model;
    this.hide();
    this.w = this.host.clientWidth; this.h = this.host.clientHeight;
    if (this.ec.getWidth() !== this.w || this.ec.getHeight() !== this.h) this.ec.resize();
    const build = model.type === 'hbar' ? hbarOption : model.type === 'dots' ? dotsOption : model.type === 'waterfall' ? waterfallOption : timeOption;
    const option = build(model, this.w, this.h, this.small);
    if (instant) option.animationDurationUpdate = 0; // a resize redraws at once; only data changes ease
    this.ec.setOption(option, true);
    this.overlays();
  }

  onResize() {
    const w = this.host.clientWidth, h = this.host.clientHeight;
    if (w === this.w && h === this.h) return;
    this.ec.resize();
    if (this.model && w > 0) this.render(this.model, true);
  }

  rect() {
    const g = this.ec.getModel().getComponent('grid');
    return g && g.coordinateSystem.getRect();
  }

  // Zero line, event rules and line-end labels are drawn after layout, from real pixel positions.
  overlays() {
    const m = this.model, t = theme(), r = this.rect();
    const g = [];
    if (m && m.type === 'dots' && r && m.missing) {
      // a market without matched quotes in the range keeps its row, with a quiet note in place of the dots
      m.cats.forEach((_, i) => {
        if (m.rowOk[i]) return;
        const y = this.ec.convertToPixel({ yAxisIndex: 0 }, i);
        g.push({ type: 'text', silent: true, z: 3, x: r.x + 2, y: y - 6, style: { text: m.missing, fill: t.text3, font: `400 12px ${t.sans}` } });
      });
    }
    // a cross-section says which date it shows, where a bar chart says what one bar is
    if (m && (m.type === 'hbar' || m.type === 'waterfall') && m.unitLabel) g.push({ type: 'text', silent: true, z: 1, x: 0, y: 1, style: { text: m.unitLabel, fill: t.text3, font: `400 11px ${t.sans}` } });
    // The watermark: the lockup in white at 14%, inside the plot's bottom-right corner, over the series. Large
    // plots over time only (never a plot whose rows are categories, where it would sit on the last row), and
    // never in an exported image, whose footer carries the lockup.
    if (m && r && this.wm && !this.bare && m.type === 'time' && r.width >= 300) {
      const k = lockup(this.wm === 16 && r.width >= 480 ? 16 : 13);
      const xr = Math.round(r.x + r.width - 12), base = Math.round(r.y + r.height - 10), size = +k.f1.split(' ')[1].replace('px', '');
      const txt = (text, font, x) => ({ type: 'text', silent: true, z: 9, x, y: base + Math.round(size * 0.24), style: { text, font, fill: '#fff', opacity: 0.14, verticalAlign: 'bottom', textVerticalAlign: 'bottom' } });
      g.push(
        { type: 'image', silent: true, z: 9, style: { image: markUri('#ffffff'), x: xr - k.width, y: base - k.mh, width: k.mw, height: k.mh, opacity: 0.14 } },
        txt('Variational', k.f1, xr - k.w2 - k.sp - k.w1), txt('Monitor', k.f2, xr - k.w2));
    }
    if (!m || m.type !== 'time' || !r) { this.ec.setOption({ graphic: g }, { replaceMerge: ['graphic'] }); return; }
    // bars say what one bar is: a small unit label at the top-left of the plot
    if (m.unitLabel) g.push({ type: 'text', silent: true, z: 1, x: 0, y: 1, style: { text: m.unitLabel, fill: t.text3, font: `400 11px ${t.sans}` } });
    const px = i => this.ec.convertToPixel({ xAxisIndex: 0 }, i);
    const py = v => this.ec.convertToPixel({ yAxisIndex: 0 }, v);
    const vis = m.series.filter(s => !s.hidden);

    // the zero line reads one step stronger than the grid: over the grid's own line where zero is the foot of
    // the axis, and as a rule of its own where the series cross it
    const ext = this.ec.getModel().getComponent('yAxis').axis.scale.getExtent();
    if (ext[0] <= 0 && ext[1] >= 0) {
      const y = Math.round(py(0)) + 0.5, crosses = vis.some(s => s.draw.some(v => v < 0));
      g.push({ type: 'line', silent: true, z: 1, shape: { x1: r.x, y1: y, x2: r.x + r.width, y2: y }, style: { stroke: crosses ? t.lineStrong : 'rgba(255,255,255,0.045)', lineWidth: 1 } });
    }

    // a line ends in a dot: 3px in the series colour with a 2px ring of the card behind it
    vis.filter(s => s.mark === 'line' && !s.gappy && !s.dim).forEach(s => {
      let i = s.draw.length - 1;
      while (i >= 0 && s.draw[i] == null) i--;
      if (i < 1) return;
      g.push({ type: 'circle', silent: true, z: 7, shape: { cx: px(i), cy: py(s.draw[i]), r: 4 }, style: { fill: colorOf(s.color), stroke: t.card, lineWidth: 2, opacity: s.faint ? 0.5 : 1 } });
    });

    if (m.events && m.events.length) {
      const font = `500 10px ${t.sans}`;
      const evs = m.events.map(e => ({ ...e, x: Math.round(px(e.index)) + 0.5, w: textWidth(e.label, font) })).sort((a, b) => a.x - b.x);
      // a label sits beside the top of its rule: to the right if it fits, else to the left, else one row up
      const rows = Array.from({ length: m.evRows || 1 }, () => []);
      const free = (row, a, b) => a >= 0 && b <= this.w && row.every(([x0, x1]) => b + 6 <= x0 || a >= x1 + 6);
      evs.forEach(e => {
        let placed = null;
        for (let k = 0; k < rows.length && !placed; k++) {
          const others = evs.filter(o => o !== e).map(o => o.x);
          const clear = (a, b) => k > 0 || others.every(x => x < a - 3 || x > b + 3);
          if (free(rows[k], e.x + 5, e.x + 5 + e.w) && clear(e.x + 5, e.x + 5 + e.w)) placed = { k, left: true, span: [e.x + 5, e.x + 5 + e.w] };
          else if (free(rows[k], e.x - 5 - e.w, e.x - 5) && clear(e.x - 5 - e.w, e.x - 5)) placed = { k, left: false, span: [e.x - 5 - e.w, e.x - 5] };
        }
        const up = placed ? placed.k * 12 : 0;
        g.push({ type: 'line', silent: true, z: 1, shape: { x1: e.x, y1: r.y - 6 - up, x2: e.x, y2: r.y + r.height }, style: { stroke: 'rgba(255,255,255,0.14)', lineWidth: 1 } });
        if (!placed) return;
        rows[placed.k].push(placed.span);
        g.push({ type: 'text', silent: true, z: 1, x: placed.left ? e.x + 5 : e.x - 5, y: r.y - 17 - up, style: { text: e.label, fill: t.text3, font, textAlign: placed.left ? 'left' : 'right' } });
      });
    }

    const ends = vis.filter(s => s.endLabel);
    if (ends.length) {
      const font = `500 11px ${t.sans}`;
      const items = ends.map(s => {
        let i = s.draw.length - 1;
        while (i >= 0 && s.draw[i] == null) i--;
        if (i < 0) return null;
        return { s, x: px(i), y: py(s.draw[i]) };
      }).filter(Boolean).sort((a, b) => a.y - b.y);
      const H = 14, top = r.y + 2, bottom = r.y + r.height - 2;
      items.forEach((it, i) => { it.ly = i ? Math.max(it.y, items[i - 1].ly + H) : Math.max(top, it.y); });
      for (let i = items.length - 1; i >= 0; i--) {
        const max = i === items.length - 1 ? bottom : items[i + 1].ly - H;
        if (items[i].ly > max) items[i].ly = max;
      }
      items.forEach(o => {
        const x0 = Math.max(o.x, r.x + r.width);
        if (Math.abs(o.ly - o.y) > 3) {
          g.push({ type: 'polyline', silent: true, z: 1, shape: { points: [[x0 + 3, o.y], [x0 + 8, o.ly], [x0 + 10, o.ly]] }, style: { stroke: 'rgba(255,255,255,0.18)', lineWidth: 1, fill: 'none' } });
        }
        g.push({ type: 'text', silent: true, z: 6, x: x0 + (Math.abs(o.ly - o.y) > 3 ? 12 : 8), y: o.ly - 6, style: { text: o.s.label, fill: o.s.strong ? t.text : t.text2, font: o.s.strong ? `600 11px ${t.sans}` : font } });
      });
    }
    this.ec.setOption({ graphic: g }, { replaceMerge: ['graphic'] });
  }

  // ----- hover layer -----
  count() { const m = this.model; return !m ? 0 : m.type === 'time' ? m.x.length : m.cats.length; }

  pointer(x, y) {
    const m = this.model;
    if (!m) return;
    const raw = m.type === 'time' ? this.ec.convertFromPixel({ xAxisIndex: 0 }, x) : this.ec.convertFromPixel({ yAxisIndex: 0 }, y);
    if (raw == null || Number.isNaN(raw)) return;
    this.setHover(this.observed(clamp(Math.round(raw), 0, this.count() - 1)), x, y, true);
  }

  /** The nearest index that holds an observation, so the crosshair never rests on an empty day. */
  observed(i) {
    const m = this.model;
    if (m.type !== 'time') return i;
    const has = k => m.series.some(s => !s.hidden && s.values[k] != null);
    for (let d = 0; d < m.x.length; d++) {
      if (i - d >= 0 && has(i - d)) return i - d;
      if (i + d < m.x.length && has(i + d)) return i + d;
    }
    return i;
  }

  key(e) {
    const n = this.count();
    if (!n) return;
    const fwd = this.model.type === 'time' ? 'ArrowRight' : 'ArrowDown', back = this.model.type === 'time' ? 'ArrowLeft' : 'ArrowUp';
    if (e.key === 'Escape') { this.leave(); return; }
    if (e.key !== fwd && e.key !== back && e.key !== 'Home' && e.key !== 'End') return;
    e.preventDefault();
    const cur = this.idx < 0 ? n - 1 : this.idx;
    let i = e.key === 'Home' ? 0 : e.key === 'End' ? n - 1 : clamp(cur + (e.key === fwd ? 1 : -1), 0, n - 1);
    if (this.model.type === 'time') {
      const has = k => this.model.series.some(s => !s.hidden && s.values[k] != null);
      const step = e.key === fwd || e.key === 'Home' ? 1 : -1;
      while (i > 0 && i < n - 1 && !has(i)) i += step;
      if (!has(i)) i = this.observed(i);
    }
    this.setHover(i, null, null, true);
  }

  focusLast() { const n = this.count(); if (n) this.setHover(this.model.type === 'time' ? this.observed(n - 1) : 0, null, null, true); }

  leave() { this.hide(); if (this.onHover) this.onHover(-1, this); }

  hide() {
    if (this.idx < 0) return;
    this.idx = -1;
    this.xh.style.opacity = '0';
    this.tip.style.opacity = '0';
    this.ec.dispatchAction({ type: 'downplay' });
  }

  /** Show crosshair and tooltip at index i. Pointer coordinates are optional (keyboard, linked panels). */
  setHover(i, x, y, emit, quiet) {
    const m = this.model, r = this.rect();
    if (!m || !r || i < 0 || i >= this.count()) return;
    if (emit) Chart.hideAll(this);
    const changed = i !== this.idx;
    this.idx = i;
    const time = m.type === 'time';
    const n = this.count();
    const c = time ? this.ec.convertToPixel({ xAxisIndex: 0 }, i) : this.ec.convertToPixel({ yAxisIndex: 0 }, i);
    const xs = this.xh.style;
    if (time) {
      const band = m.series.some(s => !s.hidden && s.mark === 'bar') ? r.width / n : 0;
      xs.left = `${band ? c - band / 2 : Math.round(c)}px`; xs.width = `${band ? band : 1}px`;
      xs.top = `${r.y}px`; xs.height = `${r.height}px`;
      xs.background = band ? 'rgba(255,255,255,0.05)' : 'rgba(255,255,255,0.24)';
    } else {
      const band = r.height / n;
      xs.left = '0px'; xs.width = `${this.w}px`; xs.top = `${c - band / 2}px`; xs.height = `${band}px`;
      xs.background = 'rgba(255,255,255,0.04)';
    }
    xs.opacity = '1';
    if (changed) {
      this.ec.dispatchAction({ type: 'downplay' });
      const idxs = this.ec.getOption().series.map((_, k) => k);
      if (time) this.ec.dispatchAction({ type: 'highlight', seriesIndex: idxs, dataIndex: i });
      if (!quiet) this.tip.replaceChildren(...tipContent(m, i));
    }
    if (quiet) { this.tip.style.opacity = '0'; }
    else {
      const tw = this.tip.offsetWidth, th = this.tip.offsetHeight;
      let left, top;
      if (time) {
        left = c + 14;
        if (left + tw > this.w) left = c - 14 - tw;
        left = clamp(left, 0, Math.max(0, this.w - tw));
        top = isPhone() || y == null ? r.y + 4 : clamp(y - th / 2, 0, Math.max(0, this.h - th));
      } else {
        left = clamp((x == null ? r.x + r.width / 2 : x) + 14, 0, Math.max(0, this.w - tw));
        if (x != null && left < x + 14) left = clamp(x - 14 - tw, 0, Math.max(0, this.w - tw));
        top = clamp(c - th / 2, 0, Math.max(0, this.h - th));
      }
      this.tip.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
      this.tip.style.opacity = '1';
    }
    if (emit && changed && this.onHover) this.onHover(i, this);
  }

  /** The rendered plot as a canvas, for PNG export: without the watermark, since the image's footer carries the lockup. */
  snapshot(pixelRatio = 2) {
    this.bare = true; this.overlays();
    const cv = this.ec.getRenderedCanvas({ pixelRatio, backgroundColor: theme().card });
    this.bare = false; this.overlays();
    return cv;
  }

  dispose() {
    ro.unobserve(this.host);
    live.delete(this);
    this.host.__chart = null;
    this.ec.dispose();
  }
}

// ---------- tooltip content (textContent only: names are untrusted API strings) ----------
function tipRow(color, value, name, cls, kind) {
  const row = el('div', 'tip-row' + (cls ? ' ' + cls : ''));
  const key = el('span', 'tip-key' + (kind ? ' ' + kind : ''));
  if (color) key.style.background = color; else key.style.visibility = 'hidden';
  row.append(key, el('span', 'tip-v', value), el('span', 'tip-n', name));
  return row;
}

function tipContent(m, i) {
  const out = [];
  if (m.type === 'time') {
    out.push(el('div', 'tip-d', m.x[i].label));
    const ev = (m.events || []).filter(e => e.index === i);
    ev.forEach(e => out.push(el('div', 'tip-e', e.label)));
    const rows = el('div', 'tip-rows');
    const vis = m.series.filter(s => !s.hidden && s.values[i] != null);
    const order = m.stacked ? [...vis].reverse() : vis;
    order.forEach(s => rows.append(tipRow(colorOf(s.color), fmt(s.values[i], s.unit || m.unit), s.label, '', s.mark === 'line' ? '' : s.mark === 'marker' ? 'dot' : 'sq')));
    if (!vis.length) rows.append(tipRow(null, '—', 'No observation'));
    if (m.total && m.total.values[i] != null && vis.length > 1) rows.append(tipRow(null, fmt(m.total.values[i], m.total.unit || m.unit), m.total.label, 'is-total'));
    // figures behind the plotted one (long, short and their difference behind a ratio)
    (m.tipExtra || []).forEach((x, k) => { if (x.values[i] != null) rows.append(tipRow(null, fmt(x.values[i], x.unit), x.label, k === 0 ? 'is-total' : '')); });
    out.push(rows);
    return out;
  }
  out.push(el('div', 'tip-d', m.tipTitles ? m.tipTitles[i] : m.cats[i]));
  const rows = el('div', 'tip-rows');
  if (m.type === 'waterfall') {
    rows.append(tipRow(m.bars[i].color, m.bars[i].text, m.name, '', 'sq'));
  } else if (m.type === 'hbar') {
    rows.append(tipRow(m.colors[i], fmt(m.values[i], m.unit), m.values2 ? m.names[0] : m.name, '', 'sq'));
    if (m.values2) rows.append(tipRow(m.colors[i], fmt(m.values2[i], m.unit), m.names[1], '', 'sq'));
    (m.extra || []).forEach(x => rows.append(tipRow(null, fmt(x.values[i], x.unit), x.label)));
  } else {
    if (!m.rowOk[i]) rows.append(tipRow(null, '\u2014', m.missing || 'No observation'));
    else m.series.forEach(s => { if (s.values[i] != null) rows.append(tipRow(colorOf(s.color), fmt(s.values[i], m.unit), s.labels ? s.labels[i] : s.label, '', 'dot')); });
    (m.extra || []).forEach(x => rows.append(tipRow(null, fmt(x.values[i], x.unit), x.label)));
  }
  out.push(rows);
  return out;
}
