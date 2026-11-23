// Page checks for the deploy gate. Needs Node 18+ and the `playwright` package (qa/package.json).
//
//   node qa/render.mjs --out qa/out/render.json            every route, every range, 1440 / 1280 / 390 wide
//   node qa/render.mjs --widths 390 --ranges 90d,all       a subset
//   node qa/render.mjs --shots qa/out/shots                also save one full-page screenshot per route and width
//   node qa/render.mjs --probe                             exit 0 if a browser can be launched, 1 if not
//
// qa/gate.py --render runs this and merges the result. Run alone, it prints the findings and exits 1 on a
// blocking one.
//
// Routes, card ids and data files are read from site/js/catalog.js. A card names its page as `route`, `tab`
// or `page`; the value is a path (omni/markets) or a page id that the exported navigation maps to a path.
// Pages without catalog cards come from the navigation (Research Library, Data). One more page is derived
// from the data: one market page (<route of mk.table>/<TICKER>).
// The sweep is range-only: the site has no granularity control.
//
// Blocking
//   catalog     card ids unique, every card names a route and a data file
//   residency   the owner's rule (v3.1): an identical chart twice blocks (two cards drawing the same series,
//               derived here from the catalog); an identical headline figure on two charts or two stat tiles
//               of the same page blocks, read from the rendered page. Cells of data tables are never read, so
//               the statement table, the markets table and ranked tables are exempt. The waterfall's bar-end
//               values count only while its Period is the page default, which is how this sweep visits it.
//               Research and Data pages point at cards and are exempt.
//   console     no console error, page error or failed request in any state
//   cards       every catalog card is on its route
//   empty       no card says "No data yet" while its data file has values; no chart without a single point
//   scroll      the page never scrolls sideways
//   fixture     the FIXTURE DATA bar is never shown
// Warnings: word budgets, banned words, colons in titles, a series drawn on two cards, differences from the
// plan's catalog.
//
// What it reads from the page (keep these hooks): html.is-ready, section.card[data-card=<id>] (or id
// card-<id>), .is-loading, .is-empty / .nodata, .card-title, .hl-v and .hl-l, .plot with an ECharts
// instance on .plot-canvas, table rows in .tbl, #fixture.
//
// Browser: Playwright's own Chromium when it is installed, otherwise the installed Google Chrome
// (GitHub's ubuntu runners ship one). VM_BROWSER=chrome forces Chrome; VM_BROWSER=/path/to/binary uses that.

import { createServer } from 'node:http';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.dirname(HERE);

const argv = process.argv.slice(2);
const opt = (name, fallback) => { const i = argv.indexOf(`--${name}`); return i >= 0 && argv[i + 1] ? argv[i + 1] : fallback; };
const SITE = path.resolve(opt('site', path.join(ROOT, 'site')));
const DATA = process.env.VM_SITE_DATA ? path.resolve(process.env.VM_SITE_DATA) : path.join(SITE, 'data');
const OUT = opt('out', null);
const SHOTS = opt('shots', null);
const DUMP = opt('dump', null);            // write what was read from every page state (debugging)
const dump = [];
const WIDTHS = opt('widths', '1440,1280,390').split(',').map(Number);
const RANGES = opt('ranges', '7d,30d,90d,180d,1y,all').split(',');
const HEIGHT = { 1440: 900, 1280: 800, 390: 844 };

