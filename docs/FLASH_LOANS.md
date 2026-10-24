# Flash loans on Arbitrum One and Base: verified facts

Scope: read-only research. No transactions were sent. No keys or wallets were used. All on-chain numbers were read at one pinned block per chain. Verification time: Arbitrum block 517872239 (2026-10-24 15:04:08 UTC). Base block 53032610 (2026-10-24 15:09:27 UTC).

Companion data file: `defi/addresses.json`. Every address listed here is in that file.

## What a flash loan is

A flash loan lets you borrow tokens with no collateral.
You must repay the loan, plus any fee, before the transaction ends.
If you do not repay, the whole transaction reverts and nothing happens.
Arbitrage bots use flash loans to trade without holding the capital.

## Fee table (read on-chain)

| Source | Arbitrum One | Base | Notes |
|---|---|---|---|
| Aave v3 `flashLoanSimple` / `flashLoan` premium | 5 bps (0.05%) | 5 bps (0.05%) | Read from `Pool.FLASHLOAN_PREMIUM_TOTAL()` on both chains. |
| Aave v3 premium share to protocol | 10000 bps | 10000 bps | Read from `FLASHLOAN_PREMIUM_TO_PROTOCOL()`. This is the share of the premium sent to the protocol, not an extra fee. The borrower pays the total premium above. |
| Uniswap v3 `flash()` | Pool fee tier | Pool fee tier | The fee equals the pool's fee tier. Tiers verified as live pools: 1, 5, 30, 100 bps. |
| Balancer v2 Vault | 0 bps | 0 bps | `ProtocolFeesCollector.getFlashLoanFeePercentage()` returns 0 on both chains. |
| PancakeSwap v3 `flash()` | Pool fee tier | Pool fee tier | Tiers verified as live pools on Base: 1, 5, 25, 100 bps. Arbitrum was not checked for flash fees. |

Aave flash-loan reserves that are active and have flash loans enabled (read from `getReserveData`):

- Arbitrum: WETH and USDC (native) are active, not frozen, and flash-enabled. USDC.e is active and flash-enabled but reads as frozen on-chain.
- Base: WETH and USDC are active, not frozen, and flash-enabled.
- `flashLoanSimple` selector (`0x42b0b77c`) is present in the Pool implementation bytecode on both chains. This is a bytecode check, not a test transaction.

## Venues and verified pools

### Factories, routers and quoters (checked on-chain)

Every factory has contract code. Every router and quoter that reports `factory()` points back to the same factory.

| Venue | Arbitrum One | Base |
|---|---|---|
| Uniswap v3 | factory `0x1F98431c8aD98523631AE4a59f267346ea31F984`; SwapRouter02 `0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45`; QuoterV2 `0x61fFE014bA17989E743c5F6cB21bF9697530B21e` | factory `0x33128a8fC17869897dcE68Ed026d694621f6FDfD`; SwapRouter02 `0x2626664c2603336E57B271c5C0b26F421741e481`; QuoterV2 `0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a` |
| SushiSwap v2 | factory `0xc35DADB65012eC5796536bD9864eD8773aBc74C4`; router `0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506` | factory `0x71524B4f93c58fcbF659783284E38825f0622859`; router `0x6BDED42c6DA8FBf0d2bA55B2fa120C5e0c8D7891` |
| SushiSwap v3 | factory `0x1af415a1EbA07a4986a52B6f2e7dE7003D82231e`; NPM `0xF0cBce1942A68BEB3d1b73F0dd86C8DCc363eF49`; router not sourced | factory `0xc35DADB65012eC5796536bD9864eD8773aBc74C4`; NPM `0x80C7DD17B01855a6D2347444a0FCC36136a314de`; router not sourced |
| Camelot v2 | factory `0x6EcCab422D763aC031210895C81787E87B43A652`; router `0xc873fEcbd354f5A56E00E710B90EF4201db2448d` (router.factory() matches; source not confirmed) | not on Base |
| Camelot Algebra v3 | factory `0x1a3c9B1d2F0529D97f2afC5136Cc23e58f1FD35B`; router `0x1F721E2E82F6676FCE4eA07A5958cF098D339e18` (router.factory() matches; source not confirmed) | not on Base |
| Aerodrome v2 | not on Arbitrum | pool factory `0x420DD381b31aEf6683db6B902084cB0FFECe40Da`; router `0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43` |
| Aerodrome Slipstream (CL) | not on Arbitrum | three deployments, all with live pools: initial `0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A`; gauge caps `0xaDe65c38CD4849aDBA595a4323a8C7DdfE89716a`; gauges v3 `0xf8f2eB4940CFE7d13603DDDD87f123820Fc061Ef`. Quoter and router addresses per deployment are in `defi/addresses.json`. |
| PancakeSwap v3 | factory `0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865`; deployer `0x41ff9AA7e16B8B1a8a8dc4f0eFacd93D02d071c9` | same factory and deployer |

### Top 5 pairs with two or more venues

Ranking: number of venues with live liquidity, then number of live pools. Liquidity is read on-chain. `liquidity()` is a raw unit on Uniswap-style pools, so it is not comparable across tiers. Token balances in the pool are shown in token units. Multiply by price to compare.

#### Arbitrum One

**WETH/USDC**: 6 venues, 15 live pools

