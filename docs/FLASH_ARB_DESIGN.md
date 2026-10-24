# FlashArb v1: design and interface spec (simulation only)

Status: approved design for plan step 2. Inputs: `docs/FLASH_LOANS.md`, `defi/addresses.json` (verified at Arbitrum block 517872239 and Base block 53032610), and the approved plan.

Ground rules (from the plan, not negotiable in v1):

- **Simulation only.** No transaction is sent. No private key is created or loaded. Nothing is deployed to a real network. The only "deployments" are on a local Hardhat chain, on a Hardhat fork, or inside an `eth_call`.
- Plain arbitrage only. No oracle manipulation, no sandwiching, no attacking protocols.
- Missing data is never reported as "no opportunity".
- `arb/defi/submit.py` always raises.

Two engineers build from this file in parallel. **Contract engineer** owns everything under `contracts/`. **Python engineer** owns `arb/defi/`, the `arb/config.py` / `config/settings.yaml` / `requirements.txt` / `arb/__main__.py` changes, and `tests/test_defi_*.py`. Neither touches the other's files. Section 5.9 has golden vectors that both sides must reproduce byte for byte.

---

## 1. Scope for v1

### 1.1 Flash source

One source only: **Aave v3 `Pool.flashLoanSimple`** (selector `0x42b0b77c`).

| Chain | Aave v3 Pool | Flash-loan tokens used in v1 | Premium |
|---|---|---|---|
| Arbitrum One (42161) | `0x794a61358D6845594F94dc1DB02A252b5b4814aD` | WETH, USDC (native) | 5 bps |
| Base (8453) | `0xA238Dd80C259a72e81d7e4664a9801593F98d1c5` | WETH, USDC | 5 bps |

- Premium formula (Aave `PercentageMath.percentMul`): `premium = (amount * 5 + 9999) / 10000` (integer division, rounds UP; verified on-chain on both chains: 999 -> 1, 1,000,000,000 -> 500,000, see docs/FLASH_ARB_LOOKUPS.md; an earlier draft said half-up, which is wrong for small amounts). The contract never computes this itself. It uses the `premium` argument Aave passes. Python uses the formula for quoting. A fork test checks that the two agree.
- USDC.e on Arbitrum is **not** a loan token in v1 (the reserve reads as frozen). It is not an allowed token at all in v1.
- Out of v1: Uniswap v3 `flash()`, Balancer (exploit history), multi-asset `flashLoan`, liquidations.

### 1.2 Venues

Four venue types. The `Venue` enum value is part of the ABI (`uint8`).

| `Venue` | Value | DEXes (registry `dex` names) | Swap call | Callback into us | Fee model | Chains |
|---|---|---|---|---|---|---|
| `UNI_V3` | 0 | `uniswap_v3`, `sushi_v3`, `aerodrome_slipstream_initial`, `aerodrome_slipstream_gaugecaps`, `aerodrome_slipstream_gaugesv3` | `swap(address,bool,int256,uint160,bytes)` `0x128acb08` | `uniswapV3SwapCallback(int256,int256,bytes)` `0xfa461e33` | `fee()` in ppm (1e-6) | both (Slipstream: Base) |
| `PANCAKE_V3` | 1 | `pancake_v3` | same `0x128acb08` | `pancakeV3SwapCallback(int256,int256,bytes)` `0x23a69e75` | `fee()` in ppm | both |
| `UNI_V2` | 2 | `sushi_v2` | `swap(uint256,uint256,address,bytes)` `0x022c0d9f` | none (we pass empty `data`) | fixed; `feeBps` stored in the whitelist (30 for Sushi v2) | both |
| `SOLIDLY_V2` | 3 | `camelot_v2`, `aerodrome_v2` | same `0x022c0d9f` | none (empty `data`) | read on-chain through `pair.getAmountOut(uint256,address)` `0xf140a35a` | Camelot: Arbitrum; Aerodrome: Base |

How the v3-style pools differ (all share the `swap` selector and argument layout):

| Pool family | Callback name | `slot0()` layout | `ticks(int24)` layout | Pool key | Fee |
|---|---|---|---|---|---|
| Uniswap v3 | `uniswapV3SwapCallback` | 7 fields, `feeProtocol` is `uint8` | 8 fields; field 1 = `liquidityNet` | fee tier | static |
| Sushi v3 | same as Uniswap (fork) | same as Uniswap | same as Uniswap | fee tier | static |
| PancakeSwap v3 | **`pancakeV3SwapCallback`** | 7 fields, `feeProtocol` is **`uint32`** | same as Uniswap | fee tier; pool deployed by the separate deployer `0x41ff…71c9` | static |
| Aerodrome Slipstream | `uniswapV3SwapCallback` (**unverified**, fork test F-B3 must confirm) | **6 fields** (no `feeProtocol`) | **more fields**; field 1 is still `liquidityNet` | **tick spacing** (`getPool(a,b,int24)`) | **dynamic** (`fee()` can change) |

Rule for Python: decode only word 0 (`sqrtPriceX96`) and word 1 (`tick`) of `slot0()`, and only word 1 (`liquidityNet`) of `ticks()`. That works for all four families.

How the v2-style pairs differ:

| Pair family | Curve | Fee | Amount-out formula (integer, exactly as on-chain) |
|---|---|---|---|
| Sushi v2 (Uniswap v2 pair) | x·y | 0.3%, fixed | `out = a*997*Rout / (Rin*1000 + a*997)` |
| Camelot v2 | x·y (volatile pairs only in v1) | per direction: `token0FeePercent()` / `token1FeePercent()`, denominator 100000, owner-changeable | `a2 = a*(100000-f); out = a2*Rout / (Rin*100000 + a2)` |
| Aerodrome v2 | volatile: x·y; stable: x³y+xy³ | per pool: `factory.getFee(pool, stable)` in bps over 10000, set by factory | volatile: `a2 = a - a*fee/10000; out = a2*Rout / (Rin + a2)` |

Aerodrome and Camelot quirks that matter:

- **Stable pools are out of the v1 scanner.** The contract can swap them (it asks the pair for `getAmountOut`), but the Python quoter does not implement the stable curve. The registry drops Aerodrome pools with `stable == true` and Camelot pairs with `stableSwap() == true`.
- Aerodrome deducts the fee from the input **before** the curve, with its own floor. Camelot multiplies by `(100000 - f)`. The rounding differs, so each family has its own formula. Never use the Uniswap 997/1000 formula for them.
- Aerodrome `getReserves()` returns `(uint256,uint256,uint256)`, not `(uint112,uint112,uint32)`. The contract never calls `getReserves` on a `SOLIDLY_V2` pair, so this does not matter on-chain. Python must decode it as three `uint256`.
- Camelot fees and Aerodrome fees can change between blocks. Python reads them every block. The contract reads the live value through `getAmountOut` at execution time.

Excluded from v1, with the reason:

| Item | Reason |
|---|---|
| Camelot Algebra v3 | Different callback (`algebraSwapCallback`), `globalState()` instead of `slot0()`, dynamic fee plugin, no sourced quoter. Candidate for v1.1. |
| Aerodrome v2 stable, Camelot stable pairs | Quoter would need the stable curve. Contract already supports them. |
| USDC.e | Frozen Aave reserve; keeps the token list short. |
| Uniswap v3 `flash()` as a loan source | Second callback surface. Aave alone is enough to measure the edge. |

### 1.3 Decision: call pools and pairs directly, never routers

**FlashArb calls pools and pairs directly.** No router is ever called or approved.

| Concern | Routers | Direct pools (chosen) |
|---|---|---|
| Approvals | Each router needs an ERC-20 allowance. Allowances left behind are the classic drain path. | **Zero DEX approvals.** v3: we pay inside the callback with `transfer`. v2: we `transfer` to the pair, then call `swap`. The only allowance ever set is Aave's exact `amount + premium`, consumed in the same call. |
| Trusted code | Router + pool. Sushi v3 and Pancake v3 router addresses are **unverified** (see `docs/FLASH_LOANS.md`). Camelot router source is unconfirmed. | Pool only. Pool addresses come from verified factories. |
| Gas | Router adds ~20–40k per hop (path decoding, extra transfer hops, deadline checks). | Lowest. |
| Callback validation | The router validates the pool callback for us. | We must validate it ourselves. This is simple because the contract knows the one pool it is calling at that moment (section 3.8). |
| Amount math | Router computes v2 `amountOut`. | Contract computes `amountOut` for `UNI_V2` from `getReserves()`, and asks the pair (`getAmountOut`) for `SOLIDLY_V2`. A wrong number only makes the pair revert (K check). |

The cost of this choice is one callback handler and a little v2 math. Both are small and fully testable with mocks.

---

## 2. Threat model

Assets at risk: tokens held by FlashArb (normally zero, because profit is pushed to the owner at the end of every arb), the owner's wallet (never approves FlashArb, so it is not reachable), and gas spent on failed attempts (simulation only in this plan).

| # | Threat | What could happen | Mitigation in the contract | Test |
|---|---|---|---|---|
| T1 | Fee-on-transfer token | We receive less than the pool reported; the books do not balance. | Only allow-listed tokens (`allowedToken`). On **every hop** the contract checks `balanceOut_after - balanceOut_before == amountOut` and `balanceIn_before - balanceIn_after == amountIn`, else `BalanceDeltaMismatch`. The loan receipt is checked the same way. Fee on the input side also breaks the pool's own K / balance check, so the pool reverts. | U-11, U-12 |
| T2 | Rebasing token | Balances move by themselves between steps. | Not allow-listed (v1 tokens: WETH, USDC, USDT, DAI, ARB, WBTC, cbBTC, cbETH; cbETH is an exchange-rate token, not rebasing). The exact balance-delta checks also catch it. | U-12 |
| T3 | Token with transfer hooks (ERC-777 style) | A hook re-enters FlashArb in the middle of a route. | Not allow-listed. Re-entry is blocked anyway: `executeArb` and `sweep` are `nonReentrant`; `executeOperation` needs `msg.sender == AAVE_POOL` and an active route hash; the swap callback needs `msg.sender == pending pool`. | U-14, U-15 |
| T4 | Blacklistable / pausable token (USDC, USDT) | FlashArb or the owner gets blacklisted; transfers revert. | The contract holds no balance between calls, so nothing is stuck. A blacklisted transfer makes the whole arb revert. `sweep` exists for any dust. | U-20 |
| T5 | Malicious or lookalike pool | A fake pool returns fake amounts, steals the input, or calls back at a bad time. | Only owner-whitelisted pools. `setPool` reads `token0()`/`token1()` from the pool and requires both tokens to be allowed and the pool to have code. Each hop's `tokenIn`/`tokenOut`/`zeroForOne`/`venue` must match the stored entry. The whitelist is built off-chain **only from canonical factory lookups** (`getPool`/`getPair` must return the same address), see section 5.3. A whitelisted pool that misbehaves can only make the tx revert, because the final profit check is on our own balance. | U-06, U-07, U-08 |
| T6 | Callback spoofing | Anyone calls `uniswapV3SwapCallback` / `pancakeV3SwapCallback` / `executeOperation` to pull tokens out. | Swap callback: `msg.sender` must equal `_pending.pool`, which is non-zero only during our own `swap()` call to a whitelisted pool, and it is cleared before we pay, so a second callback fails. Payment is capped at the hop's `amountIn` and only in the hop's input token. `executeOperation`: `msg.sender == AAVE_POOL`, `initiator == address(this)`, `_routeHash != 0`, `keccak256(params) == _routeHash`. A loan someone else starts with FlashArb as receiver fails on `initiator`. | U-09, U-10, U-13 |
| T7 | Reentrancy | Re-enter `executeArb` or `sweep` during a route. | OZ `ReentrancyGuard` on `executeArb` and `sweep`; state guards above on the callbacks. Checks-effects-interactions in the callback (clear pending, then transfer). | U-14, U-15 |
| T8 | Donation / griefing | Someone sends tokens to FlashArb to fake profit, or sends dust to break exact checks. | Profit is a **delta** from `balBefore`, taken just before the loan, so a donation never counts as profit. Per-hop checks are deltas, so existing balances do not matter. Donations stay in the contract and are sweepable by the owner. Donations to a v2 pair only increase what we receive. | U-16 |
| T9 | MEV / sandwich on our own tx | Someone moves the price inside our tx's block before it runs. | The tx is atomic. It reverts unless `finalBalance >= balBefore + amount + premium + minProfit`. The worst case is the gas of a revert. Off-chain, `minProfit` is set to at least the gas cost (section 5.5). Both sequencers keep a private mempool, so a public-mempool sandwich is not the main risk; being late is. | U-04 |
| T10 | Front-running of our route (someone takes the gap first) | Our tx lands after the gap is gone. | Same revert path (`InsufficientProfit`). A `deadline` (unix seconds, checked against `block.timestamp`) stops a stale route. We use time, not block number, because Arbitrum's `block.number` returns an L1 block estimate. The measurement in section 6 prices this case. | U-04, U-19 |
| T11 | Owner key loss or theft | Lost key: contract unusable. Stolen key: attacker controls the whitelist. | Single owner, `Ownable2Step` (two-step transfer avoids a typo), `renounceOwnership` disabled. The contract holds ~0 between calls, and the owner never gives FlashArb an allowance, so a stolen key can steal only dust. A lost key means deploying a new contract; there is no state worth keeping. In this plan no key exists at all. | U-03, U-17 |
| T12 | Approvals left behind | A later call drains via a stale allowance. | No DEX approvals at all. Aave gets `forceApprove(AAVE_POOL, amount + premium)` inside `executeOperation`. Aave pulls exactly that. After the loan, `executeArb` resets the allowance to 0 if anything is left. | U-18, I2 |
| T13 | Rounding | An off-by-one makes a v2 pair revert, or makes profit look larger than it is. | v2 `amountOut` is computed with floor division from live reserves (or read from the pair). v3 amounts are the pool's exact returned deltas. The premium comes from Aave. `minProfit` is inclusive (`>=`). Python reproduces every formula with integers. | U-02, Py golden tests |
| T14 | Partial fill on v3 (price limit reached) | Input token left over in the contract. | We pass the extreme price limits and **require `paid == amountIn`**, else `PartialFill`. No intermediate token is ever left behind. | U-21 |
| T15 | Arbitrary call / delegatecall | Attacker-controlled target. | None exist. The contract calls only: Aave pool, whitelisted pools, allow-listed tokens. No `delegatecall`, no upgradeability, no `selfdestruct`. | review |
| T16 | ETH sent to the contract | Stuck ETH. | No `receive`/`fallback`, no `payable` function, so sends revert. ETH forced by `selfdestruct` is inert and ignored. | U-22 |
| T17 | Bad enum / malformed calldata | Unknown venue value. | ABI decoding of a `Venue` value above 3 reverts (Panic 0x21). Route shape checks run before the loan. | U-05 |