// Routes, card ids and data files all come from site/js/catalog.js. The plan (.cache/plan/01-catalog.md) is
// compared with it when the file is on disk (a warning; the plan is not in the repository).
const PLAN = path.join(ROOT, '.cache', 'plan', '01-catalog.md');
const BANNED = ['revolutionary', 'game-changing', 'notably', 'comprehensive', 'robust', 'landscape', 'delve'];
const BUDGET = { page: 2, section: 1, title: 7, headline: 3, option: 3, info: 50 };
const routeValue = c => c.route ?? c.tab ?? c.page;
let pagePath = new Map();                 // page id -> path, from the navigation; filled by staticChecks
const routeOf = c => { const v = routeValue(c); return v == null ? v : (pagePath.get(v) ?? v); };
const isSummary = route => /(^|\/)(research|thesis|data|methodology)(\/|$)/.test(route);   // pointer pages: they repeat figures by design
const clean = v => String(v || '').replace(/^#?\/?/, '').replace(/\/$/, '');

const checks = [];
const check = (name, problems, { blocking = true, note = null } = {}) => checks.push({ name, blocking, problems: [...new Set(problems)], note });
const words = s => String(s || '').replace(/\{[^}]+\}/g, 'x').split(/\s+/).filter(w => w && w !== '·' && w !== '/').length;

// ---------------------------------------------------------------- browser

async function launch() {
  const { chromium } = await import('playwright');
  const want = process.env.VM_BROWSER;
  const tries = [];
  if (want && want !== 'chrome' && want !== 'bundled') tries.push(['executable ' + want, { executablePath: want }]);
  if (!want || want === 'bundled') tries.push(['bundled Chromium', {}]);
  if (!want || want === 'chrome') tries.push(['installed Chrome', { channel: 'chrome' }]);
  const errors = [];
  for (const [label, options] of tries) {
    try {
      const browser = await chromium.launch({ headless: true, ...options });
      return { browser, label: `${label} ${browser.version()}` };
    } catch (e) { errors.push(`${label}: ${String(e.message).split('\n')[0]}`); }
  }
  throw new Error('no browser could be launched (' + errors.join('; ') + '). Run: npx playwright install chromium-headless-shell');
}

// ---------------------------------------------------------------- static server

const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8', '.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.png': 'image/png' };

function serve() {
  const server = createServer(async (req, res) => {
    try {
      let rel = decodeURIComponent(new URL(req.url, 'http://x').pathname);
      if (rel.endsWith('/')) rel += 'index.html';
      const base = rel.startsWith('/data/') ? DATA : SITE;
      const file = path.normalize(path.join(base, rel.startsWith('/data/') ? rel.slice('/data'.length) : rel));
      if (!file.startsWith(base)) { res.writeHead(403).end(); return; }
      const body = await readFile(file);
      res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' }).end(body);
    } catch { res.writeHead(404, { 'Content-Type': 'text/plain' }).end('not found'); }
  });
  return new Promise(resolve => server.listen(0, '127.0.0.1', () => resolve(server)));
}

// ---------------------------------------------------------------- catalog (static)

function seriesKeys(c) {
  const keys = new Set();
  const pre = c.keyPrefix || '';
  const add = list => (list || []).forEach(s => s && s.col && keys.add(`${c.file}:${pre}${s.col}`));
  add(c.series);
  if (c.cumulative) add([c.cumulative]);
  (c.keys || []).forEach(k => keys.add(`${c.file}:${k}`));
  (c.controls || []).forEach(ctl => (ctl.options || []).forEach(o => add(o.set && o.set.series)));
  Object.values(c.byGran || {}).forEach(g => add(g.series));
  (c.panels || []).forEach(p => add(p.series));
  if (c.markers) keys.add(`${c.file}:${c.markers.list}`);
  return keys;
}

function dataPaths(c) {
  // every place in the card's file where its values may live
  const out = new Set();
  const pre = c.keyPrefix || '';
  const add = list => (list || []).forEach(s => s && s.col && out.add(pre + s.col));
  add(c.series);
  (c.controls || []).forEach(ctl => (ctl.options || []).forEach(o => add(o.set && o.set.series)));
  Object.values(c.byGran || {}).forEach(g => add(g.series));
  (c.panels || []).forEach(p => add(p.series));
  (c.keys || []).forEach(k => out.add(k));
  if (c.headline && c.headline.col) out.add(pre + c.headline.col);
  return [...out];
}

