# FlashArb Python review (task T4)

Date: 2026-10-24. Scope: `arb/defi/*.py`, the `defi` part of `arb/config.py` and `config/settings.yaml`, and `tests/test_defi_*.py`, checked against `docs/FLASH_ARB_DESIGN.md` sections 5 and 6. Simulation only: every RPC call was a read (`eth_call`, `eth_getBlockByNumber`, `eth_getCode`). No key exists and nothing was sent.

## Verdict

**The pipeline can now be trusted to produce the decision numbers, with the biases listed in section 5.** Before this review, the measurement would have **overstated** the projected after-tax $/day. It counted every route through the same gap as its own episode (a multiplier of 2x to 10x). It priced every v3 hop at a constant gas cost, while a thin pool can cost 10x that. It could also print GO after one hour of data. All three are fixed and covered by regression tests. Local quotes match the harness to the wei on real state: live quote error is 0.0 bps at k=0 on both chains.

Three things still need a decision from the user (section 8): the Base priority tip, the tax base, and what a run with fewer than 30 episodes means.

## 1. What on-chain equality does and does not prove

`quote_v3 == QuoterV2` and `local quote == harness profit` prove the **swap math**: v3 stepping, v2 formulas, fee rounding, the Aave premium (ceil) and the route encoding. They prove nothing about:

- **Costs:** gas units, gas price, L1 fee, tip, ETH/USD price, tax.
- **Sizing:** whether the size maximises **net**, not gross.
- **What is counted:** episodes, attempts, hours, hit rate.
- **Whether a gap is reachable:** latency and competition.

Those are what this review checked (sections 2 and 3).

## 2. Issues found

Severity is about the decision numbers: High means it can change the verdict. All tests named below fail on the pre-review code and pass now. Every test was checked by running it against `git checkout -- arb` and then restoring the code.