| DEX | Tier | Pool | Liquidity / reserves (token units) |
|---|---|---|---|
| camelot_algebra_v3 | dynamic fee | `0xb1026b8e7276e7ac75410f1fcbbe21796e8f7526` | liquidity() raw 5,254,139,703,411,523 WETH: 89.3429; USDC: 29,157.1457 |
| camelot_v2 | v2 | `0x54b26faf3671677c19f70c4b879a6f7b898f732c` | WETH: 0.1384; USDC: 325.9719 |
| pancake_v3 | fee 1 bps | `0x7fcdc35463e3770c2fb992716cd070b63540b947` | liquidity() raw 19,291,913,405,661,025 WETH: 131.1314; USDC: 80,064.8336 |
| pancake_v3 | fee 5 bps | `0xd9e2a1a61b6e61b275cec326465d417e52c1b95c` | liquidity() raw 61,474,955,463,857,002 WETH: 247.1415; USDC: 224,698.8959 |
| pancake_v3 | fee 25 bps | `0xb3d494d53aac707c469318f15cfd5d7857a61388` | liquidity() raw 988,132,630,642 WETH: 0.0118; USDC: 39.0249 |
| pancake_v3 | fee 100 bps | `0xbedb464ef776fe208e3f2b5c0bafbe7983ef49db` | liquidity() raw 218,296,498,430 WETH: 0.0620; USDC: 148.7717 |
| sushi_v2 | v2 | `0x57b85fef094e10b5eecdf350af688299e9553378` | WETH: 4.1333; USDC: 9,661.7380 |
| sushi_v3 | fee 1 bps | `0xb658ee5c63922d2852f24458effa2bfa2cba3574` | liquidity() raw 24,694,703,722 WETH: 0.0067; USDC: 10.4484 |
| sushi_v3 | fee 5 bps | `0xf3eb87c1f6020982173c908e7eb31aa66c1f0296` | liquidity() raw 36,212,889,438,507,725 WETH: 87.4298; USDC: 95,576.8006 |
| sushi_v3 | fee 30 bps | `0xc96525298419f7e00da8826b733ee52e271662b5` | liquidity() raw 1,030,124,718,212 WETH: 0.0190; USDC: 133.5514 |
| sushi_v3 | fee 100 bps | `0x0ed17087175a677c05328a37b9795728e9cd8821` | liquidity() raw 20,255,031,421 WETH: 0.0026; USDC: 0.9697 |
| uniswap_v3 | fee 1 bps | `0x6f38e884725a116c9c7fbf208e79fe8828a2595f` | liquidity() raw 7,030,424,331,818,249 WETH: 50.1737; USDC: 27,609.5849 |
| uniswap_v3 | fee 5 bps | `0xc6962004f452be9203591991d15f6b388e09e8d0` | liquidity() raw 3,434,874,917,588,943,546 WETH: 11,251.2971; USDC: 12,243,551.7740 |
| uniswap_v3 | fee 30 bps | `0xc473e2aee3441bf9240be85eb122abb059a3b57c` | liquidity() raw 626,904,471,293,595,757 WETH: 1,234.1917; USDC: 2,303,582.2576 |
| uniswap_v3 | fee 100 bps | `0x42fc852a750ba93d5bf772ecdc857e87a86403a9` | liquidity() raw 15,982,202,424,563,409 WETH: 38.4334; USDC: 52,713.6110 |

**WETH/USDT**: 6 venues, 14 live pools

| DEX | Tier | Pool | Liquidity / reserves (token units) |
|---|---|---|---|
| camelot_algebra_v3 | dynamic fee | `0x7cccba38e2d959fe135e79aebb57ccb27b128358` | liquidity() raw 559,497,049,887,180 WETH: 6.0688; USDT: 4,000.4646 |
| camelot_v2 | v2 | `0x97b192198d164c2a1834295e302b713bc32c8f1d` | WETH: 0.2107; USDT: 496.2796 |
| pancake_v3 | fee 1 bps | `0x389938cf14be379217570d8e4619e51fbdafaa21` | liquidity() raw 4,486,592,917,098,317 WETH: 24.8978; USDT: 17,187.1601 |
| pancake_v3 | fee 5 bps | `0x0bacc7a9717e70ea0da5ac075889bd87d4c81197` | liquidity() raw 12,445,627,789,108,806 WETH: 59.3072; USDT: 52,140.0877 |
| pancake_v3 | fee 25 bps | `0xaeab1336efc2bf74bc89fcf0e33e6013f3c8587e` | liquidity() raw 242,311,068,620,168 WETH: 0.9214; USDT: 788.1858 |
| sushi_v2 | v2 | `0xcb0e5bfa72bbb4d16ab5aa0c60601c438f04b4ad` | WETH: 7.4344; USDT: 17,388.0218 |
| sushi_v3 | fee 1 bps | `0x824d9bc425c599f46c700afdcae0867596f48ce2` | liquidity() raw 26,432,040,034 WETH: 0.0008; USDT: 10.5892 |
| sushi_v3 | fee 5 bps | `0x96ada81328abce21939a51d971a63077e16db26e` | liquidity() raw 1,030,542,878,036,670 WETH: 9.7624; USDT: 5,661.6906 |
| sushi_v3 | fee 30 bps | `0x92d543a8a158a6bc2c7018ae17803819cb9150b2` | liquidity() raw 1,041,856,064,115,049 WETH: 5.1731; USDT: 17,813.6039 |
| sushi_v3 | fee 100 bps | `0x7f9061b253545103b80698e579c9891718057dfc` | liquidity() raw 201,415,314,210 WETH: 0.0043; USDT: 10.5251 |
| uniswap_v3 | fee 1 bps | `0x42161084d0672e1d3f26a9b53e653be2084ff19c` | liquidity() raw 217,077,704,445,577 WETH: 2.2371; USDT: 2,516.7965 |
| uniswap_v3 | fee 5 bps | `0x641c00a822e8b671738d32a431a4fb6074e5c79d` | liquidity() raw 223,127,116,712,429,369 WETH: 1,771.5273; USDT: 1,468,900.6407 |
| uniswap_v3 | fee 30 bps | `0xc82819f72a9e77e2c0c3a69b3196478f44303cf4` | liquidity() raw 21,301,806,617,427,297 WETH: 68.4428; USDT: 148,133.0515 |
| uniswap_v3 | fee 100 bps | `0x58039203442c9f2a45d5536bd021a383c7f3035c` | liquidity() raw 117,571,096,237,528 WETH: 4.2341; USDT: 3,336.1912 |

