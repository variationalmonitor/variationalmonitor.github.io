// The residency map (catalog v3): navigation, pages and one declarative entry per card.
// Products are the top axis (Omni now, Pro later); financials, token and research are company-wide.
// Each card id is the only home of its series and its headline figure; assertResidency()
// enforces that in code. Copy (titles, ⓘ text) is final and pasted from the plan's catalog.

// The rail is drawn entirely from this config, so regrouping the site is an edit here and nowhere else.
//   group: { section?: 'Header', pages: [item] }
//   item:  a page    { id: 'route/path', label, kind?, sub?, range? }
//          a product { label, product: true, pages?: [page], tag?: 'Not live' }   a label, not a link; no pages when not live
//   kind:  a page that is not a card grid ('research', 'data'); sub: the page kind opened by `#/<id>/<key>`
//   filters: page-level filter rows, each { key, label, options: [[value, label]], off?: { value: { otherKey: otherValue } } };
//            they live in the URL (`asset=tradfi&inst=swap`) and the first option is the default
//   range: false when the page does not use the Range control
//   minRange: the shortest range the page offers (a seven-day window cannot hold a two-week report period);
//             shorter options are shown disabled, and arriving with one uses this range instead
// A card names its page in its own entry (`tab`); no code refers to a page by name.
export const NAV = [
  { pages: [{ id: 'overview', label: 'Overview' }] },
  {
    section: 'Products', pages: [
      {
        label: 'Omni', product: true, pages: [
          {
            // a market page opens at 7D, its own default, where its cards draw half-hourly snapshots
            id: 'omni/markets', label: 'Markets', sub: 'market', subRange: '7d',
            // a scope that lists fewer than 30 markets drops or narrows the cards that say `few`
            few: { file: 'markets_scope', col: 'listed', below: 30 },
            // Two filter values scope every card and the table on the page; the scope key is `<asset>.<inst>`.
            // The reader chooses them through one row of tabs beneath the title, each tab a pair of values, and
            // every market sits in exactly one of the last three tabs (owner, 23 Nov 2026: two independent filter
            // rows were confusing, and so was their place beside the title).
            filters: [
              { key: 'asset', label: 'Asset', options: [['all', 'All markets'], ['crypto', 'Crypto'], ['tradfi', 'TradFi']] },
              { key: 'inst', label: 'Instrument', options: [['all', 'All'], ['perp', 'Perpetuals'], ['swap', 'Swaps']], off: { swap: { asset: 'crypto' } } },
            ],
            tabs: [
              ['All markets', { asset: 'all', inst: 'all' }],
              ['Crypto perpetuals', { asset: 'crypto', inst: 'all' }],
              ['TradFi perpetuals', { asset: 'tradfi', inst: 'perp' }],
              ['Swaps', { asset: 'tradfi', inst: 'swap' }],
            ],
          },
          { id: 'omni/execution', label: 'Execution' },
          { id: 'omni/accounts', label: 'Accounts' },
          { id: 'omni/deposits', label: 'Deposits' },
          { id: 'omni/olp', label: 'OLP' },
        ],
      },
      { label: 'Pro', product: true, tag: 'Not live' },
    ],
  },
  { section: 'Financials', pages: [{ id: 'financials/results', label: 'Operating results', minRange: '30d' }, { id: 'financials/treasury', label: 'Protocol Treasury' }] },
  { section: 'Token', pages: [{ id: 'token/var', label: '$VAR' }, { id: 'token/rewards', label: 'Rewards and points' }] },
  // one list of pieces; an old link to a piece (`#/research/<slug>`) lands on the list
  { section: 'Research', pages: [{ id: 'research', label: 'Research Library', kind: 'research', range: false }] },
];
// the rail's foot
export const FOOT = { id: 'data', label: 'Data', kind: 'data', range: false };

// a page's `section` is its product when it sits under one (Omni), else its section header
const flat = (items, section) => items.flatMap(p => (p.id ? [{ ...p, section }] : []).concat(flat(p.pages || [], p.product ? p.label : section)));
export const PAGES = NAV.flatMap(g => flat(g.pages, g.section || null)).concat(FOOT);
export const HOME = PAGES[0].id;

// routes and card ids from earlier versions keep working
export const ROUTE_ALIAS = {
  markets: 'omni/markets', tradfi: 'omni/markets?asset=tradfi&inst=perp', 'omni/tradfi': 'omni/markets?asset=tradfi&inst=perp', execution: 'omni/execution', accounts: 'omni/accounts', deposits: 'omni/deposits',
  olp: 'omni/olp', rewards: 'token/rewards', 'omni/rewards': 'token/rewards', 'omni/swaps': 'omni/markets?asset=tradfi&inst=swap', financials: 'financials/results', results: 'financials/results', treasury: 'financials/treasury',
  var: 'token/var', thesis: 'research', methodology: 'data',
};
export const CARD_ALIAS = {
  'ov.peers': 'mk.peers', 'mk.volume_class': 'mk.volume', 'mk.oi_class': 'mk.oi', 'mk.volume_group': 'mk.volume', 'mk.oi_group': 'mk.oi',
  'tf.volume_class': 'mk.volume?asset=tradfi&inst=perp', 'tf.oi_class': 'mk.oi?asset=tradfi&inst=perp', 'tf.listed': 'mk.listed?asset=tradfi&inst=perp',
  'sw.volume': 'mk.volume?asset=tradfi&inst=swap', 'sw.oi': 'mk.oi?asset=tradfi&inst=swap', 'ex.pairs': 'sw.pairs', 'tf.pairs': 'sw.pairs',
  'ex.median': 'mk.slippage', 'olp.net': 'mk.ls', 'olp.matched': 'olp.offset', 'olp.pnl_bps': 'olp.period',
  'ac.flows': 'dp.flows', 'ac.balances': 'dp.balances', 'ac.concentration': 'dp.tiers',
  'fin.treasury': 'tr.holdings', 'var.points': 'rw.points',
};

// Asset classes, base to top, in Variational's own names. Class keys are read from the files;
// this table only names and colours them (the two keys of the earlier contract are still understood).
export const CLASSES = [
  ['crypto', 'Crypto', 'blue'],
  ['equity', 'Equities', 'honey'],
  ['equity_etf', 'Equities and ETFs', 'honey'],
  ['etf', 'ETFs', 'teal'],
  ['index', 'Indices', 'violet'],
  ['commodity', 'Commodities', 'pink'],
  ['fx', 'FX', 'grey'],
  ['pre_ipo', 'Pre-IPO', 'grey2'],
  ['other', 'Pre-IPO', 'grey2'],
];
export const GROUPS = [
  ['crypto_majors', 'Crypto majors', 'blue'],
  ['other_crypto', 'Other crypto', 'violet'],
  ['tradfi_perp', 'TradFi perpetuals', 'pink'],
  ['tradfi_swap', 'Swaps', 'honey'],
];
export const TYPES = { perp: 'Perpetual', swap: 'Swap' };
// venue colours on the execution dot plot, by venue key; Omni is always blue
export const VENUE_COLOR = { hl: 'teal', lighter: 'violet' };

const S = (col, label, color, extra) => ({ col, label, color, ...extra });
const byGroup = prefix => GROUPS.map(([k, l, c]) => S(`${prefix}.${k}`, l, c));
// the three groups of the whole universe: TradFi is every perpetual or swap on a non-crypto underlying
const TOP = prefix => [S(`${prefix}.crypto_majors`, 'Crypto majors', 'blue'), S(`${prefix}.other_crypto`, 'Other crypto', 'violet'), S(`${prefix}.tradfi`, 'TradFi', 'honey')];

