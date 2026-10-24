// Shared fixtures for the local (mock) test suites. TEST ONLY.
const { ethers } = require("hardhat");

const E18 = 10n ** 18n;
const VENUE = { UNI_V3: 0, PANCAKE_V3: 1, UNI_V2: 2, SOLIDLY_V2: 3 };
const MAX_UINT = 2n ** 256n - 1n;

async function deployToken(kind, name) {
  switch (kind) {
    case "fot":
      return (await ethers.getContractFactory("MockFeeOnTransferToken")).deploy(name, name);
    case "hook":
      return (await ethers.getContractFactory("MockHookToken")).deploy(name, name);
    case "blacklist":
      return (await ethers.getContractFactory("MockBlacklistToken")).deploy(name, name);
    case "rebase":
      return (await ethers.getContractFactory("MockRebasingToken")).deploy();
    default:
      return (await ethers.getContractFactory("MockERC20")).deploy(name, name, 18);
  }
}

/**
 * Builds: Aave mock, tokens A/B/C, FlashArb (Exposed unless ownerIsWrapper), and these whitelisted pools
 *   p1  UNI_V3     (A,B)  A->B gives 2.0 B per A
 *   p2  PANCAKE_V3 (A,B)  A->B gives 1.8 B per A, so B->A gives 0.5555 A per B
 *   p3  UNI_V3     (B,C)  1:1
 *   p4  UNI_V3     (A,C)  A->C gives 1.8 C per A
 *   v2ab UNI_V2 (A,B) 1000A/2000B, feeBps 30
 *   v2bc UNI_V2 (B,C) 100000/100000
 *   sol  SOLIDLY_V2 (A,C) 100000A/150000C
 * plus q1 (A,B) and q2 (A,C), never whitelisted.
 * Route A->B(p1)->A(p2) is profitable by ~11%.
 */
async function deployWorld(opts = {}) {
  const kinds = { A: "plain", B: "plain", C: "plain", ...(opts.kinds || {}) };
  const [owner, attacker, other, ...rest] = await ethers.getSigners();
  const aave = await (await ethers.getContractFactory("MockAavePool")).deploy();
  const A = await deployToken(kinds.A, "A");
  const B = await deployToken(kinds.B, "B");
  const C = await deployToken(kinds.C, "C");
  const tokens = { A, B, C };
  const addr = {};
  for (const [k, t] of Object.entries(tokens)) addr[k] = await t.getAddress();

  let arb, wrapper;
  if (opts.ownerIsWrapper) {
    wrapper = await (await ethers.getContractFactory("OwnerWrapper")).deploy(await aave.getAddress());
    arb = (await ethers.getContractFactory("FlashArb")).attach(await wrapper.arb());
  } else {
    arb = await (await ethers.getContractFactory("FlashArbExposed")).deploy(await aave.getAddress(), owner.address);
  }
  const cfg = {
    setToken: (t, allowed, cap) => (wrapper ? wrapper.setToken(t, allowed, cap) : arb.connect(owner).setToken(t, allowed, cap)),
    setPool: (p, venue, fee, en) => (wrapper ? wrapper.setPool(p, venue, fee, en) : arb.connect(owner).setPool(p, venue, fee, en)),
    executeArb: (r) => (wrapper ? wrapper.executeArb(r) : arb.connect(owner).executeArb(r)),
    sweep: (t, a) => (wrapper ? wrapper.sweep(t, a) : arb.connect(owner).sweep(t, a)),
  };
  const arbAddr = await arb.getAddress();
  const aaveAddr = await aave.getAddress();
  const ownerAddr = wrapper ? await wrapper.getAddress() : owner.address;

  const BIG = 10n ** 30n;
  const cap = 1000n * E18;
  for (const k of ["A", "B", "C"]) {
    await cfg.setToken(addr[k], true, cap);
    await tokens[k].mint(aaveAddr, BIG);
  }

  const mkV3 = async (t0, t1, pancake, num, den) => {
    const p = await (await ethers.getContractFactory("MockV3Pool")).deploy(addr[t0], addr[t1], pancake);
    await p.setRate(num, den);
    await tokens[t0].mint(await p.getAddress(), BIG);
    await tokens[t1].mint(await p.getAddress(), BIG);
    return { c: p, address: await p.getAddress(), t0: addr[t0], t1: addr[t1], venue: pancake ? VENUE.PANCAKE_V3 : VENUE.UNI_V3, feeBps: 0, num, den, kind: "v3" };
  };
  const mkV2 = async (t0, t1, r0, r1) => {
    const p = await (await ethers.getContractFactory("MockV2Pair")).deploy(addr[t0], addr[t1]);
    await tokens[t0].mint(await p.getAddress(), r0);
    await tokens[t1].mint(await p.getAddress(), r1);
    await p.sync();
    return { c: p, address: await p.getAddress(), t0: addr[t0], t1: addr[t1], venue: VENUE.UNI_V2, feeBps: 30, kind: "v2" };
  };
  const mkSol = async (t0, t1, r0, r1) => {
    const p = await (await ethers.getContractFactory("MockSolidlyPair")).deploy(addr[t0], addr[t1]);
    await tokens[t0].mint(await p.getAddress(), r0);
    await tokens[t1].mint(await p.getAddress(), r1);
    await p.sync();
    return { c: p, address: await p.getAddress(), t0: addr[t0], t1: addr[t1], venue: VENUE.SOLIDLY_V2, feeBps: 0, kind: "sol" };
  };

  const P = {
    p1: await mkV3("A", "B", false, 2n, 1n),
    p2: await mkV3("A", "B", true, 18n, 10n),
    p3: await mkV3("B", "C", false, 1n, 1n),
    p4: await mkV3("A", "C", false, 18n, 10n),
    v2ab: await mkV2("A", "B", 1000n * E18, 2000n * E18),
    v2bc: await mkV2("B", "C", 100000n * E18, 100000n * E18),
    sol: await mkSol("A", "C", 100000n * E18, 150000n * E18),
  };
  const Q = {
    q1: await mkV3("A", "B", false, 2n, 1n),
    q2: await mkV3("A", "C", false, 18n, 10n),
  };
  for (const p of Object.values(P)) await cfg.setPool(p.address, p.venue, p.feeBps, true);

  const hop = (pool, tin, tout) => ({
    pool: pool.address,
    tokenIn: addr[tin],
    tokenOut: addr[tout],
    venue: pool.venue,
    zeroForOne: addr[tin].toLowerCase() === pool.t0.toLowerCase(),
  });
  const route = async (loan, amount, hops, minProfit = 0n, deadline) => ({
    loanToken: addr[loan],
    loanAmount: amount,
    minProfit,
    deadline: deadline ?? BigInt((await ethers.provider.getBlock("latest")).timestamp) + 10000n,
    hops,
  });
  const premium = (a) => (a * 5n + 5000n) / 10000n;
  const allPools = { ...P, ...Q };
  return { owner, attacker, other, rest, aave, aaveAddr, tokens, addr, arb, arbAddr, wrapper, cfg, ownerAddr, P, Q, allPools, hop, route, premium, cap, BIG };
}