**WETH/ARB**: 6 venues, 13 live pools

| DEX | Tier | Pool | Liquidity / reserves (token units) |
|---|---|---|---|
| camelot_algebra_v3 | dynamic fee | `0xe51635ae8136abac44906a8f230c2d235e9c195f` | liquidity() raw 5,117,361,816,042,048,332,203 WETH: 4.6625; ARB: 279,152.5026 |
| camelot_v2 | v2 | `0xa6c5c7d189fa4eb5af8ba34e63dcdd3a635d433f` | WETH: 8.0589; ARB: 113,154.6262 |
| pancake_v3 | fee 1 bps | `0x11d53ec50bc8f54b9357fbfe2a7de034fc00f8b3` | liquidity() raw 269,388,715,826,067,448,680 WETH: 0.1558; ARB: 67,866.5669 |
| pancake_v3 | fee 5 bps | `0x0d7c4b40018969f81750d0a164c3839a77353efb` | liquidity() raw 75,255,265,190,091,309,389 WETH: 0.4490; ARB: 7,475.1374 |
| sushi_v2 | v2 | `0xbf6cbb1f40a542af50839cad01b0dc1747f11e18` | WETH: 5.4026; ARB: 75,792.5297 |
| sushi_v3 | fee 1 bps | `0x011cc642ae74315001eeb541c1d2d225e92ece3d` | liquidity() raw 162,071,240,990,707,895 WETH: 0.0027; ARB: 323.6033 |
| sushi_v3 | fee 5 bps | `0x99543bf98ca1830aa20d3eb12c1b9962f8eadc11` | liquidity() raw 64,293,924,204,928,231,244 WETH: 0.1356; ARB: 2,359.0680 |
| sushi_v3 | fee 30 bps | `0xb3942c9ffa04efbc1fa746e146be7565c76e3dc1` | liquidity() raw 345,674,318,054,575,772,051 WETH: 1.2333; ARB: 37,174.8979 |
| sushi_v3 | fee 100 bps | `0xb1674e700f0dcff2f7b4bfc68cb6d9dea7ddbb2e` | liquidity() raw 49,944,446,389,217,387 WETH: 0.0005; ARB: 11.0575 |
| uniswap_v3 | fee 1 bps | `0x89a4026e9ade251c67b7fb38054931a39936d9c5` | liquidity() raw 16,066,355,083,444,004,561 WETH: 0.1474; ARB: 3,508.3605 |
| uniswap_v3 | fee 5 bps | `0xc6f780497a95e246eb9449f5e4770916dcd6396a` | liquidity() raw 284,379,146,625,363,070,123,980 WETH: 246.7408; ARB: 7,228,660.5484 |
| uniswap_v3 | fee 30 bps | `0x92c63d0e701caae670c9415d91c474f686298f00` | liquidity() raw 9,018,237,150,106,240,710,025 WETH: 18.3303; ARB: 665,219.5313 |
| uniswap_v3 | fee 100 bps | `0x92fd143a8fa0c84e016c2765648b9733b0aa519e` | liquidity() raw 5,277,503,552,733,284,889,799 WETH: 19.7688; ARB: 331,783.1542 |

**WETH/USDC.e**: 6 venues, 13 live pools

| DEX | Tier | Pool | Liquidity / reserves (token units) |
|---|---|---|---|
| camelot_algebra_v3 | dynamic fee | `0x521aa84ab3fcc4c05cabac24dc3682339887b126` | liquidity() raw 159,096,375,302,725 WETH: 0.6929; USDC.e: 1,356.6344 |
| camelot_v2 | v2 | `0x84652bb2539513baf36e225c930fdd8eaa63ce27` | WETH: 32.4322; USDC.e: 75,765.1109 |
| pancake_v3 | fee 1 bps | `0x4fc7ac44dae553464d92c049175133239a8705c1` | liquidity() raw 57,654,019,254,211 WETH: 0.1208; USDC.e: 89.6132 |
| pancake_v3 | fee 5 bps | `0x0e8fcea6153205e3a3a184b0613578c3d1729eab` | liquidity() raw 114,152,714 WETH: 0.0004; USDC.e: 11.3293 |
| pancake_v3 | fee 25 bps | `0x38e81e616fa532201e4c7ff9953a9b40cc842f39` | liquidity() raw 566,742,159,801 WETH: 0.0008; USDC.e: 0.8319 |
| sushi_v2 | v2 | `0x905dfcd5649217c42684f23958568e533c711aa3` | WETH: 35.3314; USDC.e: 82,463.0187 |
| sushi_v3 | fee 1 bps | `0x5db80967733bcdcb31f0b03ffce86bf30482c82d` | liquidity() raw 490,174,707,905 WETH: 0.0126; USDC.e: 35.4256 |
| sushi_v3 | fee 5 bps | `0x15e444da5b343c5a0931f5d3e85d158d1efc3d40` | liquidity() raw 176,162,773,896,447 WETH: 0.4087; USDC.e: 1,059.1057 |
| sushi_v3 | fee 30 bps | `0x4d1576158518dd61924218446c1057cf03138d57` | liquidity() raw 16,159,096,363,406,526 WETH: 83.5073; USDC.e: 277,295.5524 |
| uniswap_v3 | fee 1 bps | `0xe754841b77c874135caca3386676e886459c2d61` | liquidity() raw 6,675,154,702,775 WETH: 0.1497; USDC.e: 688.0799 |
| uniswap_v3 | fee 5 bps | `0xc31e54c7a869b9fcbecc14363cf510d1c41fa443` | liquidity() raw 23,167,505,366,292,379 WETH: 260.0325; USDC.e: 368,103.3016 |
| uniswap_v3 | fee 30 bps | `0x17c14d2c404d167802b16c450d3c99f88f2c4f4d` | liquidity() raw 11,836,345,525,289,275 WETH: 45.2310; USDC.e: 156,272.7402 |
| uniswap_v3 | fee 100 bps | `0x7e5e4a3f855f19cc1a45b9eff1c8b2419036ce85` | liquidity() raw 53,294,301,298,439 WETH: 0.6305; USDC.e: 2,010.7520 |

