// Shell: left rail, header strip, hash router, the global Range, deep links.
// Routes are page ids from the nav config (`#/omni/markets`); a page may open a keyed sub-page
// (`#/omni/markets/<TICKER>`). Deep link: ?range=90d&card=<id>&<control>=<value>.

import { NAV, FOOT, PAGES, HOME, ROUTE_ALIAS, CATALOG, cardsOf, pageCard, byId, aliasQuery, assertResidency } from './catalog.js';
import { load, peek, lastAt, reset, onFixture, hasFixture } from './data.js';
import { Card, applyParams, paramsOf } from './card.js';
import { MarketsTable, MarketPage } from './markets.js';
import { LibraryPage } from './research.js';
import { DataPage } from './datasets.js';
import { Chart } from './chart.js';
import { fmtDate, fmtDay, fmtTime } from './format.js';
import { h, closePop } from './ui.js';

const RANGES = [['7d', '7D'], ['30d', '30D'], ['90d', '90D'], ['180d', '180D'], ['1y', '1Y'], ['all', 'All']];
const pageOf = id => PAGES.find(p => p.id === id);
// page kinds that are not card grids, and the sub-pages a page can open
const KINDS = { research: (page) => new LibraryPage(app, page), data: () => new DataPage() };
const SUBS = {
  market: (page, key) => new MarketPage(pageCard(page.id, 'market'), app, key),
};

// G.f holds the page-level filters (Markets: asset and instrument); the scope key is their values joined by a dot
const G = { range: '90d', f: {} };
// The pages share one range. A sub-page whose page names a default of its own (a market page opens at 7D)
// keeps a second one, so opening a market does not change the range of the page it was opened from.
const chosen = {};
const rangeCtx = (page, key) => (key && page.subRange ? `${page.id}/*` : 'main');
const rangeDefault = (page, key) => (key && page.subRange ? page.subRange : '90d');
const filtersOf = page => (page && page.filters) || [];
const scopeKey = () => filtersOf(pageOf(tab)).map(f => G.f[f.key] || f.options[0][0]).join('.');
const app = { G, meta: null, events: [], link, onState, open, pageOf, scopeKey, crumb };
let tab = null, sub = '', cards = [], focus = null;

const $ = id => document.getElementById(id);
const phone = () => window.matchMedia('(max-width: 760px)').matches;

// ---------- url ----------
function parse() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [p, query = ''] = raw.split('?');
  const params = new URLSearchParams(query);
  let path = p.replace(/\/+$/, '');
  try { path = decodeURIComponent(path); } catch { /* keep the raw path */ }
  // presets carried by an old card id or an old route (`#/omni/tradfi` is Markets with asset=tradfi); the link's own params win
  const preset = q => new URLSearchParams(q).forEach((v, k) => { if (!params.has(k)) params.set(k, v); });
  // a card link always lands on the card's own page, whatever route it was written for
  const target = byId(params.get('card') || '');
  if (target) { preset(aliasQuery(params.get('card'))); return { tab: target.tab, sub: '', params, target }; }
  const first = path.split('/')[0];
  const alias = ROUTE_ALIAS[path] || (!PAGES.some(x => path === x.id || path.startsWith(x.id + '/')) && ROUTE_ALIAS[first] ? ROUTE_ALIAS[first] + path.slice(first.length) : null);
  if (alias) { const [p2, q2 = ''] = alias.split('?'); path = p2; preset(q2); }
  // the longest page id that prefixes the path; the rest is the sub-page key
  const page = PAGES.filter(x => path === x.id || path.startsWith(x.id + '/')).sort((a, b) => b.id.length - a.id.length)[0] || pageOf(HOME);
  let key = page.sub && path.length > page.id.length ? path.slice(page.id.length + 1) : '';
  if (!key && page.sub === 'market' && params.get('m')) key = params.get('m'); // links from the first version
  return { tab: page.id, sub: /^[A-Za-z0-9._-]{1,80}$/.test(key) ? key : '', params, target: null };
}

function query(entry) {
  const q = new URLSearchParams();
  if (G.range !== (entry ? '90d' : rangeDefault(pageOf(tab), sub))) q.set('range', G.range);
  if (!sub || entry) filtersOf(pageOf(entry ? entry.tab : tab)).forEach(f => { const v = G.f[f.key]; if (v && v !== f.options[0][0]) q.set(f.key, v); });
  if (entry) { q.set('card', entry.id); paramsOf(entry).forEach(([k, v]) => q.set(k, v)); }
  const s = q.toString();
  return s ? '?' + s : '';
}