/** Everything the invariants talk about, as plain strings, to compare before/after. */
async function snapshotState(w) {
  const out = { bal: {}, allow: {}, storage: [], getters: {} };
  const holders = { arb: w.arbAddr, owner: w.ownerAddr, aave: w.aaveAddr };
  for (const [n, p] of Object.entries(w.allPools)) holders[n] = p.address;
  for (const [tk, t] of Object.entries(w.tokens)) {
    for (const [hn, ha] of Object.entries(holders)) out.bal[`${tk}.${hn}`] = (await t.balanceOf(ha)).toString();
    const spenders = { aave: w.aaveAddr };
    for (const [n, p] of Object.entries(w.allPools)) spenders[n] = p.address;
    for (const [sn, sa] of Object.entries(spenders)) out.allow[`${tk}.${sn}`] = (await t.allowance(w.arbAddr, sa)).toString();
  }
  for (let i = 0; i < 12; i++) out.storage.push(await ethers.provider.getStorage(w.arbAddr, i));
  if (w.arb.routeHash) {
    out.getters = {
      routeHash: await w.arb.routeHash(),
      pendingPool: await w.arb.pendingPool(),
      pendingMaxPay: (await w.arb.pendingMaxPay()).toString(),
      lastPremium: (await w.arb.lastPremium()).toString(),
    };
  }
  return out;
}

function decodeErr(arb, data) {
  const e = arb.interface.parseError(data);
  return e ? e.name : data.slice(0, 10);
}

// exact reference formulas ---------------------------------------------------------------
const v2Out = (a, rIn, rOut, feeBps = 30n) => {
  const g = a * (10000n - feeBps);
  return (g * rOut) / (rIn * 10000n + g);
};
const solOut = (a, rIn, rOut, feeBps = 30n) => {
  const a2 = a - (a * feeBps) / 10000n;
  return (a2 * rOut) / (rIn + a2);
};

module.exports = { deployWorld, snapshotState, decodeErr, v2Out, solOut, E18, VENUE, MAX_UINT, deployToken };