const pick = (obj, p) => (p === '*' ? obj : p.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj));
const hasValue = v => (Array.isArray(v) ? v.some(hasValue) : v && typeof v === 'object' ? Object.values(v).some(hasValue) : v !== null && v !== undefined);

function fileHasData(c, market) {
  const name = (c.file || '').replace('<TICKER>', market || 'BTC-PERP');
  const file = path.join(DATA, name + '.json');
  if (!existsSync(file)) return false;
  let doc;
  try { doc = JSON.parse(readFileSync(file, 'utf8')); } catch { return false; }
  if (c.requires && !hasValue(pick(doc, c.requires))) return false;
  const paths = dataPaths(c);
  return paths.length ? paths.some(p => hasValue(pick(doc, p))) : hasValue(doc);
}

// Navigation entries exported by the catalog module, whatever the export is called: every object with a string
// id that is not a card. `section` is true for an entry that holds other entries (a rail section header).
function navEntries(cat) {
  const out = [];
  const visit = (node, depth) => {
    if (Array.isArray(node)) { node.forEach(n => visit(n, depth)); return; }
    if (!node || typeof node !== 'object') return;
    const kids = Object.values(node).filter(v => Array.isArray(v) && v.some(x => x && typeof x === 'object' && !Array.isArray(x)));
    if (typeof node.id === 'string' && !('file' in node) && !('info' in node)) {
      out.push({ id: node.id, path: clean(node.path ?? node.route ?? node.href ?? node.id), label: node.label || node.title || '', section: kids.length > 0 });
    } else if (typeof node.label === 'string' && kids.length > 0) out.push({ id: null, path: null, label: node.label, section: true });
    kids.forEach(k => visit(k, depth + 1));
  };
  for (const [name, value] of Object.entries(cat)) if (name !== 'CATALOG' && Array.isArray(value)) visit(value, 0);
  return out;
}

