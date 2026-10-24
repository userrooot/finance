# FlashArb T0 lookups (read-only)

Date of lookups: 2026-10-24. No transactions were sent and no keys were used. All on-chain reads are `eth_call`, `eth_getCode` or `eth_getStorageAt` against the public RPCs `https://arb1.arbitrum.io/rpc` (Arbitrum One, chain 42161) and `https://mainnet.base.org` (Base, chain 8453). The one simulation (item d) uses an `eth_call` state override, not a transaction.

Helper scripts are in `/tmp/claude-0/-home-user-finance/d98af3a4-3da7-56d8-8dfc-05d7da88edec/scratchpad/t0/` (`rpc.py` holds the RPC wrapper and keccak selector helper, computed with pycryptodome).

## Summary

1. (a) Slipstream `CLPool.swap` calls `uniswapV3SwapCallback(int256,int256,bytes)` (selector `0xfa461e33`). No other callback selector is present in the pool code.
2. (b) Camelot v2 pair has both `swap(uint256,uint256,address,bytes)` (`0x022c0d9f`) and `getAmountOut(uint256,address)` (`0xf140a35a`). `getAmountOut` matches the constant-product formula with a 0.30% fee exactly in 4 of 4 cases. The pool is very thin (about 322 USDC on the USDC side).
3. (c) Multicall3 has code on both chains (3808 bytes each, same address).
4. (d) Deployed Aave v3 Pools round the flash-loan premium UP (ceil), per an on-chain simulation: premium for 999 base units is 1 and for 1,000,000,000 is 500,000. Source for ceil is `aave-dao/aave-v3-origin` main; deployed bytecode was not recompiled to confirm the source line by line.
5. (e) PGA is documented as "available for activation" and to activate on Arbitrum One "some time after the onchain vote passes", with Timeboost decommissioned the same day. No activation date is given, and I could not confirm activation on-chain. ArbOS is at version 61 (the prerequisite the docs name for tip collection).
6. (f) Base GasPriceOracle has `getL1FeeUpperBound(uint256)`. For 500 bytes at Base block 53034115 it returns 10,987,691,505 wei. `getL1Fee(bytes)` returns 2,454,782,851 wei for 500 zero bytes.
7. Design impact: the T1 contract needs a third callback only if the pool is Algebra or Pancake style. Slipstream does not need one; the design's "uniswapV3SwapCallback" path is correct for Slipstream.
8. Open items: Camelot official source not fetched, Timeboost docs page not located, PGA activation status unconfirmed on-chain.

## (a) Aerodrome Slipstream CLPool callback

**Answer:** `uniswapV3SwapCallback(int256,int256,bytes)`, selector `0xfa461e33`. Source and bytecode agree.

Source (`aerodrome-finance/slipstream`, `main`, fetched from raw.githubusercontent.com):
- `contracts/core/CLPool.sol` line 679: `function swap(address recipient, bool zeroForOne, int256 amountSpecified, uint160 sqrtPriceLimitX96, bytes calldata data)`. Selector `0x128acb08`.
- `contracts/core/CLPool.sol` lines 859 and 865: `ICLSwapCallback(msg.sender).uniswapV3SwapCallback(amount0, amount1, data);`
- `contracts/core/interfaces/callback/ICLSwapCallback.sol` line 16: `function uniswapV3SwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata data) external;`

Bytecode (block 53033993 for the pool address, block 53034148 for the gauge-caps pool):
- The pools are EIP-1167 minimal proxies (45 bytes). Implementation targets were read from the proxy bytes:
  - `0xb2cc224c1c9fee385f8ad6a55b4d94e92359dc59` (tick 100, initial deployment) and `0xdbc6998296caa1652a810dc8d3baf4a8294330f1` (tick 1, initial) use implementation `0xeC8E5342B19977B4eF8892e02D8DAEcfa1315831`. This matches the README "PoolImplementation" for the initial deployment.
  - `0x3fe04a59ebd38cf06080a6f60a98d124eb59392a` (tick 50, Gauges V3) uses `0xc770898522D2A9c8Da7A10D63989b6b58305B665`. This matches the README Gauges V3 PoolImplementation.
  - `0xc758d81b9b81a6fcdad075bd471874a2c46b54e0` (Gauge Caps) uses `0x942e97a4c6FdC38B4CD1c0298D37d81fDD8E5A16`. This matches the README Gauge Caps PoolImplementation.
