# FlashArb v1: security review (tasks T3 and T6)

Date: 2026-10-24. Scope: `contracts/src/FlashArb.sol`, `contracts/src/interfaces/*.sol`, `contracts/src/sim/FlashArbSimHarness.sol`, the mocks in `contracts/src/test/` and the Hardhat tests in `contracts/test/`, against the spec in `docs/FLASH_ARB_DESIGN.md` sections 1-4. **Simulation only:** nothing was deployed to a real network, no key exists, and no transaction was sent. All fork work ran on a local Hardhat fork.

## 1. Verdict

**No high or medium issue was found in `FlashArb.sol`.** Three low issues were found and fixed, each with a regression test that fails on the old code and passes on the fix: SEC-1 (a loan callback that never runs is reported as success), SEC-2 (config can change mid-route through a contract owner) and SEC-3 (the simulation harness reports some bad inputs as success). Seven threat rows had no real test, or a test that did not exercise the mitigation. They are now covered. Slither's findings were triaged, and none is a real issue.

The core property holds under adversarial review: **a successful `executeArb` can never end with less of any token than FlashArb had before.** Two things carry it. First, the final check `finalBal >= balBefore + amount + premium + minProfit` runs on the contract's own balance. Second, each hop must spend exactly `amountIn` of its input and receive exactly the reported output (exact balance deltas). Whitelisted pools, hook tokens, a malicious Aave pool and donations can make a route revert, or give the owner extra tokens. None of them can take tokens out. The trust that remains is in the loan token itself (its `balanceOf` must be honest) and in the owner key.

**This is not an audit.** A professional audit is mandatory before FlashArb holds or routes any real money. No such use is planned (section 8).

## 2. Changes made by this review

| File | Change |
|---|---|
| `contracts/src/FlashArb.sol` | SEC-1: `executeOperation` consumes `_routeHash` (sets it to 0 after the checks). `executeArb` reverts `LoanMismatch()` if the hash is still set after `flashLoanSimple`. SEC-2: `setToken` and `setPool` are `nonReentrant`. |
| `contracts/src/sim/FlashArbSimHarness.sol` | SEC-3: simulates only calldata that starts with `0xe77fcf52`, has head offset `0x20`, and whose route `loanToken` equals the `loanToken` argument. A successful return must be exactly 32 bytes. Anything else returns `(false, 0, 0, HarnessBadCalldata())`. |
| `contracts/src/test/MockAavePool.sol` | Test modes: `skipCallback`, `doubleCallback`, `pullShortBy`. |
| `contracts/src/test/MockV3Pool.sol` | Test mode `UNDERASK` (7): asks for `amountIn - 1` in the callback but reports `amountIn`. |
| `contracts/src/test/MockBlacklistToken.sol` | New USDC/USDT-style blacklist mock. |
| `contracts/test/helpers/world.js` | Token kind `"blacklist"`. |
| `contracts/test/FlashArb.security.test.js` | New: 19 tests (SEC-1..3 regressions, COV-* coverage tests). |
| `docs/FLASH_ARB_DESIGN.md` | Sections 3.5, 3.6 step 5, 3.7 step 3 and 3.12 updated to describe the fixes. |

**The public ABI is unchanged:** same functions, events and errors, and `LoanMismatch()` is reused. **The golden vectors (section 5.9) are unchanged**, and `RouteEncoding.test.js` passes. Gas changes very little: one extra SLOAD per `setToken`/`setPool`, and the `_routeHash` clear moves into the callback. The fork tests re-wrote `contracts/gas-report.json` from the fixed code.

## 3. Threat table: code path and test

Code lines refer to `contracts/src/FlashArb.sol` after the fixes. Test files are in `contracts/test/`: `unit` = `FlashArb.unit.test.js`, `adv` = `FlashArb.adversarial.test.js`, `sec` = `FlashArb.security.test.js`, `inv` = `FlashArb.invariant.test.js`, `fork` = `FlashArb.fork.test.js`.