**USDC/ARB**: 6 venues, 12 live pools

| DEX | Tier | Pool | Liquidity / reserves (token units) |
|---|---|---|---|
| camelot_algebra_v3 | dynamic fee | `0xfae2ae0a9f87fd35b5b0e24b47bac796a7eefea1` | liquidity() raw 42,480,301,034,091,141 USDC: 8,448.8307; ARB: 154,222.1255 |
| camelot_v2 | v2 | `0x138c115bdcc8709d7f782ecc29dfb3829907d74b` | USDC: 25.6500; ARB: 150.7933 |
| pancake_v3 | fee 1 bps | `0x93cce474015007b38da0ecea96671ee4dc3d40ad` | liquidity() raw 1,097,548,663,595,572 USDC: 541.3095; ARB: 19,716.1369 |
| pancake_v3 | fee 5 bps | `0x9ffca51d23ac7f7df82da414865ef1055e5afcc3` | liquidity() raw 99,839,300,081,867,359 USDC: 7,487.1103; ARB: 400,747.0832 |
| sushi_v2 | v2 | `0x1f42712540f04478ea8fbb5a27d850a1cb1884f6` | USDC: 21.6237; ARB: 131.0405 |
| sushi_v3 | fee 5 bps | `0xfa1cc0cae7779b214b1112322a2d1cf0b511c3bc` | liquidity() raw 2,653,397,642,558 USDC: 22.0942; ARB: 5,783.8333 |
| sushi_v3 | fee 30 bps | `0x14716a16ef9eeaada7e266bcf023b71d2c9adbf3` | liquidity() raw 174,064,849,056,948 USDC: 71.4819; ARB: 427.5834 |
| sushi_v3 | fee 100 bps | `0x4ea5b77307349147dcf47c2d86fbad178679265f` | liquidity() raw 90,540,688 USDC: 0.3256; ARB: 3.8692 |
| uniswap_v3 | fee 1 bps | `0x616a2a065bfe53da48e83e7d709fb428aa3c9f5b` | liquidity() raw 52,446,464,655,060 USDC: 32.2899; ARB: 2,882.2312 |
| uniswap_v3 | fee 5 bps | `0xb0f6ca40411360c03d41c5ffc5f179b8403cdcf8` | liquidity() raw 69,708,083,125,078,824 USDC: 7,294.1125; ARB: 618,827.8422 |
| uniswap_v3 | fee 30 bps | `0xaebdca1bc8d89177ebe2308d62af5e74885dccc3` | liquidity() raw 882,494,283,937,303,890 USDC: 41,699.3697; ARB: 924,993.3047 |
| uniswap_v3 | fee 100 bps | `0xac9a19e85a49bacc28bd2deecab3cdfadbfc3e00` | liquidity() raw 863,429,545,354,197 USDC: 3,373.0159; ARB: 38,075.2546 |

#### Base

**WETH/USDC**: 8 venues, 23 live pools