- Scanning the PUSH4 operands of each implementation (24,279 bytes each):
  - `fa461e33` (uniswapV3SwapCallback): present in all three implementations.
  - `128acb08` (swap(address,bool,int256,uint160,bytes)): present.
  - `2c8958f6` (algebraSwapCallback): absent.
  - `23a69e75` (pancakeV3SwapCallback): absent.
  - The string "SwapCallback" does not appear in the bytecode at all. The selector scan is the only bytecode evidence, and it is consistent with the source.

Factory used by each pool (`factory()` = `0xc45a0155`, read at the blocks above):
- `0xb2cc224c…`, `0xdbc69982…`: `0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A` (initial PoolFactory). `tickSpacing()` returns 100 and 1 respectively.
- `0x3fe04a59…`: `0xf8f2eB4940CFE7d13603DDDD87f123820Fc061Ef` (Gauges V3 PoolFactory). `tickSpacing()` returns 50.
- `0xc758d81b…`: `0xaDe65c38CD4849aDBA595a4323a8C7DdfE89716a` (Gauge Caps PoolFactory).
- All three factory addresses match the README Deployments tables and the entries in `defi/addresses.json`.

## (b) Camelot v2 pair on Arbitrum

Pair: `0x54b26faf3671677c19f70c4b879a6f7b898f732c` (WETH/USDC, listed in `defi/addresses.json`). Reads at block 517884263.

Bytecode (18,179 bytes, `eth_getCode` at latest):
- `swap(uint256,uint256,address,bytes)` = `0x022c0d9f`: present (PUSH4).
- `getAmountOut(uint256,address)` = `0xf140a35a`: present.
- Also present: `getReserves()`, `token0()`, `token1()`, `stableSwap()`, `token0FeePercent()`, `token1FeePercent()`, `factory()`, `kLast()`, `mint(address)`, `burn(address)`.
- `feePercent()` (`0x7fd6f15c`) is absent. The fee fields are `token0FeePercent` and `token1FeePercent`.

State at block 517884263:
- `token0` = `0x82aF49447D8a07e3bd95BD0d56f35241523fBab1` (WETH). `token1` = `0xaf88d065e77c8cC2239327C5EDb3A432268e5831` (native USDC). Both match `defi/addresses.json`.
- `getReserves()`: reserve0 = 140,105,143,783,130,928 (0.140105 WETH), reserve1 = 322,004,375 (322.004375 USDC).
- `stableSwap()` = false.
- `token0FeePercent()` = 300, `token1FeePercent()` = 300. With denominator 100,000 this is 0.30%. The denominator is inferred, and the formula check below confirms it.
- `factory()` = `0x6EcCab422D763aC031210895C81787E87B43A652`. This matches `defi/addresses.json`.

`getAmountOut` against the formula `out = in*(D-fee)*rOut / (rIn*D + in*(D-fee))` with D = 100000, fee = 300:

| Input | On-chain `getAmountOut` | Formula | Spot out (no fee) |
|---|---|---|---|
| 0.001 WETH (10^15) | 2,275,219 (2.275219 USDC) | 2,275,219 | 2,298,305 |
| 0.1 WETH (10^17) | 133,874,677 (133.874677 USDC) | 133,874,677 | 229,830,516 |
| 1 USDC (10^6) | 432,458,927,928,036 (WETH wei) | 432,458,927,928,036 | 435,103,230,454,961 |
| 100 USDC (10^8) | 33,123,874,598,593,276 | 33,123,874,598,593,276 | 43,510,323,045,496,176 |

The formula matches exactly in all four cases, so the fee is 0.30% and the pair behaves as a constant-product pool. The output is "sensible" in the sense that it matches the formula. The pool is very thin: the 0.1 WETH input moves the price about 42% against spot. The design should not route size through this pair without a liquidity check.

Verification calls: `eth_getCode`; `eth_call` on `token0()`, `token1()`, `getReserves()`, `stableSwap()`, `token0FeePercent()`, `token1FeePercent()`, `factory()`, `getAmountOut(uint256,address)`, each with block tag 517884263 or latest.

Not verified: the Camelot v2 Solidity source was not fetched. The 4-argument swap and the fee fields are verified from bytecode selectors and on-chain behaviour only.

## (c) Multicall3 code on Arbitrum and Base