---

## 3. Contract spec: `contracts/src/FlashArb.sol`

### 3.1 Files the contract engineer creates

| File | Purpose |
|---|---|
| `contracts/src/FlashArb.sol` | The contract. |
| `contracts/src/interfaces/IAavePoolMinimal.sol` | `flashLoanSimple` only. |
| `contracts/src/interfaces/IFlashLoanSimpleReceiverMinimal.sol` | `executeOperation` only (do not import Aave's receiver base). |
| `contracts/src/interfaces/IV3PoolMinimal.sol` | `swap`, `token0`, `token1`. |
| `contracts/src/interfaces/IV2PairMinimal.sol` | `getReserves() returns (uint112,uint112,uint32)`, `swap`, `token0`, `token1`. |
| `contracts/src/interfaces/ISolidlyPairMinimal.sol` | `getAmountOut(uint256,address) returns (uint256)`, `swap`, `token0`, `token1`. |
| `contracts/src/sim/FlashArbSimHarness.sol` | Constructor-only harness for `eth_call` simulation (section 3.12). |
| `contracts/src/test/*.sol` | Mocks and attackers (section 4.2). Test-only. |
| `contracts/test/helpers/fork.js` | Fork helpers. `hardhat.config.js` already refers to this path. |
| `contracts/test/*.test.js` | Tests (section 4). |
| `contracts/gas-report.json` | Written by the fork tests (section 3.13). |

Write our own minimal interfaces. Do **not** import `@uniswap/v3-core` libraries: `TickMath.sol` has `pragma >=0.5.0 <0.8.0` and will not compile with 0.8.24. Copy the two constants instead. Use OpenZeppelin 5.6.1 `Ownable2Step`, `Pausable`, `ReentrancyGuard` (storage-based; **not** `ReentrancyGuardTransient`, because `hardhat.config.js` sets `evmVersion: "paris"`, which has no transient storage), and `SafeERC20`. Do not change `hardhat.config.js`.

### 3.2 Types (exact; field order is ABI)

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

enum Venue { UNI_V3, PANCAKE_V3, UNI_V2, SOLIDLY_V2 }   // ABI: uint8, values 0..3

struct Hop {
    address pool;       // whitelisted pool or pair
    address tokenIn;
    address tokenOut;
    Venue   venue;      // must equal the whitelist entry
    bool    zeroForOne; // must equal (tokenIn == pool.token0)
}

struct Route {
    address loanToken;  // flash-borrowed token; profit is measured in it
    uint256 loanAmount; // raw units; input of hop 0
    uint256 minProfit;  // raw units of loanToken, net of the Aave premium
    uint64  deadline;   // unix seconds; require block.timestamp <= deadline
    Hop[]   hops;       // 2..MAX_HOPS
}

struct PoolInfo {       // storage, 2 slots
    address token0;
    address token1;
    Venue   venue;
    uint16  feeBps;     // UNI_V2 only (1..100); must be 0 for other venues
    bool    enabled;
}
```

ABI type strings (Python uses these exact strings):

- `Hop`: `(address,address,address,uint8,bool)`
- `Route`: `(address,uint256,uint256,uint64,(address,address,address,uint8,bool)[])`
- `executeArb` signature: `executeArb((address,uint256,uint256,uint64,(address,address,address,uint8,bool)[]))`, selector **`0xe77fcf52`**.
- Aave `params` blob: `abi.encode(uint256 balBefore, Route route)`.

Amount rule: hop 0's input is `route.loanAmount`. Hop *i*'s input is hop *i−1*'s exact output. There is no per-hop amount field. There is no per-hop minimum; the single final check covers everything.

### 3.3 Storage and constants

```solidity
uint256 public constant MAX_HOPS = 4;
uint160 internal constant MIN_SQRT_RATIO = 4295128739;                                           // from Uniswap TickMath
uint160 internal constant MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342;   // from Uniswap TickMath
uint256 internal constant LOAN_STEP = type(uint256).max;  // "hop index" used in errors for the loan receipt

address public immutable AAVE_POOL;

mapping(address => bool)     public allowedToken;
mapping(address => uint256)  public maxLoan;     // 0 = cannot be borrowed
mapping(address => PoolInfo) public pools;

// in-flight state; all zero outside executeArb
bytes32 private _routeHash;                      // keccak256(params) of the active loan
struct Pending { address pool; bool zeroForOne; }
Pending private _pending;                        // set only around one v3 swap() call
uint256 private _pendingMaxPay;                  // that hop's amountIn
uint256 private _lastPremium;                    // premium of the active loan, for the event
```

Plus OZ state: owner, pending owner, paused flag, reentrancy status.

### 3.4 Roles and constructor

- `constructor(address aavePool, address initialOwner)`: `Ownable(initialOwner)`. Require `aavePool.code.length > 0` (`NotAContract`). Set `AAVE_POOL`. Per-chain values come from `defi/addresses.json` → `chains.<chain>.aave_v3.pool`.
- One role: owner (`Ownable2Step`: `transferOwnership` then `acceptOwnership`).
- `renounceOwnership()` is overridden to always revert `RenounceDisabled()`.
- Pausable: `pause()` / `unpause()` are owner-only. Pausing blocks `executeArb` only. `sweep` and config work while paused.

### 3.5 External functions

| Function | Modifiers | Behaviour |
|---|---|---|
| `executeArb(Route calldata route) returns (uint256 profit)` | `onlyOwner whenNotPaused nonReentrant` | Section 3.6. |
| `executeOperation(address asset, uint256 amount, uint256 premium, address initiator, bytes calldata params) returns (bool)` | none (guarded by checks) | Section 3.7. |
| `uniswapV3SwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata)` | none (guarded) | Calls `_onV3Callback`. Section 3.8. |
| `pancakeV3SwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata)` | none (guarded) | Calls `_onV3Callback`. |
| `setToken(address token, bool allowed, uint256 maxLoanAmount)` | `onlyOwner nonReentrant` (review SEC-2) | Requires code. `maxLoanAmount > 0` requires `allowed`. Emits `TokenSet`. |
| `setPool(address pool, Venue venue, uint16 feeBps, bool enabled)` | `onlyOwner nonReentrant` (review SEC-2) | On enable: requires code; reads `token0()`/`token1()`; both must be `allowedToken`; `feeBps` in 1..100 for `UNI_V2`, else 0, otherwise `InvalidPoolConfig`. On disable: sets `enabled = false`. Emits `PoolSet`. |
| `pause()` / `unpause()` | `onlyOwner` | OZ. |
| `sweep(address token, uint256 amount)` | `onlyOwner nonReentrant` | Sends `amount` (or the full balance if `type(uint256).max`) to `owner()`. Emits `Swept`. |
| `transferOwnership` / `acceptOwnership` / `owner` / `pendingOwner` | OZ | |
| `renounceOwnership()` | `onlyOwner` | Always reverts. |

No `receive`, no `fallback`, nothing `payable`.

### 3.6 `executeArb` flow

1. `_validateRoute(route)` (view, before any external state change):
   - `2 <= hops.length <= MAX_HOPS`, else `BadHopCount(n)`.
   - `loanAmount > 0` (`ZeroAmount`), `loanAmount <= maxLoan[loanToken]` (`LoanTooLarge(amount, cap)`), `loanAmount <= uint256(type(int256).max)` (`AmountTooLarge`).
   - `block.timestamp <= deadline`, else `DeadlineExpired(deadline, block.timestamp)`.
   - `hops[0].tokenIn == loanToken` and `hops[last].tokenOut == loanToken`, else `RouteNotClosed`.
   - For each hop *i*: `pools[pool].enabled` (`PoolNotWhitelisted(pool)`); `venue == pools[pool].venue` (`VenueMismatch(i)`); `{tokenIn, tokenOut} == {token0, token1}` and `tokenIn != tokenOut` (`PoolTokenMismatch(i)`); `zeroForOne == (tokenIn == token0)` (`DirectionMismatch(i)`); `allowedToken[tokenIn] && allowedToken[tokenOut]` (`TokenNotAllowed(token)`); for *i* > 0, `hops[i].tokenIn == hops[i-1].tokenOut` (`HopChainBroken(i)`); no pool appears twice (`DuplicatePool(pool)`).
2. `balBefore = IERC20(loanToken).balanceOf(address(this))`.
3. `params = abi.encode(balBefore, route)`; `_routeHash = keccak256(params)`.
4. `IAavePoolMinimal(AAVE_POOL).flashLoanSimple(address(this), loanToken, loanAmount, params, 0)`.
5. Require `_routeHash == 0` (`executeOperation` consumed it, so the callback ran), else `LoanMismatch()` (review SEC-1). If `allowance(this, AAVE_POOL) != 0`, `forceApprove(AAVE_POOL, 0)`.
6. `balAfter = balanceOf(this)`. Require `balAfter >= balBefore + minProfit` (`InsufficientProfit(balAfter, balBefore + minProfit)`). `profit = balAfter - balBefore`.
7. If `profit > 0`, `safeTransfer(loanToken, owner(), profit)`.
8. Read and clear `_lastPremium` (set by `executeOperation` step 7). `emit ArbExecuted(keccak256(params), loanToken, loanAmount, premium, profit)`. Return `profit`.

After a successful `executeArb`, the contract's balance of every token is exactly what it was before, and the owner gained exactly `profit = finalBalance − balBefore − loanAmount − premium`.

### 3.7 `executeOperation` (Aave callback)

1. `msg.sender == AAVE_POOL`, else `NotAavePool()`.
2. `initiator == address(this)`, else `BadInitiator(initiator)`.
3. `_routeHash != 0`, else `NoActiveRoute()`. `keccak256(params) == _routeHash`, else `RouteHashMismatch()`. Then `_routeHash = 0` (consumed: one callback per loan; review SEC-1).
4. `(uint256 balBefore, Route memory route) = abi.decode(params, (uint256, Route))`. Require `asset == route.loanToken && amount == route.loanAmount`, else `LoanMismatch()`.
5. Loan receipt check: `balanceOf(asset) == balBefore + amount`, else `BalanceDeltaMismatch(LOAN_STEP, asset, balBefore + amount, actual)`.
6. `uint256 x = amount; for i: x = _swap(i, route.hops[i], x);` each emits `HopExecuted(i, pool, amountIn, amountOut)`.
7. `final = balanceOf(asset)`; `required = balBefore + amount + premium + route.minProfit`; require `final >= required`, else `InsufficientProfit(final, required)`. `_lastPremium = premium`.
8. `forceApprove(asset, AAVE_POOL, amount + premium)`. Return `true`. Aave then pulls `amount + premium`.

### 3.8 Swap step `_swap(uint256 i, Hop memory h, uint256 amountIn) returns (uint256 amountOut)`

Common: `inBefore = balanceOf(h.tokenIn)`, `outBefore = balanceOf(h.tokenOut)`. Recipient is always `address(this)`.

**`UNI_V3` and `PANCAKE_V3`:**

1. `_pending = Pending(h.pool, h.zeroForOne)`; `_pendingMaxPay = amountIn`.
2. `(int256 a0, int256 a1) = IV3PoolMinimal(h.pool).swap(address(this), h.zeroForOne, int256(amountIn), h.zeroForOne ? MIN_SQRT_RATIO + 1 : MAX_SQRT_RATIO - 1, "")`.
3. Require `_pending.pool == address(0)` (the callback ran and cleared it), else `UnexpectedCallback(h.pool)`. Clear `_pendingMaxPay = 0`.
4. `paid = uint256(zeroForOne ? a0 : a1)`; `amountOut = uint256(-(zeroForOne ? a1 : a0))`. Require `paid == amountIn` (`PartialFill(i, paid, amountIn)`) and `amountOut > 0` (`ZeroOutput(i)`).

**`_onV3Callback(int256 a0, int256 a1)`** (shared by both callback names):

1. `p = _pending`. Require `p.pool != address(0) && msg.sender == p.pool`, else `UnexpectedCallback(msg.sender)`.
2. `owed = p.zeroForOne ? a0 : a1`. Require `owed > 0 && uint256(owed) <= _pendingMaxPay`, else `BadCallbackAmount(a0, a1)`.
3. `token = p.zeroForOne ? pools[p.pool].token0 : pools[p.pool].token1`.
4. `delete _pending` (effects first), then `safeTransfer(token, msg.sender, uint256(owed))`.

The `data` argument is ignored. Security comes from `msg.sender == the pool we are calling right now`. We do not check which callback name the pool used; that adds nothing.

**`UNI_V2`:**

1. `(r0, r1,) = getReserves()`; `(rIn, rOut) = zeroForOne ? (r0, r1) : (r1, r0)`.
2. `amountOut = amountIn*(10000 - feeBps)*rOut / (rIn*10000 + amountIn*(10000 - feeBps))`. (For `feeBps = 30` this equals the Uniswap 997/1000 formula exactly.) Require `> 0`.
3. `safeTransfer(tokenIn, pool, amountIn)`; `pair.swap(zeroForOne ? 0 : amountOut, zeroForOne ? amountOut : 0, address(this), "")`.

**`SOLIDLY_V2`:**

1. `safeTransfer(tokenIn, pool, amountIn)`.
2. `amountOut = ISolidlyPairMinimal(pool).getAmountOut(amountIn, tokenIn)` (uses stored reserves, so the order relative to the transfer does not matter). Require `> 0`.
3. `pair.swap(zeroForOne ? 0 : amountOut, zeroForOne ? amountOut : 0, address(this), "")`.

**After every venue:** require `balanceOf(tokenOut) - outBefore == amountOut` and `inBefore - balanceOf(tokenIn) == amountIn`, else `BalanceDeltaMismatch(i, token, expected, actual)`.

### 3.9 Events

```solidity
event ArbExecuted(bytes32 indexed routeHash, address indexed loanToken, uint256 loanAmount, uint256 premium, uint256 profit);
event HopExecuted(uint256 indexed index, address indexed pool, uint256 amountIn, uint256 amountOut);
event TokenSet(address indexed token, bool allowed, uint256 maxLoan);
event PoolSet(address indexed pool, Venue venue, address token0, address token1, uint16 feeBps, bool enabled);
event Swept(address indexed token, address indexed to, uint256 amount);
// plus OZ: OwnershipTransferStarted, OwnershipTransferred, Paused, Unpaused
```

### 3.10 Errors (exact names and arguments; Python decodes them by selector from the artifact ABI)

```solidity
error NotAContract(address account);
error RenounceDisabled();
error InvalidPoolConfig(address pool);
error NotAavePool();
error BadInitiator(address initiator);
error NoActiveRoute();
error RouteHashMismatch();
error LoanMismatch();
error BadHopCount(uint256 n);
error RouteNotClosed();
error HopChainBroken(uint256 hop);
error PoolNotWhitelisted(address pool);
error VenueMismatch(uint256 hop);
error PoolTokenMismatch(uint256 hop);
error DirectionMismatch(uint256 hop);
error DuplicatePool(address pool);
error TokenNotAllowed(address token);
error LoanTooLarge(uint256 amount, uint256 cap);
error ZeroAmount();
error AmountTooLarge();
error DeadlineExpired(uint256 deadline, uint256 nowTs);
error UnexpectedCallback(address caller);
error BadCallbackAmount(int256 amount0Delta, int256 amount1Delta);
error PartialFill(uint256 hop, uint256 paid, uint256 amountIn);
error ZeroOutput(uint256 hop);
error BalanceDeltaMismatch(uint256 hop, address token, uint256 expected, uint256 actual);
error InsufficientProfit(uint256 balance, uint256 required);
```

OZ errors also appear: `OwnableUnauthorizedAccount(address)`, `EnforcedPause()`, `ReentrancyGuardReentrantCall()`, `SafeERC20FailedOperation(address)`.

### 3.11 Approvals: complete list

| Spender | When set | Amount | When zeroed |
|---|---|---|---|
| `AAVE_POOL` | end of `executeOperation` | exactly `amount + premium` (`forceApprove`, handles USDT-style tokens) | consumed by Aave's pull; `executeArb` step 5 resets to 0 if anything remains |
| any DEX, router or pool | never | – | – |

The owner never approves FlashArb for anything.

### 3.12 Simulation harness: `contracts/src/sim/FlashArbSimHarness.sol`

Purpose: let Python run the **exact** `executeArb` calldata against real chain state with one `eth_call`, with no deployment, no storage overrides and no key.

```solidity
struct TokenCfg { address token; uint256 maxLoan; }        // allowed = true
struct PoolCfg  { address pool; uint8 venue; uint16 feeBps; }

