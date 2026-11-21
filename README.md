# Variational Monitor

[variationalmonitor.github.io](https://variationalmonitor.github.io/) is an independent dashboard for Variational. It covers Omni markets, bi-weekly financial reports, Protocol Treasury, the Omni Liquidity Provider, VAR, Variational Pro and research by Jae (Ponyo).

## Run locally

The site in `public/` is static. Start it from the project directory with `python3 -m http.server 8000 --directory public` and open `http://localhost:8000`. Collectors use the Python standard library. Run their checks with `python3 -m unittest discover -s tests`.

Run the chart and calendar-window tests with `node --test tests/*.test.mjs`. All historical pages share 7D, 30D, 90D, 180D, 1Y and All controls. The selected range travels with the link. Tables and CSVs use the displayed observations. Cumulative report charts preserve the amounts earned before the selected window.

## Sources and accounting

- The [Omni API](https://docs.variational.io/technical-documentation/api) reports lifetime trading volume, rolling 24-hour volume, USDC balances and market statistics. Market rows carry the observation time. Public bid and ask prices are indicative quotes.
- [Variational announcements](https://t.me/s/variational_io) supply the financial reports. Each record links to its source. Covered-period totals sum dated income and allocation lines. Stocks such as Protocol Treasury Holdings and Lifetime OLP PnL retain their individual observation dates. A missing field stays missing. Historical source discrepancies remain attached to the corresponding report.
- The Arbitrum collector reads native USDC at the published [Protocol Treasury and Core OLP Vault addresses](https://docs.variational.io/technical-documentation/mainnet-contracts) at a common safe block. These balances describe the two wallets. External hedges and liabilities require other disclosures.
- Historical wallet charts store the block hash, timestamp and native USDC balance for each observed address. Historical UTC closes verify the block on each side of midnight. The ordinary daily job appends the safe-block observation it already collected.
- Cumulative volume and dual-sided open interest use Variational’s bi-weekly disclosures in USD. If a selected period has no disclosure, the charts can use actual Omni API observations of the same counter in USDC. The chart identifies the unit and source for its entire series. Between-report volume uses the change in successive disclosed counters and exports both publication dates.
- [CoinGecko](https://www.coingecko.com/en/coins/variational) provides VAR listing status. A verified contract registry and a matching listing are required before market figures appear. [Variational's allocation policy](https://docs.variational.io/token/usdvar) is dated independently.
- Comparable quote observations use a fixed size against the [Hyperliquid info endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint), book depth and the published fee assumption. A match requires adequate depth and quotes within the specified age and time difference.

## Market history and execution

- Omni charts use saved API observations within the selected UTC range. The horizontal scale follows their actual dates. Per-market volume is a rolling 24-hour amount. The API does not supply daily trade histories, hourly quote histories or per-market lifetime volume.
- The TradFi view joins an independently sourced exact-identity catalogue to the public Omni feed. Unmatched markets stay unclassified. The TradFi share shown is a lower bound using the verified subset. The daily job rebuilds group histories from saved partitions.
- Execution shows observed two-sided Omni quote availability at three sizes. The crypto comparison replays public-source captures with independently verified order-book coverage, a maximum 45-second source-time difference and 4.5 bps per side in Hyperliquid base-tier taker fees. It compares the median paired difference in indicative costs.
- Rewards are recorded from official updates. Cumulative claims retain their reported precision. The signed period rewards are separate. Treasury wallet charts use verified USDC balances and differences between recorded blocks.

## Updates

GitHub Actions schedules ordinary data collection at 19:15 UTC daily. A separate check within the same workflow scans the official announcement channel every five minutes. New or edited financial disclosures can publish between daily updates. Rewards refresh after an updated financial disclosure or during the daily job. Queue delays at GitHub and cross-posting times depend on the respective sources. See the [workflow history](https://github.com/variationalmonitor/variationalmonitor.github.io/actions) for actual run times.

The workflow deploys the contents of `public/` through GitHub Pages after validation. Failed inputs retain the last valid observations and their timestamps. No personal social-media credentials are required. An optional CoinGecko Demo key may be configured as a repository secret called `COINGECKO_DEMO_API_KEY`.

## Research

The article catalogue in `config/catalog.json` supports scheduled and published entries. Add the public version and its canonical URL after the publisher releases an article. A scheduled date alone does not publish a draft. Refresh the public catalogue with `python3 tools/supplement.py`.

The daily job runs `python3 tools/research_baselines.py --data-dir public/data` after collecting observations. It preserves the available indicators for each article’s target date in Korea. Each indicator retains its source timestamp or report coverage. The article view compares subsequent observations with the recorded baseline. Missing observations stay blank. The first capture for a given article and date is retained on later runs.
