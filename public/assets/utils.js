export const escapeHTML=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
export function number(value) {
  if (typeof value !== 'number' && (typeof value !== 'string' || !value.trim())) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

const numberFormats = new Map();
function formatNumber(value, digits, fixedDecimals) {
  const key = digits + '/' + fixedDecimals;
  if (!numberFormats.has(key)) {
    numberFormats.set(key, new Intl.NumberFormat('en-US', {
      maximumFractionDigits: digits,
      minimumFractionDigits: fixedDecimals ? digits : 0
    }));
  }
  return numberFormats.get(key).format(value);
}

export function compact(value, digits = 2) {
  const result = number(value);
  if (result === null) return '—';
  const sign = result < 0 ? '−' : '';
  const amount = Math.abs(result);
  for (const [scale, suffix] of [[1e12, 'T'], [1e9, 'B'], [1e6, 'M'], [1e3, 'K']]) {
    if (amount >= scale) return sign + formatNumber(amount / scale, digits, true) + suffix;
  }
  return sign + formatNumber(amount, digits, false);
}
export function price(value) {
  const amount = number(value);
  if (amount === null) return '—';
  if (amount === 0 || Math.abs(amount) >= 1) return formatNumber(amount, 2, true);
  if (Math.abs(amount) < 1e-8) return amount.toExponential(4);
  const digits = 5 - Math.floor(Math.log10(Math.abs(amount)));
  return formatNumber(amount, digits, false);
}
export function axisNumber(value, step) {
  if (!Number.isFinite(value) || !Number.isFinite(step) || step <= 0) return '—';
  if (value === 0) return '0';
  const units = [[1e12,'T'], [1e9,'B'], [1e6,'M'], [1e3,'K']];
  const [scale, suffix] = units.find(([threshold]) => Math.abs(value) >= threshold) || [1,''];
  const digits = Math.min(12, Math.max(0, Math.ceil(-Math.log10(step / scale)) + 1));
  return formatNumber(value / scale, digits, false) + suffix;
}
export const fixed = (value, digits = 2) => number(value) === null ? '—' : formatNumber(number(value), digits, true);
export const cash = (value, digits = 2) => number(value) === null ? '—' : '$' + compact(value, digits);
export const percent = (value, digits = 1) => number(value) === null ? '—' : fixed(number(value) * 100, digits) + '%';
export const ratio = (a, b) => number(a) !== null && number(b) !== null && number(b) !== 0 ? number(a) / number(b) : null;

const dateFormats = {
  day: new Intl.DateTimeFormat('en-GB', {day:'numeric', month:'short', timeZone:'UTC'}),
  date: new Intl.DateTimeFormat('en-GB', {day:'numeric', month:'short', year:'numeric', timeZone:'UTC'}),
  time: new Intl.DateTimeFormat('en-GB', {
    day:'numeric', month:'short', year:'numeric', hour:'2-digit', minute:'2-digit', hourCycle:'h23', timeZone:'UTC'
  })
};
function formatDate(value, mode, fallback) {
  if (typeof value !== 'string' && typeof value !== 'number' && !(value instanceof Date)) return fallback;
  const moment = new Date(value);
  if (!Number.isFinite(moment.getTime())) return fallback;
  return dateFormats[mode].format(moment) + (mode === 'time' ? ' UTC' : '');
}
export const date = value => formatDate(value, 'date', 'Unavailable');
export const shortDate = value => formatDate(value, 'day', '—');
export const timestamp = value => formatDate(value, 'time', 'Unavailable');
export function safeURL(value){if(typeof value!=='string'||!value.trim())return null;try{const url=new URL(value,location.href);return ['https:','http:'].includes(url.protocol)?url.href:null;}catch{return null;}}
export function sourceLink(url,label='Source'){const safe=safeURL(url);return safe?`<a class="inline-link" href="${escapeHTML(safe)}" target="_blank" rel="noopener noreferrer">${escapeHTML(label)}</a>`:'<span class="muted">Source unavailable</span>';}
export function downloadCSV(name,columns,rows){const cell=value=>{let text=String(value??'');if(/^[=+@\-\t\r]/.test(text)&&!/^[-+]?\d+(?:\.\d+)?$/.test(text))text="'"+text;return '"'+text.replaceAll('"','""')+'"';};const content=[columns,...rows].map(row=>row.map(cell).join(',')).join('\r\n');const url=URL.createObjectURL(new Blob(['\uFEFF'+content],{type:'text/csv;charset=utf-8;'}));const a=document.createElement('a');a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
export function sum(values){const present=values.map(number).filter(v=>v!==null);return present.length?present.reduce((a,b)=>a+b,0):null;}
export function cumulative(values){let total=0;return values.map(v=>{const n=number(v);if(n===null)return null;total+=n;return total;});}