| ID | Sev | Where | Failing scenario | Fix | Fixed / test |
|---|---|---|---|---|---|
| M-01 | **High** | `measure.find_episodes`, `run_measurement` | One mispriced pool appears as several surviving keys: WETH-loan A>B, USDC-loan B>A, other fee tiers, triangles through it, and the same thin pool against another counter-pool in the next sample (seen live on Base, where pool `0xb7f0` was paired with 3 venues and became 3 episodes). Each key counted as an episode, was replayed and was multiplied into $/day, although executing one closes the others. | Per sample, `select_attempts` picks pool-disjoint survivors greedily by net. That set is the attempts. An attempt starts a new episode only if its key did not survive in the previous complete sample **and** it shares no pool with that sample's attempts (`is_new_attempt`). The live loop replays exactly these. The old per-route count is still reported as "route keys" in table 2. | Yes. `test_m01_*` (3 tests), updated `test_run_measurement_*` |
| M-02 | **High** | `compute_chain_metrics` verdict | 360 samples (1 h) with 30+ profitable episodes gave `GO-TO-NEXT-PHASE`. The design's "≥ 2,000 complete samples" was printed as a warning but never applied. If most replays at the realistic k failed with data errors, the hit rate came from a self-selected remainder. | Data conditions are now: sample data-failure rate < 5%, **≥ 2,000 complete samples**, ≥ 30 replayed episodes at the realistic k, and **replay data-failure rate < 5%**. The verdict line lists the reasons for INCONCLUSIVE. | Yes. `test_m02_*` (2) |
| G-01 | **High** | `scanner.GasModel`, `assess`, `measure.replay_opportunity` | Gas was constant per venue (230k per v3 hop). A thin pool crosses an initialized tick at almost every tick: about 25k gas per tick, plus Pancake's lmPool hook (26k per swap and 10k per tick). The Arbitrum Pancake 1 bps WETH/USDC pool `0x7fcd…b947` costs 3.29M gas for 1 WETH (93 ticks). An exhausting hop costs 13M+ (`docs/FLASH_ARB_SECURITY.md` section 7). The scanner under-priced big trades in thin pools, sized for gross profit, and replays at 25M sim gas counted a 13M-gas route as a hit. | `GasModel.units(hops, quotes)` adds `ticks_crossed × tick_units` per v3 hop, plus `pancake_lm_units + ticks × pancake_lm_tick_units` for Pancake (new `defi.gas` fields). `assess` tries the raw-profit optimum and 10 smaller sizes, and keeps the best **net**. `capacity_usd` and `min_profit_raw` use the same per-size estimate. `details` now carries `ticks_crossed` per hop and `gas_limit = ceil(estimate × 1.3)`. A replay that needs more than `gas_limit` becomes an `OutOfGas` miss that costs the full limit (`apply_gas_limit`). Sizing never passes available liquidity (an exhausted quote is −∞ and the window edge caps the size). | Yes. `test_gas_model_charges_crossed_ticks_*`, `test_reported_gas_counts_the_ticks_*`, `test_replay_needing_more_gas_*`, `test_replays_apply_the_scanner_gas_limit`, `test_sizing_never_goes_past_the_pool_liquidity` (a guard; it already held) |
| M-03 | Medium | `measure.sampled_hours`, `find_episodes` | Hours were computed as complete samples × median spacing. When sampling slowed down (rate-limit back-off, slow scans, replays sharing the limiter, which mostly happens when gaps exist), hours were under-counted and episodes/day inflated: a cadence going from 10 s to 30 s read as 1 h instead of 2 h. Episodes also ran on across restarts and outages. | Hours are now the real time between samples, with a coverage gap (> max(5 × median, 120 s)) counted as one step, times the complete share. A coverage gap ends every episode, in the report and in the live loop. | Yes. `test_m03_*` (2) |
| P-09 | Medium | `quote.optimal_amount`, `scanner.scan_block` | (a) Every v3 route whose tick window was smaller than `max_in` got `capped=True` and the note "v3 window edge reached", even when the optimum was interior (the common case). (b) When the optimum really was at the window edge, the size and profit at the edge were reported as the opportunity, which silently understates large gaps. | `capped` is set only when the optimum sits at the edge (`at_edge`). A window-capped candidate is re-read with ±4 bitmap words and sized again. The note now distinguishes `capped at max_loan_usd` from `v3 window edge reached`. | Yes. `test_p09_*`, `test_exhausted_quotes_*` (updated) |
| P-06 | Medium | `pools.build_registry` | Dust pools: the first TVL pass could only price stables and WETH, so pools of two unpriced tokens (WBTC/ARB, cbBTC/cbETH) were all kept, including a 51-satoshi WBTC/ARB pair and Slipstream pools worth $0.01. They screened in every block with marginal rates of 1.17 to 29, which inflated `screened_in` and the gross-gap rate. Their tick reads were most of Arbitrum's 8-10 s scan time. | Second pass `_drop_dust_pools`: prices every token from the deepest **already-sized** pool and drops pools whose recorded balances are worth < `min_pool_tvl_usd` (reason in `registry.json` `rejected`). A pool is kept if any token cannot be priced reliably. Old cached registries (no `dust_pass` in meta) rebuild automatically. Live: Arbitrum dropped 2 (scan **8.8 s → 0.4-3.4 s**, screened-in 27 → 0-3), Base dropped 6. | Yes. `test_p06_*` |
| P-05 | Low | `scanner.read_block_costs` (Arbitrum) | The L1 estimate used an all-zero dummy route. Arbitrum prices brotli-compressed data, so it under-estimated by **35%** live (1,219 vs 1,891 L1 gas units for 2 hops). It was also multiplied by the header base fee, not the base fee NodeInterface divided by. Tiny in USD today (L1 is about $0.0001 per tx), but wrong if L1 fees spike. | `l1_probe_route(n)`: incompressible addresses with a real route's repeat pattern, same length (548/708 bytes). Uses the returned `baseFee` (word 1). | Yes. `test_p05_*` |
| P-08 | Low | `pools.read_snapshot` | Chainlink `updatedAt` was not checked, so a dead feed would price gas and WETH loans forever. | `updatedAt` older than 25 h is a data failure (heartbeat 24 h on Arbitrum, 20 min on Base). The fake feed now has a real `updatedAt`. | Yes. `test_p08_*` |
| P-07 | Low | `pools.build_registry` | A pool listed twice in `addresses.json` would give two PoolSpecs and "two-pool" routes through the same pool (the contract rejects these as `DuplicatePool`). There are no duplicates today. | Dedup by address, listed in `rejected`. | Yes. Part of `test_p06_*` |
| R-02 | Low | `store` (JSONL) | A run killed mid-append leaves a torn line. The next run appended onto it, and `json.loads` then made the whole day unreadable, so the report saw no data. | `DefiStore._append` writes each batch in one call and terminates a torn last line first. `_load` skips undecodable lines and counts them (`df.attrs["skipped_lines"]`). | Yes. `test_r02_*` |

