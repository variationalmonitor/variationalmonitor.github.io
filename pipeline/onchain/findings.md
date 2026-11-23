# On-chain index — findings

Established 22 Nov 2026 by sampling transactions and reconciling the ledger against Multicall balances. Selectors match the verified Oracle source on Sourcify. Evidence hashes are abbreviated (first and last characters). Labels marked *inferred* are flow patterns, not on-chain tags.

## Addresses (Arbitrum One)

| Role | Address |
|---|---|
| Native USDC | `0xaf88d065e77c8cc2239327c5edb3a432268e5831` |
| Settlement Pool Factory (deployed block 300,692,000, 30 Jan 2025; the only factory) | `0x0f820b9afc270d658a9fd7d16b1bdc45b70f074c` |
| Pool implementation | `0x8db6c8b7a085c3839e93eb8dad45b93fb1ef5836` |
| Oracle (entry point for every flow) | `0x84be56470d45b7f6629a66a219a38681f6ba6172` |
| Core OLP Vault | `0x74bbbb0e7f0bad6938509dd4b556a39a4db1f2cd` |
| Protocol Treasury | `0x5e91b40467fb8902c46a7b6cb90482363188d645` |
| Settlement router (OLP → many pools, from 7 Nov 2025; unverified contract) | `0x6df2af891379af92bfdc0a97e9feb12e53a047ba` |
| Onboarding router (create pool and first deposit, from 6 Nov 2026; unverified contract) | `0x41d676e5c1a49bef69da0f17cb672c10184a3311` |
| Referral reward pool (*inferred*) | `0x6c3d445b40a01ce4af942bd0a0a05ca33ee7e7b2` |
| Loss refund pool, legacy fee wallet (*inferred*) | `0xc47756133753280c37b227c24782984e021c4544` |
| Factory owner, oracle admin | `0x8e4d1ad423e4f37600cda314fd3d99629ceaeabf` |

Eighteen addresses hold `PROVIDER_ROLE`; the full list is in `common.py`.

## How each flow appears

| Flow | Shape | Evidence |
|---|---|---|
| Pool creation | Provider → `Oracle.createPool` → `PoolCreated`. Creator is always the vault, one user per pool, no USDC moves. | `0x672c8f83…1d734` |
| Creation with first deposit (from 6 Nov 2026) | User → onboarding router via the factory "fee" leg, then router → pool with `Deposited(payer = router)`. `feeAmount` is the deposit, not a fee. | `0xfe2b44e6…6d3c` |
| User deposit | `Oracle.depositUSDC` → `transferFrom(user, pool)` + `Deposited(payer = user)`. | `0x1276e325…f76b` |
| User withdrawal | `Oracle.withdrawUSDC` → pool → user + `Withdrawn`, net of $0.10. | `0x7345ba46…4841` |
| OLP → pool | Direct vault transfer (to Nov 2025); `Oracle.atomicDeposit` + `DepositedAtomic`; from 7 Nov 2025 the settlement router, each leg `Deposited(payer = router)`. | `0x89e6e12b…f512`, `0x4d30cd90…3969`, `0xf88243c6…b0c8` |
| Pool → OLP | `Oracle.processTreasuryManagementWithdrawals` → `withdrawUSDCNoEvent`: bare transfer pool → vault. | `0x9e7b14bc…3a81` |
| Fees | `Oracle.collectFees` → `withdrawFees`: pool → the calling provider. The treasury has pulled its share itself since 23 Jan 2026 (about 180,000 transfers a day). | `0xbebd6445…1bb6` |
| Payouts into pools | `Oracle.transferFromOLPToPool`: wallet → Oracle → recipient's pool (referral rewards, loss refunds). | `0xb680b71e…ed98`, `0xe3e0cf9e…820f` |
| Stray transfers | Plain ERC-20 transfer from an outside wallet to a pool, no event; found only by reconciliation (`repair`). | 411 transfers, $51.87, to 30 Nov 2025 |

Inferred from amounts: paired fee pulls from one pool are exactly 4 : 1, treasury to referral pool (20% and 5% of the spread). The referral pool's cumulative payouts track Variational's disclosed referral rewards on 17 reports (worst gap $121). The loss refund wallet paid $4,599,076.20 in 171,370 payouts, 5 Sep 2025 to 24 Mar 2026.

## Not separable (kept out of published series)

- OLP margin posting versus realised PnL and funding: only gross vault ↔ pool flows exist.
- The $0.10 transfer fee: swept later with other fees.
- OLP's hedging wallet: not identified.

## Validation

- Ledger against Multicall balances: all 211,826 pools exact over a recent 700,000-block window; full history to 30 Nov 2025 exact after `repair`. A second provider agreed on nine range checks.
- Private cross-check against the rival dashboard: treasury balance equal to the cent on 96 of 96 days; pools created equal on 454 of 454 days; daily active accounts equal on 447 of 454 days. Their "net flow" is pool balance change, not deposits minus withdrawals; their "live" pools are balances of at least $1.

## RPC providers (measured 22 Nov 2026)

- `arb1.arbitrum.io`: 10,000 logs per call; 30,000 blocks for topic-only queries; HTTP 429 from about four concurrent requests; no archive state.
- Tenderly gateway: 20,000 logs per call, but 1 GB of responses per day per IP.
- `nodes.sequence.app`: about 10,000 blocks or logs per call; serves archive state.
- Not usable: thirdweb, publicnode (no archive), drpc, 1rpc, blast, Alchemy public.

## Running it

- Full local run (index to head, fold, reconcile, repair, save state): `/usr/bin/python3 pipeline/onchain/run.py local`
- Progress: `/usr/bin/python3 pipeline/onchain/indexer.py status`
- CI increment from a state directory (`registry.bin.gz`, `agg_state.json.gz`, `anchors.json`; about 12 MB at 212,000 pools; one day of blocks took 97 seconds): `/usr/bin/python3 pipeline/onchain/run.py ci --state-dir <dir>`
- Site files: `/usr/bin/python3 pipeline/build_chain.py`, then `--check`. `--with-settlement` adds gross vault ↔ pool flows; publish them only as "USDC moved between the Core OLP Vault and settlement pools, gross, including margin, realised PnL and funding".
