// The one markets table (sort, filter, pages) and the market page a row opens:
// five cards of one listing's history at `#/<markets page>/<TICKER>`.

import { load } from './data.js';
import { fmt, fmtStamp, fmtDate, dayNum } from './format.js';
import { Card } from './card.js';
import { CLASSES, TYPES } from './catalog.js';
import { h, icon, infoPop, menu, toast, download, copyText, toCsv } from './ui.js';
import { renderTable } from './table.js';

const PAGE = 25, NEW_DAYS = 14;
const CLASS_LABEL = Object.fromEntries(CLASSES.map(([k, l]) => [k, l]));
// the file's one-sided total; until every row carries one, the two sides are added here
const oi = r => (r.oi !== undefined ? r.oi : r.oi_long == null && r.oi_short == null ? null : (r.oi_long || 0) + (r.oi_short || 0));
const longShare = r => { if (r.long_share !== undefined) return r.long_share; const t = oi(r); return t ? (r.oi_long || 0) / t : null; };
const carry = r => (r.type === 'swap' ? r.fin_long_ann : r.funding_ann);
const num = v => (v == null ? '—' : v.toFixed(1));

const tableState = { filter: 'all', q: '', sort: { key: 'vol24h', dir: -1 }, page: 0 };

function head(card, entry, title, extra) {
  card.titleEl = h('h2', { class: 'card-title', id: `t-${entry.id}`, text: title });
  // the ⓘ: catalog text, then Source and As of beneath a hairline (there is no source line on the face)
  card.infoBtn = h('button', { class: 'icon-btn info', type: 'button', 'aria-label': `About: ${title}`, 'aria-expanded': 'false',
    onclick: () => infoPop(card.infoBtn, entry.info, [['Source', entry.source], ['As of', card.asOf ? fmtStamp(card.asOf) : '']]) }, icon('info'));
  card.ctlEl = h('div', { class: 'card-ctl' }, extra);
  card.bodyEl = h('div', { class: 'card-body' });
  card.el = h('section', { class: `card span-${entry.span} is-fit`, id: `card-${entry.id}`, 'data-card': entry.id, 'aria-labelledby': `t-${entry.id}` },
    h('header', { class: 'card-head' }, h('div', { class: 'card-t' }, card.titleEl, card.infoBtn), card.ctlEl), card.bodyEl);
}
const common = {
  flash() { this.el.classList.add('is-target'); setTimeout(() => this.el.classList.remove('is-target'), 1600); },
  hold() { this.el.classList.add('is-loading'); },
};

// ---------- the table ----------
export class MarketsTable {
  constructor(entry, app) {
    this.entry = entry; this.app = app; this.st = tableState;
    this.chips = h('div', { class: 'seg', role: 'group', 'aria-label': 'Filter markets' });
    this.search = h('input', { class: 'search-in', type: 'search', placeholder: 'Search', 'aria-label': 'Search markets', autocomplete: 'off', spellcheck: 'false' });
    this.search.value = this.st.q;
    this.search.addEventListener('input', () => { this.st.q = this.search.value.trim(); this.st.page = 0; this.render(); });
    this.more = h('button', { class: 'icon-btn more', type: 'button', 'aria-label': `More: ${entry.title}`, 'aria-haspopup': 'menu', 'aria-expanded': 'false' }, icon('more'));
    this.more.addEventListener('click', () => menu(this.more, [{ label: 'Download CSV', value: 'csv' }, { label: 'Copy link', value: 'link' }], it => {
      if (it.value === 'csv') download(`variational-monitor_${entry.id}.csv`, new Blob([toCsv(this.csv())], { type: 'text/csv' }));
      else copyText(this.app.link(this.entry)).then(() => toast('Link copied'));
    }, { align: 'right' }));
    head(this, entry, entry.title, [this.chips, h('label', { class: 'search' }, icon('search'), this.search), this.more]);
    this.pager = h('div', { class: 'pager' });
    this.wrap = h('div', { class: 'tscroll' });
    this.bodyEl.append(this.wrap, this.pager);
  }

