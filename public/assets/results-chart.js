import {escapeHTML as esc, axisNumber, date} from './utils.js';

// Accounting and displayed amounts stay decimal-exact. Number conversion below
// is used only for bounded chart coordinates, never to reconcile source values.
const MAX_AMOUNT_CHARS = 40000;
const MAX_REPORTS = 2000;
function amountParts(value, source = false) {
  // A sum can have more whole digits than one source report. Keep the source
  // precision contract and a small, bounded allowance for aggregate carries.
  if (typeof value !== 'string' || value.length > MAX_AMOUNT_CHARS + (source ? 0 : 24)) return null;
  return (source ? /^(-?)(\d{1,17})(?:\.(\d+))?$/ : /^(-?)(\d{1,21})(?:\.(\d+))?$/).exec(value);
}
function trimFraction(fraction) {
  let end = fraction.length;
  while (end > 0 && fraction[end - 1] === '0') end--;
  return fraction.slice(0, end);
}
function normalized(value) {
  const parts = amountParts(value);
  if (!parts) return null;
  const whole = parts[2].replace(/^0+(?=\d)/, ''), fraction = trimFraction(parts[3] || '');
  return `${parts[1] && (whole !== '0' || fraction) ? '-' : ''}${whole}${fraction ? '.' + fraction : ''}`;
}
function money(value, signed = false) {
  const text = normalized(value);
  if (text === null) return 'Unavailable';
  const negative = text[0] === '-', [whole, fraction] = (negative ? text.slice(1) : text).split('.');
  return `${negative ? '−' : signed && text !== '0' ? '+' : ''}$${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',')}${fraction ? '.' + fraction : ''}`;
}
function validAmount(value) { return amountParts(value) ? value : null; }
function drawable(value) {
  const numeric = Number(value);
  return validAmount(value) !== null && Number.isFinite(numeric) && (numeric !== 0 || normalized(value) === '0');
}
function plotLine(line) {
  if (line.start === null || line.end === null) return line;
  if (!drawable(line.start) || !drawable(line.end) ||
      (Number(line.start) === Number(line.end) && normalized(line.start) !== normalized(line.end))) {
    return {...line, start: null, end: null, geometryNote: 'Below plot precision'};
  }
  return line;
}
function count(value) { return Number.isSafeInteger(value) && value >= 0 ? value : 0; }
function coverageText(coverage) {
  const reports = count(coverage?.reportCount), known = count(coverage?.reportedCount);
  return `${known} of ${reports} report${reports === 1 ? '' : 's'}`;
}
function reportedLine(row, reportCount) {
  const coverage = row?.coverage || {reportedCount: 0, reportCount, complete: false};
  return {
    key: row.key, label: row.label, basis: 'reported', kind: row.kind,
    value: validAmount(row.value), coverage,
    start: coverage.complete ? validAmount(row.chart?.start) : null,
    end: coverage.complete ? validAmount(row.chart?.end) : null,
  };
}
function adjustmentLine(check, reportCount) {
  return {
    key: `${check.id}_difference`, label: 'Source difference', basis: 'source_difference', kind: 'change',
    value: validAmount(check.reportedMinusCalculated),
    start: check.coverage?.complete ? validAmount(check.expected) : null,
    end: check.coverage?.complete ? validAmount(check.reported) : null,
    coverage: {reportedCount: count(check.coverage?.checkedReportCount), reportCount, complete: !!check.coverage?.complete},
  };
}