Not changed, but noted:

| ID | Sev | Note |
|---|---|---|
| N-1 | Medium | **Gas fit and harness gas disagree by about ±10-15%.** On live Arbitrum state, zero-tick routes measured 507k-514k (v3+v2) and 549k (Pancake+v2) in the harness. The scanner estimates 463k and 489k. The 3-hop fit (790k) is above the fork receipt (656k-677k). Only screening uses the estimate: replays use harness gas, and the gas limit (estimate × 1.3) still covers the observed +11%. Under-estimation turns some survivors into "won, below threshold" misses, which table 4 shows. **Recalibrate `defi.gas` from table 4's median gas after the first few hours** (I did not, because the fork receipts and the harness pull in different directions). |
| N-2 | Low | The harness runs FlashArb in the same tx that whitelists it, so whitelist slots and pool/token accounts are warm (saves ~2-2.5k gas each). `gasUsed` is measured before refunds (adds the ~20k route-hash refund). These roughly cancel: about ±20k gas, about $0.0005 at 0.02 gwei. |
| N-3 | — | `min_profit_raw` is built from the scanner estimate, so a real route would require estimate + threshold. Replays use `minProfit = 0` and judge off-chain with harness gas, as the design says. |
| N-4 | — | The contract's `maxLoan` per token must be ≥ `max_loan_usd / price` with headroom (WETH price moves). Otherwise real routes revert `LoanTooLarge`. The replay harness sets the cap to the loan itself. |
| N-5 | Low | A Camelot pair's `stableSwap` can be switched by its owner. It is checked only when the registry is built. |
| N-6 | Low | A permanently failing pool read makes every sample incomplete, so the run ends `INCONCLUSIVE (DATA)` (by design: never silent). Rebuild the registry with `--refresh` to drop it. |
| N-7 | Low | `StateUnavailable` is not retried (design 5.2). Public load-balanced nodes that lag at `head − 1` show up as data failures, not as bias. |
| N-8 | Low | Verdict thresholds ($1/day, 30 episodes, 2,000 samples, 5%) are constants in `measure.py`, not config. |
| N-9 | — | A per-hop `sqrtPriceLimitX96` in the contract would cap exhaustion reverts. That needs an ABI change, so it is a v1.1 decision (security doc section 7). The gas limit above bounds the cost in the measurement. |

Checked and correct (no change):

- **Flash premium:** ceil at 5 bps, matching the on-chain probe.
- **Fee formulas:** Uniswap 997/1000, Camelot f/1e5 per direction, Aerodrome fee before the curve.
- **Units:** wei, raw units and token decimals from `decimals_onchain`; stables at $1; WETH at the Chainlink price read at block N.
- **Arbitrum L2 cost:** `gas × header baseFeePerGas`, tip 0 (FCFS). NodeInterface's L1 estimate includes Nitro's 10% padding, a small over-estimate.
- **Base L1:** `getL1FeeUpperBound(len + 100)`, an upper bound and negligible ($0.00002).
- **Failed attempts:** pay L2 gas used plus L1 on both chains. A miss costs the harness gas of the reverted run (or the gas limit, G-01).
- **After-tax:** `CostModel.after_tax(net)` per hit (31.2% of positive net). Miss costs are not offset. This matches `arb/costs.py`.
- **Hit, miss and won-below-threshold** follow design 6.1.
- **Quote error:** comes from `InsufficientProfit(balance, required)` with `minProfit = 0`.
- **Encoding and goldens:** route encoding, golden vectors V1/V2, `encode_*` and `simulate` are unchanged.