contract FlashArbSimHarness {
    constructor(
        address aavePool,
        address loanToken,
        TokenCfg[] memory tokens,
        PoolCfg[]  memory pools,
        bytes      memory executeArbCalldata   // selector 0xe77fcf52 + abi-encoded Route, byte-identical to production
    ) {
        // 1. arb = new FlashArb(aavePool, address(this));
        // 2. arb.setToken(t.token, true, t.maxLoan) for each; arb.setPool(p.pool, Venue(p.venue), p.feeBps, true) for each
        //    (config failures are reported as success=false with the revert data, not bubbled)
        // 3. before = IERC20(loanToken).balanceOf(address(this)); g0 = gasleft();
        // 4. (bool ok, bytes memory ret) = address(arb).call(executeArbCalldata);
        // 5. gasUsed = g0 - gasleft();
        // 6. profit = balanceOf(this) - before; if ok, require abi.decode(ret,(uint256)) == profit,
        //    else ok = false, ret = abi.encodeWithSignature("HarnessProfitMismatch(uint256,uint256)", returned, profit)
        // (review SEC-3) before step 3: calldata must start with 0xe77fcf52, head offset 0x20 and route.loanToken == loanToken,
        //    and a successful return must be 32 bytes; otherwise ok = false, ret = abi.encodeWithSignature("HarnessBadCalldata()")
        // 7. bytes memory out = abi.encode(ok, profit, gasUsed, ok ? bytes("") : ret);
        //    assembly { return(add(out, 32), mload(out)) }
    }
}
```

`eth_call` with no `to`, `data = initcode ++ abi.encode(constructor args)`, returns `abi.encode(bool success, uint256 profit, uint256 gasUsed, bytes revertData)`.

Artifact path that Python loads: `contracts/artifacts/src/sim/FlashArbSimHarness.sol/FlashArbSimHarness.json` (field `bytecode`). Error ABI: `contracts/artifacts/src/FlashArb.sol/FlashArb.json` (field `abi`).

### 3.13 Gas estimates

Estimates, to be replaced by fork measurements:

| Part | Gas (estimate) |
|---|---|
| Intrinsic 21,000 + calldata (548 bytes for 2 hops, 708 for 3) | 27k–31k |
| Route validation (cold SLOADs of whitelist) | 10k–20k |
| `_routeHash` write and clear | ~5k net |
| Aave `flashLoanSimple` (transfer out, callback, pull back, reserve update) | 60k–80k |
| v3 hop (pool swap without tick crossing + 4 `balanceOf` + pending writes) | 100k–130k; +~20k per tick crossed |
| v2 hop (`UNI_V2` / `SOLIDLY_V2`) | 60k–80k |
| Profit transfer to owner | 10k–30k |
| **Two hops, v3 + v2** | **~300k–380k** |
| **Two hops, v3 + v3** | **~340k–420k** |
| **Three hops** | **~450k–560k** |

In USD at the measured gas prices (Arbitrum 0.030 gwei, Base 0.0226 gwei, ETH ≈ $2,320): 400k gas ≈ **$0.028 on Arbitrum** and **$0.021 on Base**, plus Base's L1 fee (well under $0.001). So one attempt costs about 2–4 US cents.

The fork tests must write the measured numbers to `contracts/gas-report.json`:

```json
{"arbitrum": {"two_hop_v3_v2": 0, "two_hop_v3_v3": 0, "three_hop": 0, "revert_insufficient_profit_two_hop": 0},
 "base":     {"two_hop_v3_v2": 0, "two_hop_v3_v3": 0, "three_hop": 0, "revert_insufficient_profit_two_hop": 0}}
