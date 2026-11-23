// One table renderer: the table view of every chart and the markets table.
// cols: [{ key, label, sub, num, sortable, cell(row) -> Node | string, cls }]; `sub` is the qualifier beneath a header's label

import { h, icon } from './ui.js';

export function renderTable({ cols, rows, sort, onSort, onRow, isSelected, rowKey, caption }) {
  const head = h('tr', null, cols.map(c => {
    const th = h('th', { scope: 'col', class: (c.num ? 'num ' : '') + (c.cls || '') });
    if (c.sortable && onSort) {
      const active = sort && sort.key === c.key;
      th.setAttribute('aria-sort', active ? (sort.dir > 0 ? 'ascending' : 'descending') : 'none');
      th.append(h('button', { type: 'button', class: 'th-sort' + (active ? ' is-on' : ''), onclick: () => onSort(c.key) },
        h('span', { class: 'th-l' }, h('span', { text: c.label }), icon(active && sort.dir > 0 ? 'up' : 'down', 'th-ar')),
        c.sub ? h('span', { class: 'th-q', text: c.sub }) : null));
    } else if (c.sub) th.append(h('span', { text: c.label }), h('span', { class: 'th-q', text: c.sub }));
    else th.textContent = c.label;
    return th;
  }));
  const body = h('tbody', null, rows.map(r => {
    const tr = h('tr', null, cols.map((c, i) => {
      const v = c.cell(r);
      const td = h(i === 0 ? 'th' : 'td', { class: (c.num ? 'num ' : '') + (c.cls || '') + (sort && sort.key === c.key ? ' is-sorted' : ''), scope: i === 0 ? 'row' : null });
      if (v instanceof Node) td.append(v); else td.textContent = v == null ? '—' : v;
      return td;
    }));
    if (onRow) {
      tr.tabIndex = 0;
      tr.classList.add('is-link');
      if (rowKey) tr.dataset.key = rowKey(r);
      if (isSelected && isSelected(r)) tr.setAttribute('aria-selected', 'true');
      tr.addEventListener('click', () => onRow(r));
      tr.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onRow(r); } });
    }
    return tr;
  }));
  return h('table', { class: 'tbl' }, caption ? h('caption', { class: 'sr', text: caption }) : null, h('thead', null, head), body);
}