// A single display-line list drives both the graphic and CSV. Itemized costs
// remain a separate, unplotted source subtotal when the bridge is calculated.
function displayLines(model) {
  if (!model || !Array.isArray(model.rows) || !count(model.reportCount)) return [];
  const source = new Map(model.rows.map(row => [row.key, row]));
  const checks = new Map((model.reconciliations || []).map(check => [check.id, check]));
  const lines = [], costs = source.get('market_making_costs');
  for (const key of ['gross_revenue', 'market_making_costs', 'net_revenue', 'rewards', 'net_profit']) {
    const row = source.get(key);
    if (!row) continue;
    if (key === 'market_making_costs' && !row.coverage?.complete &&
        source.get('gross_revenue')?.coverage?.complete && source.get('net_revenue')?.coverage?.complete &&
        validAmount(model.grossToNetChange) !== null) {
      lines.push({
        key: 'gross_to_net_change', label: 'Gross-to-net change', basis: 'calculated', kind: 'change',
        value: model.grossToNetChange,
        start: validAmount(source.get('gross_revenue').value), end: validAmount(source.get('net_revenue').value),
        coverage: {reportedCount: count(model.reportCount), reportCount: count(model.reportCount), complete: true},
      });
    } else lines.push(reportedLine(row, count(model.reportCount)));
    const check = key === 'market_making_costs' && costs?.coverage?.complete ? checks.get('gross_to_net') :
      key === 'rewards' ? checks.get('net_to_profit') : null;
    if (check && (check.status === 'mismatch' || check.exceptions?.length)) lines.push(adjustmentLine(check, count(model.reportCount)));
  }
  if (costs && !costs.coverage?.complete && lines.some(line => line.key === 'gross_to_net_change')) {
    lines.push({...reportedLine(costs, count(model.reportCount)),
      label: 'Market Making Costs (disclosed subtotal)', unplotted: true, start: null, end: null});
  }
  return lines.map(plotLine);
}

/** Exact, null-preserving row arrays for the page's CSV export. */
export function waterfallExportRows(model) {
  return displayLines(model).map(line => [
    line.label, line.basis, line.value, line.start, line.end,
    count(line.coverage?.reportedCount), count(line.coverage?.reportCount),
  ]);
}