async function staticChecks() {
  // imported from its text, so that it loads as a module whatever package.json sits above it
  const source = await readFile(path.join(SITE, 'js', 'catalog.js'), 'utf8');
  const cat = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
  const CATALOG = cat.CATALOG;
  if (!Array.isArray(CATALOG) || !CATALOG.length) throw new Error('site/js/catalog.js exports no CATALOG array');
  const nav = navEntries(cat);
  pagePath = new Map(nav.filter(n => n.id && !n.section).map(n => [n.id, n.path]));
  const cardRoutes = [...new Set(CATALOG.map(routeOf).filter(Boolean))];
  // routes to visit: every route that holds a card, then the other pages the navigation names
  const guessed = [...new Set(nav.filter(n => n.id && !n.section).map(n => n.path))].filter(r => r && !cardRoutes.includes(r));
  // a page that comes from the data: one market
  const derived = [];
  const marketsRoute = routeOf(CATALOG.find(c => c.id === 'mk.table') || {}) || cardRoutes.find(r => /(^|\/)markets$/.test(r));
  let ticker = existsSync(path.join(DATA, 'market', 'BTC-PERP.json')) ? 'BTC-PERP' : null;
  try { if (!ticker) ticker = JSON.parse(readFileSync(path.join(DATA, 'market', 'index.json'), 'utf8')).tickers[0]; } catch { /* no market files */ }
  if (marketsRoute && ticker) derived.push({ route: `${marketsRoute}/${ticker}`, what: `the market page of ${ticker}` });
  const problems = [];
  const ids = CATALOG.map(c => c.id);
  ids.filter((id, i) => ids.indexOf(id) !== i).forEach(id => problems.push(`card id ${id} appears twice`));
  CATALOG.filter(c => !routeOf(c)).forEach(c => problems.push(`card ${c.id} names no route`));
  CATALOG.filter(c => !c.file && !c.files).forEach(c => problems.push(`card ${c.id} names no data file`));
  check('catalog: card ids unique, every card has a route and a data file', problems, { note: `${ids.length} cards on ${cardRoutes.length} routes (${cardRoutes.join(', ')})` });
  if (existsSync(PLAN)) {
    // the plan keeps superseded tables under its amendments, so only one direction is meaningful:
    // a card the catalog has that the plan never names
    const named = new Set([...readFileSync(PLAN, 'utf8').matchAll(/`([a-z]+\.[a-z0-9_]+)`/g)].map(m => m[1]));
    const diff = ids.filter(id => !named.has(id)).map(id => `catalog card ${id} is not named anywhere in the plan`);
    check('catalog: every card is named in the plan (.cache/plan/01-catalog.md)', diff, { blocking: false, note: `${named.size} card ids in the plan` });
  }

  // Residency (rule 3): no identical chart, table or printed headline figure twice.
  const owner = new Map(), heads = new Map(), same = [], shared = [], charts = new Map();
  for (const c of CATALOG) {
    // a card on the summary page that names its home card (`pointer`) is that card's copy by design
    if (c.pointer) continue;
    const keys = [...seriesKeys(c)].sort();
    const signature = keys.join('|');
    if (keys.length) {
      if (charts.has(signature)) same.push(`${charts.get(signature)} and ${c.id} draw the same series (${keys.join(', ')})`);
      else charts.set(signature, c.id);
    }
    for (const k of keys) {
      if (owner.has(k) && owner.get(k) !== c.id) shared.push(`series ${k} is on ${owner.get(k)} and ${c.id}`);
      else owner.set(k, c.id);
    }
    // what a card prints as its figure: a chart's headline rule, or a stat tile's figure path
    const fig = c.headline ? `${c.file}:${c.keyPrefix || ''}${c.headline.col || signature}:${c.headline.rule}`
      : c.figure ? `${c.file}:${c.figure.path}${c.figure.of ? '/' + c.figure.of : ''}` : null;
    if (fig) {
      const h = `${routeOf(c)}|${fig}`;      // the rule is per page
      if (heads.has(h)) same.push(`${routeOf(c)}: ${heads.get(h)} and ${c.id} print the same figure (${fig})`);
      heads.set(h, c.id);
    }
  }
  check('residency: no identical chart, and no figure defined twice on one page (catalog)', same, { note: `${owner.size} series keys, ${heads.size} figures on tiles and chart headlines` });
  if (shared.length) check('residency: a series drawn on two cards (allowed only where the charts differ)', shared, { blocking: false });

  // copy: warnings only
  const copy = [];
  const banned = (where, text) => BANNED.forEach(w => { if (new RegExp(`\\b${w}\\b`, 'i').test(String(text || ''))) copy.push(`banned word "${w}" in ${where}`); });
  nav.forEach(n => { const cap = n.section ? BUDGET.section : BUDGET.page; if (n.label && words(n.label) > cap) copy.push(`${n.section ? 'section header' : 'page label'} over ${cap} word${cap > 1 ? 's' : ''}: ${n.label}`); });
  for (const c of CATALOG) {
    if (words(c.title) > BUDGET.title) copy.push(`${c.id}: title over ${BUDGET.title} words (${words(c.title)}): ${c.title}`);
    if (/[:?]/.test(c.title || '')) copy.push(`${c.id}: title has a colon or a question mark: ${c.title}`);
    if (words(c.info) > BUDGET.info) copy.push(`${c.id}: info text over ${BUDGET.info} words (${words(c.info)})`);
    if (c.headline && c.headline.label && words(c.headline.label) > BUDGET.headline) copy.push(`${c.id}: headline label over ${BUDGET.headline} words: ${c.headline.label}`);
    (c.controls || []).forEach(ctl => (ctl.options || []).forEach(o => { if (words(o.label) > BUDGET.option) copy.push(`${c.id}: control option over ${BUDGET.option} words: ${o.label}`); }));
    (c.panels || []).forEach(p => { if (/[:?]/.test(p.title || '')) copy.push(`${c.id}: panel title has a colon: ${p.title}`); });
    banned(`${c.id} title`, c.title); banned(`${c.id} info`, c.info); banned(`${c.id} source`, c.source);
  }
  check('copy: word budgets, banned words, colons in titles (catalog)', copy, { blocking: false });
  const waterfalls = CATALOG.filter(c => c.form === 'waterfall').map(c => c.id);
  const tiles = new Set(CATALOG.filter(c => c.form === 'stat').map(c => c.id));
  return { CATALOG, cardRoutes, guessed, derived, waterfalls, tiles };
}