| DEX | Tier | Pool | Liquidity / reserves (token units) |
|---|---|---|---|
| aerodrome_slipstream_gaugecaps | tick spacing 50 | `0xc758d81b9b81a6fcdad075bd471874a2c46b54e0` | liquidity() raw 218,825,836,965,101 WETH: 0.4498; USDC: 2,013.4800 |
| aerodrome_slipstream_gaugesv3 | tick spacing 1 | `0x4e392fbfe4d0557c82d2f97f02ec39daa31516dd` | liquidity() raw 5,796,546,582,425,731 WETH: 23.4180; USDC: 9,494.7715 |
| aerodrome_slipstream_gaugesv3 | tick spacing 50 | `0x3fe04a59ebd38cf06080a6f60a98d124eb59392a` | liquidity() raw 298,079,724,020,585,989 WETH: 1,421.5216; USDC: 673,895.8781 |
| aerodrome_slipstream_initial | tick spacing 1 | `0xdbc6998296caa1652a810dc8d3baf4a8294330f1` | liquidity() raw 3,949,481,104,061,068 WETH: 48.1957; USDC: 7,839.6878 |
| aerodrome_slipstream_initial | tick spacing 50 | `0xaad23a67f2ac693abbe543489aeb3f24f561d517` | liquidity() raw 851,510,925,743,604 WETH: 0.2979; USDC: 193.9076 |
| aerodrome_slipstream_initial | tick spacing 100 | `0xb2cc224c1c9fee385f8ad6a55b4d94e92359dc59` | liquidity() raw 515,225,547,291,118,191 WETH: 1,928.9438; USDC: 932,901.8674 |
| aerodrome_slipstream_initial | tick spacing 200 | `0x148bc43946a902258916e580b0e6d92aaa74746f` | liquidity() raw 84,925,590,951,931 WETH: 0.1636; USDC: 84.6776 |
| aerodrome_slipstream_initial | tick spacing 2000 | `0x0652202c4b2d09cb93aedefadc14b36869483a98` | liquidity() raw 151,119,833,640,086 WETH: 3.2136; USDC: 1,734.1332 |
| aerodrome_v2 | volatile | `0xcdac0d6c6c59727a65f871236188350531885c43` | WETH: 816.0636; USDC: 1,897,039.0227 |
| aerodrome_v2 | stable | `0x3548029694fbb241d45fb24ba0cd9c9d4e745f16` | WETH: 2.1368; USDC: 14,873.8784 |
| pancake_v3 | fee 1 bps | `0x72ab388e2e2f6facef59e3c3fa2c4e29011c2d38` | liquidity() raw 323,842,155,844,541,529 WETH: 1,579.1432; USDC: 776,211.3002 |
| pancake_v3 | fee 5 bps | `0xb775272e537cc670c65dc852908ad47015244eaf` | liquidity() raw 103,149,876,078,433,385 WETH: 422.9329; USDC: 270,948.2911 |
| pancake_v3 | fee 25 bps | `0xe9d76696f8a35e2e2520e3125875c3af23f1e69c` | liquidity() raw 823,340,556,057,834 WETH: 2.8269; USDC: 4,113.4216 |
| pancake_v3 | fee 100 bps | `0x118efef7db0712f03e067122b8f89e7a4b79262f` | liquidity() raw 10,093,730,895,564 WETH: 0.1291; USDC: 241.7416 |
| sushi_v2 | v2 | `0x2f8818d1b0f3e3e295440c1c0cddf40aaa21fa87` | WETH: 0.7365; USDC: 1,712.8399 |
| sushi_v3 | fee 1 bps | `0x482fe995c4a52bc79271ab29a53591363ee30a89` | liquidity() raw 16,941,215,787,437 WETH: 0.3439; USDC: 1,857.5254 |
| sushi_v3 | fee 5 bps | `0x57713f7716e0b0f65ec116912f834e49805480d2` | liquidity() raw 8,434,441,590,430,665 WETH: 23.6679; USDC: 28,796.4813 |
| sushi_v3 | fee 30 bps | `0x41595326aabe6132fc6c7ae71af087a3a9dbc9f6` | liquidity() raw 292,969,483,741,872 WETH: 2.8591; USDC: 4,178.2229 |
| sushi_v3 | fee 100 bps | `0x6fa08690355261b5360b2302bbf78f63e2b014f5` | liquidity() raw 527,150,191,113 WETH: 0.0105; USDC: 9.1263 |
| uniswap_v3 | fee 1 bps | `0xb4cb800910b228ed3d0834cf79d697127bbb00e5` | liquidity() raw 38,308,449,453,911,891 WETH: 179.1992; USDC: 104,972.3785 |
| uniswap_v3 | fee 5 bps | `0xd0b53d9277642d899df5c87a3966a349a798f224` | liquidity() raw 759,370,167,387,645,868 WETH: 2,778.3247; USDC: 1,989,026.1006 |
| uniswap_v3 | fee 30 bps | `0x6c561b446416e1a00e8e93e221854d6ea4171372` | liquidity() raw 20,447,392,299,788,767,797 WETH: 53,958.5328; USDC: 27,950,628.4360 |
| uniswap_v3 | fee 100 bps | `0x0b1c2dcbbfa744ebd3fc17ff1a96a1e1eb4b2d69` | liquidity() raw 68,369,072,146,364,804 WETH: 109.5229; USDC: 525,432.6274 |

**WETH/cbBTC**: 8 venues, 16 live pools

| DEX | Tier | Pool | Liquidity / reserves (token units) |
|---|---|---|---|
| aerodrome_slipstream_gaugecaps | tick spacing 200 | `0x04cb591bb235edea40eb6b993d94d51df9983927` | liquidity() raw 1,175,913,969,749 WETH: 0.1097; cbBTC: 0.0029 |
| aerodrome_slipstream_gaugesv3 | tick spacing 1 | `0x7c7420dd105e2779316423ba3e973f434315efa9` | liquidity() raw 15,223,330,440,123,128 WETH: 775.8764; cbBTC: 3.7102 |
| aerodrome_slipstream_initial | tick spacing 1 | `0x22aee3699b6a0fed71490c103bd4e5f3309891d5` | liquidity() raw 609,261,394,376,726 WETH: 23.3480; cbBTC: 0.3290 |
| aerodrome_slipstream_initial | tick spacing 100 | `0x70acdf2ad0bf2402c957154f944c19ef4e1cbae1` | liquidity() raw 39,314,531,708,263,804 WETH: 2,120.9494; cbBTC: 37.8069 |
| aerodrome_v2 | volatile | `0x2578365b3dfa7ffe60108e181efb79feddec2319` | WETH: 138.0520; cbBTC: 4.0223 |
| pancake_v3 | fee 1 bps | `0xc211e1f853a898bd1302385ccde55f33a8c4b3f3` | liquidity() raw 34,895,712,082,028,504 WETH: 1,838.4521; cbBTC: 13.4097 |
| pancake_v3 | fee 5 bps | `0xd974d59e30054cf1abeded0c9947b0d8baf90029` | liquidity() raw 1,590,862,363,304,543 WETH: 141.0763; cbBTC: 0.7382 |
| pancake_v3 | fee 25 bps | `0x5b3613ef9a535b48e82e2800acb77053dfec93b1` | liquidity() raw 836,717,440,133,516 WETH: 25.0954; cbBTC: 0.5107 |
| pancake_v3 | fee 100 bps | `0xa97c08bc49c246e583a3557f2922daa9e5d1b404` | liquidity() raw 62,642,265,359 WETH: 0.0032; cbBTC: 0.0000 |
| sushi_v2 | v2 | `0x2963c3e74e865b8684edab7e78aeb0d0bc13256e` | WETH: 0.0001; cbBTC: 0.0000 |
| sushi_v3 | fee 5 bps | `0x358228caaf6c235cdf982bb99e919f6e1028905b` | liquidity() raw 38,324,280,535 WETH: 0.0235; cbBTC: 0.0008 |
| sushi_v3 | fee 30 bps | `0x99dc44b6b406491cad1dd60aaf8992c498d4d5ba` | liquidity() raw 27,386,543,550 WETH: 0.0163; cbBTC: 0.0005 |
| uniswap_v3 | fee 1 bps | `0x4c1e404a013bf1917faf2dbf5e5cef3e9bafb5aa` | liquidity() raw 598,976,519,724 WETH: 0.1033; cbBTC: 0.0013 |
| uniswap_v3 | fee 5 bps | `0x7aea2e8a3843516afa07293a10ac8e49906dabd1` | liquidity() raw 264,933,017,561,982,319 WETH: 6,190.2348; cbBTC: 31.1686 |
| uniswap_v3 | fee 30 bps | `0x8c7080564b5a792a33ef2fd473fba6364d5495e5` | liquidity() raw 101,961,509,143,741,451 WETH: 2,929.8692; cbBTC: 41.3366 |
| uniswap_v3 | fee 100 bps | `0x233571eae953d9d59eefc0769f71dc19287ddccb` | liquidity() raw 661,608,989,063,730 WETH: 29.0675; cbBTC: 0.5553 |