```

The Python defaults in `settings.yaml` (`defi.gas`) are updated from this file by the review task (T4 in section 7), not by either engineer.

---

## 4. Invariants and the Hardhat test plan

### 4.1 Invariants

| ID | Invariant |
|---|---|
| I1 | After a successful `executeArb`: for every token, FlashArb's balance equals its balance before the call. The owner's `loanToken` balance rises by exactly `profit`, with `profit = finalBalanceInCallback − balBefore − loanAmount − premium` and `profit >= minProfit`. |
| I2 | After any call: `allowance(FlashArb, s) == 0` for every spender `s` (Aave pool and every whitelisted pool). |
| I3 | After any call: `_routeHash == 0`, `_pending.pool == 0`, `_pendingMaxPay == 0`, `_lastPremium == 0`. (Test through a test-only subclass getter or `hardhat_getStorageAt`.) |
| I4 | For every token, FlashArb's balance never decreases across any call, except `sweep` by the owner, which lowers it by exactly the swept amount and raises the owner's balance by the same amount. |
| I5 | Only the owner changes configuration, pause state, or ownership. |
| I6 | FlashArb never holds ETH from a call (sends revert). |

### 4.2 Test-only contracts (`contracts/src/test/`)

| Contract | Behaviour |
|---|---|
| `MockERC20` | Plain ERC-20 with `mint`. |
| `MockFeeOnTransferToken` | Burns 1% on every transfer. |
| `MockRebasingToken` | Owner can scale all balances. |
| `MockHookToken` | On transfer, calls a configurable target with configurable calldata (for reentrancy). |
| `MockAavePool` | `flashLoanSimple`: transfers `amount`, calls `executeOperation` with `premium = (amount*5+5000)/10000`, then `transferFrom(receiver, amount+premium)`. Configurable to pass a wrong `initiator` or wrong `params`. |
| `MockV3Pool` | Fixed-price swap that calls `uniswapV3SwapCallback` or `pancakeV3SwapCallback` (flag), checks it was paid, and can be set to: call the callback twice, ask for more than `amountIn`, return a partial fill, or pay out less than reported. |
| `MockV2Pair` | Constant product with 0.3% fee and the K check. |
| `MockSolidlyPair` | Constant product with `getAmountOut(amountIn, tokenIn)`. |
| `CallbackAttacker` | Calls the swap callbacks and `executeOperation` directly; also calls `MockAavePool.flashLoanSimple` with FlashArb as receiver. |
| `OwnerWrapper` | A contract that owns a FlashArb instance and forwards `executeArb` / `sweep` calls from anyone. Used as the hook target in U-14, so the re-entry passes `onlyOwner` and is stopped by `nonReentrant`. |
| `RouteHashProbe` | `function hashParams(uint256 balBefore, Route calldata r) external pure returns (bytes32) { return keccak256(abi.encode(balBefore, r)); }` for the golden test. |
| `FlashArbExposed is FlashArb` | Adds `swapOnce(Hop calldata h, uint256 amountIn) external onlyOwner returns (uint256)` and getters for the in-flight slots. Used only for per-venue fork checks and I3. |

### 4.3 Unit tests (local Hardhat network, mocks): `contracts/test/FlashArb.unit.test.js` and `FlashArb.adversarial.test.js`

| ID | Test | Expected |
|---|---|---|
| U-01 | Profitable 2-hop and 3-hop routes on mocks | Success. Owner gain == returned `profit` == event `profit` == `out − loan − premium`. FlashArb balances unchanged. |
| U-02 | Profit exactly equal to `minProfit` | Success (inclusive check). `minProfit + 1` reverts `InsufficientProfit`. |
| U-03 | Non-owner calls `executeArb`, `setToken`, `setPool`, `pause`, `sweep` | `OwnableUnauthorizedAccount`. |
| U-04 | Unprofitable route | Reverts `InsufficientProfit`. All balances, allowances and storage unchanged (snapshot compare). |
| U-05 | Route shape errors: 1 hop, 5 hops, not closed, broken chain, duplicate pool, wrong direction, wrong venue, venue value 7 | The matching error (Panic 0x21 for the enum). |
| U-06 | Pool not whitelisted / disabled after whitelisting | `PoolNotWhitelisted`. |
| U-07 | `setPool` with a disallowed token, an EOA, `UNI_V2` with feeBps 0 or 101, `UNI_V3` with feeBps 5 | `TokenNotAllowed` / `NotAContract` / `InvalidPoolConfig`. |
| U-08 | Hop tokens that do not match the pool's tokens | `PoolTokenMismatch`. |
| U-09 | `CallbackAttacker` calls `uniswapV3SwapCallback` and `pancakeV3SwapCallback` directly (idle and during a route via a hook token) | `UnexpectedCallback`. |
| U-10 | `MockV3Pool` calls the callback twice; asks for more than `amountIn`; asks with the wrong sign | Second call `UnexpectedCallback`; `BadCallbackAmount`. |
| U-11 | Fee-on-transfer token as intermediate and as loan token | `BalanceDeltaMismatch` (or pool revert on the input side). |
| U-12 | Rebasing token rebased by a hook mid-route | `BalanceDeltaMismatch`. |
| U-13 | `executeOperation` called directly; via a loan started by an attacker naming FlashArb as receiver; with tampered `params` | `NotAavePool`; `BadInitiator`; `RouteHashMismatch`. |
| U-14 | FlashArb owned by `OwnerWrapper`; a hook token calls the wrapper mid-route, which re-enters `executeArb` and `sweep` | `ReentrancyGuardReentrantCall`. A hook calling FlashArb directly gets `OwnableUnauthorizedAccount`. |
| U-15 | Hook token calls `executeOperation` during a route | `NotAavePool`. |
| U-16 | Donate loan token before a route | Success; reported profit excludes the donation; donation still in FlashArb; owner can sweep it. |
| U-17 | Ownership: `transferOwnership` then old owner still owner until `acceptOwnership`; wrong account cannot accept; `renounceOwnership` | `RenounceDisabled`. |
| U-18 | After success and after revert: allowance to Aave and every pool | 0. |
| U-19 | `block.timestamp > deadline` | `DeadlineExpired`. |
| U-20 | Paused | `executeArb` reverts `EnforcedPause`; `sweep` still works. Loan above `maxLoan` reverts `LoanTooLarge`; token with `maxLoan == 0` cannot be borrowed. |
| U-21 | `MockV3Pool` partial fill | `PartialFill`. |
| U-22 | Send ETH (plain send and with data) | Reverts. Contract ETH balance stays 0. |
| U-23 | Harness: `FlashArbSimHarness` via `eth_call` on a mock profitable and a mock failing route | Returns `(true, profit, gas>0, 0x)` and `(false, 0, gas>0, InsufficientProfit selector…)`. |

### 4.4 Invariant / fuzz test: `contracts/test/FlashArb.invariant.test.js`

Hardhat has no built-in fuzzer, so write a seeded property test:

- PRNG with fixed seeds; `FUZZ_RUNS` env var (default 300 sequences × 10 calls).
- Actors: owner, attacker. Actions: random valid and invalid routes (random hop count 1–5, random pools from a set of whitelisted and non-whitelisted mocks, random amounts including 0, cap, cap+1), direct callback calls, direct `executeOperation`, donations of random tokens, `sweep` by owner and attacker, pause/unpause, `setPool` toggles.
- After **every** call assert I2, I3, I4 for every token in the set, and I1 when the call was a successful `executeArb`.
- On failure print the seed and the call sequence.

### 4.5 Fork tests: `contracts/test/FlashArb.fork.test.js` (run with `FORK=arbitrum` and `FORK=base`)

Skipped unless `FORK` is set. Run as `http_proxy="$HTTPS_PROXY" FORK=arbitrum npx hardhat test test/FlashArb.fork.test.js`. Use `FORK_BLOCK` when set, else latest (public RPCs may not serve old state). Keep the number of fork tests small: each one makes many RPC reads.

Creating a gap on the fork (no real money; local chain only): `hardhat_setBalance` a local test signer, wrap ETH with `WETH.deposit()`, and sell WETH directly into a thin pair (transfer + `swap` on the pair) to move its price. Then a route that buys back on the thin pair and sells on a deep pool is profitable.

| ID | Chain | Test |
|---|---|---|
| F-A1 | Arbitrum | Dislocate Sushi v2 WETH/USDC `0x57b8…3378`. Route: borrow USDC; USDC→WETH on Sushi v2 (`UNI_V2`); WETH→USDC on Uniswap v3 5 bps `0xc696…e8d0` (`UNI_V3`). Success; I1 exact; premium in event == `(amount*5+5000)/10000`. Record gas. |
| F-A2 | Arbitrum | Same dislocation, 3 hops including Pancake v3 5 bps `0xd9e2…1b95c` (`PANCAKE_V3`). Record gas. |
| F-A3 | Arbitrum | `swapOnce` on Camelot v2 WETH/ARB `0xa6c5…f433f` (`SOLIDLY_V2`) both directions. Confirms the 4-argument `swap` and `getAmountOut`. |
| F-A4 | Arbitrum | Same route as F-A1 without the dislocation | `InsufficientProfit`; record revert gas. |
| F-B1 | Base | Dislocate Sushi v2 WETH/USDC `0x2f88…fa87`; route through Uniswap v3 5 bps `0xd0b5…f224`. Record gas. |
| F-B2 | Base | `swapOnce` on Aerodrome v2 volatile WETH/USDC `0xcdac…5c43` (`SOLIDLY_V2`) both directions. |
| F-B3 | Base | `swapOnce` on Slipstream initial, tick spacing 100, WETH/USDC `0xb2cc…dc59` (`UNI_V3`). **Confirms the Slipstream callback name.** If it fails with a missing-callback revert, stop and report; do not guess another name. |
| F-B4 | Base | `swapOnce` on Pancake v3 1 bps WETH/USDC `0x72ab…2d38` (`PANCAKE_V3`). |
| F-X | both | Harness `eth_call` for F-A1 / F-B1 on the fork returns the same `profit` as the real call. |

Full addresses are in `defi/addresses.json` (`chains.<chain>.pools[]`).

### 4.6 Golden encoding test: `contracts/test/RouteEncoding.test.js`

- `FlashArb.interface.encodeFunctionData("executeArb", [route])` equals the golden calldata in section 5.9 for V1 and V2.
- `RouteHashProbe.hashParams(0, route)` equals the golden params hash.
- `FlashArb.interface.getFunction("executeArb").selector == "0xe77fcf52"`.

### 4.7 Acceptance for the contract task

- `npx hardhat compile` clean, no warnings in `src/` except from OZ.
- `npx hardhat test` green without `FORK` (unit, adversarial, invariant, golden).
- `FORK=arbitrum` and `FORK=base` fork tests green, or a failing F-B3 reported with the exact revert.
- `contracts/gas-report.json` written.
- No `delegatecall`, `selfdestruct`, `payable`, `receive`, `fallback`, `tx.origin`, or inline assembly in `FlashArb.sol` (assembly allowed only in the harness return).

---

## 5. Off-chain interface: Python package `arb/defi/`

### 5.1 Module list

| File | Contents |
|---|---|
| `arb/defi/__init__.py` | Empty marker. |
| `arb/defi/route.py` | `Venue`, `Hop`, `Route`, ABI encoding, route validation. |
| `arb/defi/chain.py` | Async JSON-RPC client, rate limiter, error classes, failure tracking. |
| `arb/defi/pools.py` | Registry from `defi/addresses.json`, Multicall3, state snapshots. |
| `arb/defi/v3math.py` | Exact integer port of Uniswap v3 `TickMath`, `SqrtPriceMath`, `SwapMath`, `FullMath`. |
| `arb/defi/quote.py` | Hop quotes, route quotes, premium, optimal size. |
| `arb/defi/scanner.py` | Candidates, cost model, `scan_block` → `Opportunity`. |
| `arb/defi/simulate.py` | Calldata, harness initcode, `eth_call` simulation, revert decoding. |
| `arb/defi/submit.py` | Stub that always raises. |
| `arb/defi/store.py` | `DefiStore` (extends `OpportunityStore`). |
| `arb/defi/measure.py` | Plan step 5: probe, live sampling, replay, report tables. |

Dependencies to add to `requirements.txt`: `aiohttp>=3.9`, `eth-abi>=5.1,<7`, `eth-utils>=5,<7`, `eth-hash[pycryptodome]>=0.7`. **Do not add `web3` or `eth-account`.** No signing code may exist in `arb/defi/`; a test greps for `eth_account`, `sign_transaction`, `private_key` and `eth_sendRawTransaction` and fails if any appear.

All addresses inside Python are lowercase hex strings (`"0x…"`); checksum only for display.

### 5.2 `chain.py`

```python
SIM_CALLER = "0x000000000000000000000000000000000000dead"   # `from` for every eth_call; not a key

class RpcError(Exception):
    def __init__(self, method: str, code: int | None, message: str, data: bytes | None = None): ...
class RateLimited(RpcError): ...         # HTTP 429 or JSON-RPC -32005 after retries
class StateUnavailable(RpcError): ...    # "missing trie node", "header not found", "historical state", "pruned"
class ExecutionReverted(RpcError): ...   # .data = revert bytes (may be b"")
class TransportError(RpcError): ...      # timeouts, connection errors, 5xx after retries

