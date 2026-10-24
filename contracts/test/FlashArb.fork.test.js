// Fork tests (section 4.5). Run with FORK=arbitrum or FORK=base on a LOCAL Hardhat fork. Nothing is sent to a real
// network: every transaction below is executed by Hardhat's in-process chain on top of forked public state.
const { expect } = require("chai");
const { ethers } = require("hardhat");
const { takeSnapshot } = require("@nomicfoundation/hardhat-network-helpers");
const { anyValue } = require("@nomicfoundation/hardhat-chai-matchers/withArgs");
const F = require("./helpers/fork");
const { VENUE } = require("./helpers/world");

const CHAIN = process.env.FORK;
const describeFork = CHAIN ? describe : describe.skip;
const ceilPremium = (a) => (a * 5n + 9999n) / 10000n; // Aave v3 rounds the flash premium UP (verified on both chains)

// Pools by the abbreviated addresses of the design doc, resolved against defi/addresses.json.
const POOLS = {
  arbitrum: {
    quote: "USDC", base: "WETH", mid: "ARB",
    v2: ["0x57b8", "3378", "sushi_v2"], // Sushi v2 WETH/USDC (thin) - dislocated
    uni: ["0xc696", "e8d0", "uniswap_v3"], // Uniswap v3 WETH/USDC 5 bps
    cake: ["0xd9e2", "1b95c", "pancake_v3"], // Pancake v3 WETH/USDC 5 bps
    uniQuoteMid: ["0xb0f6", "dcf8", "uniswap_v3"], // Uniswap v3 USDC/ARB 5 bps
    uniBaseMid: ["0xc6f7", "396a", "uniswap_v3"], // Uniswap v3 WETH/ARB 5 bps
    swapOnce: [
      { id: "F-A3 Camelot v2 WETH/ARB", pool: ["0xa6c5", "d433f", "camelot_v2"], venue: VENUE.SOLIDLY_V2, a: "WETH", b: "ARB", amountA: ethers.parseEther("0.05") },
    ],
    loanV2: 100n * 10n ** 6n,
    wethToSell: 200n,
  },
  base: {
    quote: "USDC", base: "WETH", mid: "cbBTC",
    v2: ["0x2f88", "fa87", "sushi_v2"],
    uni: ["0xd0b5", "f224", "uniswap_v3"],
    cake: ["0xb775", "4eaf", "pancake_v3"], // Pancake v3 WETH/USDC 5 bps (270k USDC) - pumped
    uniQuoteMid: ["0xfbb6", "43ef", "uniswap_v3"], // Uniswap v3 USDC/cbBTC 5 bps
    uniBaseMid: ["0x7aea", "bd1", "uniswap_v3"], // Uniswap v3 WETH/cbBTC 5 bps
    swapOnce: [
      { id: "F-B2 Aerodrome v2 volatile WETH/USDC", pool: ["0xcdac", "5c43", "aerodrome_v2"], venue: VENUE.SOLIDLY_V2, a: "WETH", b: "USDC", amountA: ethers.parseEther("0.05") },
      { id: "F-B3 Slipstream (tick spacing 100) WETH/USDC", pool: ["0xb2cc", "dc59", "aerodrome_slipstream_initial"], venue: VENUE.UNI_V3, a: "WETH", b: "USDC", amountA: ethers.parseEther("0.05") },
      { id: "F-B4 Pancake v3 1 bps WETH/USDC", pool: ["0x72ab", "2d38", "pancake_v3"], venue: VENUE.PANCAKE_V3, a: "WETH", b: "USDC", amountA: ethers.parseEther("0.05") },
    ],
    loanV2: 100n * 10n ** 6n,
    wethToSell: 200n,
  },
};