export const CATALOG = [
  // ------------------------------------------------------------------ Overview: all products
  // The summary page, and the one page allowed to repeat (owner, 23 Nov 2026). Four totals, then the headline
  // chart of every section in its plainest form: totals, no splits. Each card names its home card (`pointer`),
  // where the same series is broken down, and its title opens that card. When a second product is live the
  // figures here are Omni plus Pro.
  {
    id: 'ov.t_volume', tab: 'overview', span: 3, form: 'stat', title: 'Cumulative volume', pointer: 'mk.volume',
    info: "USD notional traded on Variational since launch, from the Omni API's cumulative counter.",
    source: 'Omni API', file: 'volume_daily',
    headline: { rule: 'last', col: 'cumulative', label: 'All time', unit: 'usd' },
  },
  {
    id: 'ov.t_revenue', tab: 'overview', span: 3, form: 'stat', title: 'Cumulative gross revenue', pointer: 'fin.cumulative',
    info: "The spread traders paid, summed over every two-week period Variational has reported. Company-reported and unaudited.",
    source: 'Variational reports', file: 'financials', shape: 'periods',
    headline: { rule: 'sumAll', col: 'spreads_paid', label: 'All reported periods', unit: 'usd' },
  },
  {
    id: 'ov.t_profit', tab: 'overview', span: 3, form: 'stat', title: 'Cumulative net profit', pointer: 'fin.cumulative',
    info: "Gross revenue minus market-making costs and user rewards, summed over every two-week period Variational has reported. Company-reported and unaudited.",
    source: 'Variational reports', file: 'financials', shape: 'periods',
    headline: { rule: 'sumAll', col: 'net_profit', label: 'All reported periods', unit: 'usd' },
  },
  {
    id: 'ov.t_accounts', tab: 'overview', span: 3, form: 'stat', title: 'Accounts funded', pointer: 'ac.new',
    info: "Settlement pools that have received a first deposit since launch. One pool per account.",
    source: 'Arbitrum', file: 'accounts',
    headline: { rule: 'sumAll', col: 'first_funded', label: 'All time', unit: 'count' },
  },
  {
    id: 'ov.volume', tab: 'overview', span: 12, title: 'Trading volume', pointer: 'mk.volume',
    info: "USD notional traded per UTC day across all products, split into crypto and TradFi where the split is known. From 22 Nov 2026 it is the daily change in the Omni API's cumulative volume. Earlier days are DefiLlama's, and before 29 Dec 2025 Variational's reported two-week averages.",
    source: 'Omni API, DefiLlama, Variational reports', file: 'volume_daily',
    unit: 'usd', agg: 'sum', mark: 'bar',
    series: [S('total', 'Volume', 'blue')],
    // stacked by the file's crypto and TradFi split where it has one; other days are one grey Not split bar.
    // Until the file carries the split, the total is drawn alone.
    splitBy: { total: 'total', parts: [S('split.crypto', 'Crypto', 'blue'), S('split.tradfi', 'TradFi', 'pink')] },
    averaged: { col: 'src', source: 'report_avg', label: 'Two-week average, reported' },
    // Cumulative draws the pipeline's own running total, never a sum made in the browser
    cumulative: { col: 'cumulative', label: 'Cumulative volume', headline: 'All time' },
    toggles: ['cum'],
    headline: { rule: 'sum', col: 'total', label: '{range} total', delta: 'pct' },
  },
  {
    id: 'ov.oi', tab: 'overview', span: 6, title: 'Open interest', pointer: 'mk.oi',
    info: "One-sided is traders' long plus short positions in USD notional. OLP holds the opposite side, so the dual-sided figure Variational reports is twice this. Sources are the Omni API, DefiLlama's daily history from 4 Jan 2026 and Variational's reports before that. By market on Markets.",
    source: 'Omni API, DefiLlama, Variational reports', file: 'oi_daily',
    unit: 'usd', agg: 'last', mark: 'line',
    controls: [{
      key: 'basis', label: 'Basis', default: 'one', options: [
        { v: 'one', label: 'One-sided', set: { series: [S('total', 'Open interest, one-sided', 'blue')], basisLabel: 'one-sided' } },
        { v: 'dual', label: 'Dual-sided', needs: 'dual', set: { series: [S('dual', 'Open interest, dual-sided', 'blue')], basisLabel: 'dual-sided' } },
      ],
    }],
    headline: { rule: 'last', label: 'Latest, {basis}', delta: 'pct' },
  },
  {
    id: 'ov.tvl', tab: 'overview', span: 6, title: 'TVL, on-chain USDC',
    info: "USDC held on Arbitrum by settlement pools, the Core OLP Vault, the Protocol Treasury and the referral reward pool, at the end of each UTC day. OLP's hedging accounts on external venues are not included.",
    source: 'Arbitrum', file: 'tvl',
    unit: 'usd', agg: 'last', mark: 'line',
    series: [S('total', 'TVL', 'blue')],
    headline: { rule: 'last', col: 'total', label: 'Latest', delta: 'pct' },
  },
  {
    id: 'ov.revenue', tab: 'overview', span: 6, title: 'Gross revenue', pointer: 'fin.waterfall',
    info: "The spread traders paid in each two-week reporting period, which Variational reports as spreads paid. Company-reported and unaudited. Where it goes is on Operating results.",
    source: 'Variational reports', file: 'financials', shape: 'periods',
    unit: 'usd', agg: 'sum', mark: 'bar',
    series: [S('spreads_paid', 'Gross revenue', 'blue')],
    headline: { rule: 'last', col: 'spreads_paid', label: 'Latest two weeks', delta: 'pct' },
  },
  {
    id: 'ov.profit', tab: 'overview', span: 6, title: 'Net profit', pointer: 'fin.waterfall',
    info: "Gross revenue minus market-making costs and user rewards in each two-week reporting period. Company-reported and unaudited. The statement is on Operating results.",
    source: 'Variational reports', file: 'financials', shape: 'periods',
    unit: 'usd', agg: 'sum', mark: 'bar',
    series: [S('net_profit', 'Net profit', 'blue')],
    headline: { rule: 'last', col: 'net_profit', label: 'Latest two weeks', delta: 'pct' },
  },
  {
    id: 'ov.active', tab: 'overview', span: 6, title: 'Active accounts', pointer: 'ac.active',
    info: "Settlement pools with any USDC movement, including settlement of PnL and funding, within the 1, 7 or 30 days up to each date. One pool per account.",
    source: 'Arbitrum', file: 'accounts',
    unit: 'count', agg: 'last', mark: 'line',
    controls: [{
      key: 'window', label: 'Active within', default: '1d', options: [
        { v: '1d', label: '1 day', set: { series: [S('active_1d', 'Active, 1 day', 'blue')], windowLabel: '1 day' } },
        { v: '7d', label: '7 days', needs: 'active_7d', set: { series: [S('active_7d', 'Active, 7 days', 'blue')], windowLabel: '7 days' } },
        { v: '30d', label: '30 days', needs: 'active_30d', set: { series: [S('active_30d', 'Active, 30 days', 'blue')], windowLabel: '30 days' } },
      ],
    }],
    headline: { rule: 'last', label: 'Within {window}', delta: 'pct' },
  },
  {
    // Variational among other venues: a platform-level chart, so it lives here rather than on a product page
    id: 'mk.peers', tab: 'overview', span: 6, title: "Omni's share of on-chain open interest",
    info: "Omni's one-sided open interest divided by the sum for Omni and the six largest other on-chain derivatives venues, as reported by DefiLlama.",
    source: 'DefiLlama', file: 'peers_oi', shape: 'peers',
    unit: 'usd', agg: 'last', mark: 'line', dynamic: 'peers', keys: ['variational', 'venues'],
    toggles: ['venues'], // alternate view: venue levels, Omni blue, others grey
    headline: { rule: 'peersShare', label: 'Latest', unit: 'pct', delta: 'pp' },
  },
  {
    id: 'ov.treasury', tab: 'overview', span: 4, title: 'Protocol Treasury holdings', pointer: 'tr.holdings',
    info: "USDC held by the Protocol Treasury contract on Arbitrum at the end of each UTC day.",
    source: 'Arbitrum', file: 'treasury',
    unit: 'usd', agg: 'last', mark: 'line',
    series: [S('balance', 'Protocol Treasury', 'blue')],
    headline: { rule: 'last', col: 'balance', label: 'Latest', delta: 'pct' },
  },
  {
    id: 'ov.olp', tab: 'overview', span: 4, title: 'OLP PnL', pointer: 'olp.period',
    info: "OLP's reported profit or loss in each two-week period. OLP is the counterparty to every Omni trade.",
    source: 'Variational reports', file: 'financials', shape: 'periods',
    unit: 'usd', agg: 'last', mark: 'bar',
    series: [S('olp_pnl', 'OLP PnL', 'blue')],
    headline: { rule: 'last', col: 'olp_pnl', label: 'Latest two weeks', delta: 'pct' },
  },
  {
    id: 'ov.vault', tab: 'overview', span: 4, title: 'Core OLP Vault balance', pointer: 'olp.vault',
    info: "USDC in the Core OLP Vault contract on Arbitrum at the end of each UTC day. It is a working balance; OLP's hedging capital on external venues is not included.",
    source: 'Arbitrum', file: 'olp_chain',
    unit: 'usd', agg: 'last', mark: 'line',
    series: [S('vault_balance', 'Core OLP Vault', 'blue')],
    headline: { rule: 'last', col: 'vault_balance', label: 'Latest', delta: 'pct' },
  },

  // ------------------------------------------------------------------ Products · Omni · Markets
  // Every card on this page reads the scope chosen by the two filter rows (`markets_scope.scopes[<asset>.<inst>]`)
  // and is identical in every scope; three cards appear only in the scope they belong to.
  {
    id: 'mk.volume', tab: 'omni/markets', span: 6, scoped: true, title: 'Trading volume',
    info: "USD notional traded per UTC day in the selected markets. Splits by market come from Omni API snapshots, saved from 22 Nov 2026 and on earlier sampled dates. Before that the all-markets total is DefiLlama's daily figure, and before 29 Dec 2025 Variational's reported two-week totals averaged per day.",
    source: 'Omni API, DefiLlama, Variational reports', file: 'markets_scope', keys: ['volume'],
    // a flow: always bars, stacked by band; a day whose total is known without its split is one grey Not split bar
    unit: 'usd', agg: 'sum', mark: 'bar', bands: 'volume', valueName: 'Volume',
    // days whose figure is a reported two-week total averaged per day are drawn lighter (the scope's `avg` flags)
    averaged: { col: 'avg', code: 1, label: 'Two-week average, reported' },
    toggles: ['share', 'cum'],
    headline: { rule: 'sum', col: 'volume.total', label: '{range} total', delta: 'pct' },
  },
  {
    id: 'mk.oi', tab: 'omni/markets', span: 6, scoped: true, title: 'Open interest, one-sided',
    info: "Traders' long plus short positions at the end of each UTC day in the selected markets. OLP holds the opposite side. Splits by market come from Omni API snapshots, saved from 22 Nov 2026 and on earlier sampled dates. Before that the all-markets total is DefiLlama's daily figure.",
    source: 'Omni API, DefiLlama, Variational reports', file: 'markets_scope', keys: ['oi'],
    // stacked bars by band, with Not split where the total is known without its split; one series is a line
    unit: 'usd', agg: 'last', mark: 'bar', singleMark: 'line', bands: 'oi', valueName: 'Open interest',
    toggles: ['share'],
    headline: { rule: 'last', col: 'oi.total', label: 'Latest', delta: 'pct' },
  },
  {
    id: 'mk.ls', tab: 'omni/markets', span: 4, scoped: true, title: 'Long/short ratio',
    info: "Traders' long open interest divided by their short open interest in the selected markets, at the end of each UTC day. The 7D range shows every half-hourly snapshot.",
    source: 'Omni API', file: 'markets_scope',
    unit: 'ratio', agg: 'last', mark: 'line',
    series: [S('ls_ratio', 'Long/short ratio', 'blue')],
    // the tooltip also carries the two sides behind the ratio
    tip: [{ label: 'Long', col: 'long' }, { label: 'Short', col: 'short' }, { label: 'Long minus short', minus: ['long', 'short'] }],
    // at 7D the card draws the scope's half-hourly snapshots; with fewer than three points it draws the two sides as bars
    intraday: true, thinBars: { unit: 'usd', name: 'Open interest', series: [S('long', 'Long', 'long'), S('short', 'Short', 'short')] },
    headline: { rule: 'last', col: 'ls_ratio', label: 'Latest', unit: 'ratio', delta: 'abs' },
  },
  {
    id: 'mk.funding', tab: 'omni/markets', span: 4, scoped: true, title: 'Funding, annualised', titleBy: { swap: 'Financing, annualised' },
    info: "The last applied funding rate each day, annualised, for the largest markets in the selection. For swaps, the financing rate longs pay and the rate shorts receive. The 7D range shows every half-hourly snapshot.",
    source: 'Omni API', file: 'markets_scope', keys: ['funding'],
    unit: 'rate', agg: 'last', mark: 'line', dynamic: 'tickers', tickers: 'funding', valueName: 'Rate', intraday: true,
  },
  {
    id: 'mk.slippage', tab: 'omni/markets', span: 4, scoped: true, title: 'Slippage at $100K, quoted',
    info: "Half of Omni's indicative quoted bid-ask at $100,000 notional, in basis points from mid, daily median, for the largest markets in the selection. The 7D range shows every half-hourly snapshot.",
    source: 'Omni API', file: 'markets_scope', keys: ['slippage'],
    unit: 'bps', agg: 'last', mark: 'line', dynamic: 'tickers', tickers: 'slippage', valueName: 'Slippage', intraday: true,
  },
  {
    // in All / All the row is three cards (listed, concentration, TradFi share): `spanIn` is a width for one scope
    id: 'mk.listed', tab: 'omni/markets', span: 6, few: { span: 4 }, spanIn: { 'all.all': 4 }, scoped: true, title: 'Markets listed',
    info: "Perpetual and swap markets listed on Omni. Counts come from Variational's reports until 1 Nov 2026 and from the Omni API after.",
    source: 'Variational reports, Omni API', file: 'markets_scope',
    unit: 'count', agg: 'last', mark: 'line', step: true,
    series: [S('listed', 'Markets listed', 'blue')],
    headline: { rule: 'last', col: 'listed', label: 'Latest', delta: 'abs' },
  },
  {
    id: 'mk.concentration', tab: 'omni/markets', span: 6, few: 'hide', spanIn: { 'all.all': 4 }, scoped: true, title: 'Ten largest markets, share of volume',
    info: "The ten listings with the most volume that day, as a share of all volume.",
    source: 'Omni API', file: 'markets_scope',
    unit: 'pct', agg: 'last', mark: 'line',
    series: [S('top10_share', 'Ten largest markets', 'blue')],
    headline: { rule: 'last', col: 'top10_share', label: 'Latest', delta: 'pp' },
  },
  {
    // a trailing seven-day share needs seven consecutive days of snapshots; until then the card stays off the page
    id: 'tf.share', tab: 'omni/markets', span: 4, only: { scope: 'all.all' }, hideEmpty: true, title: 'TradFi share',
    info: "TradFi perpetuals and swaps as a share of all Omni volume and open interest, over the trailing seven days.",
    source: 'Omni API', file: 'tradfi',
    // the daily share is a weekly sawtooth (swaps are closed at weekends), so the lines are trailing seven-day shares
    unit: 'pct', agg: 'last', mark: 'line', unitLabel: '7-day average',
    series: [S('share_volume_7d', 'Volume', 'blue'), S('share_oi_7d', 'Open interest', 'teal')],
    // TradFi volume over the range divided by all volume on the same days
    headline: { rule: 'rangeShare', num: 'volume_cls', denFile: 'volume_daily', denCol: 'total', label: '{range} share of volume', unit: 'pct' },
  },
  {
    id: 'sw.pairs', tab: 'omni/markets', span: 12, few: { span: 8 }, only: { inst: 'swap' }, form: 'dots', dots: 'pairs', title: 'Perpetual and swap on the same underlying',
    info: "For each underlying Omni lists as both a perpetual and a swap, the two instruments' quoted bid-ask at $100,000 or cost of holding long, from the same snapshots. Carry is annualised funding for the perpetual and annualised long financing for the swap, both on a 365-day basis.",
    source: 'Omni API', file: 'swap_pairs', keys: ['range'],
    points: [{ label: 'Perpetual', color: 'pink', side: 'perp' }, { label: 'Swap', color: 'honey', side: 'swap' }],
    controls: [{
      key: 'metric', label: 'Metric', default: 'ba', options: [
        { v: 'ba', label: 'Bid-ask at $100K', set: { field: 'ba100k', unit: 'bps' } },
        { v: 'carry', label: 'Long carry', set: { field: 'carry', unit: 'rate' } },
      ],
    }],
  },
  {
    id: 'mk.table', tab: 'omni/markets', span: 12, form: 'table', title: 'Markets, latest',
    info: "Latest Omni API snapshot. Open interest is traders' long plus short. Volume to open interest is 24-hour volume divided by open interest. Funding is the last applied rate, annualised; swap financing shows the rate longs pay and shorts receive. Slippage is half of Omni's indicative quoted bid-ask at $100,000 notional, in basis points from mid.",
    source: 'Omni API', file: 'markets_latest', keys: ['rows'],
  },
  {
    // the market page, `#/<page>/<TICKER>`: not a card on the grid, opened from a row of the table
    id: 'mk.market', tab: 'omni/markets', span: 12, form: 'market', page: true, title: 'Market history',
    info: "Daily values from Omni API snapshots, UTC. Volume is the trailing 24 hours read at 00:00. Slippage is half of Omni's indicative quoted bid-ask at each size, in basis points from mid, daily median. History begins 1 Nov 2026.",
    source: 'Omni API', file: 'market/<TICKER>', keys: ['*'],
    // the same five as the Markets page, in the same order; each is a card with the latest value as its headline
    panels: [
      { key: 'volume', title: 'Trading volume, trailing 24h', unit: 'usd', agg: 'last', mark: 'bar', series: [S('vol24h', 'Volume, trailing 24h', 'blue')],
        info: 'Daily values from Omni API snapshots, UTC. Volume is the trailing 24 hours read at 00:00. History begins 1 Nov 2026.',
        headline: { rule: 'last', col: 'vol24h', label: '{date}' } },
      { key: 'oi', title: 'Open interest, long and short', unit: 'usd', agg: 'last', mark: 'area', mirror: true, series: [S('oi_long', 'Long', 'long'), S('oi_short', 'Short', 'short', { neg: true })],
        info: 'Daily values from Omni API snapshots, UTC. History begins 1 Nov 2026.',
        // the file's one-sided total; until it carries one, the two sides are added here
        headline: { rule: 'last', col: 'oi', or: 'lastStack', label: 'One-sided, {date}' } },
      { key: 'ls', title: 'Long/short ratio', unit: 'ratio', agg: 'last', mark: 'line', series: [S('ls_ratio', 'Long/short ratio', 'blue')], tip: [{ label: 'Long', col: 'oi_long' }, { label: 'Short', col: 'oi_short' }, { label: 'Long minus short', minus: ['oi_long', 'oi_short'] }],
        info: 'Daily values from Omni API snapshots, UTC. History begins 1 Nov 2026.',
        headline: { rule: 'last', col: 'ls_ratio', label: '{date}' } },
      { key: 'funding', title: 'Funding, annualised', only: 'perp', unit: 'rate', agg: 'last', mark: 'line', series: [S('funding_ann', 'Funding', 'blue')],
        info: 'Daily values from Omni API snapshots, UTC. History begins 1 Nov 2026.',
        headline: { rule: 'last', col: 'funding_ann', label: '{date}' } },
      { key: 'financing', title: 'Financing, annualised', only: 'swap', unit: 'rate', agg: 'last', mark: 'line', series: [S('fin_long_ann', 'Longs pay', 'blue'), S('fin_short_ann', 'Shorts receive', 'blue', { faint: true })],
        info: 'Daily values from Omni API snapshots, UTC. History begins 1 Nov 2026.',
        headline: { rule: 'last', col: 'fin_long_ann', label: 'Longs pay, {date}' } },
      // slippage is one way from mid; the file carries it at each size
      { key: 'slippage', title: 'Slippage by order size, quoted', unit: 'bps', agg: 'last', mark: 'line', series: [S('slip1k_bps', '$1K', 'ramp:1'), S('slip100k_bps', '$100K', 'ramp:3'), S('slip1m_bps', '$1M', 'ramp:5')],
        info: "Daily values from Omni API snapshots, UTC. Slippage is half of Omni's indicative quoted bid-ask at each size, in basis points from mid, daily median. History begins 1 Nov 2026.",
        headline: { rule: 'last', col: 'slip100k_bps', label: '$100K, {date}' } },
    ],
  },

  // ------------------------------------------------------------------ Products · Omni · Execution
  {
    id: 'ex.venue', tab: 'omni/execution', span: 12, form: 'dots', dots: 'venue', title: 'Round-trip cost, Omni and other on-chain venues',
    info: "Cost in basis points of buying and selling the stated size at once. Omni is its indicative quoted bid-ask, with no trading fee; Hyperliquid and Lighter are order-book cost at the same moment, plus each venue's base taker fee each way when fees are included. TradFi rows compare Omni's swap with each venue's perpetual (trade[XYZ] on Hyperliquid). Samples are paired within 45 seconds; figures are medians over the range.",
    source: 'Omni API and venue order books', file: 'execution_venue', unit: 'bps', keys: ['range'],
    controls: [
      { key: 'size', label: 'Size', default: '1m', options: [{ v: '100k', label: '$100K' }, { v: '1m', label: '$1M' }] },
      { key: 'fees', label: 'Fees', default: '1', options: [{ v: '1', label: 'Included', needs: 'fee' }, { v: '0', label: 'Excluded' }] },
    ],
    headline: { rule: 'cheapest', label: 'markets cheapest on Omni', unit: 'count' },
  },
  {
    id: 'ex.depth', tab: 'omni/execution', span: 12, height: 'sm', title: 'Markets quoted at $1M',
    info: "Listings with a two-sided Omni quote at $1,000,000 notional in at least one snapshot that UTC day.",
    source: 'Omni API', file: 'quote_depth',
    unit: 'count', agg: 'last', mark: 'line', scale: true,
    series: [S('quoted_1m.total', 'Markets quoted at $1M', 'blue')],
    headline: { rule: 'last', col: 'quoted_1m.total', label: 'Latest', delta: 'abs' },
  },

  // ------------------------------------------------------------------ Products · Omni · Accounts
  {
    id: 'ac.active', tab: 'omni/accounts', span: 6, title: 'Active accounts',
    info: "Settlement pools with any USDC movement, including settlement of PnL and funding, within the 1, 7 or 30 days up to each date. One pool per account.",
    source: 'Arbitrum', file: 'accounts',
    unit: 'count', agg: 'last', mark: 'line',
    controls: [{
      key: 'window', label: 'Active within', default: '1d', options: [
        { v: '1d', label: '1 day', set: { series: [S('active_1d', 'Active, 1 day', 'blue')], windowLabel: '1 day' } },
        { v: '7d', label: '7 days', needs: 'active_7d', set: { series: [S('active_7d', 'Active, 7 days', 'blue')], windowLabel: '7 days' } },
        { v: '30d', label: '30 days', needs: 'active_30d', set: { series: [S('active_30d', 'Active, 30 days', 'blue')], windowLabel: '30 days' } },
      ],
    }],
    headline: { rule: 'last', label: 'Within {window}', delta: 'pct' },
  },
  {
    id: 'ac.new', tab: 'omni/accounts', span: 6, title: 'Accounts first funded',
    info: "Settlement pools receiving their first user deposit, per UTC day. Every Omni account has its own pool on Arbitrum.",
    source: 'Arbitrum', file: 'accounts',
    unit: 'count', agg: 'sum', mark: 'bar',
    series: [S('first_funded', 'Accounts first funded', 'blue')],
    toggles: ['cum'],
    headline: { rule: 'sum', col: 'first_funded', label: '{range} total', delta: 'pct' },
  },
  {
    id: 'ac.volper', tab: 'omni/accounts', span: 6, title: 'Volume per active account, monthly',
    info: "Volume traded in the calendar month divided by settlement pools active in the 30 days to month end.",
    source: 'Arbitrum, Omni API', file: 'accounts', shape: 'monthly', keyPrefix: 'monthly.',
    unit: 'usd', agg: 'last', mark: 'bar',
    series: [S('vol_per_active', 'Volume per active account', 'blue')],
    headline: { rule: 'last', col: 'vol_per_active', label: 'Latest month', delta: 'pct' },
  },
  {
    id: 'ac.states', tab: 'omni/accounts', span: 6, title: 'Settlement pools, by state',
    info: "Every settlement pool ever created, by whether it holds at least $1 now, has held USDC and been emptied, or has never received any.",
    source: 'Arbitrum', file: 'accounts',
    unit: 'count', agg: 'last', mark: 'area', stack: true, valueName: 'Pools',
    series: [S('states.holding', 'Holding a balance', 'blue'), S('states.emptied', 'Emptied', 'violet'), S('states.never_funded', 'Never funded', 'grey')],
    toggles: ['share'],
    headline: { rule: 'last', col: 'states.holding', label: 'Holding a balance', delta: 'pct' },
  },
  {
    id: 'ac.cohorts', tab: 'omni/accounts', span: 12, form: 'heatmap', title: 'Accounts still active, by month first funded',
    info: "Of the accounts first funded in each month, the share with any USDC movement in each later month.",
    source: 'Arbitrum', file: 'cohorts', keys: ['cohorts'],
  },

  // ------------------------------------------------------------------ Products · Omni · Deposits
  {
    id: 'dp.flows', tab: 'omni/deposits', span: 12, title: 'Deposits and withdrawals',
    info: "USDC deposited into and withdrawn from settlement pools by users, per UTC day. Settlement with OLP is excluded. New accounts are those first funded that day.",
    source: 'Arbitrum', file: 'accounts',
    unit: 'usd', agg: 'sum', mark: 'bar', mirror: true,
    series: [S('deposits', 'Deposits', 'blue'), S('withdrawals', 'Withdrawals', 'honey', { neg: true })],
    net: { plus: 'deposits', minus: 'withdrawals', label: 'Net deposits' },
    controls: [{
      key: 'split', label: 'Split', options: [
        { v: 'none', label: 'None' },
        { v: 'cohort', label: 'New and returning', needs: 'net_new', set: { series: [S('net_new', 'New accounts', 'teal'), S('net_returning', 'Returning accounts', 'blue')], stack: true, mirror: false, net: null, totalLabel: 'Net deposits', thin: false } },
      ],
    }],
    toggles: ['cum'],
    headline: { rule: 'net', label: '{range} net', delta: 'abs' },
  },
  {
    id: 'dp.balances', tab: 'omni/deposits', span: 6, title: 'Settlement pool balances, on-chain USDC',
    info: "USDC held in all settlement pools at the end of each UTC day. It moves with deposits, withdrawals and settlement with OLP.",
    source: 'Arbitrum', file: 'accounts',
    unit: 'usd', agg: 'last', mark: 'line',
    series: [S('pool_balance', 'Settlement pool balances', 'blue')],
    headline: { rule: 'last', col: 'pool_balance', label: 'Latest', delta: 'pct' },
  },
  {
    id: 'dp.tiers', tab: 'omni/deposits', span: 6, form: 'hbar', title: 'Settlement pool balances, by pool size',
    info: "Settlement pools grouped by current USDC balance. The share is the 100 largest pools' balance divided by all pools' balance.",
    source: 'Arbitrum', file: 'pools_snapshot', keys: ['tiers'], list: 'tiers', category: 'Pool size',
    controls: [{
      key: 'measure', label: 'Measure', default: 'balance', options: [
        { v: 'balance', label: 'Balance', set: { field: 'balance', unit: 'usd', name: 'Balance', other: { field: 'pools', unit: 'count', name: 'Pools' } } },
        { v: 'pools', label: 'Pools', set: { field: 'pools', unit: 'count', name: 'Pools', other: { field: 'balance', unit: 'usd', name: 'Balance' } } },
      ],
    }],
    headline: { rule: 'value', path: 'top_share.100', label: 'Top 100 share', unit: 'pct' },
  },
  {
    id: 'dp.top', tab: 'omni/deposits', span: 12, form: 'rows', title: 'Largest settlement pools',
    info: "The 100 settlement pools with the largest current USDC balance. Each pool belongs to one account.",
    source: 'Arbitrum', file: 'pools_snapshot', keys: ['top'], list: 'top', pageSize: 10,
    columns: [
      { label: 'Rank', kind: 'rank' },
      { label: 'Pool', field: 'pool', kind: 'address' },
      { label: 'Balance', field: 'balance', unit: 'usd', num: true },
      { label: 'Share', field: 'share', unit: 'pct', num: true },
      { label: 'First funded', field: 'first_funded', kind: 'date', num: true },
    ],
  },

  // ------------------------------------------------------------------ Products · Omni · OLP
  // three stat tiles, one figure each (a card that is a bare figure is always a tile, never a strip)
  {
    id: 'olp.pnl', tab: 'omni/olp', span: 3, form: 'stat', title: 'OLP PnL, latest two weeks',
    info: "OLP's profit or loss in the latest two-week period, as stated in Variational's biweekly updates.",
    source: 'Variational reports', file: 'financials', keys: ['olp_summary.latest'],
    figure: { path: 'olp_summary.latest', unit: 'usd', label: 'Reported' },
  },
  {
    id: 'olp.lifetime', tab: 'omni/olp', span: 3, form: 'stat', title: 'Lifetime OLP PnL, reported',
    info: "OLP's cumulative profit or loss, as stated in Variational's latest biweekly update.",
    source: 'Variational reports', file: 'financials', keys: ['olp_summary.lifetime'],
    figure: { path: 'olp_summary.lifetime', unit: 'usd', label: 'As of {date}', date: 'olp_summary.lifetime_as_of' },
  },
  {
    id: 'olp.periods', tab: 'omni/olp', span: 3, form: 'stat', title: 'Periods in profit',
    info: "The number of reported two-week periods in which OLP made a profit, as stated in Variational's biweekly updates.",
    source: 'Variational reports', file: 'financials', keys: ['olp_summary.positive'],
    figure: { path: 'olp_summary.positive', of: 'olp_summary.periods', label: 'Two-week periods' },
  },
  {
    id: 'olp.offset', tab: 'omni/olp', span: 3, form: 'stat', title: 'Offsetting open interest',
    info: "In each market traders' long and short positions partly offset, which reduces what OLP must hedge. The smaller side summed across markets, divided by the larger side summed across markets.",
    source: 'Omni API', file: 'olp_market',
    headline: { rule: 'last', col: 'matched_share', label: '{date}', unit: 'pct' },
  },
  {
    id: 'olp.period', tab: 'omni/olp', span: 6, title: 'OLP PnL, per two-week period',
    info: "OLP's reported profit or loss in each two-week period, in USD or divided by the volume traded in that period.",
    source: 'Variational reports', file: 'financials', shape: 'periods',
    agg: 'last', mark: 'bar',
    controls: [{
      key: 'unit', label: 'Unit', default: 'usd', options: [
        { v: 'usd', label: 'USD', set: { unit: 'usd', series: [S('olp_pnl', 'OLP PnL', 'blue')] } },
        { v: 'bps', label: 'Per dollar of volume', needs: 'periods', set: { unit: 'bps', series: [S('olp_bps', 'OLP PnL per dollar of volume', 'blue')] } },
      ],
    }],
  },
  {
    id: 'olp.cum', tab: 'omni/olp', span: 6, title: 'OLP PnL, cumulative, reported',
    info: "Lifetime OLP PnL as stated in each of Variational's biweekly updates.",
    source: 'Variational reports', file: 'financials', shape: 'periods',
    unit: 'usd', agg: 'last', mark: 'line',
    series: [S('lifetime_olp_pnl', 'Lifetime OLP PnL', 'blue')],
  },
  {
    id: 'olp.vault', tab: 'omni/olp', span: 12, height: 'xs', title: 'Core OLP Vault balance, on-chain USDC',
    info: "USDC in the Core OLP Vault contract on Arbitrum at the end of each UTC day. It is a working balance; OLP's hedging capital on external venues is not included.",
    source: 'Arbitrum', file: 'olp_chain',
    unit: 'usd', agg: 'last', mark: 'line',
    series: [S('vault_balance', 'Core OLP Vault', 'blue')],
    headline: { rule: 'last', col: 'vault_balance', label: 'Latest', delta: 'pct' },
  },

  // ------------------------------------------------------------------ Financials · Operating results
  // a row of stat tiles: sums of the report periods that end inside the selected range, each with its
  // change against the same number of preceding periods. A figure in a tile is not repeated as a chart headline.
  {
    id: 'fin.t_spreads', tab: 'financials/results', span: 3, form: 'stat', title: 'Gross revenue',
    info: "The spread traders paid, which Variational reports as spreads paid. Sum of the biweekly updates whose period ends inside the selected range. Company-reported and unaudited.",
    source: 'Variational reports', file: 'financials', shape: 'periods',
    headline: { rule: 'periodSum', col: 'spreads_paid', label: '{range} total', unit: 'usd', delta: 'pct' },
  },
  {
    id: 'fin.t_netrev', tab: 'financials/results', span: 3, form: 'stat', title: 'Net revenue',
    info: "Sum of Variational's biweekly updates whose period ends inside the selected range. Company-reported and unaudited.",
    source: 'Variational reports', file: 'financials', shape: 'periods',
    headline: { rule: 'periodSum', col: 'net_revenue', label: '{range} total', unit: 'usd', delta: 'pct' },
  },
  {
    id: 'fin.t_rewards', tab: 'financials/results', span: 3, form: 'stat', title: 'Rewards',
    info: "Sum of Variational's biweekly updates whose period ends inside the selected range. Company-reported and unaudited. Referral rewards and, until March 2026, loss refunds paid to users.",
    source: 'Variational reports', file: 'financials', shape: 'periods',
    headline: { rule: 'periodSum', col: 'rewards', label: '{range} total', unit: 'usd', delta: 'pct' },
  },
  {
    id: 'fin.t_profit', tab: 'financials/results', span: 3, form: 'stat', title: 'Net profit',
    info: "Sum of Variational's biweekly updates whose period ends inside the selected range. Company-reported and unaudited.",
    source: 'Variational reports', file: 'financials', shape: 'periods',
    headline: { rule: 'periodSum', col: 'net_profit', label: '{range} total', unit: 'usd', delta: 'pct' },
  },
  {
    id: 'fin.waterfall', tab: 'financials/results', span: 12, form: 'waterfall', title: 'Gross revenue to net profit',
    info: "From gross revenue, the spread traders paid, subtract market-making costs to reach net revenue and user rewards to reach net profit, which is split between the Protocol Treasury and OLP. Market-making costs are gross revenue minus net revenue. Company-reported and unaudited.",
    source: 'Variational reports', file: 'financials', unit: 'usd',
    // Period: the latest two weeks, the report periods ending inside the selected range, or any one report
    controls: [{ key: 'period', label: 'Period', default: 'latest', periods: true, options: [{ v: 'latest', label: 'Latest two weeks' }, { v: 'range', label: 'Selected range' }] }],
    steps: [
      { label: 'Gross revenue', field: 'spreads_paid', kind: 'total' },
      { label: 'Market-making costs', field: 'mm_costs', kind: 'minus' },
      { label: 'Net revenue', field: 'net_revenue', kind: 'total' },
      { label: 'Rewards', field: 'rewards', kind: 'minus' },
      { label: 'Net profit', field: 'net_profit', kind: 'total' },
      { label: 'Protocol Treasury', field: 'treasury_inflow', kind: 'part', color: 'blue' },
      { label: 'OLP', field: 'olp_pnl', kind: 'part', color: 'teal' },
    ],
  },
  {
    id: 'fin.cumulative', tab: 'financials/results', span: 6, title: 'Cumulative results',
    info: "Running totals of the reported two-week figures since the update of 6 Mar 2026.",
    source: 'Variational reports', file: 'financials', shape: 'ends:cumulative',
    unit: 'usd', agg: 'last', mark: 'line',
    series: [S('spreads_paid', 'Gross revenue', 'blue'), S('net_revenue', 'Net revenue', 'teal'), S('net_profit', 'Net profit', 'honey')],
  },
  {
    id: 'fin.statement', tab: 'financials/results', span: 6, title: 'Gross revenue, by destination',
    info: "Where each two-week period's gross revenue went: Protocol Treasury inflow, user rewards, market-making costs and OLP PnL. Treasury plus OLP is net profit.",
    source: 'Variational reports', file: 'financials', shape: 'periods',
    unit: 'usd', agg: 'sum', mark: 'bar', stack: true, totalLabel: 'Gross revenue',
    series: [
      S('treasury_inflow', 'Protocol Treasury', 'blue'), S('rewards', 'Rewards', 'violet'),
      S('mm_costs', 'Market-making costs', 'grey'), S('olp_pnl', 'OLP', 'teal'),
    ],
    toggles: ['share'], defaults: { share: true },
  },
  {
    id: 'fin.margins', tab: 'financials/results', span: 6, title: 'Net revenue and net profit, share of gross revenue',
    info: "Net revenue and net profit as a share of gross revenue in each two-week period.",
    source: 'Variational reports', file: 'financials', shape: 'periods',
    unit: 'pct', agg: 'last', mark: 'line',
    series: [S('net_revenue_margin', 'Net revenue', 'teal'), S('net_profit_margin', 'Net profit', 'honey')],
  },
  {
    id: 'fin.perdollar', tab: 'financials/results', span: 6, title: 'Gross revenue per dollar of volume',
    info: "Reported gross revenue, net revenue and net profit divided by the volume traded in the same two weeks, in basis points.",
    source: 'Variational reports', file: 'financials', shape: 'periods',
    unit: 'bps', agg: 'last', mark: 'line',
    series: [S('spreads_bps', 'Gross revenue', 'blue'), S('net_revenue_bps', 'Net revenue', 'teal'), S('net_profit_bps', 'Net profit', 'honey')],
  },
  {
    id: 'fin.daily', tab: 'financials/results', span: 12, title: 'Gross revenue, daily, implied from treasury inflow',
    info: "Daily USDC received by the Protocol Treasury, divided by the treasury's share of gross revenue (20% since 17 Apr 2026). Over each reporting period it lands within 5% of the gross revenue Variational reports. Shown from 17 Apr 2026.",
    source: 'Arbitrum, Variational reports', file: 'spreads_daily',
    unit: 'usd', agg: 'sum', mark: 'bar',
    series: [S('implied', 'Gross revenue, implied', 'blue')],
    toggles: ['cum'],
    headline: { rule: 'mean', col: 'implied', label: '{range} daily average', delta: 'pct', noCum: true },
  },
  {
    id: 'fin.table', tab: 'financials/results', span: 12, form: 'statement', title: 'Operating results, by two-week period',
    info: "Variational's biweekly updates. `Latest \u00d7 26` annualises the latest two-week figures. Volume is the change in reported cumulative volume between updates.",
    source: 'Variational reports', file: 'financials',
    // line items as rows; `sum` rows add over periods, ratio rows divide two sums
    lines: [
      { label: 'Volume', field: 'volume', unit: 'usd' },
      { label: 'Gross revenue', field: 'spreads_paid', unit: 'usd', strong: true },
      { label: 'Market-making costs', field: 'mm_costs', unit: 'usd' },
      { label: 'Net revenue', field: 'net_revenue', unit: 'usd', strong: true },
      { label: 'Rewards', field: 'rewards', unit: 'usd' },
      { label: 'Net profit', field: 'net_profit', unit: 'usd', strong: true },
      { label: 'Protocol Treasury inflow', field: 'treasury_inflow', unit: 'usd' },
      { label: 'OLP PnL', field: 'olp_pnl', unit: 'usd' },
      { label: 'Gross revenue per dollar of volume, bps', field: 'spreads_bps', unit: 'bare', ratio: ['spreads_paid', 'volume', 1e4], rule: true },
      { label: 'Net revenue margin', field: 'net_revenue_margin', unit: 'pct', ratio: ['net_revenue', 'spreads_paid', 1] },
      { label: 'Net profit margin', field: 'net_profit_margin', unit: 'pct', ratio: ['net_profit', 'spreads_paid', 1] },
    ],
  },

  // ------------------------------------------------------------------ Financials · Protocol Treasury
  {
    id: 'tr.t_onchain', tab: 'financials/treasury', span: 3, form: 'stat', title: 'Holdings, on-chain',
    info: "USDC held by the Protocol Treasury contract on Arbitrum at the end of the latest UTC day.",
    source: 'Arbitrum', file: 'treasury',
    headline: { rule: 'last', col: 'balance', label: 'Latest', unit: 'usd', delta: 'pct' },
  },
  {
    id: 'tr.t_reported', tab: 'financials/treasury', span: 3, form: 'stat', title: 'Holdings, reported',
    info: "Protocol Treasury holdings as stated in Variational's latest biweekly update.",
    source: 'Variational reports', file: 'treasury',
    headline: { rule: 'listLast', list: 'reported', value: 'holdings', date: 'date', label: 'As of {date}', unit: 'usd' },
  },
  {
    // a range total would equal the holdings at long ranges (the treasury has never paid out), so the tile is a daily average
    id: 'tr.t_inflow', tab: 'financials/treasury', span: 3, form: 'stat', title: 'Inflow, daily average',
    info: "Mean USDC received by the Protocol Treasury per day over the selected range. The treasury collects its share of gross revenue directly from settlement pools.",
    source: 'Arbitrum', file: 'treasury',
    headline: { rule: 'mean', col: 'inflow', label: '{range} average', unit: 'usd', delta: 'pct' },
  },
  {
    id: 'tr.t_annual', tab: 'financials/treasury', span: 3, form: 'stat', title: 'Inflow, annualised',
    info: "USDC received by the Protocol Treasury over the last 30 days, scaled to a year.",
    source: 'Arbitrum', file: 'treasury',
    headline: { rule: 'annual30', col: 'inflow', label: 'Last 30 days', unit: 'usd' },
  },
  {
    id: 'tr.holdings', tab: 'financials/treasury', span: 6, title: 'Protocol Treasury holdings, on-chain USDC',
    info: "USDC held by the Protocol Treasury contract on Arbitrum at the end of each UTC day. Markers show the holdings Variational reported.",
    source: 'Arbitrum, Variational reports', file: 'treasury',
    unit: 'usd', agg: 'last', mark: 'line',
    series: [S('balance', 'On-chain', 'blue')],
    markers: { list: 'reported', date: 'date', value: 'holdings', label: 'Reported', color: 'tint' },
  },
  {
    id: 'tr.inflow', tab: 'financials/treasury', span: 6, title: 'Protocol Treasury inflow, on-chain USDC',
    info: "USDC received by the Protocol Treasury per UTC day. The treasury collects its share of gross revenue directly from settlement pools and has made no payments out.",
    source: 'Arbitrum', file: 'treasury',
    unit: 'usd', agg: 'sum', mark: 'bar',
    series: [S('inflow', 'Inflow', 'blue')],
    toggles: ['cum'],
  },
  {
    id: 'tr.recon', tab: 'financials/treasury', span: 6, title: 'Protocol Treasury inflow, reported and on-chain',
    info: "Protocol Treasury inflow as Variational reports it for each two-week period, beside the USDC the treasury contract received on-chain over the same days.",
    source: 'Variational reports, Arbitrum', file: 'financials', shape: 'periods',
    unit: 'usd', agg: 'sum', mark: 'bar', paired: true,
    series: [S('treasury_inflow', 'Reported', 'grey'), S('treasury_inflow_onchain', 'On-chain', 'blue')],
  },
  {
    id: 'tr.share', tab: 'financials/treasury', span: 6, title: 'Protocol Treasury share of gross revenue',
    info: "Reported Protocol Treasury inflow divided by reported gross revenue, per two-week period. Variational describes the share as subject to change.",
    source: 'Variational reports', file: 'financials', shape: 'periods',
    unit: 'pct', agg: 'last', mark: 'line', step: true,
    series: [S('treasury_share', 'Protocol Treasury share', 'blue')],
  },

  // ------------------------------------------------------------------ Token · $VAR
  {
    id: 'var.fdv', tab: 'token/var', span: 6, rows: 2, title: 'FDV one day after launch, Polymarket-implied',
    info: 'Polymarket prices for "Variational FDV above X one day after launch", read as probabilities. The median is the valuation at 50%, interpolated on a log scale between the two nearest strikes.',
    source: 'Polymarket', file: 'token', keys: ['fdv.strikes', 'fdv.history'],
    controls: [{
      key: 'view', label: 'View', default: 'strike', options: [
        { v: 'strike', label: 'By strike', set: { form: 'hbar', list: 'fdv.strikes', prefix: 'Above ', category: 'Strike', field: 'p', unit: 'pct', name: 'Probability above' } },
        { v: 'median', label: 'Median over time', needs: 'fdv.history.median', min: 7, set: { form: 'time', shape: 'fdvhist', unit: 'usd', agg: 'last', mark: 'line', scale: true, series: [S('median', 'Implied median', 'blue')] } },
      ],
    }],
    headline: { rule: 'value', path: 'fdv.median', label: 'Implied median', unit: 'usd' },
  },
  {
    id: 'var.launch', tab: 'token/var', span: 6, title: 'Token launch by date, Polymarket-implied',
    info: 'Polymarket prices for "Will Variational launch a token by" each date, read as probabilities.',
    source: 'Polymarket', file: 'token', shape: 'launch',
    unit: 'pct', agg: 'last', mark: 'line', dynamic: 'launch', keys: ['launch'],
    // until a market has seven daily readings the card shows current values as bars in one hue, not lines
    snapshotUntil: 7, snapshot: { form: 'hbar', list: 'launch', prefix: 'By ', category: 'Market', oneHue: true, field: 'p', unit: 'pct', name: 'Probability' },
    headline: { rule: 'launchP', label: 'By 31 Dec 2026', match: '31 Dec 2026', unit: 'pct', delta: 'pp' },
  },
  {
    id: 'var.alloc', tab: 'token/var', span: 6, form: 'segmented', title: '$VAR supply at genesis, by allocation',
    info: "From Variational's $VAR documentation. Genesis distribution is unlocked at launch; team and investor tokens are locked for 12 months, then vest over at least three years.",
    source: 'Variational docs', file: 'token', keys: ['allocation'],
    colors: ['blue', 'teal', 'grey'],
  },

  // ------------------------------------------------------------------ Token · Rewards and points
  {
    id: 'rw.t_referral', tab: 'token/rewards', span: 4, form: 'stat', title: 'Referral rewards paid',
    info: "USDC paid into users' settlement pools from the referral reward wallet, since the first payment. Referrers earn 5% of the spreads their referees pay.",
    source: 'Arbitrum', file: 'rewards',
    headline: { rule: 'sumAll', col: 'referral', label: 'Lifetime', unit: 'usd' },
  },
  {
    id: 'rw.t_refunds', tab: 'token/rewards', span: 4, form: 'stat', title: 'Loss refunds paid',
    info: "USDC paid into users' settlement pools from the loss refund wallet. Loss refunds ended in March 2026.",
    source: 'Arbitrum', file: 'rewards',
    headline: { rule: 'sumAll', col: 'loss_refund', label: 'Lifetime, ended Mar 2026', unit: 'usd' },
  },
  {
    id: 'rw.t_points', tab: 'token/rewards', span: 4, form: 'stat', title: 'Accounts receiving points',
    info: "Accounts that received any of the 150,000 Omni points distributed in the latest week, as announced by Variational.",
    source: 'Variational announcements', file: 'token', shape: 'points',
    headline: { rule: 'last', col: 'accounts', label: 'Week {n}', unit: 'count', delta: 'pct' },
  },
  {
    id: 'rw.paid', tab: 'token/rewards', span: 12, title: 'Rewards paid, by type',
    info: "USDC paid into users' settlement pools from the referral reward wallet and the loss refund wallet, per UTC day. Loss refunds ended in March 2026. Wallet roles are inferred from payment patterns and match the totals Variational discloses.",
    source: 'Arbitrum', file: 'rewards',
    unit: 'usd', agg: 'sum', mark: 'bar', stack: true, valueName: 'Rewards',
    series: [S('referral', 'Referral rewards', 'violet'), S('loss_refund', 'Loss refunds', 'grey')],
    toggles: ['cum'],
    headline: { rule: 'sumStack', label: '{range} total', delta: 'pct' },
  },
  {
    id: 'rw.points', tab: 'token/rewards', span: 6, title: 'Accounts receiving weekly points',
    info: "Accounts that received any of the 150,000 Omni points distributed each Friday, as announced by Variational.",
    source: 'Variational announcements', file: 'token', shape: 'points', keyPrefix: 'points.',
    unit: 'count', agg: 'last', mark: 'bar', minPoints: 12,
    series: [S('accounts', 'Accounts', 'blue')],
  },
  {
    id: 'rw.claimants', tab: 'token/rewards', span: 6, form: 'rows', title: 'Referral rewards, by claimant',
    info: "Wallets ranked by referral rewards received. Referrers earn 5% of the spreads their referees pay.",
    source: 'Arbitrum', file: 'rewards', keys: ['claimants'], list: 'claimants', pageSize: 10,
    columns: [
      { label: 'Wallet', field: 'wallet', kind: 'address' },
      { label: 'Claims', field: 'claims', unit: 'count', num: true },
      { label: 'Total', field: 'total', unit: 'usd', num: true },
      { label: 'Share', field: 'share', unit: 'pct', num: true },
    ],
    headline: { rule: 'value', path: 'top10_share', label: 'Top 10 share', unit: 'pct' },
  },
];