function syncUrl() {
  const entry = focus && byId(focus);
  const next = `#/${tab}${sub ? '/' + sub : ''}${query(!sub && entry && entry.tab === tab ? entry : null)}`;
  if (location.hash !== next) history.replaceState(null, '', next);
}

function link(entry) {
  // a card on a sub-page (a market's five cards) links to that sub-page
  if (entry.sub) return `${location.origin}${location.pathname}#/${entry.tab}/${encodeURIComponent(entry.sub)}${G.range !== rangeDefault(pageOf(entry.tab), entry.sub) ? `?range=${G.range}` : ''}`;
  return `${location.origin}${location.pathname}#/${entry.tab}${query(entry)}`;
}

function onState(entry) { focus = entry.id; syncUrl(); }

/** A card on the summary page opens its home card (that page's default filters, the range in view):
 *  a stat tile as a whole, a chart by its title. */
function point(card, entry) {
  const t = byId(entry.pointer);
  if (!t) return;
  const go = () => {
    const q = new URLSearchParams();
    filtersOf(pageOf(t.tab)).forEach(f => q.set(f.key, f.options[0][0]));
    q.set('range', G.range); q.set('card', t.id);
    location.hash = `#/${t.tab}?${q}`;
  };
  const el = entry.form === 'stat' ? card.el : card.titleEl;
  el.classList.add('is-pointer');
  el.setAttribute('role', 'link');
  el.tabIndex = 0;
  el.title = `Open on ${pageOf(t.tab).label}`;
  el.addEventListener('click', ev => { if (el === card.titleEl || !ev.target.closest('button, a, input, .pop, .menu')) go(); });
  el.addEventListener('keydown', ev => { if (ev.key === 'Enter' && ev.target === el) go(); });
}

/** Open a page's sub-page, e.g. a market from a row of the markets table. */
function open(pageId, key) { location.hash = `#/${pageId}/${encodeURIComponent(key)}`; }

// ---------- header ----------
// a page may set a shortest range; shorter options are shown disabled there
const tooShort = (page, v) => !!page.minRange && RANGES.findIndex(x => x[0] === v) < RANGES.findIndex(x => x[0] === page.minRange);

function drawRange() {
  const page = pageOf(tab);
  $('range').replaceChildren(...RANGES.map(([v, label]) => h('button', { type: 'button', 'aria-pressed': String(G.range === v), disabled: tooShort(page, v), text: label, onclick: () => setRange(v) })));
  // a page without a range does not show the control at all
  $('range').classList.toggle('is-off', pageOf(tab).range === false);
}

function setRange(v) {
  G.range = chosen[rangeCtx(pageOf(tab), sub)] = v;
  closePop();
  Chart.hideAll();
  drawRange();
  syncUrl();
  cards.forEach(c => c.update());
}

function drawAsOf() {
  const m = app.meta, els = document.querySelectorAll('[data-asof]');
  if (!m || !m.data_as_of) { els.forEach(e => { e.textContent = ''; }); return; }
  const stale = Date.now() - Date.parse(m.data_as_of) > (m.stale_after_minutes || 120) * 60000;
  const day = m.data_as_of.slice(0, 10);
  els.forEach(e => {
    e.classList.toggle('is-stale', stale);
    e.replaceChildren(...[
      h('span', { class: 'long', text: 'As of ' }),
      h('b', { class: 'long', text: `${fmtDate(day)}, ${fmtTime(m.data_as_of)}` }),
      h('b', { class: 'short', text: `${fmtDay(day)}, ${fmtTime(m.data_as_of)}` }),
      stale ? h('span', { text: ', stale' }) : null,
    ].filter(Boolean));
  });
  fitHead();
}

// ---------- page-level filters: a row of tabs beneath the title (Markets), or segmented controls after the title ----------
const offNow = (f, v) => { const rule = f.off && f.off[v]; return !!rule && Object.entries(rule).every(([k, x]) => G.f[k] === x); };

const tabOn = set => Object.entries(set).every(([k, v]) => G.f[k] === v);