| # | Threat | Mitigation (code) | Test that proves it | Review note |
|---|---|---|---|---|
| T1 | Fee-on-transfer | Exact per-hop deltas, L303-308; loan receipt, L274-276; pair K check | unit "U-11 fee-on-transfer" (3 tests) | OK |
| T2 | Rebasing | Same delta checks; not allow-listed | unit "U-12 rebasing token" (up, down) | OK |
| T3 | Hook token re-entry | `nonReentrant` on `executeArb`/`sweep`/(now) `setToken`/`setPool`; callback guards L257-261, L365 | unit "hook token attacks" (U-09/U-14/U-15 at every transfer index), unit "U-14 re-entry through an owner wrapper", sec "SEC-2" | **Was a gap:** config could be changed mid-route through a contract owner (SEC-2, fixed). |
| T4 | Blacklistable token | Atomic revert; nothing held between calls | sec "COV-T4 blacklistable token" (owner, FlashArb, pool blacklisted) | **Was a gap:** the design pointed at U-20, which tests pause and caps, not blacklisting. |
| T5 | Malicious / lookalike pool | Whitelist + `_checkHop` L235-248; tokens read on-chain in `setPool` L159-170; payment capped at `amountIn` in the hop's input token L367-371 | unit U-06, U-07, U-08; adv "every pool misbehaviour mode...", "a malicious whitelisted pool cannot make FlashArb pay more" | OK. Whether the whitelist is canonical (factory `getPool` match) is enforced off-chain only (section 5.3). |
| T6 | Callback spoofing | `msg.sender == _pending.pool`, cleared before paying, L363-371; `executeOperation` checks L257-262 | unit U-09, U-10 (twice, overask, wrong sign, no callback), U-13; sec "COV-T6/T14 underask" | OK. Added the "ask less, report full" case. |
| T7 | Reentrancy | OZ `ReentrancyGuard`; CEI in the callback | unit U-14, U-15; sec SEC-1 (second `executeOperation` → `NoActiveRoute`) | OK. |
| T8 | Donation / griefing | Profit is a delta from `balBefore` L187, L203-204; per-hop deltas | unit U-16; adv "dust donations..."; sec "COV-T8 ... donated in the middle of a route" | **Was a gap (mid-route).** A mid-route donation in the loan token, during a hop that does not touch it, is counted as profit and sent to the owner. It never stays, and it is a gift, not a loss. |
| T9 | Sandwich | Final profit check L284-286, L201-203 | unit U-04 (snapshot unchanged) | OK |
| T10 | Late landing | Same + `deadline` vs `block.timestamp` L220 | unit U-04, U-19 | OK |
| T11 | Owner key | `Ownable2Step`, `renounceOwnership` reverts L128 | unit U-03, U-17; adv "a stranger cannot..." | OK |
| T12 | Leftover approvals | Exact `forceApprove(amount + premium)` L266; reset L197-199 | unit U-18; inv I2 after every call; sec "COV-T12 Aave leaves part of the allowance unused" | **Was a gap:** the reset branch never ran in any test, because the mock always pulled exactly. Mutation check: with the reset removed, COV-T12 fails (`expected 1 to equal 0`). |
| T13 | Rounding | Floor v2 math L336-346; pool-reported v3 deltas; Aave's own premium | unit U-02; RouteEncoding golden; fork F-A3/F-B2 (`swapOnce` == `getAmountOut`) | OK. See I-1 on the premium formula in the design doc. |
| T14 | v3 partial fill | Extreme price limits; `paid == amountIn` L331; input delta L308 | unit "U-21 partial fill"; sec "COV-T6/T14 underask" | OK. See section 7 for the gas cost of partial fills. |
| T15 | Arbitrary call / delegatecall | No such code | sec "COV-T15/T16 runtime bytecode has no DELEGATECALL, CALLCODE, SELFDESTRUCT, CREATE*, ORIGIN, NUMBER, PUSH0, TLOAD/TSTORE" (opcode scan of the deployed bytecode) | **Was "review" only:** now a machine check. |
| T16 | ETH | No `receive`/`fallback`/`payable` | unit U-22; sec "COV-T16 ETH forced ... is inert"; ABI check in the opcode test | OK. Forced ETH (selfdestruct, coinbase) is stuck forever, by design (I-5). |
| T17 | Bad enum | ABI decoder | unit U-05 "venue value 7" | **Spec mismatch (I-2):** the 0.8.24 calldata decoder reverts with empty data, not `Panic(0x21)`. The test asserts the real behaviour. |

Invariants I1-I6: `inv` checks I2, I3, I4, I5 and I6 after every call, and I1 after every successful `executeArb`. It runs 300 sequences x 10 calls (`FUZZ_RUNS` raises the count). I1 is literally true only for tokens a hop touches. A whitelisted pool or a hook could donate a non-loan token mid-route, and that donation would stay in FlashArb (harmless, sweepable). I4 always holds.

