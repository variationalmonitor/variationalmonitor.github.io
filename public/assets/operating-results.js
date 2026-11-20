/** Exact operating-result rows for an already selected set of official reports.
 *
 * Selection belongs to the caller: no fallback to the latest report, no use of
 * dataset.totals, and no proration by publication date. Dollar strings remain
 * strings all the way to the renderer/export; BigInt is internal only.
 */
// Match financials.py's source-report and count bounds. Decimal-normalized source
// strings can have many leading fractional zeros; a fixed cents/18-place limit
// would silently impose a narrower financial contract than the collector.
const MAX_AMOUNT_CHARS = 40000;
const MAX_REPORTS = 2000;

const FIELDS = Object.freeze([
  {key: 'gross_revenue', label: 'Spreads Paid (Gross Revenue)', kind: 'total'},
  {key: 'market_making_costs', label: 'Market Making Costs (Cost of Revenue)', kind: 'change'},
  {key: 'net_revenue', label: 'Net Revenue', kind: 'total'},
  {key: 'rewards', label: 'Rewards', kind: 'change'},
  {key: 'net_profit', label: 'Net Profit', kind: 'total'},
].map(Object.freeze));

export class OperatingResultsError extends Error {
  constructor(code) {
    super(code);
    this.name = 'OperatingResultsError';
    this.code = code;
  }
}

function fail(code) { throw new OperatingResultsError(code); }

function decimalMath(input) {
  if (!Array.isArray(input) || input.length > MAX_REPORTS) fail('invalid_report_selection');
  const parts = new Map();
  let digits = 0;
  for (const report of input) {
    for (const field of FIELDS) {
      const value = report?.period_amounts?.[field.key];
      if (value === null || value === undefined || value === '' || parts.has(value)) continue;
      // Check length and normalized notation before any BigInt/power allocation.
      if (typeof value !== 'string' || value.length > MAX_AMOUNT_CHARS) fail('invalid_dollar_string');
      const match = /^(-?)(\d{1,17})(?:\.(\d+))?$/.exec(value);
      if (!match) fail('invalid_dollar_string');
      parts.set(value, match);
      digits = Math.max(digits, (match[3] || '').length);
    }
  }
  const scale = 10n ** BigInt(digits);
  const maximum = 10n ** 16n * scale;
  const parsed = new Map();
  for (const [value, match] of parts) {
    const fraction = (match[3] || '').padEnd(digits, '0');
    const magnitude = BigInt(match[2]) * scale + BigInt(fraction || '0');
    if (magnitude > maximum) fail('dollar_amount_out_of_bounds');
    parsed.set(value, match[1] ? -magnitude : magnitude);
  }
  return {
    parse: value => value === null || value === undefined || value === '' ? null : parsed.get(value),
    text(value) {
      if (value === null) return null;
      if (value === 0n) return '0';
      const negative = value < 0n;
      const absolute = negative ? -value : value;
      const whole = absolute / scale;
      const rawFraction = digits ? (absolute % scale).toString().padStart(digits, '0') : '';
      let end = rawFraction.length;
      // A suffix regex can backtrack quadratically across a long zero prefix.
      // This bounded linear scan also handles extremely small source decimals.
      while (end > 0 && rawFraction[end - 1] === '0') end -= 1;
      const fraction = rawFraction.slice(0, end);
      return `${negative ? '-' : ''}${whole}${fraction ? '.' + fraction : ''}`;
    },
  };
}

function publication(value) {
  if (typeof value !== 'string' || value.length > 40 ||
      !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)) {
    fail('invalid_publication_timestamp');
  }
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) fail('invalid_publication_timestamp');
  return parsed;
}

function prepareReports(input, decimal) {
  if (!Array.isArray(input) || input.length > MAX_REPORTS) fail('invalid_report_selection');
  const ids = new Set();
  return input.map(report => {
    if (!report || typeof report !== 'object' ||
        typeof report.id !== 'string' || !/^[a-zA-Z0-9:_-]{1,120}$/.test(report.id)) fail('invalid_report_id');
    if (ids.has(report.id)) fail('duplicate_report_id');
    ids.add(report.id);
    const published = publication(report.published_at);
    const values = {};
    const labels = {};
    for (const field of FIELDS) {
      const value = decimal(report.period_amounts?.[field.key]);
      if (value !== null && report.units?.[field.key] !== '$') fail('incompatible_dollar_unit');
      // The current source contract reports both deduction lines with a negative
      // sign. Do not reinterpret a future positive line as a refund automatically.
      if (value !== null && field.kind === 'change' && value > 0n) fail('deduction_sign_changed');
      values[field.key] = value;
      const label = report.labels?.[field.key];
      if (label !== null && label !== undefined && label !== '') {
        if (typeof label !== 'string' || label.length > 120 ||
            label.replace(/^2w\s+/i, '').toLowerCase() !== field.label.toLowerCase()) fail('source_label_changed');
        if (value !== null) labels[field.key] = label;
      }
    }
    return {id: report.id, publishedAt: report.published_at, published, values, labels};
  }).sort((a, b) => a.published - b.published || a.id.localeCompare(b.id));
}