### Changed behaviour (read this)

- **Episode definition changed** from design 6.1 ("same key in consecutive samples") to independent attempts (M-01). The old count is still reported in table 2 as "route keys".
- **The verdict has two more data conditions** (M-02).
- **`SizeResult.capped`** now means "optimum at the edge", not "the window was smaller than `max_in`".
- **`GasModel.units(hops, quotes=None)`** gains an optional argument (backward compatible). `GasModel` has three new fields with default 0.
- **`DefiGasCfg` gains** `tick_units`, `pancake_lm_units`, `pancake_lm_tick_units` and `limit_margin` (set in `config/settings.yaml`).
- **Records:** opportunity `details` gain `gas_limit` and per-hop `ticks_crossed`. Block records gain `attempt_keys`.
- **Unchanged:** `encode_execute_arb`, the goldens, the harness ABI and the CLI.
- **Git note:** the contract reviewer's commit `f1a2bd3` ("Security review of FlashArb…") picked up my in-progress `arb/defi` and `tests/` edits (M-01 to R-02 and their tests). The later changes (the episode continuation rule `is_new_attempt`, G-01, its tests, `arb/config.py`, `config/settings.yaml`) are uncommitted in the working tree. Nothing was committed by me.

## 3. Runs

| Run | Result |
|---|---|
| `python3 -m pytest -q` | **405 passed, 9 skipped** (before the review: 388 passed) |
| `DEFI_LIVE=1 pytest tests/test_defi_quote_live.py tests/test_defi_simulate.py` | **28 passed, 0 skipped**: QuoterV2 and Slipstream quoter exact match, harness integration on Arbitrum and Base |
| `defi scan --chain arbitrum` (blocks around 517,897,650) | 49 pools (2 dust rejected), 838 candidates, screened-in 0-3, gross-positive 0-3, surviving 0, **0.4-3.4 s per block** (was 8.8 s) |
| `defi scan --chain base` (around 53,035,850) | 50 pools (6 dust + 1 stable rejected), 834 candidates, screened-in 6-7, gross-positive 3-4 (each $0.0001-$0.0003 against $0.019 gas), surviving 0, 3.4-6.7 s |
| `defi measure --chain base` 2 min, then `defi report` | 17/17 complete samples, 0 RPC errors, 0 episodes. All 7 tables render; verdict `INCONCLUSIVE (DATA)` (17 < 2,000 samples, 0 < 30 episodes) as expected |
| Forced replays (scratch settings with `min_profit_usd: -1`, Arbitrum and Base, about 90 s each) | Replay path end to end on real state: quote error **0.0 bps at k = 0** on every replay. Arbitrum k = 2/8: the Uniswap 1 bps route reverted `InsufficientProfit` (the gap had moved). Harness gas 507k-653k with no ticks crossed; Pancake `0x7fcd` route 549k at 0 ticks. |

`data/defi` and `reports/defi` were deleted afterwards.

## 4. What each report table means

1. **Coverage:**
   - complete/all samples; observed hours (real time between samples × complete share)
   - data-failure rate = incomplete samples / all samples
   - RPC failures by type (after retries); state window from `defi probe`; simulation backend
   - **median scan latency**: seconds this pipeline needs per block, and about how many blocks that is
