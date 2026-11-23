// Small DOM helpers shared by every module: element builder, icons, popover, menu, toast, downloads.

export function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === 'class') e.className = v;
      else if (k === 'text') e.textContent = v;
      else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
      else if (k === 'style') Object.assign(e.style, v);
      else e.setAttribute(k, v === true ? '' : v);
    }
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) e.append(kid);
  return e;
}

// Icons are static, trusted markup (never data).
const ICONS = {
  info: '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor"><circle cx="8" cy="8" r="6.25" stroke-width="1.25"/><path d="M8 7.2v3.6M8 5.1v.2" stroke-width="1.4" stroke-linecap="round"/></svg>',
  more: '<svg viewBox="0 0 16 16" width="16" height="16" fill="currentColor"><circle cx="3.5" cy="8" r="1.1"/><circle cx="8" cy="8" r="1.1"/><circle cx="12.5" cy="8" r="1.1"/></svg>',
  chev: '<svg viewBox="0 0 8 8" width="8" height="8" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M1.2 2.8 4 5.6l2.8-2.8"/></svg>',
  check: '<svg viewBox="0 0 12 12" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="m2.2 6.3 2.5 2.5 5-5.4"/></svg>',
  up: '<svg viewBox="0 0 8 8" width="8" height="8" fill="currentColor"><path d="M4 1.2 7.4 6.8H.6z"/></svg>',
  down: '<svg viewBox="0 0 8 8" width="8" height="8" fill="currentColor"><path d="M4 6.8.6 1.2h6.8z"/></svg>',
  arrow: '<svg viewBox="0 0 12 12" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M2 6h8M6.6 2.6 10 6l-3.4 3.4"/></svg>',
  ext: '<svg viewBox="0 0 12 12" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M4.5 2.5h5v5M9.5 2.5 3 9"/></svg>',
  search: '<svg viewBox="0 0 12 12" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"><circle cx="5.2" cy="5.2" r="3.6"/><path d="m8 8 2.6 2.6"/></svg>',
  prev: '<svg viewBox="0 0 12 12" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M7.5 2.5 4 6l3.5 3.5"/></svg>',
  next: '<svg viewBox="0 0 12 12" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M4.5 2.5 8 6 4.5 9.5"/></svg>',
};
export function icon(name, cls) {
  const s = h('span', { class: 'ic' + (cls ? ' ' + cls : ''), 'aria-hidden': 'true' });
  s.innerHTML = ICONS[name];
  return s;
}

// ---------- floating layer ----------
const layer = () => document.getElementById('layer');
let open = null; // { el, anchor, close }

function place(el, anchor, align) {
  const r = anchor.getBoundingClientRect();
  const vw = document.documentElement.clientWidth;
  const w = el.offsetWidth, ht = el.offsetHeight;
  if (align === 'side') {
    // beside the anchor, top-aligned with it, so what sits under the anchor stays readable: to the right, or
    // to the left near the viewport's edge; with room on neither side it opens beneath, as a menu does
    const fitR = r.right + 8 + w <= vw - 8, fitL = r.left - 8 - w >= 8;
    if (fitR || fitL) {
      const top = Math.max(8, Math.min(r.top, window.innerHeight - ht - 8));
      el.style.left = `${Math.round((fitR ? r.right + 8 : r.left - 8 - w) + window.scrollX)}px`;
      el.style.top = `${Math.round(top + window.scrollY)}px`;
      return;
    }
  }
  let left = align === 'right' ? r.right - w : r.left;
  left = Math.max(8, Math.min(left, vw - w - 8));
  let top = r.bottom + 6;
  if (top + ht > window.innerHeight - 8 && r.top - ht - 6 > 8) top = r.top - ht - 6;
  el.style.left = `${Math.round(left + window.scrollX)}px`;
  el.style.top = `${Math.round(top + window.scrollY)}px`;
}

export function closePop() {
  if (!open) return;
  const o = open;
  open = null;
  o.el.remove();
  o.anchor.setAttribute('aria-expanded', 'false');
  if (o.onClose) o.onClose();
}