function normaliseFilters(page) {
  const fs = filtersOf(page);
  fs.forEach(f => {
    const v = G.f[f.key];
    if (!f.options.some(o => o[0] === v) || offNow(f, v)) G.f[f.key] = f.options[0][0];
  });
  // a page with tabs shows one combination per tab; a combination without a tab of its own (an older link)
  // lands on the tab that shares most of it
  const tabs = page && page.tabs;
  if (tabs && !tabs.some(t => tabOn(t[1]))) {
    const dflt = k => fs.find(f => f.key === k).options[0][0];
    const score = set => Object.entries(set).filter(([k, v]) => v !== dflt(k) && G.f[k] === v).length;
    Object.assign(G.f, tabs.reduce((a, t) => (score(t[1]) > score(a[1]) ? t : a), tabs[0])[1]);
  }
}

let filterEls = [], tabbed = false;
function drawFilters() {
  const page = pageOf(tab), fs = sub ? [] : filtersOf(page);
  tabbed = !sub && !!page.tabs;
  if (tabbed) {
    // one row of tabs beneath the title; a tab is a set of filter values, and exactly one tab is selected
    filterEls = [h('div', { class: 'tabs', role: 'tablist', 'aria-label': page.label }, page.tabs.map(([label, set]) => h('button', {
      type: 'button', role: 'tab', 'aria-selected': String(tabOn(set)), text: label, onclick: () => setFilters(set),
    })))];
  } else {
    // the options name themselves, so the controls carry no visible label
    filterEls = fs.map(f => h('div', { class: 'seg', role: 'group', 'aria-label': f.label }, f.options.map(([v, label]) => h('button', {
      type: 'button', 'aria-pressed': String(G.f[f.key] === v), disabled: offNow(f, v), text: label, onclick: () => setFilter(f.key, v),
    }))));
  }
  fitHead();
}

// The title block holds the title, the as-of time and, on a wide screen, the Range control. Beneath it sits
// one row: the page's tabs (Markets). On a phone the Range control moves into that row, which sticks under
// the top bar while the title scrolls away. The height of everything that stays stuck is published for anchors.
let tuck = 0;
function fitHead() {
  const head = $('head'), row = $('subhead'), range = $('range'), small = phone();
  const slot = small ? $('subhead-in') : $('head-in');
  if (range.parentElement !== slot) slot.append(range);
  $('scope2').replaceChildren(...filterEls);
  const hasTabs = tabbed && filterEls.length > 0;
  const hasRange = !range.classList.contains('is-off');
  const show = filterEls.length > 0 || (small && hasRange);
  row.hidden = !show;
  row.classList.toggle('is-tabs', hasTabs);
  head.classList.toggle('has-sub', show);
  head.classList.toggle('no-range', !hasRange);
  // on a wide screen the block's first line (the as-of time) slides under the top edge once the page scrolls
  tuck = small ? 0 : parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--hd-tuck')) || 0;
  const stuck = (small ? 48 : head.offsetHeight - tuck) + (show ? row.offsetHeight : 0);
  document.documentElement.style.setProperty('--head-h', `${stuck}px`);
  stick();
}
// once that line is out of sight the time rides beside the Range, so the header always shows the data time
function stick() { $('head').classList.toggle('is-stuck', tuck > 0 && window.scrollY >= tuck); }

const setFilter = (key, v) => setFilters({ [key]: v });
function setFilters(set) {
  if (tabOn(set)) return;
  Object.assign(G.f, set);
  normaliseFilters(pageOf(tab));
  closePop();
  Chart.hideAll();
  focus = null;
  build();          // the scope decides which cards the page shows
  drawFilters();
  syncUrl();
  cards.forEach(c => c.update());
}

// ---------- rail: section, product, page ----------
function navItem(p) {
  const tag = p.tag ? h('span', { class: 'nav-tag', text: p.tag }) : null;
  if (p.product) {
    // a product label is not a link; it is strong while one of its pages is open, and dimmed when it has none
    const kids = p.pages || [], cur = kids.some(c => c.id === tab);
    const label = h('div', { class: 'nav-p' + (kids.length ? '' : ' is-off') + (cur ? ' is-cur' : '') }, h('span', { text: p.label }), tag);
    return kids.length ? [label, h('div', { class: 'nav-g' }, kids.flatMap(navItem))] : [label];
  }
  return [h('a', { class: 'nav-a', href: `#/${p.id}`, 'aria-current': p.id === tab ? 'page' : null }, h('span', { class: 'nav-l', text: p.label }), tag)];
}