// One line on what each dataset holds, for the Data page. Files not listed here are described by their cards.
export const DATASETS = {
  volume_daily: 'Trading volume per UTC day, with the asset-class split and the cumulative total.',
  oi_daily: 'Open interest at the end of each UTC day, one-sided and dual-sided, with the asset-class split.',
  peers_oi: 'One-sided open interest for Omni and the six largest other on-chain venues, per day.',
  markets_latest: 'Every listing in the latest Omni API snapshot.',
  markets_scope: 'Volume, open interest, long and short, listings, funding and slippage per day, for each asset and instrument scope.',
  tradfi: 'TradFi share of volume and open interest, by asset class and by instrument, and TradFi listings.',
  swap_pairs: 'Bid-ask and carry for each underlying listed as both a perpetual and a swap.',
  quote_depth: 'Listings quoted at $1M, per day.',
  execution_venue: 'Round-trip cost on Omni and other on-chain venues, matched samples, per range and per day.',
  olp_market: 'Offsetting long and short open interest, per day.',
  olp_chain: 'Core OLP Vault balance at the end of each UTC day.',
  accounts: 'Accounts first funded, active accounts, deposits, withdrawals, pool balances and pool states, per day.',
  cohorts: 'Share of each first-funded month still active in each later month.',
  pools_snapshot: 'Settlement pools by balance tier and the 100 largest pools.',
  tvl: 'USDC held on-chain at the end of each UTC day.',
  treasury: 'Protocol Treasury holdings and inflow, per day, with reported holdings.',
  rewards: 'Referral rewards and loss refunds paid per day, and the largest claimants.',
  financials: "Variational's two-week operating results, as reported.",
  spreads_daily: 'Gross revenue per day, implied from Protocol Treasury inflow.',
  token: 'Polymarket-implied FDV and launch dates, weekly points and the supply allocation.',
  research: 'Research pieces, with publisher, date and where each is published.',
};

