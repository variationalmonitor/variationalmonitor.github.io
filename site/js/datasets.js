// Data: every dataset behind the site, with JSON and CSV downloads, then the definitions.
// Generated from the catalog and the data files; nothing here is written by hand except
// the one-line dataset descriptions in catalog.js.

import { CATALOG, DATASETS, PAGES } from './catalog.js';
import { load } from './data.js';
import { fmtStamp } from './format.js';
import { h, download, toCsv } from './ui.js';

// A file as a table: date-indexed files give one row per date and one column per series;
// other files give the rows of their largest list.
function tableOf(file) {
  if (Array.isArray(file.dates)) {
    const n = file.dates.length, cols = [];
    const walk = (obj, path, depth) => {
      for (const [k, v] of Object.entries(obj)) {
        if (k === 'dates') continue;
        if (Array.isArray(v)) { if (v.length === n && v.every(x => x === null || typeof x !== 'object')) cols.push([path + k, v]); }
        else if (v && typeof v === 'object' && depth < 3 && !Array.isArray(v.dates)) walk(v, `${path}${k}.`, depth + 1);
      }
    };
    walk(file, '', 0);
    return [['date'].concat(cols.map(c => c[0]))].concat(file.dates.map((d, i) => [d].concat(cols.map(c => c[1][i]))));
  }
  const lists = Object.values(file).filter(v => Array.isArray(v) && v.length && v[0] && typeof v[0] === 'object' && !Array.isArray(v[0]));
  if (!lists.length) return null;
  const rows = lists.sort((a, b) => b.length - a.length)[0];
  const keys = [...new Set(rows.flatMap(r => Object.keys(r).filter(k => r[k] === null || typeof r[k] !== 'object')))];
  return [keys].concat(rows.map(r => keys.map(k => r[k])));
}

export class DataPage {
  constructor() {
    this.names = Object.keys(DATASETS);
    this.rows = new Map();
    const sets = h('section', { class: 'card span-12 method is-fit', id: 'card-datasets' },
      h('h2', { class: 'card-title', text: 'Datasets' }),
      h('div', { class: 'ds-list', role: 'table', 'aria-label': 'Datasets' },
        this.names.map(name => {
          const asof = h('span', { class: 'ds-a', role: 'cell' });
          const csv = h('button', { class: 'ds-b', type: 'button', text: 'CSV', disabled: true, 'aria-label': `${name}, CSV` });
          const json = h('a', { class: 'ds-b', href: `data/${name}.json`, download: `${name}.json`, text: 'JSON', 'aria-label': `${name}, JSON` });
          const row = h('div', { class: 'ds-row', role: 'row' },
            h('span', { class: 'ds-n', role: 'cell', text: name }), h('span', { class: 'ds-d', role: 'cell', text: DATASETS[name] }), asof,
            h('span', { class: 'ds-g', role: 'cell' }, json, csv));
          this.rows.set(name, { row, asof, csv, json });
          return row;
        }),
        h('div', { class: 'ds-row', role: 'row' },
          h('span', { class: 'ds-n', role: 'cell', text: 'market/<TICKER>' }), h('span', { class: 'ds-d', role: 'cell', text: 'Daily history of each listing, one file per ticker; the index lists the tickers.' }),
          h('span', { class: 'ds-a', role: 'cell' }),
          h('span', { class: 'ds-g', role: 'cell' }, h('a', { class: 'ds-b', href: 'data/market/index.json', download: 'market-index.json', text: 'Index', 'aria-label': 'Market index, JSON' })))));
    const defs = h('section', { class: 'card span-12 method is-fit', id: 'card-definitions' },
      h('h2', { class: 'card-title', text: 'Definitions' }),
      PAGES.filter(p => CATALOG.some(c => c.tab === p.id)).map(p => h('div', { class: 'method-tab' },
        // the section and the page as two elements: `Omni` quieter, then `Markets`
        h('h3', { class: 'th-group' }, p.section ? [h('span', { class: 'th-sec', text: p.section }), ' '] : null, h('span', { text: p.label })),
        CATALOG.filter(c => c.tab === p.id).map(c => h('a', { class: 'method-row', href: c.page ? `#/${p.id}` : `#/${p.id}?card=${c.id}` },
          h('span', { class: 'method-t', text: c.title }),
          h('span', { class: 'method-i', text: c.info }),
          h('span', { class: 'method-s', text: c.source }))))));
    this.el = h('div', { class: 'span-12 stack' }, sets, defs);
  }

  async update() {
    await Promise.all(this.names.map(async name => {
      const file = await load(name), r = this.rows.get(name);
      if (this.dead) return;
      if (!file) { r.row.classList.add('is-missing'); r.asof.textContent = 'No data yet'; r.json.removeAttribute('href'); r.json.setAttribute('aria-disabled', 'true'); r.csv.disabled = true; return; }
      r.row.classList.remove('is-missing');
      r.json.setAttribute('href', `data/${name}.json`); r.json.removeAttribute('aria-disabled');
      r.asof.textContent = file.as_of ? fmtStamp(file.as_of) : '';
      const table = tableOf(file);
      r.csv.disabled = !table;
      r.csv.onclick = table ? () => download(`${name}.csv`, new Blob([toCsv(table)], { type: 'text/csv' })) : null;
    }));
  }

  flash() {}
  hold() {}
  dispose() { this.dead = true; }
}