class Transport(Protocol):
    async def post(self, url: str, payload: dict) -> tuple[int, dict, dict[str, str]]: ...  # (status, json, headers)

class RateLimiter:                       # token bucket
    def __init__(self, rate_per_s: float, burst: int, clock=time.monotonic, sleep=asyncio.sleep): ...
    async def acquire(self) -> None: ...

@dataclass(frozen=True)
class BlockHeader:
    number: int
    timestamp: int
    base_fee_wei: int
    hash: str

class ChainClient:
    calls: int
    failures: list[str]                  # same idea as arb/connectors/tracking.py TrackingSource
    def __init__(self, name: str, chain_id: int, rpc_url: str, limiter: RateLimiter, *,
                 transport: Transport | None = None, timeout_s: float = 10.0, max_retries: int = 4): ...
    async def request(self, method: str, params: list) -> Any: ...
    async def chain_id(self) -> int: ...
    async def block_number(self) -> int: ...
    async def header(self, block: int | str) -> BlockHeader: ...
    async def eth_call(self, to: str | None, data: bytes, block: int | str, *,
                       from_: str = SIM_CALLER, gas: int | None = None) -> bytes: ...
    async def get_code(self, address: str, block: int | str = "latest") -> bytes: ...
    def drain_failures(self) -> list[str]: ...   # returns and clears
    async def close(self) -> None: ...

async def connect(name: str, cfg: "DefiChainCfg", expected_chain_id: int) -> ChainClient: ...
    # resolves the URL (env var first, then default), checks eth_chainId == expected, else raises RpcError
```

Rules:

- Every request goes through the limiter. Retries: on 429, 5xx, timeouts; backoff 0.5, 1, 2, 4 s (honour `Retry-After`). No retry on `ExecutionReverted` or `StateUnavailable`.
- Every final failure appends `"{method}@{block}: {type}: {message[:200]}"` to `failures` **and** raises. Nothing returns an empty result on failure.
- Block numbers are sent as hex (`hex(n)`), never as decimal.

### 5.3 `pools.py`

```python
MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"   # checked with get_code at startup; missing code -> RpcError

V3_DEXES = {"uniswap_v3", "sushi_v3", "pancake_v3",
            "aerodrome_slipstream_initial", "aerodrome_slipstream_gaugecaps", "aerodrome_slipstream_gaugesv3"}
V2_DEXES = {"sushi_v2", "camelot_v2", "aerodrome_v2"}
V2Formula = Literal["uniswap", "camelot", "aerodrome"]

@dataclass(frozen=True)
class Token:
    symbol: str
    address: str
    decimals: int

@dataclass(frozen=True)
class PoolSpec:
    chain: str
    dex: str                     # registry name from addresses.json
    venue: Venue
    address: str
    token0: Token                # by address order, read on-chain (the "pair" label is NOT address order)
    token1: Token
    fee_ppm: int | None          # v3 static tier from addresses.json; None for Slipstream (dynamic) and v2
    tick_spacing: int | None     # v3 only
    v2_formula: V2Formula | None
    v2_fee_bps: int | None       # UNI_V2 whitelist value (30 for sushi_v2); None otherwise

@dataclass(frozen=True)
class V2State:
    pool: PoolSpec
    reserve0: int
    reserve1: int
    fee0: int                    # input-fee numerator when token0 is the input
    fee1: int                    # input-fee numerator when token1 is the input
    fee_den: int                 # 1000 (uniswap: fee = 3), 100000 (camelot), 10000 (aerodrome)
    block: int

@dataclass(frozen=True)
class V3State:
    pool: PoolSpec
    sqrt_price_x96: int
    tick: int
    liquidity: int
    fee_ppm: int                 # read from fee() every block
    tick_spacing: int
    liquidity_net: dict[int, int]  # initialized ticks inside the loaded window; empty until read_ticks
    word_lo: int                 # loaded bitmap words [word_lo, word_hi]
    word_hi: int
    block: int

PoolState = V2State | V3State

@dataclass(frozen=True)
class Snapshot:
    chain: str
    block: int
    timestamp: int
    base_fee_wei: int
    eth_usd: float               # Chainlink feed in addresses.json, read at the same block
    states: dict[str, PoolState] # by pool address
    errors: list[str]            # per-pool read failures; those pools are absent from states

@dataclass(frozen=True)
class Call:
    target: str
    data: bytes
    allow_failure: bool = True

@dataclass(frozen=True)
class CallResult:
    success: bool
    data: bytes

class Multicall:
    def __init__(self, client: ChainClient, address: str = MULTICALL3, chunk: int = 100): ...
    async def aggregate(self, calls: list[Call], block: int) -> list[CallResult]: ...  # aggregate3, 0x82ad56cb

class PoolRegistry:
    chain: str
    tokens: dict[str, Token]     # by symbol
    pools: list[PoolSpec]
    def by_address(self, address: str) -> PoolSpec: ...
    def pools_for_pair(self, a: str, b: str) -> list[PoolSpec]: ...   # symbols, any order
    def to_json(self) -> dict: ...
    @classmethod
    def from_json(cls, d: dict) -> "PoolRegistry": ...

async def build_registry(client: ChainClient, chain: str, addresses: dict, cfg: "DefiCfg",
                         cache_path: Path) -> PoolRegistry: ...
async def read_snapshot(client: ChainClient, registry: PoolRegistry, block: int) -> Snapshot: ...
async def read_ticks(client: ChainClient, snapshot: Snapshot, pools: set[str], words_each_side: int = 1) -> Snapshot: ...
```

`build_registry` steps (once per chain, cached to `data/defi/<chain>/registry.json`; rebuild with `--refresh`):

1. Take `chains.<chain>.pools` from `defi/addresses.json`. Keep only dexes in `cfg.chains[chain].venues`, both tokens in `cfg.chains[chain].tokens`, `liquidity_raw != 0` (v3) or both reserves non-zero (v2), and estimated TVL ≥ `cfg.min_pool_tvl_usd` (USD stables at $1, WETH at the feed price; pools without a priced token are kept).
2. Read `token0()`, `token1()` (and `tickSpacing()` for v3) by Multicall.
3. **Factory check** (lookalike defence): `getPool(t0,t1,uint24 fee)` for Uniswap/Sushi/Pancake v3, `getPool(t0,t1,int24 tickSpacing)` for Slipstream (the matching deployment's factory), `getPair(t0,t1)` for Sushi v2/Camelot v2, `getPool(t0,t1,bool stable)` for Aerodrome v2. The result must equal the pool address. Mismatches are dropped and listed in `registry.json` under `rejected`.
4. Drop stable pools: Aerodrome `stable() == true`, Camelot `stableSwap() == true`.

`read_snapshot` (stage 0, one Multicall batch per chunk, all at block tag `block`): header (`eth_getBlockByNumber`), Chainlink `latestRoundData()`, and per pool: v3 `slot0()` (words 0–1), `liquidity()`, `fee()`; v2 `getReserves()` (decode words 0 and 1 as `uint256`), Camelot `token0FeePercent()`/`token1FeePercent()`, Aerodrome `factory.getFee(pool, false)`.

`read_ticks` (stage 1, only for v3 pools on screened-in candidates): `tickBitmap(int16)` for words `w-1, w, w+1` where `w = floor(tick / tick_spacing) >> 8` (floor toward −∞), then `ticks(int24)` for every set bit, decoding word 1 as `int128 liquidityNet`.

### 5.4 `v3math.py` and `quote.py`

**Quoting decision: local exact integer math, not QuoterV2.** QuoterV2 costs one `eth_call` per (pool, amount). A size search needs ~40 evaluations per route, which public-RPC rate limits cannot support. QuoterV2 is also not sourced for Pancake and Sushi v3. Local math needs only two Multicall rounds per block, and is exact as long as the swap stays inside the loaded tick window. The truth check is the harness simulation (5.6), which runs the real pools. QuoterV2 (Uniswap; Slipstream quoter with `int24 tickSpacing`) is used **only in tests** to prove the local math.

```python
# v3math.py: exact ports, Python ints, no floats
MIN_TICK = -887272; MAX_TICK = 887272
MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342
def get_sqrt_ratio_at_tick(tick: int) -> int: ...
def mul_div(a: int, b: int, d: int) -> int: ...
def mul_div_rounding_up(a: int, b: int, d: int) -> int: ...
def get_amount0_delta(sqrt_a: int, sqrt_b: int, liquidity: int, round_up: bool) -> int: ...
def get_amount1_delta(sqrt_a: int, sqrt_b: int, liquidity: int, round_up: bool) -> int: ...
def get_next_sqrt_price_from_input(sqrt_p: int, liquidity: int, amount_in: int, zero_for_one: bool) -> int: ...
def compute_swap_step(sqrt_cur: int, sqrt_target: int, liquidity: int,
                      amount_remaining: int, fee_ppm: int) -> tuple[int, int, int, int]: ...
    # (sqrt_next, amount_in, amount_out, fee_amount), exact-input only
```

```python
# quote.py
@dataclass(frozen=True)
class HopQuote:
    amount_in: int
    amount_out: int
    fee_paid: int          # raw units of tokenIn (v3: sum of fee_amount per step; v2: a - a_after_fee)
    ticks_crossed: int
    exhausted: bool        # v3: left the loaded window or ran out of liquidity -> quote is invalid

@dataclass(frozen=True)
class SizeResult:
    amount_in: int
    amount_out: int
    premium: int
    profit_raw: int        # amount_out - amount_in - premium (can be negative)
    quotes: list[HopQuote]
    method: Literal["closed_form", "golden_section"]
    capped: bool           # hit max_in or the window edge

def aave_premium(amount: int, premium_bps: int = 5) -> int: ...      # (amount*bps + 5000) // 10000
def quote_v2(state: V2State, amount_in: int, zero_for_one: bool) -> HopQuote: ...
def quote_v3(state: V3State, amount_in: int, zero_for_one: bool) -> HopQuote: ...
def quote_hop(state: PoolState, amount_in: int, zero_for_one: bool) -> HopQuote: ...
def quote_route(states: Sequence[PoolState], hops: Sequence[Hop], amount_in: int) -> list[HopQuote]: ...
def marginal_rate(states: Sequence[PoolState], hops: Sequence[Hop]) -> float: ...
def v2_composite(states: Sequence[V2State], hops: Sequence[Hop]) -> tuple[float, float, float]: ...
def optimal_amount(states: Sequence[PoolState], hops: Sequence[Hop], max_in: int,
                   premium_bps: int = 5, iters: int = 64) -> SizeResult: ...
```

`quote_v3` steps through initialized ticks exactly like `UniswapV3Pool.swap` (exact input, price limit at the extreme). Crossing tick *t* adds `liquidityNet[t]` (negated when moving down). Reaching the window edge sets `exhausted = True`.

`marginal_rate` (screen, float): product over hops of (marginal price in the hop direction × (1 − fee)). v2: `Rout/Rin × (1 − fee)`. v3: `P = (sqrtP/2^96)^2`, `P` for 0→1 and `1/P` for 1→0, times `(1 − fee_ppm/1e6)`. A candidate passes the screen only if `marginal_rate > 1 + premium_bps/1e4`. Without this, no size is profitable.

**Optimal size, all-v2 routes (exact formula).** Each v2 hop is the map `f(x) = g·R_out·x / (R_in + g·x)` with `g` the input fee factor. A chain of such maps is again `f(x) = A·x / (B + C·x)`. Start with `A=1, B=1, C=0`. For each hop: `A' = g·R_out·A`, `B' = R_in·B`, `C' = R_in·C + g·A`. Profit is `A·x/(B+C·x) − (1+p)·x` with `p = premium_bps/1e4`. Setting the derivative to zero:

    x* = ( sqrt(A·B/(1+p)) − B ) / C        (no trade if x* <= 0)

Compute `x*` in floats, clamp to `[0, max_in]`, then refine with 20 golden-section steps on the **exact integer** profit inside `[0.9·x*, 1.1·x*]` to absorb rounding. For Aerodrome the fee is taken before the curve, so `g = 1 − fee/10000` exactly; Camelot `g = 1 − f/100000`; Sushi `g = 0.997`.