**USDC/cbBTC**: 6 venues, 15 live pools

| DEX | Tier | Pool | Liquidity / reserves (token units) |
|---|---|---|---|
| aerodrome_slipstream_gaugecaps | tick spacing 1 | `0x95dada6bc214a86af204250f7b6ff873b52e0289` | liquidity() raw 551,450,143 USDC: 3,264.9860; cbBTC: 0.0341 |
| aerodrome_slipstream_gaugecaps | tick spacing 200 | `0x4cf105267d8088698f9668059bc94d8948c03bdf` | liquidity() raw 150,540,810 USDC: 399.4587; cbBTC: 0.0000 |
| aerodrome_slipstream_gaugesv3 | tick spacing 1 | `0x9d14ff91ae2c6e3d1a760542248b6c7f206894b0` | liquidity() raw 12,519,609,478 USDC: 13,090.4049; cbBTC: 0.2482 |
| aerodrome_slipstream_gaugesv3 | tick spacing 50 | `0x160d7e9d948b16c163332a277b393c288408eb12` | liquidity() raw 956,121,499,473 USDC: 1,183,366.3871; cbBTC: 16.9993 |
| aerodrome_slipstream_initial | tick spacing 1 | `0x4f5905e36ac07ee1f01ffb939aa7f212a58d5cdf` | liquidity() raw 3,426,764,069 USDC: 8,112.9688; cbBTC: 0.0667 |
| aerodrome_slipstream_initial | tick spacing 100 | `0x4e962bb3889bf030368f56810a9c96b83cb3e778` | liquidity() raw 1,007,012,750,901 USDC: 1,687,504.8177; cbBTC: 39.1704 |
| aerodrome_slipstream_initial | tick spacing 2000 | `0x3e66e55e97ce60096f74b7c475e8249f2d31a9fb` | liquidity() raw 998,166,585,978 USDC: 3,694,712.2272; cbBTC: 32.3781 |
| aerodrome_v2 | volatile | `0x9c38b55f9a9aba91bbcedeb12bf4428f47a6a0b8` | USDC: 126,536.5703; cbBTC: 1.5821 |
| pancake_v3 | fee 1 bps | `0xb94b22332abf5f89877a14cc88f2abc48c34b3df` | liquidity() raw 405,955,353,204 USDC: 569,320.9110; cbBTC: 14.5204 |
| pancake_v3 | fee 5 bps | `0x26e263efdc91f0d3279e2ec2bd58a7ca5c2fce62` | liquidity() raw 24,711,052,080 USDC: 48,366.8648; cbBTC: 1.7285 |
| pancake_v3 | fee 25 bps | `0x748bb91fc5f4200302d9a79185bc161260a1b461` | liquidity() raw 2,666,691,886 USDC: 12,809.1664; cbBTC: 0.2326 |
| uniswap_v3 | fee 1 bps | `0xe9e25e35aa99a2a60155010802b81a25c45ba185` | liquidity() raw 1,868,412 USDC: 116.6621; cbBTC: 0.0047 |
| uniswap_v3 | fee 5 bps | `0xfbb6eed8e7aa03b138556eedaf5d271a5e1e43ef` | liquidity() raw 2,237,940,196,232 USDC: 4,564,328.7593; cbBTC: 62.1361 |
| uniswap_v3 | fee 30 bps | `0xec558e484cc9f2210714e345298fdc53b253c27d` | liquidity() raw 575,800,274,914 USDC: 917,277.9705; cbBTC: 12.3599 |
| uniswap_v3 | fee 100 bps | `0x3e7586d52a9d07f8611b8ecf6ccc8a689c34a659` | liquidity() raw 1,000,733,240 USDC: 2,075.9336; cbBTC: 0.0174 |

**USDC/DAI**: 6 venues, 12 live pools