// ---------------------------------------------------------------- one page state

const inspect = waterfalls => {
  const text = el => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  const cards = [...document.querySelectorAll('section.card')].map(el => {
    const id = el.getAttribute('data-card') || el.id.replace(/^card-/, '');
    // a waterfall draws the value at each bar's end on the canvas: read the text elements it drew
    let barEnds = [];
    if (waterfalls.includes(id) && window.echarts) {
      for (const host of el.querySelectorAll('.plot-canvas, .plot')) {
        const inst = window.echarts.getInstanceByDom(host);
        if (!inst) continue;
        barEnds = inst.getZr().storage.getDisplayList().map(e => e.style && e.style.text).filter(t => typeof t === 'string' && /\d/.test(t)).map(t => t.trim());
        break;
      }
    }
    const plots = [...el.querySelectorAll('.plot')].map(p => {
      const host = p.querySelector('.plot-canvas') || p;
      const inst = window.echarts && (window.echarts.getInstanceByDom(host) || window.echarts.getInstanceByDom(p));
      let points = null;
      if (inst) {
        points = 0;
        for (const s of inst.getOption().series || []) {
          for (const d of s.data || []) {
            const v = d && typeof d === 'object' && !Array.isArray(d) ? d.value : d;
            const last = Array.isArray(v) ? v.filter(x => x !== null && x !== undefined && x !== '-') : [v];
            if (Array.isArray(v) ? last.length >= 2 || (last.length === 1 && typeof last[0] === 'number') : (v !== null && v !== undefined && v !== '-')) points++;
          }
        }
      }
      const r = p.getBoundingClientRect();
      return { points, w: Math.round(r.width), h: Math.round(r.height), canvas: !!p.querySelector('canvas') };
    });
    return {
      id, barEnds,
      hidden: el.hidden || getComputedStyle(el).display === 'none',
      empty: el.classList.contains('is-empty') || !!el.querySelector('.nodata'),
      nodata: text(el.querySelector('.nodata')),
      loading: el.classList.contains('is-loading'),
      title: text(el.querySelector('.card-title')),
      headline: text(el.querySelector('.hl-v')), headlineLabel: text(el.querySelector('.hl-l')),
      figures: [...el.querySelectorAll('.hl-v, .stat-v')].map(text).filter(Boolean),
      rows: el.querySelectorAll('.tbl tbody tr, tbody tr, [role="row"]:not(.th-head), .method-row:not([hidden])').length,
      cells: el.querySelectorAll('.heat-cell, .hm-cell, [data-cell]').length,
      plots,
    };
  });
  const de = document.documentElement;
  const fx = document.getElementById('fixture');
  return {
    cards, hash: location.hash, title: document.title,
    scrollWidth: de.scrollWidth, clientWidth: de.clientWidth,
    fixture: !!fx && !fx.hidden,
    stale: !!document.querySelector('.asof.is-stale'),
    widest: de.scrollWidth > de.clientWidth + 1
      ? [...document.querySelectorAll('body *')].filter(e => e.getBoundingClientRect().right > de.clientWidth + 1).slice(0, 3)
        .map(e => `${e.tagName.toLowerCase()}${e.id ? '#' + e.id : ''}${e.className && typeof e.className === 'string' ? '.' + e.className.trim().split(/\s+/).join('.') : ''}`)
      : [],
  };
};