function drawNav() {
  $('nav').replaceChildren(...NAV.flatMap(g => [
    g.section ? h('h2', { class: 'nav-h', text: g.section }) : null,
    ...g.pages.flatMap(navItem),
  ]).filter(Boolean));
  $('foot').replaceChildren(h('a', { class: 'nav-a', href: `#/${FOOT.id}`, 'aria-current': FOOT.id === tab ? 'page' : null, text: FOOT.label }));
}

function setRail(on) {
  $('rail').classList.toggle('is-open', on);
  $('scrim').hidden = !on;
  $('menu').setAttribute('aria-expanded', String(on));
  document.documentElement.classList.toggle('rail-open', on);
}

// ---------- pages ----------
// Whether the current scope lists fewer markets than the page's threshold (read from the page's own file).
function fewOf(page) {
  const f = page.few, file = f && peek(f.file);
  const arr = file && file.scopes && file.scopes[scopeKey()] && file.scopes[scopeKey()][f.col];
  const L = Array.isArray(arr) ? lastAt(arr, arr.length - 1) : null;
  return !!L && L.v < f.below;
}
let builtFew = false;

function build() {
  cards.forEach(c => c.dispose());
  const page = pageOf(tab);
  if (sub && SUBS[page.sub]) cards = [SUBS[page.sub](page, sub)];
  else if (KINDS[page.kind]) cards = [KINDS[page.kind](page)];
  else {
    // a card may belong to one scope only (`only: { scope: 'all.all' }`) or to one filter value (`only: { inst: 'swap' }`)
    const key = scopeKey();
    // in a scope with few markets a card may be left out (`few: 'hide'`) or take another width (`few: { span }`)
    const few = builtFew = fewOf(page);
    const shown = e => !(few && e.few === 'hide') && (!e.only || Object.entries(e.only).every(([k, v]) => (k === 'scope' ? key === v : G.f[k] === v)));
    cards = cardsOf(tab).filter(shown).map(e => {
      const c = e.form === 'table' ? new MarketsTable(e, app) : new Card(e, app);
      // a card's width in a scope with few markets (`few: { span }`), or in one named scope (`spanIn: { 'all.all': 4 }`)
      const sp = (few && e.few && e.few.span) || (e.spanIn && e.spanIn[key]);
      if (sp) c.el.classList.replace(`span-${e.span}`, `span-${sp}`);
      if (e.pointer) point(c, e);
      return c;
    });
  }
  // a page with no cards yet (a product that is not live) shows the quiet empty state
  $('page').replaceChildren(...(cards.length ? cards.map(c => c.el) : [h('div', { class: 'nodata span-12', text: 'No data yet' })]));
  // a market page is titled by its trail (Markets › BTC-PERP, then the name once its file arrives); every other page by its label
  const label = sub && page.sub === 'market' ? sub : page.label;
  $('title').classList.toggle('is-trail', !!sub && page.sub === 'market');
  if (sub && page.sub === 'market') {
    $('title').replaceChildren(h('a', { class: 'crumb', href: `#/${page.id}`, text: page.label }), h('span', { class: 'crumb-s', 'aria-hidden': 'true', text: '›' }),
      h('span', { class: 'crumb-c', text: sub }), h('span', { class: 'crumb-n', id: 'crumb-n' }));
  } else $('title').textContent = label;
  document.title = tab === HOME ? 'Variational Monitor' : `${label}, Variational Monitor`;
}

/** A sub-page names itself once its file has loaded: the plain name after the trail, then any further facts
 *  (the listing date), each its own element. */
function crumb(parts) { const n = $('crumb-n'); if (n) n.replaceChildren(...[].concat(parts || []).filter(Boolean).map(t => h('span', { text: String(t) }))); }