2. **Gap test:** one row per kind, plus one "all (independent attempts)" row.
   - Candidates and screened-in per block are medians over complete samples. Screened-in means marginal rate > 1 + premium.
   - **Gross-gap rate:** share of complete samples with ≥ 1 candidate whose optimal size has `profit_raw > 0` (after swap fees and premium, before gas).
   - **Surviving-gap rate:** share with ≥ 1 candidate whose net after premium, fees, tick-dependent L2 gas, L1 and tip is ≥ `min_profit_usd`.
   - **Episodes/day:** independent attempts that start (M-01) / observed hours × 24. "Route keys" is the old over-count.
   - Net and capacity columns are over all surviving routes, as predicted at N by local quotes. Capacity is the largest loan with net ≥ 0.
3. **Persistence(k):** for replayed episode starts, the share whose key still survives (local quote, own optimal size) at N + k. k = 0 is 100% by definition.
4. **Replay (per offset k):** the route sized at N is simulated through the harness at block N + k, which means landing first in block N + k + 1.
   - replays = runs that executed; data failures are excluded.
   - **hit** = success and harness net (with N + k base fee, tip and L1) ≥ `min_profit_usd`.
   - **won, below threshold** = success but net below the threshold. It counts as a miss, because a real route's `minProfit` would have reverted it.
   - **OutOfGas** = needed more gas than the estimate × 1.3. It counts as a miss at the full limit.
   - **miss cost** = mean gas USD of all non-hits.
   - **EV/attempt** = h × mean hit net − (1 − h) × mean miss cost. The after-tax version taxes each hit at 31.2% and does not offset misses.
   - **projected after-tax $/day** = episodes/day × after-tax EV.
5. **Top 15 keys:** episode-starting attempts by count, hit rate at k = 0 and at the realistic k, mean predicted net.
6. **Quote accuracy:** |predicted profit − simulated (or implied) profit| at k = 0, in bps of the loan, per venue mix. Expected 0. A median above 1 bp means a bug.
7. **Verdict:** see M-02. GO needs projected after-tax $/day > $1 at the realistic k (Arbitrum 8 ≈ 2 s, Base 1 ≈ 2 s). Any chain GO makes the overall result GO; both chains NO-GO makes it NO-GO; anything else is INCONCLUSIVE.

## 5. Known biases (direction for the GO case)

| Bias | Direction |
|---|---|
| **Latency.** The realistic k is about 2 s. This pipeline needs 0.4-3.4 s (Arbitrum) or 3.4-6.7 s (Base) per scan, plus up to a 10 s sampling interval, and competitors act inside the block. The projection describes a hypothetical bot much faster than this one. | **Optimistic** (largest) |
| **Short gaps.** Gaps shorter than the 10 s cadence are seen only with probability ≈ duration / 10 s, so episodes/day under-counts short gaps. But only gaps that persist matter for a bot this slow. | Pessimistic for a fast bot, about right for this one |
| **Flickering gaps.** A gap that dips below the threshold for one sample and returns counts twice. | Optimistic (slightly) |
| **Pool-disjoint selection.** Two real, independent gaps that share a hub pool count as one attempt per sample. | Pessimistic (slightly) |
| **Incomplete samples are excluded.** If RPC failures happen more in volatile periods (when gaps exist), gaps are missed. Watch the data-failure rate. | Pessimistic if failures correlate with activity |
| **Replays only at episode start.** Later samples of the same episode are not replayed, matching "one attempt per episode". | Neutral |
| **Replay sizing.** The replay uses the size chosen at N; a bot at N + k would re-size. | Pessimistic (slightly) |
| **Gas.** The scanner estimate runs about 10% under harness gas (N-1), so marginal survivors get replayed and then show as below-threshold misses. Replays themselves use harness gas, with N-2's ±20k. | Small either way |
| **Base tip.** A 0.01 gwei tip is 2/3 of Base gas cost. Winning priority on Base may need far more. | Optimistic if real bids are higher |
| **Tax base.** Tax is charged on net after gas and premium, per `arb/costs.py`. If gas and premium are not deductible (section 8), tax is higher. | Optimistic |
| **Stables at $1; window-capped gaps.** Window-capped gaps are now re-read with 4 words, but can still be capped. | Negligible / pessimistic |

## 6. Recommended sampling plan (measured RPC speed)