## 4. Findings

| ID | Severity | Location | Status |
|---|---|---|---|
| SEC-1 | Low | `executeArb` / `executeOperation` | **Fixed** |
| SEC-2 | Low | `setToken`, `setPool` | **Fixed** |
| SEC-3 | Low | `FlashArbSimHarness` constructor | **Fixed** |
| I-1 | Info | `docs/FLASH_ARB_DESIGN.md` section 1.1, `MockAavePool` | Accepted (doc note) |
| I-2 | Info | T17 / U-05 | Accepted (spec wording) |
| I-3 | Info | `_runRoute` L285 | Accepted |
| I-4 | Info | `setPool` venue not checked against pool code | Accepted |
| I-5 | Info | forced ETH | Accepted |
| I-6 | Info | revert-data size (returnbomb) | Accepted |
| I-7 | Info | Pancake v3 / thin-pool gas | Explained (section 7), no contract change |

**SEC-1 (Low, fixed): `executeArb` reported success when the loan callback never ran.** `executeArb` used to clear `_routeHash` itself after `flashLoanSimple`, and never checked that `executeOperation` had run. Suppose the `AAVE_POOL` address accepts the call without lending: a contract with a permissive fallback (WETH9's fallback is `deposit()`), a wrong address passed to the harness, or a future pool upgrade. Then `executeArb` returned success with `profit = 0` and emitted `ArbExecuted` with premium 0, although no route ran. In the harness this produced `(true, 0, gas, 0x)`. For the measurement that looks like "won, below threshold", not a configuration error. A related point: during the route, `_routeHash` stayed set, so a second `executeOperation` for the same loan would have passed the hash check (only Aave could do that). Fix: `executeOperation` sets `_routeHash = 0` right after its checks, and `executeArb` reverts `LoanMismatch()` if the hash is still non-zero. Tests: sec "SEC-1 ... never calls executeOperation" (old: no revert), "SEC-1 ... second executeOperation" (old: `BalanceDeltaMismatch`, new: `NoActiveRoute`), and sec "SEC-3 ... wrong Aave address" (harness).

**SEC-2 (Low, fixed): configuration could change in the middle of a route.** `setToken` and `setPool` were `onlyOwner` only. If the owner is a contract that forwards calls (a multisig module, a wrapper, a smart account), a hook token reachable mid-route could change the whitelist or caps through it. `setPool` re-reads `token0()`/`token1()`. The v3 callback reads `pools[pool].token0/1` at payment time, and the v2 hop reads `feeBps` at swap time. With honest pools the impact is a revert or a different v2 output, because the final check still holds. Still, mid-flight config changes break the "validated before the loan" model. Fix: `nonReentrant` on both. Config still works while paused. Test: sec "SEC-2 ... setPool / setToken re-entered through the owner" at transfer index 0, 1 and 2 (old: `lastOk == true`).

**SEC-3 (Low, fixed): the harness could report success for input that is not an `executeArb` call.** The harness forwarded any `executeArbCalldata` and decoded the return as `uint256`. Calldata for `maxLoan(token)` with a cap of 0 returned `(true, 0, gas, 0x)`. Empty return data made the whole `eth_call` revert instead of reporting a failure. A `loanToken` argument that differs from the route's loan token measured profit in the wrong token. Fix: check the selector, the `0x20` head offset and the loan token; require a 32-byte return; report `HarnessBadCalldata()` (selector `0x807cd841`) otherwise. Python decodes unknown selectors as `0x<hex>`. The constructor ABI (`HARNESS_ARGS_ABI`) and the result layout are unchanged. Tests: sec "SEC-3" (5 tests; the first 4 fail on the old code).

**I-1 (Info): the premium formula in the design doc is wrong for the deployed Aave.** Section 1.1 says half-up `(a*5+5000)/10000`. The deployed Pools on both chains round up (`docs/FLASH_ARB_LOOKUPS.md` item d, confirmed by the coordinator, and asserted by the fork tests with `ceilPremium`). The contract uses the premium Aave passes, so it is not affected. `arb/defi/quote.py` already uses ceil. `MockAavePool` still uses half-up. That is harmless for the unit tests, but it is not a faithful mock.

**I-2 (Info):** an out-of-range `Venue` in calldata reverts with empty data, not `Panic(0x21)` as T17/U-05 say. The tests assert the real behaviour.

**I-3 (Info):** a `minProfit` near `2^256` makes `balBefore + amount + premium + minProfit` overflow, which gives `Panic(0x11)` instead of `InsufficientProfit`. Only the owner sets it, and the call reverts either way.

**I-4 (Info):** `setPool` does not check that `venue` matches the pool's code. A wrong venue only reverts, because the swap is atomic: the selector is missing, or the K check fails. For `SOLIDLY_V2` the input is transferred before `getAmountOut`, and that is undone by the revert. The off-chain registry is the control.

**I-5 (Info):** ETH forced in by `selfdestruct` or as a coinbase reward cannot be recovered (no ETH sweep). This is by design, and the amounts are negligible.

**I-6 (Info), returnbomb:** OZ 5.6.1 `SafeERC20` copies at most 32 bytes of return data. High-level calls with static return types do not copy extra return data. Revert data from pools and tokens is bubbled with `returndatacopy`, so a hostile whitelisted pool could inflate memory cost, but only on a call that reverts anyway. The harness copies the full revert data from FlashArb. Same reasoning: the gas is the attacker's too, and the result is a failure.

**I-7 (Info): Pancake v3 / thin-pool gas.** See section 7.

## 5. Specific questions from the review brief

- **Callback authentication.** Only `msg.sender == _pending.pool` is accepted. `_pending` is set only around our own `swap()` call, and it is deleted before the payment, so a second callback fails. The callback `data` is ignored, so a whitelisted pool cannot name another pool. Which callback name is used is irrelevant. A whitelisted pool B cannot call back during pool A's swap (`msg.sender != A`). Duplicate pools in a route revert `DuplicatePool`. An allowed hook token calling the callbacks or `executeOperation` mid-hop gets `UnexpectedCallback` / `NotAavePool` (tested at every transfer index). `executeOperation` requires `msg.sender == AAVE_POOL`, `initiator == this`, a non-zero hash and a matching hash, and now consumes the hash.
- **A malicious Aave pool** cannot take tokens either. It must lend before the receipt check passes. The approval is exactly `amount + premium` and is set with `forceApprove` (overwrite, not add). `executeArb` checks `balAfter >= balBefore + minProfit` after the loan.
- **Profit and donations.** Profit is measured against the balance before the loan. Pre-existing balances, of the loan token and of intermediate tokens, are never spent, because every hop's input delta must equal exactly the previous hop's output.
- **v3 math.** `amountIn <= int256.max` is checked before the cast. A negative `outDelta` of `int256.min` would revert on negation (checked math). The price limits are `MIN+1` / `MAX-1`. Partial fills are rejected (`paid == amountIn` plus the input delta).
- **v2 math.** Floor division, the same as the pair. Overflow needs `amountIn > ~2^130` and reverts (checked math). A wrong `getAmountOut` from a Solidly pair only trips the pair's own K check.
- **Access control.** Every state-changing function except the guarded callbacks is `onlyOwner`. Two-step transfer; renounce disabled. `sweep` sends only to `owner()`. No ETH can enter through a call.
- **Compiler and EVM.** solc 0.8.24, optimizer 200, `evmVersion: paris`. The bytecode has no `PUSH0` (checked by test), which is also fine on Arbitrum and Base, both of which support Shanghai and later. No transient storage (storage-based `ReentrancyGuard`). No `block.number` (the `NUMBER` opcode is absent); only `block.timestamp` is used. FlashArb has no `unchecked` blocks. The 0.8.24 known-bug list (YulOptimizerKnowledgeBaseStaleBookkeeping, MemoryByteArrayElementDeleteClearsWholeWord, SpillSlotCollisionAcrossMutualRecursion, UnsoundSpillInMutualRecursion, LostStorageArrayWriteOnSlotOverflow) was checked against the code. None applies: there is no via-IR, no recursion, no `delete` on bytes elements, and no storage arrays.

## 6. Slither (0.11.6) triage

Run: `slither src/FlashArb.sol` and `src/sim/FlashArbSimHarness.sol` with the Hardhat-downloaded solc 0.8.24 binary, `--optimize --evm-version paris`, `--exclude-dependencies`.

| Detector (impact) | Count | Triage |
|---|---|---|
| `reentrancy-balance` (High) | 7 | **False positive / by design.** The "stale" `inBefore`/`outBefore`/`balBefore` are the reference points of the exact delta checks. Re-entry that moves a balance makes the check fail (strict), or adds tokens (a gift). No path removes tokens. |
| `incorrect-equality` (Medium) | 1 | False positive: `rh == bytes32(0)` compares a stored hash sentinel, not a balance. (It appeared with the SEC-1 change.) |
| `unused-return` (Medium) | 1 | Intentional: the third `getReserves()` value (timestamp) is not needed. |
| `missing-zero-check` (Low) | 1 | OZ `Ownable2Step.transferOwnership`: a zero pending owner just cancels the transfer. |
| `calls-loop` (Low) | 12-14 | Intended: at most 4 hops to whitelisted pools. A reverting pool reverts the whole tx, which is the desired behaviour. |
| `reentrancy-benign` / `reentrancy-events` (Low) | 4 | State writes and events after calls, all inside `nonReentrant` or guarded paths. No impact. |
| `timestamp` (Low) | 1 | Intended deadline check (T10). |
| `assembly`, `low-level-calls`, `pragma`, `solc-version`, `dead-code`, `naming-convention`, `unindexed-event-address` (Info) | – | OZ library code, or the harness (assembly and low-level call by design). `solc-version` refers to OZ's floating pragmas; the compiler is pinned to 0.8.24 (see section 5). |

No high or medium Slither finding remains unreviewed.

## 7. Why Pancake v3 hops cost so much gas (I-7, coordinator question)

Observation from the Python side: harness simulations of routes with Pancake v3 hops used 3.4M-4.5M inner gas, against 0.4M-0.6M for other venue mixes. Correctness matched to the wei.

**This is pool behaviour, not FlashArb.** Measured on local forks (Arbitrum and Base, latest block, 2026-10-24) with a direct pool swap (`ForkSwapper.v3Swap`, no FlashArb involved) next to the same swap through `FlashArbExposed.swapOnce`:

| Chain | Pool | Swap | Ticks moved | Pool only | Through FlashArb |
|---|---|---|---|---|---|
| Arbitrum | Uniswap v3 5 bps WETH/USDC (deep) | 0.05 WETH | 0 | 135,582 | 151,745 |
| Arbitrum | Pancake v3 5 bps WETH/USDC | 0.05 WETH | 1 | 178,360 | 207,019 |
| Arbitrum | Uniswap v3 1 bps WETH/USDC | 0.05 WETH | 8 | 144,252 | 158,681 |
| Arbitrum | **Pancake v3 1 bps WETH/USDC** `0x7fcd…b947` | 0.05 WETH | 5 | 317,963 | 329,522 |
| Arbitrum | **Pancake v3 1 bps WETH/USDC** | **1 WETH** | **93** | **3,288,757** | **3,300,316** |
| Arbitrum | same, `lmPool` code stubbed to `STOP` | 1 WETH | 92 | 2,407,891 | – |
| Arbitrum | Uniswap v3 1 bps, liquidity exhausted | 500 WETH | 112,948 (to the limit) | 13,113,050 | 13,124,549 |
| Arbitrum | Pancake v3 1 bps / 5 bps, liquidity exhausted | 500 / 2000 WETH | – | out of gas at 16M | out of gas (15.75M) |
| Base | Pancake v3 1 bps WETH/USDC (deep) | 1 WETH | 3 | 263,028 | 272,693 |
| Base | Uniswap v3 1 bps WETH/USDC (thin) | 1 WETH | 25 | 433,550 | 443,155 |

What drives the cost:

1. **Initialized ticks crossed.** Every crossed initialized tick costs about 25k gas in any Uniswap-v3-style pool (cold SLOAD/SSTORE of the tick's fee and oracle accumulators). The Arbitrum Pancake 1 bps WETH/USDC pool is thin (~131 WETH) and has an initialized tick at almost every tick (tick spacing 1). A 1 WETH trade crosses ~93 of them, which is the 3.3M above and matches the 3.4-4.5M seen by Python. A thin Uniswap pool behaves the same way (Base Uniswap 1 bps above).
2. **Pancake's farm hook.** Pancake v3 pools with an attached `lmPool` (MasterChef v3 farm) call it once per swap (`accumulateReward`) and once per crossed tick (`crossLmTick`). That adds about 26k per swap plus about 10k per crossed tick: 3.29M with the hook against 2.41M with the hook stubbed.
3. **Exhausting a pool.** FlashArb passes the extreme price limits (`MIN_SQRT_RATIO+1` / `MAX_SQRT_RATIO-1`). If a hop's size exceeds the pool's liquidity, the pool walks the tick bitmap all the way to the limit before FlashArb rejects the partial fill. That costs millions of gas (13M on Uniswap 1 bps; more than 16M on Pancake). Correctness is kept (`PartialFill`), but a revert like this is the most expensive kind of miss.

FlashArb's own overhead is a constant ~10k-30k per hop (four `balanceOf`, the pending slot writes, the event). It does not depend on the venue or on the ticks crossed. The callback, approval and return-data handling on the Pancake path do no per-tick work, and there is no loop in FlashArb beyond the ≤4 hops.

Recommendations (Python side, not changed here): price gas per hop as `base + ticks_crossed x (25k, or ~35k for a Pancake pool with lmPool)` from the quoter's tick walk, instead of a constant per venue; never size a hop beyond the loaded tick window or the available liquidity; and consider dropping the thinnest Pancake pools from the registry. A contract-side bound, a per-hop `sqrtPriceLimitX96` (or one derived from a max-ticks budget), would cap the cost of exhaustion reverts. It changes the ABI and the golden vectors, so it is a v1.1 decision for the user, not part of this review.

## 8. What was not reviewed, and residual risks

Not reviewed:

- The real Aave v3 Pool, Uniswap/Sushi/Pancake v3 pools, Pancake's `lmPool`, Slipstream, Camelot and Aerodrome pair code. They are treated as behaving like their sources, and the fork tests exercise one block on each chain.
- The off-chain whitelist build (factory `getPool`/`getPair` canonical check) and everything under `arb/` (owned by the Python engineer; T4 reviews it).
- OpenZeppelin 5.6.1 internals (assumed correct).
- Formal verification. No symbolic execution or property-based fuzzing beyond the seeded Hardhat fuzz (300 x 10 calls by default; T6 asks for 5,000 sequences via `FUZZ_RUNS`, not run here because it takes over 40 minutes).
- Gas-optimisation review.

Residual risks a human auditor must still look at before any real-money use:

1. **Loan-token honesty.** Safety rests on the loan token's `balanceOf` and `transfer` being honest. A malicious or upgradeable allowed token (USDC and USDT are upgradeable proxies) is outside what FlashArb can defend against.
2. **Owner key / owner contract.** A stolen owner key can whitelist anything and sweep dust. No allowance from the owner exists, so the owner's wallet is not exposed. If the owner is a contract, its own call-forwarding surface matters (SEC-2 closes the mid-route path only).
3. **Pool families whose callbacks are not yet in the whitelist** (Algebra, future Slipstream versions): adding one means a new callback name, and that needs a new review.
4. **Pool upgrades or governance changes** (Aave flash-premium changes, Camelot/Aerodrome fee changes, Pancake farm hooks) change gas and profitability, not safety. They are re-checked only by the fork tests.
5. **Gas-cost exposure on misses** (section 7), mainly a measurement and economics risk.
6. The design's statement that "both sequencers keep a private mempool" (T9) is an ordering assumption, not something the contract enforces.

**A professional, independent smart-contract audit is mandatory before FlashArb is funded or deployed anywhere with real value. No such deployment is planned: this project is simulation only.**

## 9. Tests run for this review

| Run | Result |
|---|---|
| Baseline before changes: `npx hardhat test` | 69 passing, 3 pending (fork tests skipped without `FORK`) |
| New security tests against the **old** contract and harness | 10 of 19 failing as intended: SEC-1 (2), SEC-2 (3), SEC-3 (4), plus the opcode scan before its data-segment handling was fixed; the 9 coverage tests pass |
| Mutation check: allowance reset removed | COV-T12 fails (`expected 1 to equal 0`) |
| After the fixes: `npx hardhat test` (unit, adversarial, invariant fuzz 300x10, golden, security) | **88 passing**, 3 pending |
| `FORK=arbitrum npx hardhat test test/FlashArb.fork.test.js` (block 517895660) | **4 passing** (F-A1+F-X, F-A4, F-A2, F-A3) |
| `FORK=base npx hardhat test test/FlashArb.fork.test.js` (block 53035490) | **6 passing** (F-B1+F-X, F-B6, F-B5, F-B2, F-B3 Slipstream callback, F-B4 Pancake 1 bps) |
| Slither 0.11.6 on both entry points, before and after the fixes | Triaged in section 6 |
| Fork gas probe (section 7), both chains | Script kept outside the repo (scratchpad) |