/** Open a floating element under an anchor. Clicking the anchor again, pressing Escape or clicking away closes it. */
export function openPop(anchor, el, { align = 'left', onClose } = {}) {
  if (open && open.anchor === anchor) { closePop(); return null; }
  closePop();
  layer().append(el);
  place(el, anchor, align);
  anchor.setAttribute('aria-expanded', 'true');
  open = { el, anchor, onClose };
  return el;
}

document.addEventListener('pointerdown', e => {
  if (open && !open.el.contains(e.target) && !open.anchor.contains(e.target)) closePop();
}, true);
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && open) { const a = open.anchor; closePop(); a.focus(); }
});
window.addEventListener('resize', closePop);
window.addEventListener('hashchange', closePop);

/** ⓘ popover: the catalog text, then Source and As of beneath a hairline. meta: [[label, value], …] */
export function infoPop(anchor, text, meta = []) {
  const id = 'pop-info';
  const paras = (Array.isArray(text) ? text : [text]).filter(Boolean).map(p => h('p', { text: String(p) }));
  const rows = meta.filter(m => m && m[1]).map(([k, v]) => h('div', { class: 'pop-meta-r' }, h('span', { class: 'pop-meta-k', text: k }), h('span', { text: String(v) })));
  openPop(anchor, h('div', { class: 'pop pop-info', role: 'tooltip', id }, paras, rows.length ? h('div', { class: 'pop-meta' }, rows) : null), { align: 'side' });
  anchor.setAttribute('aria-describedby', id);
}

/**
 * Menu. items: [{ label, value, checked, kind: 'radio' | 'check' | 'action', hint }]
 * onPick(item) is called after the menu closes.
 */
export function menu(anchor, items, onPick, { align = 'left' } = {}) {
  const el = h('div', { class: 'pop menu', role: 'menu' });
  const btns = items.map(it => {
    if (it.sep) return h('div', { class: 'menu-sep', role: 'separator' });
    const b = h('button', {
      class: 'menu-i', type: 'button',
      role: it.kind === 'radio' ? 'menuitemradio' : it.kind === 'check' ? 'menuitemcheckbox' : 'menuitem',
      'aria-checked': it.kind === 'radio' || it.kind === 'check' ? String(!!it.checked) : null,
      onclick: () => { closePop(); anchor.focus(); onPick(it); },
    }, h('span', { class: 'menu-l', text: it.label }), it.checked ? icon('check', 'menu-ck') : null);
    return b;
  });
  el.append(...btns);
  const focusable = btns.filter(b => b.tagName === 'BUTTON');
  el.addEventListener('keydown', e => {
    const i = focusable.indexOf(document.activeElement);
    if (e.key === 'ArrowDown') { e.preventDefault(); focusable[(i + 1) % focusable.length].focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); focusable[(i - 1 + focusable.length) % focusable.length].focus(); }
    else if (e.key === 'Home') { e.preventDefault(); focusable[0].focus(); }
    else if (e.key === 'End') { e.preventDefault(); focusable[focusable.length - 1].focus(); }
    else if (e.key === 'Tab') { closePop(); }
  });
  if (!openPop(anchor, el, { align })) return;
  const cur = focusable.find(b => b.getAttribute('aria-checked') === 'true') || focusable[0];
  if (cur) cur.focus({ preventScroll: true });
}

let toastTimer = null;
export function toast(text) {
  document.querySelectorAll('.toast').forEach(t => t.remove());
  const t = h('div', { class: 'toast', role: 'status', text });
  document.body.append(t);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.remove(), 1800);
}

export function download(name, blob) {
  const url = URL.createObjectURL(blob);
  const a = h('a', { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

export async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch {
    const ta = h('textarea', { class: 'sr' });
    ta.value = text;
    document.body.append(ta);
    ta.select();
    try { document.execCommand('copy'); } catch { /* nothing else to try */ }
    ta.remove();
  }
}

export const csvCell = v => {
  if (v == null) return '';
  const s = String(v);
  // neutralise spreadsheet formulas in untrusted names
  const safe = /^[=+\-@\t\r]/.test(s) && Number.isNaN(Number(s)) ? "'" + s : s;
  return /[",\n]/.test(safe) ? '"' + safe.replace(/"/g, '""') + '"' : safe;
};
export const toCsv = rows => rows.map(r => r.map(csvCell).join(',')).join('\n') + '\n';