`0xcA11bde05977b3631167028862bE2a173976CA11`:
- Arbitrum One: code present, 3808 bytes. `getBlockNumber()` (`0x42cbb15c`) returns 26,263,559 at block 517884963. `getChainId()` returns 42161.
- Base: code present, 3808 bytes. `getBlockNumber()` returns 53,034,120 at block 53034120. `getChainId()` returns 8453.

Verification calls: `eth_getCode`, `eth_call` on `getBlockNumber()` and `getChainId()`.

## (d) Aave v3 Pool premium rounding

Proxy and implementation (EIP-1967 slot `0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc`, read with `eth_getStorageAt`):

| Chain | Pool proxy | Block | Implementation | Matches `addresses.json` |
|---|---|---|---|---|
| Arbitrum One | `0x794a61358D6845594F94dc1DB02A252b5b4814aD` | 517884442 | `0xf05fd3cc911b4c5e36e53c00354f645e22922c9a` | yes |
| Base | `0xA238Dd80C259a72e81d7e4664a9801593F98d1c5` | 53034051 | `0xa4abc5fcba6d0d7e3d144d6dbf6cb6128599dfdb` | yes |

On-chain getters (both chains, same values): `FLASHLOAN_PREMIUM_TOTAL()` = 5 (bps, i.e. 0.05%); `FLASHLOAN_PREMIUM_TO_PROTOCOL()` = 10000. The second value is consistent with the v3.4 change that fixed it at 100%.

Source (fetched from GitHub raw):
- `aave-dao/aave-v3-origin` (README says "Aave V3.7 Origin"), `main`:
  - `src/contracts/protocol/libraries/logic/FlashLoanLogic.sol` line 178: `uint256 totalPremium = params.amount.percentMulCeil(params.flashLoanPremium);`
  - `src/contracts/protocol/libraries/math/PercentageMath.sol`: `percentMulCeil` returns `ceil(value * percentage / 10000)`, i.e. `product / 1e4` plus 1 when `product % 1e4 != 0`.
  - The same file also defines `percentMul` (half-up), which the flash-loan path no longer uses.
  - `docs/3.5/Aave-v3.5-features.md` and `docs/3.7/*` describe the rounding changes.
- `aave/aave-v3-core`, `master` (older line): `FlashLoanLogic.sol` line 193: `uint256 totalPremium = params.amount.percentMul(params.flashLoanPremiumTotal);`, with `percentMul` = `(value * pct + 5000) / 10000` (half-up).

Deployed-code evidence:
- Selector-set comparison: the deployed Arbitrum Pool implementation contains 62 of the 65 selectors in the origin `IPool.sol` (PUSH4 scan of `eth_getCode`), including `getVirtualUnderlyingBalance(address)`, `getReserveDeficit(address)`, `syncIndexesState(address)`, `updateFlashloanPremium(uint128)` and `configureEModeCategoryIsolated(uint8,bool)`. It lacks `updateFlashloanPremiums(uint128,uint128)`, which is in the core master `IPool.sol`. This places the deployed Pool on the origin line, not the core master line. Of the three not matched, `setConfiguration` and `configureEModeCategory` take struct parameters that my simple parser did not expand into ABI tuples, so they are a parser artifact. `liquidationCall(address,address,address,uint256,bool)` (`0x00a718a9`) is not found in the implementation bytecode. I did not resolve this, so the deployed-bytecode-to-origin-source mapping is not fully confirmed.
- Direct on-chain premium probe (no transaction): `eth_call` to `flashLoanSimple(receiver, USDC, amount, "", 0)` from an arbitrary EOA, with a state override that gives the receiver address a 10-byte contract: `PUSH1 0x44 CALLDATALOAD PUSH1 0 MSTORE PUSH1 0x20 PUSH1 0 REVERT`. This reverts with the third argument of `executeOperation`, which is the premium.

| Amount (USDC base units) | Arbitrum One premium, block 517884778 | Base premium, block 53034095 | Half-up (`percentMul`) | Ceil (`percentMulCeil`) | Exact |
|---|---|---|---|---|---|
| 1,000,000,000 | 500,000 | 500,000 | 500,000 | 500,000 | 500,000 |
| 999 | 1 | 1 | 0 | 1 | 0.4995 |

The on-chain probe matches the ceil formula in both cases. Half-up would give 0 for 999, so the deployed Pool does not use half-up for the flash-loan premium.