async function route(first) {
  // a page whose layout depends on its file waits for that file once; after that it is in memory
  const pre = pageOf(parse().tab);
  if (pre && pre.few && peek(pre.few.file) === undefined) { try { await load(pre.few.file); } catch { /* the cards show the empty state */ } }
  const { tab: next, sub: key, params, target } = parse();
  // page filters from the link; a link that names a card or a filter sets them, a bare page link keeps them
  const page = pageOf(next), before = JSON.stringify(G.f);
  filtersOf(page).forEach(f => { const v = params.get(f.key); if (v) G.f[f.key] = v; });
  // a link to a card that lives in one scope only opens that scope, unless the link says otherwise
  if (target && target.only) {
    const fs = filtersOf(page), want = target.only.scope ? Object.fromEntries(target.only.scope.split('.').map((v, i) => [fs[i].key, v])) : target.only;
    Object.entries(want).forEach(([k, v]) => { if (!params.has(k)) G.f[k] = v; });
    fs.forEach(f => { if (offNow(f, G.f[f.key])) Object.keys(f.off[G.f[f.key]]).forEach(k => { if (!params.has(k)) G.f[k] = fs.find(x => x.key === k).options[0][0]; }); });
  }
  normaliseFilters(page);
  const changed = next !== tab || key !== sub || JSON.stringify(G.f) !== before;
  // the range from the link; without one, the range last chosen in this context, or the context's default
  const r = params.get('range'), ctx = rangeCtx(page, key);
  if (r && RANGES.some(x => x[0] === r)) chosen[ctx] = r;
  G.range = chosen[ctx] || rangeDefault(page, key);
  if (target) { applyParams(target, params); focus = target.id; } else if (changed) focus = null;
  tab = next; sub = key;
  // arriving on a page with a range shorter than it offers uses the page's shortest range (the URL follows)
  if (tooShort(pageOf(tab), G.range)) G.range = chosen[ctx] = pageOf(tab).minRange;
  closePop();
  setRail(false);
  if (changed) { build(); drawNav(); }
  drawRange();
  drawFilters();
  syncUrl();
  await Promise.all(cards.map(c => c.update()));
  if (target) {
    const c = cards.find(x => x.entry && x.entry.id === target.id);
    if (c && !c.el.hidden) { c.el.scrollIntoView({ block: 'start' }); c.flash(); }
  } else if (changed && !first) window.scrollTo(0, 0);
}

// ---------- data-level state ----------
async function loadMeta() {
  const [meta, ev] = await Promise.all([load('meta'), load('events')]);
  app.meta = meta;
  const list = Array.isArray(ev) ? ev : ev && Array.isArray(ev.events) ? ev.events : [];
  app.events = list.filter(e => e && /^\d{4}-\d{2}-\d{2}$/.test(e.date) && typeof e.label === 'string').slice(0, 5);
  drawAsOf();
}

// A new build replaces the data in place; cards hold their previous frame while it reloads.
async function refresh() {
  try {
    const res = await fetch('data/meta.json', { cache: 'no-cache' });
    if (res.ok) {
      const meta = await res.json();
      if (app.meta && meta.built_at && meta.built_at !== app.meta.built_at) {
        cards.forEach(c => c.hold());
        reset();
        await loadMeta();
        await Promise.all(cards.map(c => c.update()));
        const page = pageOf(tab);
        if (page.few && !sub) {
          try { await load(page.few.file); } catch { /* keep the layout */ }
          if (fewOf(page) !== builtFew) { build(); await Promise.all(cards.map(c => c.update())); }
        }
      }
    }
  } catch { /* offline: keep what is on screen */ }
  drawAsOf();
}

function fixtureBar() {
  const on = hasFixture();
  $('fixture').hidden = !on;
  document.documentElement.style.setProperty('--fx-h', on ? '24px' : '0px');
}

async function start() {
  assertResidency();
  onFixture(fixtureBar);
  const fonts = Promise.all(['400 11px Inter', '500 12px Inter', '600 14px Inter', '400 11px "Geist Mono"', '500 12px "Geist Mono"', '600 16px "Zalando Sans Expanded"', '400 16px "Zalando Sans Expanded"'].map(f => document.fonts.load(f))).catch(() => {});
  await Promise.all([loadMeta(), Promise.race([fonts, new Promise(r => setTimeout(r, 1500))])]);
  window.addEventListener('hashchange', () => route(false));
  window.addEventListener('resize', fitHead);
  window.addEventListener('scroll', stick, { passive: true });
  document.addEventListener('pointerdown', e => { if (!(e.target instanceof Element) || !e.target.closest('.plot')) Chart.hideAll(); });
  $('menu').addEventListener('click', () => setRail(!$('rail').classList.contains('is-open')));
  $('scrim').addEventListener('click', () => setRail(false));
  $('rail').addEventListener('click', e => { if (e.target instanceof Element && e.target.closest('a') && phone()) setRail(false); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && $('rail').classList.contains('is-open')) { setRail(false); $('menu').focus(); } });
  await route(true);
  document.documentElement.classList.add('is-ready');
  setInterval(refresh, 5 * 60000);
  setInterval(drawAsOf, 60000);
}

start();

// expose the catalog size for QA scripts
window.__vm = { cards: CATALOG.length };
