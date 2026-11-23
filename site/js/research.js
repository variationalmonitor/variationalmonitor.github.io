// Research Library: one plain list of pieces, newest first. A row is the title (linking to the page where the
// piece is published), the publisher and the date; a piece not yet out carries a quiet Scheduled tag.
// A new piece in data/research.json simply adds a row.

import { load } from './data.js';
import { fmtDate } from './format.js';
import { h } from './ui.js';

const DAY = /^\d{4}-\d{2}-\d{2}/;
// the file's `date`; an index written before the list became plain names the day in published_at or scheduled_for
const dayOf = p => [p.date, p.published_at, p.scheduled_for].map(v => (typeof v === 'string' && DAY.test(v) ? v.slice(0, 10) : null)).find(Boolean) || null;
const statusOf = p => (p.status === 'scheduled' || p.status === 'published' ? p.status : p.published_at ? 'published' : p.scheduled_for ? 'scheduled' : null);
const urlOf = p => (typeof p.url === 'string' && /^https:\/\/[^\s]+$/.test(p.url) ? p.url : null);

export class LibraryPage {
  constructor(app, page) {
    this.app = app; this.page = page;
    this.el = h('section', { class: 'card span-12 lib is-fit', id: 'card-research', role: 'table', 'aria-label': page.label });
  }

  async update() {
    const file = await load('research');
    if (this.dead) return;
    const pieces = file && Array.isArray(file.pieces) ? file.pieces.filter(p => p && typeof p.title === 'string') : [];
    if (!pieces.length) {
      this.el.classList.add('is-empty');
      this.el.replaceChildren(h('div', { class: 'nodata', text: 'No data yet' }));
      return;
    }
    this.el.classList.remove('is-empty');
    // newest first (a stable sort keeps the file's order within a day)
    const rows = pieces.map(p => ({ p, day: dayOf(p), url: urlOf(p), scheduled: statusOf(p) === 'scheduled' }))
      .sort((a, b) => String(b.day || '').localeCompare(String(a.day || '')));
    const head = h('div', { class: 'lib-row th-head', role: 'row' },
      ['Title', 'Publisher', 'Date'].map(t => h('span', { role: 'columnheader', text: t })));
    this.el.replaceChildren(head, ...rows.map(({ p, day, url, scheduled }) => h('div', { class: 'lib-row', role: 'row' },
      h('span', { class: 'lib-t', role: 'cell' },
        url ? h('a', { href: url, target: '_blank', rel: 'noopener noreferrer', text: p.title }) : h('span', { text: p.title }),
        scheduled ? h('span', { class: 'tag-sched', text: 'Scheduled' }) : null),
      h('span', { class: 'lib-c', role: 'cell', text: typeof p.publisher === 'string' && p.publisher ? p.publisher : '—' }),
      h('span', { class: 'lib-d', role: 'cell', text: day ? fmtDate(day) : '—' }))));
  }

  flash() {}
  hold() {}
  dispose() { this.dead = true; }
}