function fieldCoverage(count, reportCount) {
  const complete = reportCount > 0 && count === reportCount;
  return {
    reportedCount: count, reportCount, complete,
    status: reportCount === 0 ? 'empty' : count === 0 ? 'missing' : complete ? 'complete' : 'partial',
  };
}

function summarize(reports, key) {
  let sum = 0n;
  let count = 0;
  for (const report of reports) {
    const value = report.values[key];
    if (value !== null) { sum += value; count += 1; }
  }
  return {sum: count ? sum : null, coverage: fieldCoverage(count, reports.length)};
}

function reconciliation(reports, summary, id, left, change, target, text) {
  const incomplete = [];
  const exceptions = [];
  for (const report of reports) {
    const a = report.values[left], b = report.values[change], reported = report.values[target];
    if (a === null || b === null || reported === null) {
      incomplete.push(report.id);
      continue;
    }
    const expected = a + b;
    const residual = expected - reported;
    if (residual !== 0n) exceptions.push({
      reportId: report.id, publishedAt: report.publishedAt,
      expected: text(expected), reported: text(reported), residual: text(residual),
      reportedMinusCalculated: text(-residual),
    });
  }
  const complete = reports.length > 0 && incomplete.length === 0;
  const expected = complete ? summary[left].sum + summary[change].sum : null;
  const reported = summary[target].sum;
  const residual = complete ? expected - reported : null;
  const status = reports.length === 0 ? 'empty' : exceptions.length ? 'mismatch' : complete ? 'reconciled' : 'incomplete';
  return {
    id, status, expected: text(expected), reported: text(reported), residual: text(residual),
    reportedMinusCalculated: residual === null ? null : text(-residual),
    residualDefinition: 'expected_minus_reported',
    reportedCoverage: summary[target].coverage,
    coverage: {checkedReportCount: reports.length - incomplete.length, reportCount: reports.length, complete},
    missingReportIds: incomplete, affectedReportIds: exceptions.map(item => item.reportId), exceptions,
  };
}

/**
 * Five source rows plus two independently checked identities. An incomplete
 * item's value is only the sum actually reported; null chart geometry prevents
 * presenting it as a full-period flow. Source totals remain anchors on mismatch.
 *
 * grossToNetChange is a separate derived endpoint difference, NOT imputed Market
 * Making Costs. Rewards is the reported total: loss-refund/referral sub-lines,
 * treasury distributions and lifetime/stock figures must not be added again.
 */
export function buildOperatingResults(selectedReports) {
  const {parse, text} = decimalMath(selectedReports);
  const reports = prepareReports(selectedReports, parse);
  const summary = Object.fromEntries(FIELDS.map(field => [field.key, summarize(reports, field.key)]));
  const rows = FIELDS.map(field => {
    const entry = summary[field.key];
    let start = null, end = null;
    if (entry.coverage.complete) {
      if (field.kind === 'total') {
        start = 0n; end = entry.sum;
      } else {
        const anchorKey = field.key === 'market_making_costs' ? 'gross_revenue' : 'net_revenue';
        if (summary[anchorKey].coverage.complete) {
          start = summary[anchorKey].sum;
          end = start + entry.sum;
        }
      }
    }
    return {
      key: field.key, label: field.label, kind: field.kind, value: text(entry.sum),
      coverage: entry.coverage,
      sourceLabels: [...new Set(reports.map(report => report.labels[field.key]).filter(Boolean))],
      chart: {start: text(start), end: text(end)},
    };
  });
  const checks = [
    reconciliation(reports, summary, 'gross_to_net', 'gross_revenue', 'market_making_costs', 'net_revenue', text),
    reconciliation(reports, summary, 'net_to_profit', 'net_revenue', 'rewards', 'net_profit', text),
  ];
  const hasMismatch = checks.some(check => check.status === 'mismatch');
  const hasIncomplete = reports.length > 0 && rows.some(row => !row.coverage.complete);
  const endpointsComplete = summary.gross_revenue.coverage.complete && summary.net_revenue.coverage.complete;
  return {
    unit: '$', reportCount: reports.length, reportIds: reports.map(report => report.id),
    firstPublishedAt: reports[0]?.publishedAt ?? null,
    lastPublishedAt: reports.at(-1)?.publishedAt ?? null,
    rows, reconciliations: checks,
    grossToNetChange: endpointsComplete ? text(summary.net_revenue.sum - summary.gross_revenue.sum) : null,
    status: reports.length === 0 ? 'empty' : hasMismatch ? 'mismatch' : hasIncomplete ? 'incomplete' : 'reconciled',
    hasMismatch, hasIncomplete,
  };
}