Result: the premium is `ceil(amount * FLASHLOAN_PREMIUM_TOTAL / 10000)`. Verified on-chain for both chains with the 999 and 1,000,000,000 inputs. Verified against the origin source only at the level of the function used; the deployed bytecode was not recompiled, so the mapping from source to bytecode is not proven line by line.

Verification calls: `eth_getStorageAt` (impl slot), `eth_call` for `FLASHLOAN_PREMIUM_TOTAL()` and `FLASHLOAN_PREMIUM_TO_PROTOCOL()`, `eth_getCode` on the implementation, and the `eth_call` with state override described above. `getRevision()` is not public on the Pool and reverts, so the revision was not read.

## (e) Arbitrum Timeboost replacement (PGA)

Official docs, fetched 2026-10-24:
- `https://docs.arbitrum.io/how-arbitrum-works/priority-gas-auction/pga` ("Introduction to PGA (Priority Gas Auctions)")
- `https://docs.arbitrum.io/launch-arbitrum-chain/chain-config/sequencer/pga` ("PGA for Arbitrum chains")
- `https://docs.arbitrum.io/how-arbitrum-works/priority-gas-auction` (index)

Exact wording:
- "PGA is available for activation on supported Arbitrum chains with priority fee collection and a PGA-capable Sequencer build."
- "Arbitrum One has used Timeboost since April 2025."
- "PGA activates on Arbitrum One some time after the onchain vote passes. On the same day, Timeboost's express lane and its delay logic will be decommissioned:" followed by: "The Timeboost autonomous auctioneer stops accepting new bids. The express lane endpoint shuts down. The Sequencer stops enforcing the 200ms delay on transactions outside the express lane."
- "On Arbitrum One, PGA sends 97% of the priority fees it collects to the Arbitrum DAO treasury and 3% to the Arbitrum Developer Guild."
- The sequencer page: "Activating PGA requires enabling tip collection on your chain. Once your chain collects tips, the Sequencer switches to PGA ordering automatically." Step 2 says it "activates PGA immediately" and requires "ArbOS 61 or newer".

Status: documented as planned, activation tied to the onchain vote, no date given. Not confirmed active.

On-chain:
- `ArbSys.arbOSVersion()` (`0x0000…0064`, selector `0x051038f2`) returns 116 at Arbitrum block 517885014. The ArbSys value is 55 plus the ArbOS version, so this is ArbOS 61. That meets the docs' prerequisite.
- A tip-collection flag was not readable: `ArbGasInfo` (`0x…006c`) `getCollectTips()` and `getCollectTipsStatus()` both reverted. Their presence in the precompile is unverified, so activation is not confirmed on-chain.

Timeboost's own docs page was not located: `docs.arbitrum.io/time-boost/...` URLs returned 404, and the docs sitemap (`https://docs.arbitrum.io/sitemap.xml`) lists no Timeboost page. The docs site itself was reachable.

## (f) Base GasPriceOracle

Address `0x420000000000000000000000000000000000000F`, block 53034115. Code: 2055 bytes.

| Call | Result |
|---|---|
| `isFjord()` | 1 |
| `isEcotone()` | 1 |
| `l1BaseFee()` | 256,239,727 wei |
| `baseFeeScalar()` | 2269 |
| `blobBaseFee()` | 14,440,093 |
| `blobBaseFeeScalar()` | 1,055,762 |
| `getL1FeeUpperBound(uint256)` with 500 | 10,987,691,505 wei |
| `getL1Fee(bytes)` with 500 zero bytes | 2,454,782,851 wei |
| `getL1GasUsed(bytes)` with 500 zero bytes | 1600 |

`getL1FeeUpperBound(uint256)` exists and returns a value for 500 bytes. The `getL1Fee`-style functions are also present. The `getL1Fee(bytes)` value is for zero bytes, so it is lower than the upper bound, as expected.

Verification calls: `eth_getCode`, `eth_call` for each selector at block 53034115.

## Items not verified

- Camelot v2 official source: not fetched. Bytecode and on-chain behaviour only.
- Aave: the deployed bytecode was not recompiled from `aave-v3-origin`. The ceil result rests on the on-chain simulation plus the source function. `liquidationCall` selector `0x00a718a9` is absent from the implementation bytecode, which is unexplained and should be checked before relying on the origin source for other Pool behaviour.
- PGA activation on Arbitrum One: not confirmed on-chain. The docs give no date.
- Timeboost docs page: not located.