function scaleFor(lines) {
  const values = lines.flatMap(line => [line.start, line.end]).filter(value => validAmount(value) !== null).map(Number);
  if (!values.length) return null;
  let min = Math.min(0, ...values), max = Math.max(0, ...values);
  const rawStep = (max - min || 1) / 3;
  const magnitude = 10 ** Math.floor(Math.log10(rawStep));
  const step = ([1, 2, 2.5, 5, 10].find(value => value >= rawStep / magnitude) || 10) * magnitude;
  if (!Number.isFinite(step) || step <= 0) return null;
  min = Math.floor(min / step) * step;
  max = Math.ceil(max / step) * step;
  if (min === max) max = min + step;
  const ticks = [];
  for (let value = min, n = 0; value <= max + step / 100 && n < 12; value = min + ++n * step) ticks.push(value);
  const position = value => Math.max(0, Math.min(100, (Number(value) - min) / (max - min) * 100));
  return {min, max, step, ticks, position};
}
function percentPosition(value) { return Number.isFinite(value) ? value.toFixed(6) + '%' : '0%'; }
function grid(scale) {
  return scale ? scale.ticks.map(value => `<i class="results-wf-grid${value === 0 ? ' is-zero' : ''}" style="left:${percentPosition(scale.position(value))}"></i>`).join('') : '';
}
function geometry(line, scale) {
  if (!scale || validAmount(line.start) === null || validAmount(line.end) === null) {
    return `<span class="results-wf-unavailable">${esc(line.geometryNote || 'No complete bridge')}</span>`;
  }
  const start = scale.position(line.start), end = scale.position(line.end);
  const zero = normalized(line.start) === normalized(line.end);
  return `<span class="results-wf-bar is-${esc(line.basis)} is-${esc(line.kind)}${zero ? ' is-zero' : ''}" style="left:${percentPosition(Math.min(start, end))};width:${percentPosition(Math.abs(end - start))}"></span>${line.kind === 'change' ? `<i class="results-wf-end" style="left:${percentPosition(end)}"></i>` : ''}`;
}
function chartLabel(line) {
  if (line.key === 'gross_revenue') return 'Spreads Paid';
  if (line.key === 'market_making_costs') return 'Market Making Costs';
  return line.label;
}
function lineNote(line) {
  if (line.basis === 'calculated') return 'Calculated from reported endpoints';
  if (line.basis === 'source_difference') return 'Reported minus calculated';
  if (!line.coverage.complete) return line.value === null ? 'Not disclosed' : `Disclosed subtotal · ${coverageText(line.coverage)}`;
  if (line.key === 'gross_revenue') return 'Gross Revenue';
  if (line.key === 'market_making_costs') return 'Cost of Revenue';
  return '';
}
function costDisclosure(model) {
  const costs = model.rows.find(row => row.key === 'market_making_costs');
  if (!costs || costs.coverage?.complete) return '';
  const amount = validAmount(costs.value);
  return `<p class="results-wf-note">${amount === null ? count(model.reportCount) === 1 ? 'Market Making Costs omitted from this update.' : 'Market Making Costs omitted from the selected updates.' : `Itemized Market Making Costs: ${esc(money(amount))} across ${count(costs.coverage?.reportedCount)} of ${count(costs.coverage?.reportCount)} updates.`}</p>`;
}
function sourceDetails(model) {
  const checks = (model.reconciliations || []).filter(check => check.exceptions?.length || !check.coverage?.complete);
  if (!checks.length) return '';
  const exceptionCount = checks.reduce((total, check) => total + (check.exceptions?.length || 0), 0);
  const title = exceptionCount ? `${exceptionCount} source difference${exceptionCount === 1 ? '' : 's'}` : 'Disclosure coverage';
  const sections = checks.map(check => {
    const name = check.id === 'gross_to_net' ? 'Spreads Paid + Market Making Costs → Net Revenue' : 'Net Revenue + Rewards → Net Profit';
    const missing = count(check.coverage?.reportCount) - count(check.coverage?.checkedReportCount);
    const exceptions = (check.exceptions || []).map(item => `<li><strong>${esc(date(item.publishedAt))}</strong><dl><div><dt>Calculated</dt><dd>${esc(money(item.expected))}</dd></div><div><dt>Reported</dt><dd>${esc(money(item.reported))}</dd></div><div class="is-difference"><dt>Reported − calculated</dt><dd>${esc(money(item.reportedMinusCalculated, true))}</dd></div></dl></li>`).join('');
    return `<section><h4>${esc(name)}</h4>${missing ? `<p>${missing} of ${count(check.coverage?.reportCount)} reports lack an input for this identity.</p>` : ''}${exceptions ? `<ul class="results-exceptions">${exceptions}</ul>` : ''}</section>`;
  }).join('');
  return `<details class="results-details${exceptionCount ? ' has-difference' : ''}"><summary>${esc(title)}</summary><div class="results-details-body">${exceptionCount ? '<p>Source differences reconcile published totals; they are not additional income. Individual exceptions remain visible even when they offset.</p>' : ''}${sections}</div></details>`;
}

/** Pure HTML body. Panel headings, selection controls and sources belong to caller. */
export function waterfallBody(model) {
  const lines = displayLines(model).filter(line => !line.unplotted);
  if (!lines.length) return '<div class="results-waterfall"><p class="results-empty">No reports in this selection.</p></div>';
  const scale = scaleFor(lines);
  const legend = [['total', 'Reported total'], ['change', 'Reported change'],
    ...(lines.some(line => line.basis === 'calculated') ? [['calculated', 'Calculated change']] : []),
    ...(lines.some(line => line.basis === 'source_difference') ? [['difference', 'Source difference']] : [])];
  const axis = scale ? `<div class="results-wf-axis-row" aria-hidden="true"><span>USD</span><div class="results-wf-axis">${scale.ticks.map((value, index) => `<span class="${index === 0 ? 'is-first' : index === scale.ticks.length - 1 ? 'is-last' : ''}" style="left:${percentPosition(scale.position(value))}">${esc(axisNumber(value, scale.step))}</span>`).join('')}</div><span>Amount</span></div>` : '';
  const rows = lines.map(line => `<li class="results-wf-row${line.basis === 'source_difference' ? ' is-difference' : ''}"><div class="results-wf-label"><span>${esc(chartLabel(line))}</span>${lineNote(line) ? `<small>${esc(lineNote(line))}</small>` : ''}</div><div class="results-wf-track" aria-hidden="true">${grid(scale)}${geometry(line, scale)}</div><div class="results-wf-amount">${esc(money(line.value, line.kind === 'change'))}</div></li>`).join('');
  return `<div class="results-waterfall"><div class="results-wf-legend">${legend.map(([key, label]) => `<span><i class="is-${key}" aria-hidden="true"></i>${esc(label)}</span>`).join('')}</div>${axis}<ol class="results-wf-rows" aria-label="Reported operating result waterfall">${rows}</ol>${costDisclosure(model)}${sourceDetails(model)}</div>`;
}