- **Cadence:** keep `sample_every_s: 10` on both chains.
  - After the dust fix, Arbitrum scans take 0.4-3.4 s and Base 3.4-6.7 s, within the 10 s budget, at about 3-8 requests per sample (< 1 rps on average, under the 5 rps and 3 rps limits).
  - The old 8-10 s Arbitrum scan was the dust pools' tick reads.
  - Each episode start adds 3 replays; replays at k > 0 also rescan the block, about 2-15 extra requests.
  - If rate limiting doubles the interval, the new hours formula keeps episodes/day honest.
- **Duration:** 24 h on both chains at the same time (8,640 samples each; covers the US, Europe and Asia sessions). Then:
  - **Data requirement:** ≥ 2,000 complete samples per chain at a data-failure rate < 5%. A 24 h run gives ~8,600.
  - **≥ 30 replayed episodes at the realistic k:** decide normally.
  - **Fewer than 30 after 24 h:** extend to 72 h, keeping the same day files (the store resumes; a restart only starts new episodes).
  - **A GO near $1/day:** aim for ≥ 100 replayed episodes. With 30 episodes, the 95% interval on a hit rate around 0.5 is about ±0.18.
- **Zero survivors in ≥ 2,000 complete samples:** the rule of three bounds the surviving-gap rate below 3/N (0.035% of samples at 8,640). The verdict still reads INCONCLUSIVE (fewer than 30 episodes); see section 8.
- **Recalibrate gas** (`defi.gas`) from table 4's median gas after the first hours (N-1).
- **Fallback for creation `eth_call`:** if the public RPC refuses it, use the Hardhat fork backend `--sim-rpc` (design 6.5) with `sim_gas: 16000000`. With the 1.3× gas limit, a 16M sim gas is enough for every route the scanner would send.

## 7. Files changed

- `arb/defi/measure.py`: `select_attempts`, `key_pools`, `is_new_attempt`, `find_episodes`, `sampled_hours`, verdict conditions, `apply_gas_limit`, report columns.
- `arb/defi/scanner.py`: `GasModel` tick gas, net-optimal sizing, `l1_probe_route`, NodeInterface base fee, window re-read, `at_edge` labelling.
- `arb/defi/quote.py`: `capped` semantics, `at_edge`.
- `arb/defi/pools.py`: dedup, `_drop_dust_pools`, feed staleness, cache auto-rebuild.
- `arb/defi/store.py`: torn-line-safe append and load.
- `arb/config.py`, `config/settings.yaml`: `defi.gas` block only (four new fields).
- Tests: `tests/test_defi_measure.py`, `test_defi_scanner.py`, `test_defi_pools.py`, `test_defi_quote.py`, `tests/defi_fakes.py` (feed `updatedAt`).

## 8. Decisions for the user

1. **Base priority tip** (`tip_gwei: 0.01`). Base orders by priority fee, and competing bots bid more. Keep 0.01 as the floor, or rerun the report at 0.05-0.1 as a sensitivity check.
2. **Tax base.** Indian VDA rules (section 115BBH) allow no deduction except the cost of acquisition. If gas and the flash premium are not deductible, tax should apply to the gross swap gain, not the net. The repo's convention (`arb/costs.py`) taxes net. Ask the CA.
3. **Few or zero episodes.** As designed, a quiet market can never be NO-GO: it stays INCONCLUSIVE until 30 episodes are replayed. Two options:
   - accept INCONCLUSIVE and extend the run;
   - add a rule such as "NO-GO if data conditions hold and even the 95% upper bound on episodes/day × the p90 predicted after-tax net ≤ $1/day".

   I did not change the rule.
4. **Thinnest Pancake pools.** The security review suggests dropping them. Tick gas now makes them pay their real cost, so they no longer distort the numbers. Dropping them would only save scan time.
5. **Commit hygiene:** part of this review was committed inside `f1a2bd3` by the other reviewer (section 2). Decide whether to keep that commit as is or split it. The rest is uncommitted.