| DEX | Tier | Pool | Liquidity / reserves (token units) |
|---|---|---|---|
| aerodrome_slipstream_gaugecaps | tick spacing 1 | `0x4a20660ec636c69af94cb4b251f6055f4bff40ec` | liquidity() raw 208,470,633,005,394,095 USDC: 48.0831; DAI: 5.0789 |
| aerodrome_v2 | volatile | `0x4f5f4b7381b2445937dab831fc1952ecbb834e53` | USDC: 4.7075; DAI: 4.6926 |
| aerodrome_v2 | stable | `0x67b00b46fa4f4f24c03855c5c8013c0b938b3eec` | USDC: 2,640.2246; DAI: 2,369.5476 |
| pancake_v3 | fee 1 bps | `0x345825a980bd94e1480bc4f20fe4e3dae2f23dd3` | liquidity() raw 1,071,431,451,705,123,135 USDC: 5,048.0241; DAI: 4,613.4172 |
| pancake_v3 | fee 5 bps | `0x486d1e5e5f97f44ea8db217598c1e61e21f54084` | liquidity() raw 136,228,233,583,937,618 USDC: 66.3889; DAI: 3.9189 |
| sushi_v2 | v2 | `0xf12084a6393b01c1bf7490cf2430cfd5d390ba5d` | USDC: 4.3461; DAI: 4.3432 |
| sushi_v3 | fee 1 bps | `0x3bd740dc10864f35001aab7771c56a85c48c901b` | liquidity() raw 3,766,853,870,169,371,741 USDC: 2,421.2765; DAI: 1,385.9593 |
| sushi_v3 | fee 5 bps | `0x7b0167b54294f8fe6b14aaf70cc934d0d4f3f8db` | liquidity() raw 89,995,380,471 USDC: 0.1063; DAI: 0.1062 |
| sushi_v3 | fee 30 bps | `0xf336d4b2cae611133bb72ba6f68f73d12b160a48` | liquidity() raw 271,094,537,358,816 USDC: 1.5552; DAI: 1.1850 |
| uniswap_v3 | fee 1 bps | `0xc18f50d6a832f12f6dcaaeee8d0c87a65b96787e` | liquidity() raw 281,387,112,345,529,503,967 USDC: 7,978.4313; DAI: 9,331.8972 |
| uniswap_v3 | fee 5 bps | `0x19a8b1542b807cd6a76fcbb5ff5f53c6169f36d7` | liquidity() raw 336,477,451,049,404,318 USDC: 322.3425; DAI: 38.2369 |
| uniswap_v3 | fee 30 bps | `0xe63174bc0ee37b76d28956b680af6ba4c508f48a` | liquidity() raw 196,281,880,899,295 USDC: 9.1158; DAI: 9.5123 |

**WETH/cbETH**: 6 venues, 12 live pools

| DEX | Tier | Pool | Liquidity / reserves (token units) |
|---|---|---|---|
| aerodrome_slipstream_initial | tick spacing 1 | `0x47ca96ea59c13f72745928887f84c9f52c3d7348` | liquidity() raw 2,316,269,040,209,510,971,823 WETH: 131.6964; cbETH: 3.0734 |
| aerodrome_v2 | volatile | `0x44ecc644449fc3a9858d2007caa8cfaa4c561f91` | WETH: 22.7689; cbETH: 19.9892 |
| aerodrome_v2 | stable | `0x9e8bfeb5c73f3f897bebdb49cc4161fece0b0c55` | WETH: 0.9165; cbETH: 0.3882 |
| pancake_v3 | fee 1 bps | `0x257fcbae4ac6b26a02e4fc5e1a11e4174b5ce395` | liquidity() raw 8,364,029,826,879,123,242,749 WETH: 33.5664; cbETH: 3.4020 |
| pancake_v3 | fee 5 bps | `0xc0efc182479319ff258eca420e2647cd82d3790c` | liquidity() raw 5,613,178,706,663,976,358 WETH: 5.3382; cbETH: 0.1549 |
| pancake_v3 | fee 100 bps | `0x98d2e8e18de100ff872eaa60392d580146a9d606` | liquidity() raw 3,527,740,261,467,882 WETH: 0.0013; cbETH: 0.0011 |
| sushi_v2 | v2 | `0xc8d3aa9182c29a0bd8cf71da3e6c2a35c05bbdbb` | WETH: 0.0000; cbETH: 0.0000 |
| sushi_v3 | fee 5 bps | `0xb81b9aef36c76d740850c31c45697c83468dcb54` | liquidity() raw 1,590,563,574,793,109,752,473 WETH: 1.4617; cbETH: 0.0627 |
| uniswap_v3 | fee 1 bps | `0xa9dafa443a02fbc907cb0093276b3e6f4ef02a46` | liquidity() raw 1,360,689,509,579,478,587,748 WETH: 12.5856; cbETH: 3.0725 |
| uniswap_v3 | fee 5 bps | `0x10648ba41b8565907cfa1496765fa4d95390aa0d` | liquidity() raw 914,273,038,739,337,988,203 WETH: 16.5177; cbETH: 3.9577 |
| uniswap_v3 | fee 30 bps | `0x7b9636266734270de5be02544c04e27046903ff8` | liquidity() raw 2,009,051,818,126,710,664 WETH: 0.7454; cbETH: 0.5201 |
| uniswap_v3 | fee 100 bps | `0x6fb974dcea795d10ade524bcc3fc9e3e9d29ea87` | liquidity() raw 22,342,871,407,808,828 WETH: 0.0092; cbETH: 0.0073 |

Pools that exist but have zero liquidity are in `defi/addresses.json` (`pools` list, `liquidity_raw` = 0).

## Gas cost: measured, not estimated

Method: scan recent blocks for transactions to SwapRouter02. Keep only receipts with exactly one Uniswap-style `Swap` event (a single-pool swap). Read `gasUsed` and `effectiveGasPrice` from the receipt. Base also adds `l1Fee`. Arbitrum's `gasUsed` already includes the L1 part, per the Arbitrum doc on `gasUsedForL1`. ETH price is the Chainlink ETH/USD feed read at the same block as each swap.

| Chain | Samples | Median gas used | Median gas price | Median total fee (USD) | Min / max (USD) | Notes |
|---|---|---|---|---|---|---|
| Arbitrum One | 11 single-hop swaps (blocks 517872678-517873707) | 133,543 | 0.0300 gwei | $0.0094 | $0.0059 / $0.0114 | ETH at about 2,315-2,325 USD. The L1 part is `gasUsedForL1` of about 90-150 units. |
| Base | 10 single-hop swaps (blocks 53033333-53033339) | 161,460 | 0.0226 gwei | $0.0084 | $0.0022 / $0.3275 | One sample at 0.42 gwei gives the max. The L1 `l1Fee` is about 1.3e-8 ETH (about $0.00003). ETH at about 2,295 USD. |

Base block time measured: 2.0 s average over 300 blocks. Arbitrum block time measured: 0.257 s average over 300 blocks.