describeFork(`FlashArb fork (${CHAIN})`, function () {
  this.timeout(900000);
  const cfg = POOLS[CHAIN] || { swapOnce: [] };
  let owner, arb, arbAddr, swapper, swapperAddr, snap;
  let T, P; // tokens, pools
  let weth, usdc;

  const pool = (spec) => F.poolAddr(spec[0], spec[1], spec[2]);
  const erc = (sym) => new ethers.Contract(T[sym], F.ERC20_ABI, owner);
  const v2Info = async (addr) => {
    const c = new ethers.Contract(addr, F.V2_ABI, ethers.provider);
    const [r0, r1] = await c.getReserves();
    return { r0, r1, t0: ethers.getAddress(await c.token0()) };
  };

  async function fundSwapper(sym, amount) {
    await (await erc(sym).transfer(swapperAddr, amount)).wait();
  }
  async function getWethToSwapper(amount) {
    const w = await F.wrapEth(owner, T.WETH, amount);
    await (await w.transfer(swapperAddr, amount)).wait();
  }
  async function sellV3(poolAddr, tokenIn, amount) {
    const zfo = (await F.token0Of(poolAddr)) === ethers.getAddress(tokenIn);
    return swapper.v3Swap.staticCall(poolAddr, zfo, amount).then(async (out) => {
      await (await swapper.v3Swap(poolAddr, zfo, amount)).wait();
      return out;
    });
  }

  before(async function () {
    [owner] = await ethers.getSigners();
    // Hardhat reports chainId 31337 on forks; check we really forked the right chain by the Aave pool code
    expect((await ethers.provider.getCode(F.aavePool())).length).to.be.gt(100);
    T = { WETH: F.tokenAddr("WETH"), USDC: F.tokenAddr("USDC"), [cfg.mid]: F.tokenAddr(cfg.mid) };
    P = {
      v2: pool(cfg.v2), uni: pool(cfg.uni), cake: pool(cfg.cake),
      uniQM: pool(cfg.uniQuoteMid), uniBM: pool(cfg.uniBaseMid),
    };
    swapper = await (await ethers.getContractFactory("ForkSwapper")).deploy();
    swapperAddr = await swapper.getAddress();
    arb = await (await ethers.getContractFactory("FlashArbExposed")).deploy(F.aavePool(), owner.address);
    arbAddr = await arb.getAddress();
    await arb.setToken(T.WETH, true, ethers.parseEther("1000"));
    await arb.setToken(T.USDC, true, 5_000_000n * 10n ** 6n);
    await arb.setToken(T[cfg.mid], true, 0n);
    await arb.setPool(P.v2, VENUE.UNI_V2, 30, true);
    await arb.setPool(P.uni, VENUE.UNI_V3, 0, true);
    await arb.setPool(P.cake, VENUE.PANCAKE_V3, 0, true);
    await arb.setPool(P.uniQM, VENUE.UNI_V3, 0, true);
    await arb.setPool(P.uniBM, VENUE.UNI_V3, 0, true);
    console.log(`      forked ${CHAIN} at block ${await ethers.provider.getBlockNumber()}`);
  });
  beforeEach(async () => {
    snap = await takeSnapshot();
  });
  afterEach(async () => {
    await snap.restore();
  });

  const deadline = async () => BigInt((await ethers.provider.getBlock("latest")).timestamp) + 3600n;
  const mkRoute = async (loanSym, amount, hops, minProfit = 0n) => ({
    loanToken: T[loanSym], loanAmount: amount, minProfit, deadline: await deadline(), hops,
  });

  /** Sell all the WETH reserve of the thin v2 pair into it, so WETH is cheap there. */
  async function dislocateV2() {
    const { r0, r1, t0 } = await v2Info(P.v2);
    const wethIs0 = t0 === ethers.getAddress(T.WETH);
    const wethReserve = wethIs0 ? r0 : r1;
    await getWethToSwapper(wethReserve + 10n ** 15n);
    await (await swapper.v2Swap(P.v2, wethIs0, wethReserve, 30)).wait();
    const after = await v2Info(P.v2);
    return wethIs0 ? after.r1 : after.r0; // USDC reserve left
  }

  const v2Route = async (amount, minProfit = 0n) =>
    mkRoute("USDC", amount, [await F.mkHop(P.v2, VENUE.UNI_V2, T.USDC, T.WETH), await F.mkHop(P.uni, VENUE.UNI_V3, T.WETH, T.USDC)], minProfit);

  async function executeAndCheck(route) {
    const loan = route.loanToken;
    const tok = new ethers.Contract(loan, F.ERC20_ABI, owner);
    const watched = [T.WETH, T.USDC, T[cfg.mid]].map((a) => new ethers.Contract(a, F.ERC20_ABI, owner));
    const before = await Promise.all(watched.map((c) => c.balanceOf(arbAddr)));
    const ownerBefore = await tok.balanceOf(owner.address);
    const tx = await arb.executeArb(route);
    const rc = await tx.wait();
    const ev = rc.logs.map((l) => { try { return arb.interface.parseLog(l); } catch { return null; } }).find((e) => e && e.name === "ArbExecuted");
    expect(ev).to.not.equal(undefined);
    const { loanAmount, premium, profit } = ev.args;
    // I1: FlashArb ends with the same balance of every token, owner gains exactly profit
    const afterBal = await Promise.all(watched.map((c) => c.balanceOf(arbAddr)));
    expect(afterBal.map(String)).to.deep.equal(before.map(String));
    expect((await tok.balanceOf(owner.address)) - ownerBefore).to.equal(profit);
    expect(profit).to.be.gt(0n);
    // premium: Aave v3 rounds up; the design-doc formula (+5000) would be wrong for amounts such as 999
    expect(premium).to.equal(ceilPremium(loanAmount));
    // I2: no allowance left to Aave or any whitelisted pool
    for (const s of [F.aavePool(), P.v2, P.uni, P.cake, P.uniQM, P.uniBM]) expect(await tok.allowance(arbAddr, s)).to.equal(0n);
    expect(await arb.routeHash()).to.equal(ethers.ZeroHash);
    return { rc, profit, premium, loanAmount };
  }

  it("F-A1/F-B1 two hops UNI_V2 -> UNI_V3 on a dislocated Sushi v2 pair; F-X harness agrees", async () => {
    const usdcLeft = await dislocateV2();
    const amounts = [2n, 5n, 10n, 20n, 40n].map((p) => (usdcLeft * p) / 1000n).filter((a) => a > 0n);
    const best = await F.bestLoan(arb, (a) => v2Route(a), amounts);
    const route = await v2Route(best.amount);

    // F-X: harness eth_call on the same state
    const H = await ethers.getContractFactory("FlashArbSimHarness");
    const calldata = arb.interface.encodeFunctionData("executeArb", [route]);
    const init = await H.getDeployTransaction(
      F.aavePool(), T.USDC,
      [[T.WETH, ethers.parseEther("1000")], [T.USDC, 5_000_000n * 10n ** 6n]],
      [[P.v2, VENUE.UNI_V2, 30], [P.uni, VENUE.UNI_V3, 0]],
      calldata
    );
    const raw = await ethers.provider.call({ from: owner.address, data: init.data, gasLimit: 16_000_000 });
    const [ok, simProfit, simGas, rd] = ethers.AbiCoder.defaultAbiCoder().decode(["bool", "uint256", "uint256", "bytes"], raw);
    expect(ok, "harness success; revert=" + rd).to.equal(true);
    expect(simGas).to.be.gt(0n);

    const r = await executeAndCheck(route);
    expect(simProfit).to.equal(r.profit); // F-X
    console.log(`      two_hop_v3_v2: loan ${best.amount} USDC raw, profit ${r.profit}, gas ${r.rc.gasUsed}, harness inner gas ${simGas}`);
    F.recordGas(CHAIN, "two_hop_v3_v2", r.rc.gasUsed);
  });

  it("F-A4/F-B6 same route without a dislocation reverts InsufficientProfit; revert gas recorded", async () => {
    const route = await v2Route(cfg.loanV2, cfg.loanV2); // minProfit == loan: unreachable, deterministic
    await expect(arb.executeArb.staticCall(route)).to.be.revertedWithCustomError(arb, "InsufficientProfit");
    const data = arb.interface.encodeFunctionData("executeArb", [route]);
    const { gas, status } = await F.revertedTxGas(owner, arbAddr, data);
    expect(status).to.equal(0);
    expect(gas).to.be.gt(100000n);
    console.log(`      revert_insufficient_profit_two_hop gas ${gas}`);
    F.recordGas(CHAIN, "revert_insufficient_profit_two_hop", gas);
    expect(await arb.routeHash()).to.equal(ethers.ZeroHash);
  });

  it("F-A2/F-B5 two hops UNI_V3 -> PANCAKE_V3 and three hops via a third token on a pumped Pancake pool", async () => {
    // sell WETH on the deep Uniswap pool for USDC, then pump the Pancake 5 bps WETH/USDC price up
    await getWethToSwapper(ethers.parseEther(String(cfg.wethToSell)));
    const usdcGot = await sellV3(P.uni, T.WETH, ethers.parseEther("100"));
    const cakeUsdcIs0 = (await F.token0Of(P.cake)) === ethers.getAddress(T.USDC);
    const cakeUsdc = new ethers.Contract(T.USDC, F.ERC20_ABI, ethers.provider);
    const poolUsdc = await cakeUsdc.balanceOf(P.cake);
    const chunk = poolUsdc / 25n;
    expect(usdcGot).to.be.gt(chunk * 10n);

    const twoHop = async (a) =>
      mkRoute("USDC", a, [await F.mkHop(P.uni, VENUE.UNI_V3, T.USDC, T.WETH), await F.mkHop(P.cake, VENUE.PANCAKE_V3, T.WETH, T.USDC)]);
    const threeHop = async (a) =>
      mkRoute("USDC", a, [
        await F.mkHop(P.uniQM, VENUE.UNI_V3, T.USDC, T[cfg.mid]),
        await F.mkHop(P.uniBM, VENUE.UNI_V3, T[cfg.mid], T.WETH),
        await F.mkHop(P.cake, VENUE.PANCAKE_V3, T.WETH, T.USDC),
      ]);
    const loans = [1n, 3n, 10n, 30n, 100n, 300n, 1000n].map((x) => x * 10n ** 6n);

    let pumped = 0;
    let best2 = null, best3 = null;
    for (; pumped < 10 && !(best2 && best3); pumped++) {
      await (await swapper.v3Swap(P.cake, cakeUsdcIs0, chunk)).wait();
      if (!best2) best2 = await F.bestLoan(arb, twoHop, loans).catch(() => null);
      if (!best3) best3 = await F.bestLoan(arb, threeHop, loans).catch(() => null);
    }
    expect(best2, "a profitable v3->v3 two-hop size after pumping").to.not.equal(null);
    expect(best3, "a profitable three-hop size after pumping").to.not.equal(null);
    console.log(`      pumped Pancake with ${pumped} chunks of ${chunk} raw USDC`);

    const s = await takeSnapshot();
    const r2 = await executeAndCheck(await twoHop(best2.amount));
    console.log(`      two_hop_v3_v3: loan ${best2.amount} profit ${r2.profit} gas ${r2.rc.gasUsed}`);
    F.recordGas(CHAIN, "two_hop_v3_v3", r2.rc.gasUsed);
    await s.restore();
    const r3 = await executeAndCheck(await threeHop(best3.amount));
    console.log(`      three_hop: loan ${best3.amount} profit ${r3.profit} gas ${r3.rc.gasUsed}`);
    F.recordGas(CHAIN, "three_hop", r3.rc.gasUsed);
  });

  for (const sc of cfg.swapOnce) {
    it(`${sc.id}: swapOnce both directions`, async () => {
      const addr = pool(sc.pool);
      await arb.setToken(F.tokenAddr(sc.b), true, 0n);
      await arb.setPool(addr, sc.venue, 0, true);
      const wethc = await F.wrapEth(owner, T.WETH, sc.amountA * 2n);
      await (await wethc.transfer(arbAddr, sc.amountA)).wait();
      const a = F.tokenAddr(sc.a), b = F.tokenAddr(sc.b);
      const tb = new ethers.Contract(b, F.ERC20_ABI, owner);
      const ta = new ethers.Contract(a, F.ERC20_ABI, owner);

      const h1 = await F.mkHop(addr, sc.venue, a, b);
      let expected = null;
      if (sc.venue === VENUE.SOLIDLY_V2) {
        const p = new ethers.Contract(addr, ["function getAmountOut(uint256,address) view returns (uint256)"], ethers.provider);
        expected = await p.getAmountOut(sc.amountA, a);
      }
      const out1 = await arb.swapOnce.staticCall(h1, sc.amountA);
      await arb.swapOnce(h1, sc.amountA);
      expect(out1).to.be.gt(0n);
      if (expected !== null) expect(out1).to.equal(expected);
      expect(await tb.balanceOf(arbAddr)).to.equal(out1);
      expect(await ta.balanceOf(arbAddr)).to.equal(0n);

      const h2 = await F.mkHop(addr, sc.venue, b, a);
      const out2 = await arb.swapOnce.staticCall(h2, out1);
      await arb.swapOnce(h2, out1);
      expect(out2).to.be.gt(0n);
      expect(await ta.balanceOf(arbAddr)).to.equal(out2);
      expect(await tb.balanceOf(arbAddr)).to.equal(0n);
      // round trip costs fees but cannot create value
      expect(out2).to.be.lt(sc.amountA);
      expect(await arb.pendingPool()).to.equal(ethers.ZeroAddress);
      // no pool approval was ever needed
      expect(await ta.allowance(arbAddr, addr)).to.equal(0n);
      expect(await tb.allowance(arbAddr, addr)).to.equal(0n);
    });
  }
});
