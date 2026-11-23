# Variational Monitor: data branch

The pipeline's store. Written only by GitHub Actions (`capture.yml` every 30 minutes, `publish.yml` every hour);
do not edit by hand. Code lives on `main`; this branch shares no history with it.

- `snapshots/` raw Omni stats API responses, one per half hour, kept 14 days (the API has no history)
- `snapshots_seed/` snapshots saved by hand on 2, 4 and 11 Nov 2026, and five Internet Archive captures (22 Jan, 13, 17 and 25 Sep, 9 Oct 2026) converted to the same form; the archived originals are in `wayback/`
- `exec/` matched Omni and Hyperliquid samples per UTC day; `capture/` one log line per tick
- `days/` sealed UTC days, `platform.jsonl` one line per snapshot, `reports.json`
- `coingecko/`, `defillama/`, `docs/`, `polymarket/`, `telegram/` source caches
- `onchain/aggregates/` daily on-chain aggregates; `onchain/state/` compact indexer state, rewritten weekly

History is squashed to one commit about once a week, so old snapshots do not accumulate. See `ci/datastore.py` on `main`.