Gas numbers cover a simple single-pool swap only. A flash-loan arbitrage tx does more work: a flash loan, two or more swaps, and a repayment. Expect a higher gas figure for the full arbitrage.

## Sequencer and ordering facts

### Arbitrum One

- The sequencer's default policy is first-come, first-served (FCFS). Arrival time sets the order. Source: https://docs.arbitrum.io/how-arbitrum-works/sequencer
- Timeboost (since April 2025) sold a 60-second express lane in a sealed-bid auction. Other transactions were delayed by 200 ms. Source: https://docs.arbitrum.io/how-arbitrum-works/timeboost/
- The current Arbitrum doc says Timeboost is being sunset. Priority Gas Auctions (PGA) replace it on Arbitrum One after an on-chain vote. The doc says PGA is available for activation. I did not confirm whether it has been activated. So do not assume the express lane is active. Check the vote status before relying on it. Source: https://docs.arbitrum.io/how-arbitrum-works/timeboost/

### Base

- Transactions are ordered by priority fee and arrival time. Blocks are built with priority-fee auctions every 200 ms (Flashblocks). Once a Flashblock is built and broadcast, its ordering is locked. Source: https://docs.base.org/base-chain/flashblocks/overview
- Base block time is 2 s (measured). Flashblocks give preconfirmations every 200 ms.

## Balancer v2 Vault note

- The Vault is at `0xBA12222222228d8Ba445958a75a0704d566BF2C8`. It has contract code on both chains. Arbitrum is listed in the official npm deployments package `@balancer-labs/v2-deployments` (`dist/addresses/arbitrum.json`). Base is not in that package, so Base is on-chain only.
- The Balancer v2 docs state that flash-loan fees are zero. Source: https://docs-v2.balancer.fi/reference/contracts/flash-loans.html
- Exploit notice: Balancer's post-mortem for the Nov 3, 2025 exploit is on Medium (https://medium.com/balancer-protocol/nov-3-exploit-post-mortem-51dcbeb6b020). The search summary says it is dated Nov 18, 2025. I could not open that page from this environment. I know its existence and title from a search summary only. That summary says the exploit hit Balancer v2 Composable Stable pools and that v3 was not affected. It also says Arbitrum and Base held Balancer value, but it did not list affected Arbitrum or Base pools. Loss figures differ between sources.
- The v2 flash-loan docs I fetched do not mention the exploit. The v3 flash-loan page (https://docs.balancer.fi/concepts/vault/flash-loans.html) does not mention it either.
- Not recommended by this research. Only the facts above are reported.

## Unverified or not confirmed

- Camelot v2 and Algebra factory and router addresses: on-chain only. The router's `factory()` matches the factory, but I did not find an official source. The Camelot docs pages render without addresses in static HTML.
- SushiSwap v3 router address (both chains): not in the Sushi SDK and not checked.
- Balancer v2 Vault on Base as an official deployment: on-chain code only.
- PancakeSwap v3 router and quoter: not sourced. Only the factory and deployer are listed.
- Base USDT and DAI: addresses come from the Uniswap default token list. Their symbol and decimals were read on-chain. They are not Aave reserves.
- WBTC, ARB, cbBTC and cbETH USD prices were not read, so pool values for those tokens are in token units only.
- Arbitrum USDC.e reserve reads as frozen on-chain. Whether frozen reserves allow flash loans was not tested with a transaction.
- Which Aerodrome Slipstream factory is current was not determined. All three have live WETH/USDC pools.
- Public RPCs were rate-limited. Base log scans returned HTTP 429, so Base gas samples come from a direct block scan.
- Gas figures are from small samples (11 Arbitrum and 10 Base single-pool swaps), not from a full-day study.

## How we verified

- Aave addresses: `npm pack @bgd-labs/aave-address-book@4.44.22`, then read `src/AaveV3Arbitrum.sol` and `src/AaveV3Base.sol`.
- DEX addresses: `docs.uniswap.org/contracts/v3/reference/deployments/{arbitrum,base}-deployments` (HTML), `npm pack @sushiswap/v2-sdk@0.0.11 @sushiswap/v3-sdk@1.0.12 @pancakeswap/v3-sdk@3.10.3`, `raw.githubusercontent.com/aerodrome-finance/contracts/main/README.md`, and `raw.githubusercontent.com/aerodrome-finance/slipstream/main/README.md`.
- Token addresses: Aave book plus `npm pack @uniswap/default-token-list`.
- On-chain (JSON-RPC `eth_call`, `eth_getCode`, `eth_getStorageAt`, `eth_getLogs`, `eth_getTransactionReceipt`), with all reads pinned to the same block: `Pool.FLASHLOAN_PREMIUM_TOTAL()`, `FLASHLOAN_PREMIUM_TO_PROTOCOL()`, `ADDRESSES_PROVIDER()`, `PoolAddressesProvider.getPool()`, `getReserveData(address)`, EIP-1967 implementation slot `0x3608...bbc`, `symbol()`, `decimals()`, `factory()` on routers and quoters, `getPool(address,address,uint24|int24|bool)`, `getPair`, `poolByPair`, `liquidity()`, `getReserves()`, `balanceOf(pool)`, Chainlink `description()`, `decimals()`, `latestRoundData()`, `getProtocolFeesCollector()`, `getFlashLoanFeePercentage()`.
- Gas: `eth_getBlockByNumber`, `eth_getTransactionReceipt`, `eth_call` at the swap's block for the Chainlink ETH/USD feed.
- RPC endpoints: `https://arb1.arbitrum.io/rpc` (chain 42161) and `https://mainnet.base.org` (chain 8453). Both returned matching `eth_chainId`.
- Scripts and raw outputs are in the scratchpad (`verify_results.json`, `gas_results.json`, `gas_base_results.json`).