function allocationMath(reports) {
  const parts = new Map();
  let digits = 0;
  for (const report of reports) for (const key of ['treasury_inflow', 'olp_pnl', 'net_profit']) {
    const value = report?.period_amounts?.[key], match = amountParts(value, true);
    if (report?.units?.[key] !== '$' || !match) continue;
    parts.set(value, match); digits = Math.max(digits, (match[3] || '').length);
  }
  const scale = 10n ** BigInt(digits), maximum = 10n ** 16n * scale, parsed = new Map();
  for (const [value, match] of parts) {
    const amount = BigInt(match[2]) * scale + BigInt((match[3] || '').padEnd(digits, '0') || '0');
    parsed.set(value, amount <= maximum ? match[1] ? -amount : amount : null);
  }
  return {
    value: (report, key) => report?.units?.[key] === '$' ? parsed.get(report?.period_amounts?.[key]) ?? null : null,
    text(value) {
      if (value === null) return null;
      const magnitude = value < 0n ? -value : value;
      const fraction = digits ? trimFraction((magnitude % scale).toString().padStart(digits, '0')) : '';
      return `${value < 0n ? '-' : ''}${magnitude / scale}${fraction ? '.' + fraction : ''}`;
    },
  };
}
function summarize(reports, key, math) {
  const values = reports.map(report => math.value(report, key));
  const known = values.filter(value => value !== null);
  return {value: known.length ? known.reduce((total, value) => total + value, 0n) : null, count: known.length, complete: reports.length > 0 && known.length === reports.length};
}
function absoluteSourceLink(report) {
  for (const value of Array.isArray(report?.source_urls) ? report.source_urls : []) {
    try {
      const url = new URL(value);
      if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) continue;
      return `<a href="${esc(url.href)}" target="_blank" rel="noopener noreferrer">${esc(date(report.published_at))}</a>`;
    } catch { /* Invalid source URLs are never inserted into markup. */ }
  }
  return esc(date(report?.published_at));
}
function share(value, total) { return Number((value * 1000n * 2n + total) / (total * 2n)) / 10; }
function allocationWidth(value, total) { return Number(value * 100000000n / total) / 1000000; }