// an alias may carry presets (`mk.volume?asset=tradfi`): the card id is the part before the `?`
export const byId = id => CATALOG.find(c => c.id === (CARD_ALIAS[id] || id).split('?')[0]);
export const aliasQuery = id => ((CARD_ALIAS[id] || '').split('?')[1] || '');
export const cardsOf = tab => CATALOG.filter(c => c.tab === tab && !c.page);
export const pageCard = (tab, form) => CATALOG.find(c => c.tab === tab && c.page && c.form === form);

// ---------- residency ----------
// v3.1: several cards read the same reported fields in different forms (tiles, waterfall, statement,
// table). What must never happen is an identical chart or an identical printed figure in two places.
function seriesKeys(c) {
  const keys = new Set();
  const pre = c.keyPrefix || '';
  const add = list => (list || []).forEach(s => keys.add(`${pre}${s.col}`));
  add(c.series);
  if (c.cumulative) add([c.cumulative]);
  (c.keys || []).forEach(k => keys.add(`${pre}${k}`));
  (c.controls || []).forEach(ctl => ctl.options.forEach(o => add(o.set && o.set.series)));
  (c.panels || []).forEach(p => add(p.series));
  (c.steps || c.lines || []).forEach(x => keys.add(x.field));
  return keys;
}

/** No identical chart and no identical headline figure may appear in two catalog entries. */
export function assertResidency() {
  const problems = [];
  const ids = new Set(), charts = new Map(), heads = new Map();
  for (const c of CATALOG) {
    if (ids.has(c.id)) problems.push(`duplicate card id ${c.id}`);
    ids.add(c.id);
    if (!PAGES.some(p => p.id === c.tab)) problems.push(`card ${c.id} is on an unknown page ${c.tab}`);
    if (c.pointer && !CATALOG.some(x => x.id === c.pointer && !x.pointer)) problems.push(`card ${c.id} points to an unknown card ${c.pointer}`);
    if (c.pointer && c.tab !== HOME) problems.push(`card ${c.id} is a pointer outside the summary page`);
    const keys = [...seriesKeys(c)].sort().join('+');
    if (keys && c.form !== 'stat' && !c.pointer) {
      // the same series from the same file in the same form is the same chart
      const sig = `${c.file}|${c.form || 'time'}|${c.shape || ''}|${c.mark || ''}|${keys}`;
      if (charts.has(sig)) problems.push(`${charts.get(sig)} and ${c.id} draw the same chart (${sig})`);
      charts.set(sig, c.id);
    }
    const hd = c.headline, fig = c.figure;
    if ((hd || fig) && !c.pointer) {
      // a headline is identified by what it reads and how: its file, column or path or part, and rule
      const what = fig ? fig.path : hd.col || hd.path || hd.value || (hd.of || []).join('+') || keys || hd.rule;
      const h = `${c.file}:${c.keyPrefix || ''}${what}:${fig ? 'value' : hd.rule}`;
      if (heads.has(h)) problems.push(`headline ${h} is printed on ${heads.get(h)} and ${c.id}`);
      heads.set(h, c.id);
    }
  }
  if (problems.length) {
    const msg = 'Residency violated:\n' + problems.join('\n');
    console.error(msg);
    if (['localhost', '127.0.0.1'].includes(location.hostname)) throw new Error(msg);
  }
  return problems;
}