**Optimal size, any route with a v3 hop (bounded numeric search).** Exact integer profit is concave in `x` (each AMM output is concave, the premium is linear). Golden-section search on integers over `[0, hi]`, 64 iterations, then check `best ± 1..3`. `hi = min(max_in, largest x with no exhausted quote)`; find that edge by doubling from `max_in/1024`. An exhausted quote counts as profit `−∞`.

`max_in` for a loan token = `cfg.max_loan_usd / price(loan token)` in raw units. We assume Aave liquidity is far above our caps (WETH and USDC reserves are millions of dollars); the simulation catches it if not.

### 5.5 `scanner.py`

```python
@dataclass(frozen=True)
class Candidate:
    chain: str
    kind: Literal["two_pool", "triangular"]
    loan_symbol: str
    hops: tuple[Hop, ...]        # amounts are not part of a candidate
    pools: tuple[PoolSpec, ...]
    key: str                     # e.g. "arbitrum WETH uniswap_v3:0xc696..e8d0>sushi_v2:0x57b8..3378"

@dataclass(frozen=True)
class GasModel:
    base_units: int
    v3_hop_units: int
    v2_hop_units: int
    def units(self, hops: Sequence[Hop]) -> int: ...

@dataclass(frozen=True)
class BlockCosts:
    chain: str
    block: int
    base_fee_wei: int
    tip_wei: int                 # from config; Arbitrum default 0 (FCFS ignores tips)
    l1_fee_wei_by_hops: dict[int, int]   # Base: GasPriceOracle; Arbitrum: L1 gas units * base fee
    eth_usd: float
    token_usd: dict[str, float]  # stables = 1.0 (assumption, noted in risk_notes); others from the deepest pool vs WETH/USDC

@dataclass
class ScanResult:
    chain: str
    block: int
    timestamp_ms: int
    candidates: int
    screened_in: int             # passed marginal_rate
    gross_positive: int          # best size has profit_raw > 0 before gas
    opportunities: list[Opportunity]   # net_pnl_usd >= min_profit_usd
    errors: list[str]
    complete: bool               # False if any read failed; never silently "no opportunity"
    duration_s: float

def enumerate_candidates(registry: PoolRegistry, loan_symbols: Sequence[str],
                         max_hops: int = 3, tri_top_k: int = 3) -> list[Candidate]: ...
async def read_block_costs(client: ChainClient, registry: PoolRegistry, snapshot: Snapshot,
                           cfg: "DefiCfg") -> BlockCosts: ...
def evaluate(candidate: Candidate, snapshot: Snapshot, costs: BlockCosts,
             settings: Settings) -> Opportunity | None: ...
async def scan_block(client: ChainClient, registry: PoolRegistry, block: int,
                     settings: Settings) -> ScanResult: ...
```

Candidates:

- **two_pool:** for every pair with ≥2 pools and a loan token in the pair: every ordered pool pair (i, j), i ≠ j. Route: loan → other on pool i, other → loan on pool j.
- **triangular:** loan → A → B → loan, A and B in the chain's token list, the top `tri_top_k` pools by TVL per edge.

L1 data fee:

- **Base:** `GasPriceOracle` predeploy `0x420000000000000000000000000000000000000F`, `getL1FeeUpperBound(uint256)` (`0xf1c7a58b`) with `size = calldata_len + 100`. Calldata length: 548 bytes for 2 hops, 708 for 3, +160 per extra hop. One Multicall per block.
- **Arbitrum:** `NodeInterface` `0x00000000000000000000000000000000000000C8`, `gasEstimateL1Component(address,bool,bytes)` (`0x77d488a2`), called directly with `eth_call` (not through Multicall; it is a virtual contract) once per block per hop count, with a dummy route of that length. `l1_fee_wei = gasEstimateForL1 × base_fee_wei`.

`Opportunity` mapping (reuse `arb/models.py` unchanged):

| Field | Value |
|---|---|
| `strategy` | `"defi_two_pool"` or `"defi_triangular"` |
| `timestamp_ms` | block timestamp × 1000 |
| `key` | `Candidate.key` |
| `legs` | one `Leg` per hop: `exchange=f"{chain}:{dex}"`, `symbol=f"{tokenIn}/{tokenOut}"`, `market="spot"`, `side="sell"`, `price` = out/in in human units, `qty` = amount in, human units |
| `notional_usd` | loan amount × price(loan) |
| `costs_usd` | `{"flash_premium", "swap_fees", "l2_gas", "l1_data", "priority_tip"}` |
| `net_pnl_usd` | `(out − loan − premium)` × price(loan) − `l2_gas` − `l1_data` − `priority_tip` |
| `gross_pnl_usd` | `net_pnl_usd + sum(costs_usd.values())` (= value before any fee or gas) |
| `net_after_tax_usd` | `CostModel(settings).after_tax(net_pnl_usd)` (31.2% on gains) |
| `net_bps` | `net_pnl_usd / notional_usd × 1e4` |
| `capacity_usd` | largest loan (USD) with `net_pnl_usd >= 0`, by bisection on `[x*, max_in]` |
| `holding` | `"instant"` |
| `risk_notes` | e.g. `"stablecoin priced at $1"`, `"v3 window edge reached"`, `"price of ARB from pool 0x…"` |
| `details` | `{chain, block, loan_token, amount_in_raw, amount_out_raw, premium_raw, min_profit_raw, gas_units, base_fee_wei, tip_wei, l1_fee_wei, eth_usd, hops: [{pool, dex, venue, token_in, token_out, zero_for_one, amount_in, amount_out, fee_paid}]}` |

Cost formulas: `l2_gas = gas_units × base_fee_wei × 1e-18 × eth_usd`; `priority_tip = gas_units × tip_wei × 1e-18 × eth_usd`; `swap_fees = Σ fee_paid_i / 10^dec_i × price(tokenIn_i)`; `flash_premium = premium_raw / 10^dec × price(loan)`.

`min_profit_raw` for a real route = `ceil((l2_gas + l1_data + priority_tip + min_profit_usd) / price(loan) × 10^dec)`. In the measurement replay, simulations use `minProfit = 0` and judge success off-chain (section 6).

### 5.6 `simulate.py`

```python
HARNESS_ARTIFACT = Path("contracts/artifacts/src/sim/FlashArbSimHarness.sol/FlashArbSimHarness.json")
FLASHARB_ARTIFACT = Path("contracts/artifacts/src/FlashArb.sol/FlashArb.json")
HARNESS_ARGS_ABI = ["address", "address", "(address,uint256)[]", "(address,uint8,uint16)[]", "bytes"]
HARNESS_RESULT_ABI = ["bool", "uint256", "uint256", "bytes"]
SIM_GAS = 25_000_000

class SimulatorUnavailable(RuntimeError): ...   # artifact missing, or RPC refuses creation eth_call

@dataclass(frozen=True)
class SimRequest:
    chain: str
    block: int | str
    route: Route
    aave_pool: str
    pools: tuple[PoolSpec, ...]          # whitelisted inside the harness
    token_caps: dict[str, int]           # token address -> maxLoan raw (loan token must be present)

@dataclass(frozen=True)
class SimResult:
    chain: str
    block: int | str
    success: bool
    revert_reason: str | None            # "InsufficientProfit(balance=…, required=…)", "Error(…)", "Panic(0x11)", "0x<hex>"
    profit_raw: int
    gas_used_inner: int
    gas_estimate_tx: int                 # inner + 21000 + calldata gas (16/nonzero byte, 4/zero byte)
    gas_cost_usd: float                  # gas_estimate_tx * (base_fee + tip) + l1 fee
    profit_usd: float
    net_usd: float                       # profit_usd - gas_cost_usd
    calldata_hash: str                   # keccak256(executeArb calldata), 0x-hex

def build_execute_arb_calldata(route: Route) -> bytes: ...
def build_harness_initcode(req: SimRequest, calldata: bytes, artifact: Path = HARNESS_ARTIFACT) -> bytes: ...
def decode_revert(data: bytes, abi_path: Path = FLASHARB_ARTIFACT) -> str: ...
async def simulate(client: ChainClient, req: SimRequest, costs: BlockCosts) -> SimResult: ...
```

`simulate` sends `eth_call({from: SIM_CALLER, data: initcode, gas: SIM_GAS}, hex(block))`. The same function works against a Hardhat fork: point the client at `http://127.0.0.1:8545` from `npx hardhat node --fork <rpc> --fork-block-number N`. That is the fallback backend (section 6.5). The harness gets the **same bytes** that `executeArb` would get.

### 5.7 `submit.py`

```python
class SubmissionDisabled(RuntimeError): ...

def submit(*args, **kwargs) -> NoReturn:
    raise SubmissionDisabled("Simulation only: sending transactions is out of scope. "
                             "A new explicit decision by the user is required.")
```

No flag, no environment variable, no code path that sends anything.

### 5.8 Config, storage, report, CLI

`arb/config.py` additions (the `defi` field is optional, so existing settings files still load):

```python
class DefiGasCfg(BaseModel):
    base_units: int = 120_000
    v3_hop_units: int = 120_000
    v2_hop_units: int = 75_000

class DefiChainCfg(BaseModel):
    rpc_url_env: str                     # ARBITRUM_RPC / BASE_RPC (same names as contracts/hardhat.config.js)
    rpc_url_default: str
    max_rps: float = 3.0
    burst: int = 6
    tip_gwei: float = 0.0
    tokens: list[str]
    venues: list[str]
    lateness_blocks: list[int]
    sample_every_s: float = 10.0

class DefiCfg(BaseModel):
    enabled: bool = False
    addresses_file: str = "defi/addresses.json"
    storage_dir: str = "data/defi"
    loan_tokens: list[str] = ["WETH", "USDC"]
    min_pool_tvl_usd: float = 10_000.0
    min_profit_usd: float = 0.05         # net, after gas
    max_loan_usd: float = 50_000.0
    max_hops: int = 3
    tri_top_k: int = 3
    premium_bps: int = 5                 # checked against addresses.json at startup
    gas: DefiGasCfg = DefiGasCfg()
    chains: dict[str, DefiChainCfg]

class Settings(BaseModel):
    ...
    defi: DefiCfg | None = None
```

`config/settings.yaml` addition:

```yaml
defi:
  enabled: true
  addresses_file: defi/addresses.json
  storage_dir: data/defi
  loan_tokens: [WETH, USDC]
  min_pool_tvl_usd: 10000
  min_profit_usd: 0.05
  max_loan_usd: 50000
  max_hops: 3
  tri_top_k: 3
  premium_bps: 5
  gas: {base_units: 120000, v3_hop_units: 120000, v2_hop_units: 75000}   # replaced from contracts/gas-report.json in task T4
  chains:
    arbitrum:
      rpc_url_env: ARBITRUM_RPC
      rpc_url_default: https://arb1.arbitrum.io/rpc
      max_rps: 5
      burst: 10
      tip_gwei: 0.0
      tokens: [WETH, USDC, USDT, DAI, WBTC, ARB]
      venues: [uniswap_v3, sushi_v3, pancake_v3, sushi_v2, camelot_v2]
      lateness_blocks: [0, 2, 8]      # ~0 s, ~0.5 s, ~2 s at 0.257 s/block
      sample_every_s: 10
    base:
      rpc_url_env: BASE_RPC
      rpc_url_default: https://mainnet.base.org
      max_rps: 3
      burst: 6
      tip_gwei: 0.01
      tokens: [WETH, USDC, USDT, DAI, cbBTC, cbETH]
      venues: [uniswap_v3, sushi_v3, pancake_v3, aerodrome_slipstream_initial, aerodrome_slipstream_gaugecaps, aerodrome_slipstream_gaugesv3, sushi_v2, aerodrome_v2]
      lateness_blocks: [0, 1, 2]      # ~0 s, ~2 s, ~4 s
      sample_every_s: 10
```

RPC URLs with keys go only in `.env` (`ARBITRUM_RPC=…`, `BASE_RPC=…`), which `arb/__main__.py` already loads. Never in YAML, never committed.

`arb/defi/store.py`:

```python
class DefiStore(OpportunityStore):
    # base_dir = f"{storage_dir}/{chain}"; reuses _append/_load and the daily JSONL layout
    def append_blocks(self, rows: list[dict]) -> None: ...   # kind "blocks"
    def append_sims(self, rows: list[dict]) -> None: ...     # kind "sims"
    def load_blocks(self, day: date) -> pd.DataFrame: ...
    def load_sims(self, day: date) -> pd.DataFrame: ...
```

Opportunities go through the inherited `append`; each scan cycle calls the inherited `log_scan(ts_ms, counts, errors, duration_s)`, so `arb/report.py` `build_report` works unchanged on `data/defi/<chain>` and prints its existing "missing data is not the same as no edge" warning.

CLI in `arb/__main__.py` (new subcommand group `defi`):

| Command | Does |
|---|---|
| `python -m arb defi probe --chain arbitrum` | Section 6.4 probe; prints the state window and harness status. |
| `python -m arb defi scan --chain base --blocks 5` | Scans the last N blocks, prints opportunities, writes to the store. |
| `python -m arb defi measure --chain arbitrum --hours 24` | Live measurement (section 6). |
| `python -m arb defi report --from 2026-10-25 --to 2026-10-26` | Writes `reports/defi/<from>_<to>.md`. |

### 5.9 Route encoding and golden vectors (both sides must pass)

`arb/defi/route.py`:

```python
class Venue(IntEnum):
    UNI_V3 = 0
    PANCAKE_V3 = 1
    UNI_V2 = 2
    SOLIDLY_V2 = 3

@dataclass(frozen=True)
class Hop:
    pool: str
    token_in: str
    token_out: str
    venue: Venue
    zero_for_one: bool
    def as_abi(self) -> tuple: return (self.pool, self.token_in, self.token_out, int(self.venue), self.zero_for_one)

@dataclass(frozen=True)
class Route:
    loan_token: str
    loan_amount: int
    min_profit: int
    deadline: int
    hops: tuple[Hop, ...]
    def as_abi(self) -> tuple: return (self.loan_token, self.loan_amount, self.min_profit, self.deadline,
                                       [h.as_abi() for h in self.hops])

HOP_ABI = "(address,address,address,uint8,bool)"
ROUTE_ABI = "(address,uint256,uint256,uint64,(address,address,address,uint8,bool)[])"
EXECUTE_ARB_SELECTOR = bytes.fromhex("e77fcf52")

class RouteError(ValueError):
    def __init__(self, code: str, hop: int | None = None): ...   # code = the Solidity error name it predicts

def encode_execute_arb(route: Route) -> bytes:       # EXECUTE_ARB_SELECTOR + eth_abi.encode([ROUTE_ABI], [route.as_abi()])
def encode_flash_params(bal_before: int, route: Route) -> bytes:   # eth_abi.encode(["uint256", ROUTE_ABI], [...])
def route_hash(bal_before: int, route: Route) -> str: # 0x + keccak256(encode_flash_params(...)).hex()
def validate_route(route: Route, registry: PoolRegistry, caps: dict[str, int], now_ts: int) -> None:
    # same checks and order as FlashArb._validateRoute; raises RouteError("PoolTokenMismatch", 1) etc.
```

ABI types match the Solidity struct one to one: `address`→`address`, `uint256`→Python `int`, `uint64 deadline`→`int`, `Venue`→`uint8`, `bool`→`bool`, `Hop[]`→dynamic array of tuples. The struct is dynamic (it holds an array), so the calldata starts with the offset word `0x…20`.

**Golden vector V1** (Arbitrum, two hops):

```
loanToken  = 0x82aF49447D8a07e3bd95BD0d56f35241523fBab1 (WETH)
loanAmount = 1000000000000000000
minProfit  = 100000000000000
deadline   = 1792900000
hops[0]    = (0xC6962004f452bE9203591991D15f6b388e09E8D0, WETH, USDC 0xaf88d065e77c8cC2239327C5EDb3A432268e5831, 0 UNI_V3, true)
hops[1]    = (0x57b85FEf094e10b5eeCDF350Af688299E9553378, USDC, WETH, 2 UNI_V2, false)

calldata (548 bytes):
0xe77fcf52000000000000000000000000000000000000000000000000000000000000002000000000000000000000000082af49447d8a07e3bd95bd0d56f35241523fbab10000000000000000000000000000000000000000000000000de0b6b3a764000000000000000000000000000000000000000000000000000000005af3107a4000000000000000000000000000000000000000000000000000000000006add7ba000000000000000000000000000000000000000000000000000000000000000a00000000000000000000000000000000000000000000000000000000000000002000000000000000000000000c6962004f452be9203591991d15f6b388e09e8d000000000000000000000000082af49447d8a07e3bd95bd0d56f35241523fbab1000000000000000000000000af88d065e77c8cc2239327c5edb3a432268e58310000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000100000000000000000000000057b85fef094e10b5eecdf350af688299e9553378000000000000000000000000af88d065e77c8cc2239327c5edb3a432268e583100000000000000000000000082af49447d8a07e3bd95bd0d56f35241523fbab100000000000000000000000000000000000000000000000000000000000000020000000000000000000000000000000000000000000000000000000000000000
keccak256(calldata)                       = 0xfd08794518b3a2d01555e7e06a3c07442085aa3ea179bf9425aec650880fbe59
keccak256(abi.encode(uint256(0), route))  = 0x1697f7ad346426d962e700b7f783c44c0dc40f51c7d1987d768a0e9087cc5e4f   (576 bytes)
```

**Golden vector V2** (Arbitrum, three hops, venues 0, 3 and 1; encoding only, not a profitable route):

```
loanToken  = 0xaf88d065e77c8cC2239327C5EDb3A432268e5831 (USDC)
loanAmount = 2500000000
minProfit  = 0
deadline   = 1792900000
hops[0]    = (0xb0f6cA40411360c03d41C5fFc5F179b8403CdcF8, USDC, ARB 0x912CE59144191C1204E64559FE8253a0e49E6548, 0 UNI_V3, false)
hops[1]    = (0xa6c5C7D189fA4eB5Af8ba34E63dCDD3a635D433f, ARB, WETH, 3 SOLIDLY_V2, false)
hops[2]    = (0xd9e2a1a61B6E61b275cEc326465d417e52C1b95c, WETH, USDC, 1 PANCAKE_V3, true)

calldata (708 bytes):
0xe77fcf520000000000000000000000000000000000000000000000000000000000000020000000000000000000000000af88d065e77c8cc2239327c5edb3a432268e5831000000000000000000000000000000000000000000000000000000009502f9000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000006add7ba000000000000000000000000000000000000000000000000000000000000000a00000000000000000000000000000000000000000000000000000000000000003000000000000000000000000b0f6ca40411360c03d41c5ffc5f179b8403cdcf8000000000000000000000000af88d065e77c8cc2239327c5edb3a432268e5831000000000000000000000000912ce59144191c1204e64559fe8253a0e49e654800000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000a6c5c7d189fa4eb5af8ba34e63dcdd3a635d433f000000000000000000000000912ce59144191c1204e64559fe8253a0e49e654800000000000000000000000082af49447d8a07e3bd95bd0d56f35241523fbab100000000000000000000000000000000000000000000000000000000000000030000000000000000000000000000000000000000000000000000000000000000000000000000000000000000d9e2a1a61b6e61b275cec326465d417e52c1b95c00000000000000000000000082af49447d8a07e3bd95bd0d56f35241523fbab1000000000000000000000000af88d065e77c8cc2239327c5edb3a432268e583100000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000001
keccak256(calldata)                       = 0x4baf5fda4d2877fb705513ad98acebb3e984325816bb8185b442c0dda606fc9e
keccak256(abi.encode(uint256(0), route))  = 0x07d1edac5b16433bbd716f0663544de7cb05a5699993d09470b1a282353f0a2d   (736 bytes)
```

These vectors were produced with ethers 6.17.0 `AbiCoder` from `contracts/node_modules`. Python test: `tests/test_defi_route.py`. Hardhat test: `contracts/test/RouteEncoding.test.js`. Both hard-code the hex above. Direction check: in V1, WETH (`0x82…`) < USDC (`0xaf…`) so WETH is token0. In V2, ARB (`0x91…`) < USDC and WETH (`0x82…`) < ARB.

### 5.10 Python tests (all offline by default)

| File | Covers |
|---|---|
| `tests/defi_fakes.py` | `FakeTransport` (scripted responses; can return 429, timeouts, "missing trie node", reverts), hand-built `V2State`/`V3State` factories. Values are fixtures, not market data. |
| `tests/test_defi_route.py` | Golden V1/V2; `validate_route` raises the same error name the contract would for each U-05..U-08 case. |
| `tests/test_defi_v3math.py` | `get_sqrt_ratio_at_tick(0) == 2**96`, MIN/MAX tick values, known `compute_swap_step` cases from the Uniswap v3-core test suite, round-trip properties. |
| `tests/test_defi_quote.py` | Each v2 formula against hand-computed integers; premium rounding (`aave_premium(1999) == 1`, `aave_premium(999) == 0`); closed-form size within 1 wei-step of brute force on hand-built 2- and 3-hop v2 routes; golden-section within 0.01% of brute force on v3 states; `exhausted` handling. |
| `tests/test_defi_chain.py` | Rate limiter timing with a fake clock; retry and backoff; every failure recorded in `failures`; error classification. |
| `tests/test_defi_scanner.py` | Known gap → one `Opportunity` with the exact cost breakdown; `gross == net + Σcosts`; marginal screen rejects a no-gap pair; RPC failure → `complete=False` and errors, not an empty "success". |
| `tests/test_defi_simulate.py` | Calldata matches golden; initcode layout; `decode_revert` for every FlashArb error, `Error(string)`, `Panic`; integration test against the real harness skipped unless the artifact exists and `DEFI_LIVE=1`. |
| `tests/test_defi_submit.py` | `submit()` always raises; grep test for signing code in `arb/defi/`. |
| `tests/test_defi_measure.py` | Metric formulas on synthetic block and sim records; episode grouping; report tables render. |
| `tests/test_defi_quote_live.py` | Marked `live`, skipped unless `DEFI_LIVE=1`: local `quote_v3` == Uniswap QuoterV2 `quoteExactInputSingle` (`0xc6a5026a`) exactly, for 20 sizes on 3 Uniswap pools per chain; same against the Slipstream initial-deployment quoter (`0x9e7defe6`, `int24 tickSpacing`). |

Acceptance for the Python task: the existing suite stays green; all new offline tests green; `python -m arb defi scan --chain arbitrum --blocks 1` and `--chain base` run read-only against the public RPC and either write results or exit with a clear data-failure message.

---

## 6. The decision measurement (plan step 5)

### 6.1 Definitions

- **State at block N** = the state after every tx in block N. An `eth_call` at block tag N reads it. It is also the state the first tx of block N+1 would see.
- **Candidate set C(N)** = `enumerate_candidates` over the registry pools that read cleanly at N.
- **Gross gap at N** = a candidate whose optimal size has `profit_raw > 0` (after swap fees and the flash premium, before gas).
- **Surviving gap at N** = a candidate with `net_pnl_usd >= min_profit_usd` at N (after premium, swap fees, L2 gas, L1 fee and the assumed tip). This is the **end-of-block gap test**: if professionals already closed every gap inside block N, nothing survives to its end.
- **Episode** = the same `key` surviving in consecutive samples of the same chain. A bot would try once per episode, so attempts are counted per episode, not per block.
- **Replay** of a surviving gap found at N, at offset k: build the route with the size chosen at N (`Route.min_profit = 0`, `deadline = timestamp(N) + 3600`), and `simulate` it at block tag N+k. k = 0 means "we landed first in block N+1" (best case). k > 0 means "we landed k blocks late". Offsets: Arbitrum {0, 2, 8} (about 0, 0.5, 2 s), Base {0, 1, 2} (about 0, 2, 4 s).
- **Hit** at offset k = the simulation succeeds **and** its `net_usd >= min_profit_usd`, with gas cost priced at block N+k's base fee. A simulation that succeeds with a smaller `net_usd` counts as a **miss**: a real route would carry `minProfit` = gas + threshold (section 5.5) and would have reverted. Its gas counts as miss cost. It is also shown in its own column, "won, below threshold".
- **Miss cost** = `gas_used_inner` of the failed simulation, converted with `gas_estimate_tx` and N+k gas prices. A reverted tx still pays gas on both chains.