/** Separate allocation identity. Treasury/OLP are not extra operating expenses. */
export function allocationBody(input) {
  const reports = Array.isArray(input) && input.length <= MAX_REPORTS ? input : [];
  if (!reports.length) return '<div class="results-allocation"><p class="results-empty">No reports in this selection.</p></div>';
  const math = allocationMath(reports), decimalText = math.text;
  const treasury = summarize(reports, 'treasury_inflow', math), olp = summarize(reports, 'olp_pnl', math), profit = summarize(reports, 'net_profit', math);
  const fields = [treasury, olp, profit], complete = fields.every(item => item.complete);
  const exceptions = [], negativePeriods = [];
  let checked = 0;
  for (const report of reports) {
    const t = math.value(report, 'treasury_inflow'), o = math.value(report, 'olp_pnl'), p = math.value(report, 'net_profit');
    if (t !== null && o !== null && p !== null) {
      checked++;
      if (t + o !== p) exceptions.push({report, expected: t + o, reported: p, difference: p - t - o});
      if (t < 0n || o < 0n || p < 0n) negativePeriods.push({report, treasury: t, olp: o, profit: p});
    }
  }
  const reconciled = complete && checked === reports.length && exceptions.length === 0 && treasury.value + olp.value === profit.value;
  const composition = reconciled && profit.value > 0n && treasury.value >= 0n && olp.value >= 0n;
  const rows = [['Protocol Treasury Inflow', treasury, 'treasury'], ['OLP PnL', olp, 'olp']];
  const content = rows.map(([label, item, key]) => `<div class="results-allocation-row"><dt><i class="is-${key}" aria-hidden="true"></i>${esc(label)}${!item.complete ? `<small>${item.count} of ${reports.length} reports${item.value === null ? ' · not disclosed' : ' · disclosed subtotal'}</small>` : ''}</dt><dd>${esc(money(decimalText(item.value)))}${composition ? `<small>${share(item.value, profit.value).toFixed(1)}%</small>` : ''}</dd></div>`).join('');
  const allocationBar = composition ? `<div class="results-allocation-strip" aria-label="Reconciled allocation of selected reported profit"><span class="is-treasury" style="width:${percentPosition(allocationWidth(treasury.value, profit.value))}"></span><span class="is-olp" style="width:${percentPosition(100 - allocationWidth(treasury.value, profit.value))}"></span></div>` : '';
  const state = !complete ? `Allocation incomplete: ${checked} of ${reports.length} reports include all three amounts.` :
    exceptions.length ? `${exceptions.length} source allocation difference${exceptions.length === 1 ? '' : 's'}. Percentages withheld.` :
    profit.value === 0n ? 'Reported allocations reconcile to zero. No percentage allocation.' :
    !composition ? 'Signed reported allocations reconcile. No percentage allocation for this loss or negative component.' : 'Protocol Treasury Inflow + OLP PnL = Net Profit';
  const identity = `<div class="results-allocation-total"><span>Net Profit${!profit.complete ? ' · disclosed subtotal' : ''}</span><strong>${esc(money(decimalText(profit.value)))}</strong></div>`;
  const issues = exceptions.map(item => `<li><strong>${absoluteSourceLink(item.report)}</strong><dl><div><dt>Treasury + OLP</dt><dd>${esc(money(decimalText(item.expected)))}</dd></div><div><dt>Net Profit</dt><dd>${esc(money(decimalText(item.reported)))}</dd></div><div class="is-difference"><dt>Profit − allocations</dt><dd>${esc(money(decimalText(item.difference), true))}</dd></div></dl></li>`).join('');
  const losses = negativePeriods.map(item => `<li><strong>${absoluteSourceLink(item.report)}</strong><dl><div><dt>Protocol Treasury Inflow</dt><dd>${esc(money(decimalText(item.treasury)))}</dd></div><div><dt>OLP PnL</dt><dd>${esc(money(decimalText(item.olp)))}</dd></div><div><dt>Net Profit</dt><dd>${esc(money(decimalText(item.profit)))}</dd></div></dl></li>`).join('');
  const details = issues || losses ? `<details class="results-details${issues ? ' has-difference' : ''}"><summary>${issues ? 'Allocation source differences' : 'Periods with a negative amount'}</summary><div class="results-details-body">${issues ? `<ul class="results-exceptions">${issues}</ul>` : ''}${losses ? `<p>Selected totals retain these signed source amounts.</p><ul class="results-exceptions">${losses}</ul>` : ''}</div></details>` : '';
  return `<div class="results-allocation">${allocationBar}<dl class="results-allocation-values">${content}</dl>${identity}<p class="results-allocation-status${exceptions.length ? ' has-difference' : ''}">${esc(state)}</p>${details}</div>`;
}