  async update() {
    const file = await load(this.entry.file);
    if (this.dead) return;
    this.el.classList.remove('is-loading');
    if (!file || !Array.isArray(file.rows) || !file.rows.length) {
      this.el.classList.add('is-empty');
      this.ctlEl.hidden = true;
      this.bodyEl.replaceChildren(h('div', { class: 'nodata', text: 'No data yet' }));
      return;
    }
    this.file = file;
    this.asOf = file.as_of;
    // a listing is new for 14 days after it first appeared
    const today = file.as_of ? dayNum(file.as_of.slice(0, 10)) : null;
    this.isNew = r => !!r.listed && today != null && today - dayNum(String(r.listed).slice(0, 10)) < NEW_DAYS;
    // the page's Asset and Instrument filters scope the table; the chips then narrow it by asset class
    const f = this.app.G.f || {};
    this.inScope = r => (f.asset === 'crypto' ? r.cls === 'crypto' : f.asset === 'tradfi' ? r.cls !== 'crypto' : true)
      && (f.inst === 'perp' ? r.type === 'perp' : f.inst === 'swap' ? r.type === 'swap' : true);
    const scoped = file.rows.filter(this.inScope);
    const classes = CLASSES.filter(([k]) => scoped.some(r => r.cls === k)).filter((c, i, all) => all.findIndex(x => x[0] === c[0]) === i);
    this.filters = [{ v: 'all', label: 'All', test: () => true }]
      .concat(classes.length > 1 ? classes.map(([k, label]) => ({ v: k, label, test: r => r.cls === k })) : [])
      .concat(scoped.some(this.isNew) ? [{ v: 'new', label: 'New', test: this.isNew }] : []);
    if (!this.filters.some(f => f.v === this.st.filter)) this.st.filter = 'all';
    this.chips.replaceChildren(...this.filters.map(f => h('button', { type: 'button', 'data-v': f.v, text: f.label, onclick: () => { this.st.filter = f.v; this.st.page = 0; this.render(); } })));
    this.render();
  }

  rows() {
    const st = this.st, f = this.filters.find(x => x.v === st.filter) || this.filters[0];
    const q = st.q.toLowerCase();
    const get = {
      ticker: r => r.ticker, type: r => r.type, cls: r => CLASS_LABEL[r.cls] || r.cls, vol24h: r => r.vol24h, oi,
      long: longShare, turn: r => r.turnover, carry, ba: r => r.slip100k_bps,
    }[st.sort.key];
    return this.file.rows
      .filter(r => this.inScope(r) && f.test(r) && (!q || String(r.ticker).toLowerCase().includes(q) || String(r.name || '').toLowerCase().includes(q)))
      .sort((a, b) => {
        const x = get(a), y = get(b);
        if (x == null && y == null) return String(a.ticker).localeCompare(String(b.ticker));
        if (x == null) return 1;
        if (y == null) return -1;
        return (typeof x === 'string' ? x.localeCompare(y) : x - y) * st.sort.dir || String(a.ticker).localeCompare(String(b.ticker));
      });
  }

  csv() {
    const head = ['ticker', 'name', 'type', 'group', 'class', 'listed', 'volume_24h_usd', 'oi_usd', 'oi_long_usd', 'oi_short_usd', 'long_share', 'volume_to_open_interest', 'funding_ann', 'financing_long_ann', 'financing_short_ann', 'slippage_1k_bps', 'slippage_100k_bps', 'slippage_1m_bps', 'bid_ask_1k_bps', 'bid_ask_100k_bps', 'bid_ask_1m_bps', 'quote_at'];
    return [head].concat(this.rows().map(r => [r.ticker, r.name, r.type, r.group, r.cls, r.listed, r.vol24h, oi(r), r.oi_long, r.oi_short, longShare(r), r.turnover, r.funding_ann, r.fin_long_ann, r.fin_short_ann, r.slip1k_bps, r.slip100k_bps, r.slip1m_bps, r.ba1k_bps, r.ba100k_bps, r.ba1m_bps, r.quote_at]));
  }