### 6.2 Metrics (per chain, per offset)

| Metric | Formula |
|---|---|
| Sampled blocks | count of block records with `complete == true` |
| Data-failure rate | incomplete samples / all samples |
| Gross-gap rate | samples with ≥1 gross gap / sampled blocks |
| Surviving-gap rate | samples with ≥1 surviving gap / sampled blocks |
| Episodes per day | episodes / sampled hours × 24 |
| Persistence(k) | surviving gaps at N whose key also survives (local quote) at N+k / surviving gaps at N |
| Hit rate h(k) | hits at k / replays at k |
| Profit per hit | median and mean `net_usd` of hits |
| Gas per attempt | median `gas_estimate_tx`, and its USD cost |
| Miss cost | mean USD cost of a failed replay |
| EV per attempt(k) | `h(k) · mean(net_usd of hits) − (1 − h(k)) · mean(miss cost)` |
| After-tax EV per attempt(k) | `h(k) · mean(after_tax(net_usd of hits)) − (1 − h(k)) · mean(miss cost)` (losses not set off) |
| Projected after-tax per day(k) | `episodes per day × after-tax EV per attempt(k)` |
| Quote error | `|predicted profit at N − simulated profit at offset 0|` in bps of the loan, median and p90 |

### 6.3 Output tables (`reports/defi/<from>_<to>.md`, built with `arb.report._table`)

1. **Coverage:** chain, mode (live/history), start, end, samples, data-failure rate, RPC errors by type, state window (blocks), sim backend used.
2. **Gap test:** chain, kind (two_pool / triangular), candidates per block (median), screened-in per block, gross-gap rate, surviving-gap rate, episodes/day, median and p90 net USD of surviving gaps, median capacity USD.
3. **Persistence:** chain, k, persistence(k).
4. **Replay:** chain, offset k (blocks and seconds), replays, hits, won-below-threshold, hit rate, median/mean profit per hit, median gas, gas USD, mean miss cost USD, EV/attempt, after-tax EV/attempt, projected after-tax USD/day.
5. **Top 15 keys:** key, episodes, hit rate at k=0 and at the realistic k (Arbitrum 8, Base 1), mean net USD.
6. **Quote accuracy:** chain, venue mix, replays at k=0, median and p90 quote error bps. A median above 1 bp is a bug to fix before reading any other table.
7. **Verdict line** for step 7 (computed, not judged): `GO-TO-NEXT-PHASE` if at the realistic offset the projected after-tax USD/day > $1.00 **and** ≥ 30 episodes were replayed **and** the data-failure rate < 5%; `NO-GO` if those data conditions hold and the projection ≤ $1.00; otherwise `INCONCLUSIVE (DATA)`. The $1/day bar is a suggestion; Opus revisits it in step 7.

Raw records: `data/defi/<chain>/blocks/<day>.jsonl` (one row per sample: `chain, block, timestamp, candidates, screened_in, gross_positive, surviving_keys, best_net_usd, errors, complete`) and `data/defi/<chain>/sims/<day>.jsonl` (one row per replay: `chain, key, block_found, offset, block_sim, success, revert_reason, profit_raw, net_usd, gas_estimate_tx, gas_cost_usd, predicted_net_usd, calldata_hash`).

### 6.4 Sampling plan within public-RPC limits

**Mode: live, recent blocks only.** We do not need archive state. Instead of reading the past, we record the present: every `sample_every_s` (10 s) take `N = head − 1`, read its state right away, and run the replays at N, N+k as soon as those blocks exist (a few seconds later). Every read is at a block a few seconds old, which every full node serves.

Steps:

1. **Probe** (`defi probe`): `eth_chainId`; Multicall3 code; `eth_call` of `WETH.balanceOf(aavePool)` at `head − d` for d in {1, 16, 128, 1024, 10000, 100000, 1 day of blocks}; record the deepest d that answers as the **state window**. Then one harness simulation at `head − 1` of a deliberately unprofitable 2-hop route (`loanAmount` 0.01 WETH). Expected: `success=false` with `InsufficientProfit`. That proves the creation `eth_call` path works end to end.
2. **Run** both chains at the same time (different hosts, separate limiters) for **24 hours**, so US, Europe and Asia hours are covered. Target 8,640 samples per chain. Minimum to report: 2,000 complete samples per chain.
3. **History mode** (optional): only if the probe shows a window ≥ 7 days. Then also sample 2,000 random blocks from the last 7 days, stratified by hour. Report it as a separate row; do not mix it with live data.

Request budget per sample, per chain:

| Step | Requests |
|---|---|
| Header + stage-0 Multicall (≈40–60 pools, ≤100 calls per batch) | 2–3 |
| Arbitrum NodeInterface L1 estimate (or Base oracle in the Multicall) | 0–2 |
| Stage-1 ticks for screened-in v3 pools | 0–4 |
| Replays (3 offsets per surviving gap; usually 0) | 0–3 per gap |

That is about 3–10 requests per 10 s sample, **under 1 request/s on average**. The limiter caps at 5 rps (Arbitrum) and 3 rps (Base). We never call `eth_getLogs` (the earlier Base 429s came from log scans).

Static data (token order, tick spacing, factory checks) is read once into `registry.json`.

### 6.5 When the public RPC refuses

| Symptom | Action |
|---|---|
| HTTP 429 / `-32005` | Back off (section 5.2). If it persists, double `sample_every_s` (up to 60 s) and log it. |
| `StateUnavailable` at N or N+k | Record a data failure for that sample or replay. Never count it as a miss or as "no gap". |
| Creation `eth_call` refused, code-size or gas-cap error | Switch the sim backend to a local Hardhat fork: `http_proxy="$HTTPS_PROXY" npx hardhat node --fork $RPC` (latest block), and point the sim client at `http://127.0.0.1:8545`. Restart the fork every 5 minutes to stay near head. Record the backend in the coverage table. |
| Data-failure rate > 5% over the last 200 samples | Stop the session. Write the report with verdict `INCONCLUSIVE (DATA)` and the error counts. Tell the user they can put a free-tier RPC URL in `.env` as `ARBITRUM_RPC` / `BASE_RPC` (never committed) and re-run. |

---

## 7. Task breakdown

Each task lists the files it creates or changes. No two parallel tasks touch the same file.

| ID | Task | Model | Creates / changes | Depends on | Acceptance |
|---|---|---|---|---|---|
| T0 | Lookups: (a) Slipstream `CLPool.swap` callback name; (b) Camelot v2 pair has 4-arg `swap` and `getAmountOut(uint256,address)`; (c) Multicall3 code at `0xcA11…CA11` on both chains; (d) premium rounding in the deployed Aave Pool version; (e) Arbitrum PGA activation status; (f) Base GasPriceOracle `getL1FeeUpperBound` live. Read-only. | Haiku | `docs/FLASH_ARB_LOOKUPS.md` only | – | Each item answered with source URL or on-chain read and block number, or marked "could not verify". |
| T1 | Contract, harness, mocks, all Hardhat tests (sections 3, 4) | Sonnet | everything under `contracts/src/`, `contracts/test/`, `contracts/gas-report.json` | this doc | Section 4.7. |
| T2 | Python package, config, CLI, tests (section 5) | Sonnet | `arb/defi/*.py`, `arb/config.py`, `config/settings.yaml`, `requirements.txt`, `arb/__main__.py`, `tests/test_defi_*.py`, `tests/defi_fakes.py` | this doc | Section 5.10 acceptance; suite green. |
| T3 | Line-by-line security review of T1 (threat table rows T1–T17, invariants) | **Opus** | review notes in the PR / commit message only | T1 | Every threat row traced to code and a passing test; no unresolved high/medium issue. |
| T4 | Review of T2: v3 math port, sizing formula, cost model, encoding; copy measured gas from `contracts/gas-report.json` into `config/settings.yaml` `defi.gas` | **Opus** (review), Sonnet (edit) | `config/settings.yaml` (gas block only) | T1, T2 | Live quote test (`DEFI_LIVE=1`) exact-match; harness integration test passes on both chains. |
| T5 | Probe + 24 h live measurement on both chains | Sonnet (run) | `data/defi/**` (not committed), `reports/defi/<from>_<to>.md` | T3, T4 | Report has all seven tables; ≥ 2,000 complete samples per chain or verdict `INCONCLUSIVE (DATA)` with reasons. |
| T6 | Security pass: Slither on `contracts/src`, raise `FUZZ_RUNS` to 5,000, fix findings | Sonnet (run), **Opus** (decide) | `contracts/src/FlashArb.sol` fixes only, `docs/FLASH_ARB_SECURITY.md` | T3 | No unreviewed high/medium Slither finding. |
| T7 | Interpret the measurement; go/no-go note with real numbers | **Opus** | `docs/FLASH_ARB_RESULTS.md` | T5, T6 | Clear verdict, numbers from the report, explicit list of what would change it. |
| T8 | README section for `arb defi` commands | Haiku | `README.md` (new section only) | T2 | Commands match `--help`. |

Run T0, T1 and T2 in parallel now. If T0 finds that Slipstream uses a different callback name, T1 adds that third callback (calling the same `_onV3Callback`) and nothing else changes.

---

## 8. What could still go wrong

Honest expectations:

- **Competition is the main risk.** Professional searchers on both chains close most gaps inside the same block. The most likely result of step 5 is: many gross gaps, few surviving gaps, and a low hit rate at the realistic offset. That would be a valid and useful "no-go".
- **Latency.** Arbitrum is first-come, first-served with 0.25 s blocks. Base orders by priority fee with 200 ms Flashblocks. A bot on a remote server that polls a public RPC every 10 s is far slower than the offsets we test. Offset 0 is a best case that we could not actually reach.
- **Tiny pools.** Many listed pools hold a few hundred dollars. Gaps there are real but too small to pay 2–4 cents of gas. The TVL filter and `capacity_usd` make this visible instead of hiding it.
- **Gas is cheap but not free.** At ~$0.03 per attempt, 100 misses cost $3. The miss-cost metric is there so a high gross-gap count does not look like profit.
- **Tax.** Each winning trade is taxed at 31.2%; failed-attempt gas cannot be set off. The after-tax columns apply this per trade.
- **Local quote drift.** If a v3 swap leaves the loaded tick window, the quote is marked exhausted and the size is capped. That can understate rare large gaps. The quote-accuracy table shows how often it happens.
- **Fee changes.** Camelot, Aerodrome and Slipstream fees can change between our read and execution. The contract reads them live, so the risk is a revert, not a loss.

Facts not verified (from `docs/FLASH_LOANS.md` plus this design):

- Slipstream swap callback name (`uniswapV3SwapCallback` assumed). Fork test F-B3 and lookup T0(a) decide it.
- Camelot v2 4-argument `swap` and `getAmountOut(uint256,address)` on the pair. Fork test F-A3 decides it.
- Multicall3 at `0xcA11bde05977b3631167028862bE2a173976CA11` on Arbitrum and Base (well known, but not in `defi/addresses.json`). Checked at startup.
- Aave premium rounding in the exact deployed Pool version. The contract does not depend on it; Python does, and F-A1 checks it.
- Whether public RPCs allow a creation `eth_call` with 25M gas, and how deep their state window is. The probe decides; the Hardhat fork is the fallback.
- Camelot v2 and Algebra addresses have no official source (on-chain match only). Sushi v3 and Pancake v3 routers are unsourced; this design does not need them.
- Which Slipstream factory is current. v1 includes pools from all three, each checked against its own factory.
- Arbitrum PGA (Timeboost replacement) activation status. It only changes `tip_gwei` for Arbitrum.
- Aerodrome stable-pool rounding against the K check. Not used by the v1 scanner.
- Gas numbers in section 3.13 are estimates until `contracts/gas-report.json` exists.
