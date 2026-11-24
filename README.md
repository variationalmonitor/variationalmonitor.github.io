# Variational Monitor

[variationalmonitor.github.io](https://variationalmonitor.github.io/) is an independent analytics dashboard for
[Variational](https://variational.io/): its trading venue Omni, its liquidity provider OLP, its reported operating
results, its Protocol Treasury and its token.

by Ponyo. Independent analytics, not affiliated with Variational.

## Pages

| Page | What it shows |
|---|---|
| Overview | Volume, open interest, on-chain TVL, gross revenue, net profit, accounts, and Omni's share of on-chain open interest |
| Omni, Markets | Volume, open interest, long/short, funding and quoted slippage for all markets or one asset class, the full market table, and a page per market |
| Omni, Execution | Round-trip cost on Omni against Hyperliquid and Lighter at $100K and $1M, and how many markets Omni quotes at $1M |
| Omni, Accounts | Active accounts, first-time funding, volume per account, and retention by the month an account was first funded |
| Omni, Deposits | Deposits and withdrawals, settlement pool balances by size, and the largest pools |
| Omni, OLP | OLP's profit and loss per two-week period, how much of traders' positions offset each other, and the Core OLP Vault balance |
| Financials, Operating results | Gross revenue through to net profit for every two-week report, margins, and revenue per dollar of volume |
| Financials, Protocol Treasury | Holdings and inflow on-chain, beside what Variational reports |
| Token, $VAR | Polymarket-implied launch date and valuation, and the genesis allocation |
| Token, Rewards and points | Referral rewards and loss refunds paid on-chain, and accounts receiving points |
| Research Library | Published research on Variational |
| Data | Every dataset behind the site as JSON and CSV, with the definitions |

## Sources

| Source | Used for |
|---|---|
| [Omni stats API](https://docs.variational.io/technical-documentation/api) | Cumulative volume, open interest, funding and quotes for every market. The API has no history, so a reading is saved every half hour (since 22 Nov 2026) |
| [DefiLlama](https://defillama.com/protocol/variational) | Daily volume from 29 Dec 2025 and daily open interest from 4 Jan 2026, up to the first saved readings; open interest of other on-chain venues |
| [Variational's biweekly updates](https://t.me/s/variational_io) | Gross revenue, net revenue, rewards, net profit, Protocol Treasury inflow and OLP profit and loss; also volume and open interest before DefiLlama's history begins. Company-reported and unaudited |
| Arbitrum | USDC in settlement pools, the Core OLP Vault, the Protocol Treasury and the referral reward pool, from an index of the published [contracts](https://docs.variational.io/technical-documentation/mainnet-contracts) |
| Hyperliquid and Lighter order books | The cost comparison on Execution |
| [Polymarket](https://polymarket.com/) | Implied token launch date and valuation |
| [Variational docs](https://docs.variational.io/) | Token allocation and contract addresses |

## Definitions

- **Open interest** is one-sided: traders' long plus short positions. OLP holds the other side, so the dual-sided
  figure Variational reports is twice this.
- **Gross revenue** is the spread traders paid in a two-week reporting period. Variational reports it as spreads paid.
- **An account** is a settlement pool on Arbitrum. There is one pool per account.
- **TVL** is USDC held on Arbitrum by settlement pools, the Core OLP Vault, the Protocol Treasury and the referral
  reward pool. OLP's hedging accounts on other venues are not included.
- **Round-trip cost** is the cost of buying and selling a size at once. For Omni it is the quoted bid-ask, which
  carries no trading fee. For Hyperliquid and Lighter it is the order-book cost at the same moment plus each venue's
  base taker fee each way. Samples are paired within 45 seconds and shown as medians.
- **Daily gross revenue** is implied: USDC received by the Protocol Treasury each day, divided by the treasury's
  share of gross revenue (20% since 17 Apr 2026). Over each reporting period it lands within 5% of the reported figure.

Every card carries its own definition, source and time in its info popover.

## How it stays current

- A reading of the Omni API every half hour, and a rebuild of the site every hour.
- On-chain figures cover complete UTC days and update after each day closes.
- Variational's channel is read every hour, so a new biweekly update appears within about an hour of its post.
- Every build passes a gate before it is published: totals must equal their parts, figures must tie back to the raw
  sources, published history must not change, and every page must render at desktop and phone widths. When a check
  fails, the last good build stays up.

## Repository

| Path | What it is |
|---|---|
| `site/` | The page: plain ES modules and ECharts, with every card described in `site/js/catalog.js` |
| `pipeline/` | Builds the data files from the stored readings and sources (Python standard library) |
| `qa/` | The gate that every build must pass |
| `ci/`, `.github/workflows/` | The jobs: take a reading, build, check, publish |
| `timer/` | The timer that starts each reading |
| branch `data` | The store: raw readings, sealed days and source caches, written only by the jobs |
| `AGENTS.md` | How the system runs and the rules for changing it |

`public/`, `tools/`, `config/` and `data/` belong to the first version of the site and are not used.

## Run it locally

Python 3.9 or later and Node 18 or later.

    git clone https://github.com/variationalmonitor/variationalmonitor.github.io.git
    cd variationalmonitor.github.io
    git clone --depth 1 --branch data https://github.com/variationalmonitor/variationalmonitor.github.io.git .store
    . ci/env.sh .store
    python3 pipeline/build.py --offline
    python3 -m http.server 8900 --directory site

Then open http://localhost:8900. The build reads the stored readings only and calls no outside service.

Tests:

    python3 -m unittest discover -s tests -p "test_*.py"
    node --test tests/*.test.mjs