  render() {
    const st = this.st, all = this.rows(), n = all.length;
    const pages = Math.max(1, Math.ceil(n / PAGE));
    st.page = Math.min(st.page, pages - 1);
    const rows = all.slice(st.page * PAGE, st.page * PAGE + PAGE);
    this.chips.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.v === st.filter)));
    const cols = [
      { key: 'ticker', label: 'Market', sortable: true, cls: 'c-mkt', cell: r => h('div', { class: 'mkt' },
        h('span', { class: 'mkt-t' }, h('span', { text: r.ticker }), this.isNew(r) ? h('span', { class: 'tag-new', text: 'New' }) : null),
        h('span', { class: 'mkt-n', text: r.name || '' })) },
      { key: 'type', label: 'Type', sortable: true, cls: 'c-txt', cell: r => TYPES[r.type] || r.type },
      { key: 'cls', label: 'Class', sortable: true, cls: 'c-txt', cell: r => CLASS_LABEL[r.cls] || r.cls },
      { key: 'vol24h', label: 'Volume', sub: '24h', num: true, sortable: true, cell: r => fmt(r.vol24h, 'usd') },
      { key: 'oi', label: 'Open interest', sub: 'one-sided', num: true, sortable: true, cell: r => fmt(oi(r), 'usd') },
      { key: 'long', label: 'Long share', num: true, sortable: true, cell: r => fmt(longShare(r), 'pct') },
      { key: 'turn', label: 'Volume to open interest', num: true, sortable: true, cell: r => fmt(r.turnover, 'times') },
      { key: 'carry', label: 'Funding', sub: 'annualised', num: true, sortable: true, cell: r => (r.type === 'swap' ? (r.fin_long_ann == null && r.fin_short_ann == null ? '—' : `${fmt(r.fin_long_ann, 'rate')} / ${fmt(r.fin_short_ann, 'rate')}`) : fmt(r.funding_ann, 'rate')) },
      { key: 'ba', label: 'Slippage', sub: '$100K, bps', num: true, sortable: true, cell: r => (r.slip100k_bps == null && r.type === 'swap' ? h('span', { class: 'closed', text: 'Closed' }) : num(r.slip100k_bps)) },
    ].filter(Boolean);
    const table = renderTable({
      cols, rows, sort: st.sort, caption: this.entry.title,
      onSort: key => { st.sort = st.sort.key === key ? { key, dir: -st.sort.dir } : { key, dir: ['ticker', 'type', 'cls'].includes(key) ? 1 : -1 }; st.page = 0; this.render(); },
      // a row opens the market page
      onRow: r => this.app.open(this.entry.tab, r.ticker),
      rowKey: r => r.ticker,
    });
    table.classList.add('tbl-mk');
    this.wrap.replaceChildren(n ? table : h('div', { class: 'nodata', text: 'No markets match' }));
    const from = n ? st.page * PAGE + 1 : 0, to = Math.min(n, (st.page + 1) * PAGE);
    this.pager.replaceChildren(
      h('span', { class: 'pager-t', text: `${fmt(from, 'count')}–${fmt(to, 'count')} of ${fmt(n, 'count')}` }),
      h('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Previous page', disabled: st.page === 0, onclick: () => { st.page--; this.render(); } }, icon('prev')),
      h('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Next page', disabled: st.page >= pages - 1, onclick: () => { st.page++; this.render(); } }, icon('next')));
  }

  dispose() { this.dead = true; }
}
Object.assign(MarketsTable.prototype, common);

// ---------- the market page ----------
// Five cards of the standard anatomy on the page grid (two wide, then three), one per panel of the catalog
// entry: title, the latest value with its date, the plot. They share one crosshair, matched by date.
const SPANS = [6, 6, 4, 4, 4];
export class MarketPage {
  constructor(entry, app, ticker) {
    this.entry = entry; this.app = app; this.ticker = ticker; this.cards = []; this.seq = 0; this.built = null;
    this.el = h('div', { class: 'mkpage is-loading' });
  }

  async update() {
    const seq = ++this.seq, ticker = this.ticker;
    const [file, latest] = /^[A-Za-z0-9._-]{1,40}$/.test(ticker) ? await Promise.all([load(`market/${ticker}`), load('markets_latest')]) : [null, null];
    if (this.dead || seq !== this.seq) return;
    this.el.classList.remove('is-loading');
    if (!file || !Array.isArray(file.dates) || !file.dates.length) {
      this.drop();
      this.el.replaceChildren(h('div', { class: 'nodata span-12', text: 'No data yet' }));
      return;
    }
    // the header's trail carries the market's name and, when the markets file has one, its listing date
    const row = latest && Array.isArray(latest.rows) ? latest.rows.find(r => r.ticker === ticker) : null;
    const listed = file.listed || (row && row.listed) || null;
    this.app.crumb([file.name, listed ? `Listed ${fmtDate(String(listed).slice(0, 10))}` : '']);
    // the cards are built once per market (a swap and a perpetual carry different panels) and then only updated
    if (this.built !== file.type) {
      this.drop();
      this.built = file.type;
      const panels = this.entry.panels.filter(p => !p.only || p.only === file.type);
      this.cards = panels.map((p, k) => {
        const c = new Card({ ...p, id: `mk.market.${p.key || k}`, tab: this.entry.tab, sub: ticker, span: SPANS[k] || 4, file: `market/${ticker}`, source: this.entry.source, info: p.info || this.entry.info, thin: false, intraday: true }, this.app);
        c.onHover = (i, src) => this.link(i, src);
        return c;
      });
      this.el.replaceChildren(...this.cards.map(c => c.el));
    }
    await Promise.all(this.cards.map(c => c.update()));
  }

  // one crosshair across the cards, matched by date
  link(i, src) {
    if (!src.model || src.model.type !== 'time') return;
    const key = i >= 0 && src.model.x[i] ? src.model.x[i].key : null;
    this.cards.forEach(card => {
      const c = card.chart;
      if (!c || c === src || !c.model || c.model.type !== 'time') return;
      const j = key ? c.model.x.findIndex(p => p.key === key) : -1;
      if (j < 0) c.hide(); else c.setHover(j, null, null, false, true);
    });
  }

  drop() { this.cards.forEach(c => c.dispose()); this.cards = []; this.built = null; }
  flash() {}
  hold() { this.cards.forEach(c => c.hold()); }
  dispose() { this.dead = true; this.drop(); }
}
