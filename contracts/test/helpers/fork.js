// Helpers for fork tests (local Hardhat fork only; nothing is sent to a real network).
const { ethers, network } = require("hardhat");
const fs = require("fs");
const path = require("path");

const ADDRESSES = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "..", "..", "defi", "addresses.json"), "utf8"));
const GAS_REPORT = path.join(__dirname, "..", "..", "gas-report.json");

const ERC20_ABI = [
  "function balanceOf(address) view returns (uint256)",
  "function transfer(address,uint256) returns (bool)",
  "function approve(address,uint256) returns (bool)",
  "function allowance(address,address) view returns (uint256)",
  "function decimals() view returns (uint8)",
];
const WETH_ABI = [...ERC20_ABI, "function deposit() payable"];
const POOL_ABI = [
  "function token0() view returns (address)",
  "function token1() view returns (address)",
  "function getReserves() view returns (uint256,uint256,uint256)",
];
const V2_ABI = ["function getReserves() view returns (uint112,uint112,uint32)", "function token0() view returns (address)", "function token1() view returns (address)"];

function chainKey() {
  return process.env.FORK; // "arbitrum" | "base"
}
function chainCfg(key = chainKey()) {
  return ADDRESSES.chains[key];
}
function tokenAddr(sym, key = chainKey()) {
  const t = chainCfg(key).tokens[sym];
  if (!t) throw new Error(`token ${sym} not in addresses.json for ${key}`);
  return t.address;
}
/** Pool address from addresses.json by the abbreviated form used in the design doc (0xabcd...ef12). */
function poolAddr(prefix, suffix, dex, key = chainKey()) {
  const hits = chainCfg(key).pools.filter(
    (p) => p.pool.toLowerCase().startsWith(prefix.toLowerCase()) && p.pool.toLowerCase().endsWith(suffix.toLowerCase()) && (!dex || p.dex === dex)
  );
  if (hits.length !== 1) throw new Error(`pool ${prefix}..${suffix} (${dex}) matched ${hits.length} entries`);
  return ethers.getAddress(hits[0].pool);
}
function aavePool(key = chainKey()) {
  return chainCfg(key).aave_v3.pool;
}

async function token0Of(pool) {
  return ethers.getAddress(await new ethers.Contract(pool, POOL_ABI, ethers.provider).token0());
}

/** Hop with zeroForOne read from the pool itself. */
async function mkHop(pool, venue, tokenIn, tokenOut) {
  const t0 = await token0Of(pool);
  return { pool, tokenIn, tokenOut, venue, zeroForOne: ethers.getAddress(tokenIn) === t0 };
}

async function wrapEth(signer, weth, amount) {
  const c = new ethers.Contract(weth, WETH_ABI, signer);
  await ethers.provider.send("hardhat_setBalance", [signer.address, "0x" + (10n ** 24n).toString(16)]);
  await (await c.deposit({ value: amount })).wait();
  return c;
}

/** Try each loan amount with a static call and return the one with the highest profit (profit > 0). */
async function bestLoan(arb, build, amounts) {
  let best = null;
  const seen = [];
  for (const a of amounts) {
    try {
      const profit = await arb.executeArb.staticCall(await build(a));
      seen.push([a, profit]);
      if (best === null || profit > best.profit) best = { amount: a, profit };
    } catch (e) {
      seen.push([a, "revert " + (e.shortMessage || e.message).slice(0, 80)]);
    }
  }
  if (!best || best.profit <= 0n) throw new Error("no profitable loan size found: " + JSON.stringify(seen, (k, v) => (typeof v === "bigint" ? v.toString() : v)));
  return best;
}

function recordGas(chain, key, value) {
  let r = {};
  try {
    r = JSON.parse(fs.readFileSync(GAS_REPORT, "utf8"));
  } catch {}
  r[chain] = { two_hop_v3_v2: 0, two_hop_v3_v3: 0, three_hop: 0, revert_insufficient_profit_two_hop: 0, ...(r[chain] || {}) };
  r[chain][key] = Number(value);
  const other = chain === "arbitrum" ? "base" : "arbitrum";
  r[other] = r[other] || { two_hop_v3_v2: 0, two_hop_v3_v3: 0, three_hop: 0, revert_insufficient_profit_two_hop: 0 };
  const ordered = { arbitrum: r.arbitrum, base: r.base };
  fs.writeFileSync(GAS_REPORT, JSON.stringify(ordered, null, 1) + "\n");
}

/** Gas of a transaction that reverts on chain (hardhat throws on revert; the error carries the tx hash). */
async function revertedTxGas(signer, to, data) {
  try {
    await signer.sendTransaction({ to, data, gasLimit: 5_000_000 });
  } catch (e) {
    const hash = e.transactionHash || (e.data && e.data.txHash) || (e.error && e.error.transactionHash);
    if (!hash) throw new Error("revert but no tx hash on error: " + Object.keys(e).join(","));
    const rc = await ethers.provider.getTransactionReceipt(hash);
    return { gas: rc.gasUsed, status: rc.status, error: e };
  }
  throw new Error("expected the transaction to revert");
}

module.exports = { ADDRESSES, ERC20_ABI, WETH_ABI, V2_ABI, chainKey, chainCfg, tokenAddr, poolAddr, aavePool, mkHop, wrapEth, bestLoan, recordGas, revertedTxGas, token0Of };