async function settle(page, traffic) {
  // no request in flight, no card still loading, then two frames for the charts
  const deadline = Date.now() + 15000;
  let quiet = 0;
  while (Date.now() < deadline) {
    const loading = await page.evaluate(() => !document.documentElement.classList.contains('is-ready') || !!document.querySelector('.card.is-loading') || !document.querySelector('section.card'));
    if (!loading && traffic.open === 0) { if (++quiet >= 2) break; } else quiet = 0;
    await page.waitForTimeout(60);
  }
  await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
  return Date.now() < deadline;
}

// ---------------------------------------------------------------- main

async function main() {
  if (argv.includes('--probe')) {
    try { const { browser, label } = await launch(); await browser.close(); console.log('browser ok: ' + label); process.exit(0); }
    catch (e) { console.log(String(e.message)); process.exit(1); }
  }
  const started = Date.now();
  if (!existsSync(path.join(SITE, 'index.html'))) throw new Error(`no index.html in ${SITE}`);
  const { CATALOG, cardRoutes, guessed, derived, waterfalls, tiles } = await staticChecks();
  const byId = new Map(CATALOG.map(c => [c.id, c]));
  const routes = (opt('routes', null) ? opt('routes').split(',') : cardRoutes.concat(guessed, derived.map(d => d.route)));
  const notRoutes = new Set();

  const server = await serve();
  const base = `http://127.0.0.1:${server.address().port}/`;
  const { browser, label } = await launch();
  const found = { console: [], empty: [], blank: [], scroll: [], fixture: [], settle: [], missing: [], headline: new Map(), budget: [], inspect: [] };
  let states = 0, stale = false;

  let unsettled = 0;
  visit: for (const width of WIDTHS) {
    const context = await browser.newContext({ viewport: { width, height: HEIGHT[width] || 900 }, deviceScaleFactor: 1, reducedMotion: 'reduce' });
    const page = await context.newPage();
    const traffic = { open: 0 };
    let where = 'start';
    page.on('request', () => { traffic.open++; });
    const done = () => { traffic.open = Math.max(0, traffic.open - 1); };
    page.on('requestfinished', done);
    page.on('requestfailed', req => { done(); found.console.push(`${where}: request failed ${req.url().replace(base, '')} (${req.failure() ? req.failure().errorText : ''})`); });
    page.on('response', res => {
      if (res.status() < 400) return;
      const rel = res.url().replace(base, '').split('?')[0];
      found.console.push(`${where}: HTTP ${res.status()} for ${rel}`);
    });
    page.on('console', msg => { if (msg.type() === 'error' && !/Failed to load resource/.test(msg.text())) found.console.push(`${where}: console error: ${msg.text().slice(0, 200)}`); });
    page.on('pageerror', err => found.console.push(`${where}: page error: ${String(err.message).slice(0, 200)}`));

    await page.goto(base + '#/overview?range=90d', { waitUntil: 'load' });
    await settle(page, traffic);
    for (const route of routes) {
      for (const range of RANGES) {
        where = `${route} ${range} @${width}`;
        await page.evaluate(h => { location.hash = h; }, `#/${route}?range=${range}`);
        const ok = await settle(page, traffic);
        if (!ok) {
          found.settle.push(`${where}: the page did not settle within 15 seconds`);
          if (++unsettled >= 3) { found.settle.push('three states in a row did not settle: the remaining states were not visited'); break visit; }
        } else unsettled = 0;
        const s = await page.evaluate(inspect, waterfalls);
        states++;
        if (DUMP) dump.push({ route, range, width, ...s });
        stale = stale || s.stale;
        if (!s.hash.startsWith(`#/${route}`)) {
          // a page name taken from the navigation that the router does not know is simply not a route
          if (guessed.includes(route)) { notRoutes.add(route); states--; break; }
          const d = derived.find(x => x.route === route);
          found.settle.push(d ? `#/${route} (${d.what}) is not a page: the router went to ${s.hash}` : `${where}: the router went to ${s.hash}`);
          if (d) { notRoutes.add(route); states--; break; }
        }
        if (s.fixture) found.fixture.push(`${where}: the FIXTURE DATA bar is showing`);
        if (s.scrollWidth > s.clientWidth + 1) found.scroll.push(`${where}: page is ${s.scrollWidth}px wide in a ${s.clientWidth}px viewport (${s.widest.join(', ')})`);
        const visible = s.cards.filter(c => !c.hidden);
        if (!visible.length) found.empty.push(`${where}: no card on the page`);
        // a card flagged `page: true` lives on the sub-pages of its route (the market page), not on the route itself,
        // and each of its panels is a card there (`<id>.<panel>`). A card with `only` belongs to one scope or one
        // filter value; this sweep visits the default scope, so only the cards of `all.all` are expected.
        // a card flagged `hideEmpty` stays off the page until its series exists, so its absence is not a fault
        const inDefault = c => !c.hideEmpty && (!c.only || c.only.scope === 'all.all');
        for (const c of CATALOG.filter(c => !c.requires && inDefault(c) && (c.page === true ? route.startsWith(routeOf(c) + '/') : routeOf(c) === route))) {
          if (s.cards.some(x => (x.id === c.id || (c.page === true && String(x.id).startsWith(c.id + '.'))) && !x.hidden)) continue;
          found.missing.push(`${route}: card ${c.id} is not on the page`);
        }
        for (const c of visible) {
          const entry = byId.get(c.id);
          if (c.empty) {
            const has = entry ? fileHasData(entry) : true;      // a page without catalog cards (research, data, a market) always has data
            if (has) found.empty.push(`${where}: card ${c.id} says "${c.nodata || 'empty'}" but ${entry ? entry.file + '.json has values' : 'its page has data'}`);
            continue;
          }
          if (((!entry && isSummary(route) && c.plots.length === 0) || (entry && entry.form === 'table')) && c.rows === 0) found.empty.push(`${where}: card ${c.id} has no rows`);
          c.plots.forEach((p, i) => {
            if (p.points === 0) found.blank.push(`${where}: card ${c.id} chart ${i + 1} has no point`);
            else if (p.points === null && !p.canvas) found.inspect.push(`${c.id}: chart ${i + 1} could not be inspected`);
            if (p.w < 40 || p.h < 40) found.blank.push(`${where}: card ${c.id} chart ${i + 1} is ${p.w}x${p.h}px`);
          });
          if (entry) {
            if (words(c.title) > BUDGET.title) found.budget.push(`${c.id}: rendered title over ${BUDGET.title} words: ${c.title}`);
            if (c.headlineLabel && words(c.headlineLabel) > BUDGET.headline) found.budget.push(`${c.id}: headline label over ${BUDGET.headline} words: ${c.headlineLabel}`);
            if (!isSummary(route)) {
              // figures of this page: stat tiles, chart headlines, and the waterfall's bar ends (its Period is the default here)
              for (const figure of new Set(c.figures.concat(c.barEnds || []))) {
                const key = `${route}|${range}@${width}|${figure}`;
                if (!found.headline.has(key)) found.headline.set(key, new Set());
                found.headline.get(key).add(c.id);
              }
            }
          }
        }
        if (SHOTS && range === '90d') {
          await mkdir(SHOTS, { recursive: true });
          await page.screenshot({ path: path.join(SHOTS, `${route}-${width}.png`), fullPage: true });
        }
      }
    }
    await context.close();
  }
  await browser.close();
  server.close();

  // a headline figure printed on two cards: the same text with at least three digits is a duplicate figure
  const dup = new Map(), dupSmall = new Map(), ranges = new Map();
  for (const [key, ids] of found.headline) {
    if (ids.size < 2) continue;
    const [route, state, ...rest] = key.split('|');
    const figure = rest.join('|');
    const target = (figure.match(/\d/g) || []).length >= 3 ? dup : dupSmall;
    const kind = id => (tiles.has(id) ? 'tile ' : waterfalls.includes(id) ? 'waterfall ' : 'chart ') + id;
    const k = `${route}: ${figure} on ${[...ids].sort().map(kind).join(' and ')}`;
    if (!ranges.has(k)) ranges.set(k, new Set());
    ranges.get(k).add(state.split('@')[0]);
    target.set(k, (target.get(k) || 0) + 1);
  }
  const collapse = list => { const m = new Map(); list.forEach(x => m.set(x, (m.get(x) || 0) + 1)); return [...m.keys()]; };
  const visited = routes.filter(r => !notRoutes.has(r));
  const note = `${states} page states (${visited.length} routes x ${RANGES.length} ranges x ${WIDTHS.length} widths: ${visited.join(', ')}) in ${label}`;
  check('page: every route, range and width settles on its route', collapse(found.settle), { note });
  check('page: no console error, page error or failed request', collapse(found.console), { note });
  check('page: every catalog card is on its route', collapse(found.missing));

  check('page: no empty card where data exists', collapse(found.empty));
  check('page: no chart without a point', collapse(found.blank));
  check('page: no horizontal page scroll', collapse(found.scroll));
  check('page: the fixture bar is never shown', collapse(found.fixture));
  // qa/accept.json may list pairs of cards whose figures are allowed to coincide: {"headline": [{"cards": ["a.x", "b.y"], "reason": "..."}]}
  let waived = [];
  try { waived = (JSON.parse(readFileSync(path.join(HERE, 'accept.json'), 'utf8')).headline || []).map(w => [...(w.cards || [])].sort().join(' and ')); } catch { /* none */ }
  const bare = k => k.replace(/\b(tile|chart|waterfall) /g, '');
  const isWaived = k => waived.some(w => bare(k).endsWith(` on ${w}`));
  const at = k => `${k} (at ${[...(ranges.get(k) || [])].join(', ')})`;
  check('residency: no headline figure on two charts or tiles of the same page (rendered)', [...dup.keys()].filter(k => !isWaived(k)).map(at));
  if ([...dup.keys()].some(isWaived)) check('residency: coinciding figures accepted in qa/accept.json', [...dup.keys()].filter(isWaived).map(at), { blocking: false });
  if (dupSmall.size) check('residency: short figures that coincide on a page', [...dupSmall.keys()].map(at), { blocking: false });
  check('copy: rendered word budgets', collapse(found.budget), { blocking: false });
  if (found.inspect.length) check('page: charts that could not be inspected', collapse(found.inspect), { blocking: false });
  if (stale) check('page: the as-of label reads stale', ['the header shows "stale": data_as_of is older than stale_after_minutes'], { blocking: false });

  if (DUMP) await writeFile(DUMP, JSON.stringify(dump, null, 1));
  const result = { ok: !checks.some(c => c.blocking && c.problems.length), states, browser: label, seconds: Math.round((Date.now() - started) / 1000), checks };
  if (OUT) { await mkdir(path.dirname(path.resolve(OUT)), { recursive: true }); await writeFile(OUT, JSON.stringify(result, null, 1)); }
  for (const c of checks) {
    console.log(`${c.problems.length ? (c.blocking ? 'FAIL' : 'warn') : 'pass'}  ${c.name}${c.note ? '  ·  ' + c.note : ''}`);
    c.problems.slice(0, 10).forEach(p => console.log('      ' + p));
    if (c.problems.length > 10) console.log(`      ... and ${c.problems.length - 10} more`);
  }
  console.log(`render: ${result.ok ? 'PASS' : 'FAIL'} in ${result.seconds}s`);
  process.exit(result.ok ? 0 : 1);
}

main().catch(async err => {
  const result = { ok: false, states: 0, checks: [{ name: 'page: render.mjs ran', blocking: true, problems: [String(err && err.message || err).slice(0, 400)], note: null }] };
  if (OUT) { try { await mkdir(path.dirname(path.resolve(OUT)), { recursive: true }); await writeFile(OUT, JSON.stringify(result, null, 1)); } catch { /* nothing more to do */ } }
  console.error('render: could not run: ' + (err && err.stack || err));
  process.exit(2);
});
