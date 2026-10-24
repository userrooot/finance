const { expect } = require("chai");
const { ethers } = require("hardhat");
const { anyValue } = require("@nomicfoundation/hardhat-chai-matchers/withArgs");
const { deployWorld, snapshotState, decodeErr, v2Out, solOut, E18, VENUE } = require("./helpers/world");

const L = 100n * E18;

/** Reference execution of a route on the mock pools (exact integer math). */
async function reference(w, hops, amountIn) {
  let x = amountIn;
  const outs = [];
  for (const h of hops) {
    const p = Object.values(w.allPools).find((q) => q.address === h.pool);
    let out;
    if (p.kind === "v3") out = h.zeroForOne ? (x * p.num) / p.den : (x * p.den) / p.num;
    else if (p.kind === "v2") {
      const [r0, r1] = await p.c.getReserves();
      out = h.zeroForOne ? v2Out(x, r0, r1) : v2Out(x, r1, r0);
    } else {
      const r0 = await p.c.reserve0();
      const r1 = await p.c.reserve1();
      out = h.zeroForOne ? solOut(x, r0, r1) : solOut(x, r1, r0);
    }
    outs.push(out);
    x = out;
  }
  return outs;
}

describe("FlashArb unit", function () {
  let w;
  beforeEach(async () => {
    w = await deployWorld();
  });

  const twoHop = async (amount = L, minProfit = 0n) =>
    w.route("A", amount, [w.hop(w.P.p1, "A", "B"), w.hop(w.P.p2, "B", "A")], minProfit);

  describe("U-01 profitable routes", () => {
    async function check(hops, amount = L) {
      const outs = await reference(w, hops, amount);
      const prem = w.premium(amount);
      const profit = outs[outs.length - 1] - amount - prem;
      expect(profit).to.be.gt(0n);
      const route = await w.route("A", amount, hops);
      const before = await snapshotState(w);
      const ownerBefore = await w.tokens.A.balanceOf(w.owner.address);
      expect(await w.arb.executeArb.staticCall(route)).to.equal(profit);
      const tx = w.arb.executeArb(route);
      let ex = expect(tx).to.emit(w.arb, "ArbExecuted").withArgs(anyValue, w.addr.A, amount, prem, profit);
      await ex;
      for (let i = 0; i < hops.length; i++) {
        const ain = i === 0 ? amount : outs[i - 1];
        await expect(tx).to.emit(w.arb, "HopExecuted").withArgs(i, hops[i].pool, ain, outs[i]);
      }
      const ownerAfter = await w.tokens.A.balanceOf(w.owner.address);
      expect(ownerAfter - ownerBefore).to.equal(profit);
      // FlashArb balances unchanged
      for (const t of ["A", "B", "C"]) {
        expect(await w.tokens[t].balanceOf(w.arbAddr)).to.equal(BigInt(before.bal[`${t}.arb`]));
      }
      return profit;
    }

    it("2 hops (UNI_V3 -> PANCAKE_V3)", async () => {
      await check([w.hop(w.P.p1, "A", "B"), w.hop(w.P.p2, "B", "A")]);
    });
    it("3 hops (all UNI_V3)", async () => {
      await check([w.hop(w.P.p1, "A", "B"), w.hop(w.P.p3, "B", "C"), w.hop(w.P.p4, "C", "A")]);
    });
    it("3 hops covering UNI_V3, UNI_V2 and SOLIDLY_V2", async () => {
      await check([w.hop(w.P.p1, "A", "B"), w.hop(w.P.v2bc, "B", "C"), w.hop(w.P.sol, "C", "A")]);
    });
    it("2 hops UNI_V2 -> UNI_V3 (v2 first)", async () => {
      // v2ab: A->B at ~2:1 with slippage; p2 gives back 0.5555 A per B
      await check([w.hop(w.P.v2ab, "A", "B"), w.hop(w.P.p2, "B", "A")], 10n * E18);
    });
    it("4 hops (MAX_HOPS) with all four venues", async () => {
      await check([
        w.hop(w.P.p1, "A", "B"),
        w.hop(w.P.p2, "B", "A"),
        w.hop(w.P.p4, "A", "C"),
        w.hop(w.P.sol, "C", "A"),
      ]);
    });
  });

  it("U-02 profit exactly minProfit passes (inclusive); minProfit + 1 reverts", async () => {
    const profit = await w.arb.executeArb.staticCall(await twoHop());
    await w.arb.executeArb(await twoHop(L, profit));
    const w2 = await deployWorld();
    const r = await w2.route("A", L, [w2.hop(w2.P.p1, "A", "B"), w2.hop(w2.P.p2, "B", "A")], profit + 1n);
    await expect(w2.arb.executeArb(r)).to.be.revertedWithCustomError(w2.arb, "InsufficientProfit");
  });

  it("U-03 non-owner calls revert OwnableUnauthorizedAccount", async () => {
    const a = w.arb.connect(w.attacker);
    const r = await twoHop();
    const err = (p) => expect(p).to.be.revertedWithCustomError(w.arb, "OwnableUnauthorizedAccount").withArgs(w.attacker.address);
    await err(a.executeArb(r));
    await err(a.setToken(w.addr.A, true, 1n));
    await err(a.setPool(w.P.p1.address, 0, 0, true));
    await err(a.pause());
    await err(a.unpause());
    await err(a.sweep(w.addr.A, 1n));
    await err(a.transferOwnership(w.attacker.address));
    await err(a.renounceOwnership());
  });

  it("U-04 unprofitable route reverts InsufficientProfit and leaves everything unchanged", async () => {
    // p1 A->B x2, then p1' ... use q1 (not whitelisted) so use p1 and a worse pool: set p2 so B->A returns exactly A
    await w.P.p2.c.setRate(2n, 1n); // B->A = 0.5 A per B -> out == loan, minus premium -> loss
    const before = await snapshotState(w);
    const r = await twoHop();
    await expect(w.arb.executeArb(r)).to.be.revertedWithCustomError(w.arb, "InsufficientProfit");
    expect(await snapshotState(w)).to.deep.equal(before);
    // static call of the same
    await expect(w.arb.executeArb.staticCall(r)).to.be.revertedWithCustomError(w.arb, "InsufficientProfit");
  });

  describe("U-05 route shape errors", () => {
    it("1 hop and 5 hops: BadHopCount", async () => {
      const h = w.hop(w.P.p1, "A", "B");
      await expect(w.arb.executeArb(await w.route("A", L, [h]))).to.be.revertedWithCustomError(w.arb, "BadHopCount").withArgs(1);
      const five = [h, w.hop(w.P.p2, "B", "A"), h, w.hop(w.P.p2, "B", "A"), h];
      await expect(w.arb.executeArb(await w.route("A", L, five))).to.be.revertedWithCustomError(w.arb, "BadHopCount").withArgs(5);
      await expect(w.arb.executeArb(await w.route("A", L, []))).to.be.revertedWithCustomError(w.arb, "BadHopCount").withArgs(0);
    });
    it("not closed", async () => {
      const r = await w.route("A", L, [w.hop(w.P.p1, "A", "B"), w.hop(w.P.p3, "B", "C")]);
      await expect(w.arb.executeArb(r)).to.be.revertedWithCustomError(w.arb, "RouteNotClosed");
      const r2 = await w.route("A", L, [w.hop(w.P.p3, "B", "C"), w.hop(w.P.p4, "C", "A")]);
      await expect(w.arb.executeArb(r2)).to.be.revertedWithCustomError(w.arb, "RouteNotClosed");
    });
    it("broken chain", async () => {
      const r = await w.route("A", L, [w.hop(w.P.p1, "A", "B"), w.hop(w.P.p4, "C", "A")]);
      await expect(w.arb.executeArb(r)).to.be.revertedWithCustomError(w.arb, "HopChainBroken").withArgs(1);
    });
    it("duplicate pool", async () => {
      const r = await w.route("A", L, [w.hop(w.P.p1, "A", "B"), w.hop(w.P.p1, "B", "A")]);
      await expect(w.arb.executeArb(r)).to.be.revertedWithCustomError(w.arb, "DuplicatePool").withArgs(w.P.p1.address);
    });
    it("wrong direction", async () => {
      const h0 = w.hop(w.P.p1, "A", "B");
      h0.zeroForOne = false;
      const r = await w.route("A", L, [h0, w.hop(w.P.p2, "B", "A")]);
      await expect(w.arb.executeArb(r)).to.be.revertedWithCustomError(w.arb, "DirectionMismatch").withArgs(0);
    });
    it("wrong venue", async () => {
      const h1 = w.hop(w.P.p2, "B", "A");
      h1.venue = VENUE.UNI_V3; // p2 is PANCAKE_V3
      const r = await w.route("A", L, [w.hop(w.P.p1, "A", "B"), h1]);
      await expect(w.arb.executeArb(r)).to.be.revertedWithCustomError(w.arb, "VenueMismatch").withArgs(1);
    });
    it("venue value 7 (ABI decoder rejects it)", async () => {
      // SPEC-DEVIATION (spec 2 T17 / U-05 says Panic 0x21): the Solidity 0.8.24 ABI decoder rejects an
      // out-of-range enum in calldata with an EMPTY revert, Panic(0x21) only comes from explicit uint->enum casts.
      const h0 = w.hop(w.P.p1, "A", "B");
      h0.venue = 7;
      const r = await w.route("A", L, [h0, w.hop(w.P.p2, "B", "A")]);
      const data = w.arb.interface.encodeFunctionData("executeArb", [r]);
      await expect(w.owner.sendTransaction({ to: w.arbAddr, data })).to.be.revertedWithoutReason();
    });
    it("zero amount, too large, token not allowed", async () => {
      const hops = [w.hop(w.P.p1, "A", "B"), w.hop(w.P.p2, "B", "A")];
      await expect(w.arb.executeArb(await w.route("A", 0n, hops))).to.be.revertedWithCustomError(w.arb, "ZeroAmount");
      await expect(w.arb.executeArb(await w.route("A", w.cap + 1n, hops)))
        .to.be.revertedWithCustomError(w.arb, "LoanTooLarge")
        .withArgs(w.cap + 1n, w.cap);
      // intermediate token B disallowed
      await w.arb.setToken(w.addr.B, false, 0n);
      await expect(w.arb.executeArb(await w.route("A", L, hops)))
        .to.be.revertedWithCustomError(w.arb, "TokenNotAllowed")
        .withArgs(w.addr.B);
    });
  });

  it("U-06 pool not whitelisted / disabled after whitelisting", async () => {
    const hq = w.hop(w.Q.q1, "A", "B");
    await expect(w.arb.executeArb(await w.route("A", L, [hq, w.hop(w.P.p2, "B", "A")])))
      .to.be.revertedWithCustomError(w.arb, "PoolNotWhitelisted")
      .withArgs(w.Q.q1.address);
    await w.arb.setPool(w.P.p2.address, VENUE.PANCAKE_V3, 0, false);
    await expect(w.arb.executeArb(await twoHop()))
      .to.be.revertedWithCustomError(w.arb, "PoolNotWhitelisted")
      .withArgs(w.P.p2.address);
    await expect(w.arb.setPool(w.P.p2.address, VENUE.PANCAKE_V3, 0, false))
      .to.emit(w.arb, "PoolSet")
      .withArgs(w.P.p2.address, VENUE.PANCAKE_V3, w.P.p2.t0, w.P.p2.t1, 0, false);
    // and it can be re-enabled
    await w.arb.setPool(w.P.p2.address, VENUE.PANCAKE_V3, 0, true);
    await w.arb.executeArb(await twoHop());
  });

  it("U-07 setPool / setToken validation", async () => {
    const D = await (await ethers.getContractFactory("MockERC20")).deploy("D", "D", 18);
    const bad = await (await ethers.getContractFactory("MockV3Pool")).deploy(w.addr.A, await D.getAddress(), false);
    await expect(w.arb.setPool(await bad.getAddress(), VENUE.UNI_V3, 0, true))
      .to.be.revertedWithCustomError(w.arb, "TokenNotAllowed")
      .withArgs(await D.getAddress());
    await expect(w.arb.setPool(w.other.address, VENUE.UNI_V3, 0, true))
      .to.be.revertedWithCustomError(w.arb, "NotAContract")
      .withArgs(w.other.address);
    const v2 = w.Q.q1.address; // a v3 mock has token0/token1 too; config rules do not depend on the real kind
    for (const fee of [0, 101]) {
      await expect(w.arb.setPool(v2, VENUE.UNI_V2, fee, true)).to.be.revertedWithCustomError(w.arb, "InvalidPoolConfig").withArgs(v2);
    }
    await expect(w.arb.setPool(v2, VENUE.UNI_V3, 5, true)).to.be.revertedWithCustomError(w.arb, "InvalidPoolConfig").withArgs(v2);
    await expect(w.arb.setPool(v2, VENUE.SOLIDLY_V2, 1, true)).to.be.revertedWithCustomError(w.arb, "InvalidPoolConfig");
    await w.arb.setPool(v2, VENUE.UNI_V2, 100, true);
    await w.arb.setPool(v2, VENUE.UNI_V2, 1, true);
    // setToken
    await expect(w.arb.setToken(w.other.address, true, 1n)).to.be.revertedWithCustomError(w.arb, "NotAContract");
    await expect(w.arb.setToken(await D.getAddress(), false, 1n)).to.be.revertedWithCustomError(w.arb, "TokenNotAllowed");
    await expect(w.arb.setToken(await D.getAddress(), true, 5n)).to.emit(w.arb, "TokenSet").withArgs(await D.getAddress(), true, 5n);
    // constructor
    const F = await ethers.getContractFactory("FlashArb");
    await expect(F.deploy(w.other.address, w.owner.address)).to.be.revertedWithCustomError(F, "NotAContract");
  });

  it("U-08 hop tokens that do not match the pool's tokens", async () => {
    const bad = w.hop(w.P.p1, "A", "B");
    bad.tokenOut = w.addr.C; // p1 is (A,B)
    const r = await w.route("A", L, [bad, w.hop(w.P.p4, "C", "A")]);
    await expect(w.arb.executeArb(r)).to.be.revertedWithCustomError(w.arb, "PoolTokenMismatch").withArgs(0);
    const same = w.hop(w.P.p1, "A", "B");
    same.tokenOut = w.addr.A;
    await expect(w.arb.executeArb(await w.route("A", L, [same, w.hop(w.P.p2, "B", "A")])))
      .to.be.revertedWithCustomError(w.arb, "PoolTokenMismatch");
  });

  describe("U-10 / U-21 misbehaving v3 pools", () => {
    const modes = { DOUBLE_CALLBACK: 1, OVERASK: 2, PARTIAL: 3, SHORT_PAY: 4, NEG_OWED: 5, NO_CALLBACK: 6 };
    it("callback twice: second call UnexpectedCallback", async () => {
      await w.P.p1.c.setMode(modes.DOUBLE_CALLBACK);
      await expect(w.arb.executeArb(await twoHop()))
        .to.be.revertedWithCustomError(w.arb, "UnexpectedCallback")
        .withArgs(w.P.p1.address);
    });
    it("asks for more than amountIn: BadCallbackAmount", async () => {
      await w.P.p1.c.setMode(modes.OVERASK);
      await expect(w.arb.executeArb(await twoHop()))
        .to.be.revertedWithCustomError(w.arb, "BadCallbackAmount")
        .withArgs(L + 1n, -(L * 2n));
    });
    it("wrong sign: BadCallbackAmount", async () => {
      await w.P.p1.c.setMode(modes.NEG_OWED);
      await expect(w.arb.executeArb(await twoHop())).to.be.revertedWithCustomError(w.arb, "BadCallbackAmount");
    });
    it("never calls back: UnexpectedCallback(pool)", async () => {
      await w.P.p1.c.setMode(modes.NO_CALLBACK);
      await expect(w.arb.executeArb(await twoHop()))
        .to.be.revertedWithCustomError(w.arb, "UnexpectedCallback")
        .withArgs(w.P.p1.address);
    });
    it("U-21 partial fill: PartialFill", async () => {
      await w.P.p1.c.setMode(modes.PARTIAL);
      await expect(w.arb.executeArb(await twoHop()))
        .to.be.revertedWithCustomError(w.arb, "PartialFill")
        .withArgs(0, L / 2n, L);
    });
    it("pays out less than reported: BalanceDeltaMismatch", async () => {
      await w.P.p1.c.setMode(modes.SHORT_PAY);
      await expect(w.arb.executeArb(await twoHop()))
        .to.be.revertedWithCustomError(w.arb, "BalanceDeltaMismatch")
        .withArgs(0, w.addr.B, L * 2n, L * 2n - 1n);
    });
    it("state is clean after each failure", async () => {
      for (const m of Object.values(modes)) {
        await w.P.p1.c.setMode(m);
        await expect(w.arb.executeArb(await twoHop())).to.be.reverted;
        expect(await w.arb.routeHash()).to.equal(ethers.ZeroHash);
        expect(await w.arb.pendingPool()).to.equal(ethers.ZeroAddress);
      }
    });
  });

  describe("U-11 fee-on-transfer", () => {
    it("as intermediate token: BalanceDeltaMismatch at the hop", async () => {
      const f = await deployWorld({ kinds: { B: "fot" } });
      const r = await f.route("A", L, [f.hop(f.P.p1, "A", "B"), f.hop(f.P.p2, "B", "A")]);
      await expect(f.arb.executeArb(r)).to.be.revertedWithCustomError(f.arb, "BalanceDeltaMismatch").withArgs(0, f.addr.B, L * 2n, (L * 2n * 99n) / 100n);
    });
    it("as loan token: BalanceDeltaMismatch at the loan receipt", async () => {
      const f = await deployWorld({ kinds: { A: "fot" } });
      const r = await f.route("A", L, [f.hop(f.P.p1, "A", "B"), f.hop(f.P.p2, "B", "A")]);
      const max = 2n ** 256n - 1n;
      await expect(f.arb.executeArb(r))
        .to.be.revertedWithCustomError(f.arb, "BalanceDeltaMismatch")
        .withArgs(max, f.addr.A, L, (L * 99n) / 100n);
    });
    it("on the input side of a v2 pair the pair's own K check fails", async () => {
      const f = await deployWorld({ kinds: { A: "fot" } });
      await f.tokens.A.mint(f.arbAddr, 10n * E18);
      const h = f.hop(f.P.v2ab, "A", "B");
      await expect(f.arb.swapOnce(h, 1n * E18)).to.be.revertedWith("K");
    });
  });

  describe("U-12 rebasing token", () => {
    for (const [name, idx] of [["up", 2n * E18], ["down", E18 / 2n]]) {
      it(`rebased ${name} by the token mid-route: BalanceDeltaMismatch`, async () => {
        const f = await deployWorld({ kinds: { B: "rebase" } });
        const r = await f.route("A", L, [f.hop(f.P.p1, "A", "B"), f.hop(f.P.p2, "B", "A")]);
        // first B transfer of the route is the pool paying B to FlashArb
        await f.tokens.B.armRebase(0, idx);
        await expect(f.arb.executeArb(r)).to.be.revertedWithCustomError(f.arb, "BalanceDeltaMismatch");
        expect(await f.arb.routeHash()).to.equal(ethers.ZeroHash);
      });
    }
  });

  describe("hook token attacks (U-09, U-14, U-15)", () => {
    let h, attacker;
    const lastErr = async (hook) => decodeErr(h.arb, await hook.lastRet());
    beforeEach(async () => {
      h = await deployWorld({ kinds: { A: "hook" } });
      attacker = await (await ethers.getContractFactory("CallbackAttacker")).deploy();
    });
    const hops = () => [h.hop(h.P.p1, "A", "B"), h.hop(h.P.p2, "B", "A")];

    // transfers of A during the route: 0 loan out of Aave, 1 FlashArb pays p1, 2 p2 pays FlashArb (pending set),
    // 3 Aave pulls repayment, 4 profit to owner
    for (let skip = 0; skip <= 4; skip++) {
      it(`U-09 direct Uniswap/Pancake callbacks at A-transfer #${skip}: UnexpectedCallback`, async () => {
        for (const fn of ["callUni", "callPancake"]) {
          const data = attacker.interface.encodeFunctionData(fn, [h.arbAddr, 1n, 1n]);
          await h.tokens.A.arm(await attacker.getAddress(), data, skip);
          await h.arb.executeArb(await h.route("A", L, hops()));
          expect(await h.tokens.A.fired()).to.equal(1n);
          expect(await h.tokens.A.lastOk()).to.equal(false);
          expect(await lastErr(h.tokens.A)).to.equal("UnexpectedCallback");
        }
      });
      it(`U-15 executeOperation at A-transfer #${skip}: NotAavePool`, async () => {
        const data = attacker.interface.encodeFunctionData("callExecuteOperation", [h.arbAddr, h.addr.A, 1n, 0n, h.arbAddr, "0x"]);
        await h.tokens.A.arm(await attacker.getAddress(), data, skip);
        await h.arb.executeArb(await h.route("A", L, hops()));
        expect(await h.tokens.A.fired()).to.equal(1n);
        expect(await lastErr(h.tokens.A)).to.equal("NotAavePool");
      });
    }

    it("U-09 direct callbacks while idle: UnexpectedCallback", async () => {
      await expect(attacker.callUni(h.arbAddr, 1n, 1n)).to.be.revertedWithCustomError(h.arb, "UnexpectedCallback").withArgs(await attacker.getAddress());
      await expect(attacker.callPancake(h.arbAddr, 1n, 1n)).to.be.revertedWithCustomError(h.arb, "UnexpectedCallback");
      await expect(h.arb.connect(h.attacker).uniswapV3SwapCallback(1n, 1n, "0x")).to.be.revertedWithCustomError(h.arb, "UnexpectedCallback");
    });

    it("U-14 hook calling FlashArb directly: OwnableUnauthorizedAccount(token)", async () => {
      const route = await h.route("A", L, hops());
      for (const data of [
        h.arb.interface.encodeFunctionData("executeArb", [route]),
        h.arb.interface.encodeFunctionData("sweep", [h.addr.A, 1n]),
      ]) {
        await h.tokens.A.arm(h.arbAddr, data, 0);
        await h.arb.executeArb(route);
        expect(await lastErr(h.tokens.A)).to.equal("OwnableUnauthorizedAccount");
      }
    });
  });

  describe("U-14 re-entry through an owner wrapper", () => {
    it("executeArb and sweep re-entry: ReentrancyGuardReentrantCall", async () => {
      const h = await deployWorld({ kinds: { A: "hook" }, ownerIsWrapper: true });
      const route = await h.route("A", L, [h.hop(h.P.p1, "A", "B"), h.hop(h.P.p2, "B", "A")]);
      for (const skip of [0, 1, 2, 3]) {
        for (const data of [
          h.wrapper.interface.encodeFunctionData("executeArb", [route]),
          h.wrapper.interface.encodeFunctionData("sweep", [h.addr.A, 1n]),
        ]) {
          await h.tokens.A.arm(await h.wrapper.getAddress(), data, skip);
          await h.wrapper.executeArb(route);
          expect(await h.tokens.A.fired()).to.equal(1n);
          expect(await h.tokens.A.lastOk()).to.equal(false);
          expect(decodeErr(h.arb, await h.tokens.A.lastRet())).to.equal("ReentrancyGuardReentrantCall");
        }
      }
    });
  });

  it("U-13 executeOperation guards", async () => {
    const attacker = await (await ethers.getContractFactory("CallbackAttacker")).deploy();
    const atk = await attacker.getAddress();
    // called directly
    await expect(w.arb.connect(w.attacker).executeOperation(w.addr.A, 1n, 0n, w.arbAddr, "0x")).to.be.revertedWithCustomError(w.arb, "NotAavePool");
    await expect(attacker.callExecuteOperation(w.arbAddr, w.addr.A, 1n, 0n, w.arbAddr, "0x")).to.be.revertedWithCustomError(w.arb, "NotAavePool");
    // a loan someone else starts naming FlashArb as receiver
    await expect(attacker.startLoan(w.aaveAddr, w.arbAddr, w.addr.A, L, "0x"))
      .to.be.revertedWithCustomError(w.arb, "BadInitiator")
      .withArgs(atk);
    // Aave claims FlashArb as initiator but no route is active
    await w.aave.setFakeInitiator(w.arbAddr);
    await expect(attacker.startLoan(w.aaveAddr, w.arbAddr, w.addr.A, L, "0x")).to.be.revertedWithCustomError(w.arb, "NoActiveRoute");
    await w.aave.setFakeInitiator(ethers.ZeroAddress);
    // tampered params during a real route
    await w.aave.setTamperParams(true);
    await expect(w.arb.executeArb(await twoHop())).to.be.revertedWithCustomError(w.arb, "RouteHashMismatch");
    await w.aave.setTamperParams(false);
    await w.arb.executeArb(await twoHop());
  });

  it("U-16 donation does not count as profit and stays sweepable", async () => {
    const don = 5n * E18;
    await w.tokens.A.mint(w.arbAddr, don);
    const outs = await reference(w, [w.hop(w.P.p1, "A", "B"), w.hop(w.P.p2, "B", "A")], L);
    const profit = outs[1] - L - w.premium(L);
    const before = await w.tokens.A.balanceOf(w.owner.address);
    await expect(w.arb.executeArb(await twoHop())).to.emit(w.arb, "ArbExecuted").withArgs(anyValue, w.addr.A, L, w.premium(L), profit);
    expect((await w.tokens.A.balanceOf(w.owner.address)) - before).to.equal(profit);
    expect(await w.tokens.A.balanceOf(w.arbAddr)).to.equal(don);
    await expect(w.arb.sweep(w.addr.A, ethers.MaxUint256)).to.emit(w.arb, "Swept").withArgs(w.addr.A, w.owner.address, don);
    expect(await w.tokens.A.balanceOf(w.arbAddr)).to.equal(0n);
    expect((await w.tokens.A.balanceOf(w.owner.address)) - before).to.equal(profit + don);
    // partial sweep
    await w.tokens.B.mint(w.arbAddr, 10n);
    await w.arb.sweep(w.addr.B, 4n);
    expect(await w.tokens.B.balanceOf(w.arbAddr)).to.equal(6n);
  });

  it("U-17 ownership: two step, no renounce", async () => {
    await w.arb.transferOwnership(w.other.address);
    expect(await w.arb.owner()).to.equal(w.owner.address);
    expect(await w.arb.pendingOwner()).to.equal(w.other.address);
    await w.arb.executeArb(await twoHop()); // old owner still works
    await expect(w.arb.connect(w.attacker).acceptOwnership()).to.be.revertedWithCustomError(w.arb, "OwnableUnauthorizedAccount");
    await w.arb.connect(w.other).acceptOwnership();
    expect(await w.arb.owner()).to.equal(w.other.address);
    await expect(w.arb.executeArb(await twoHop())).to.be.revertedWithCustomError(w.arb, "OwnableUnauthorizedAccount");
    await expect(w.arb.connect(w.other).renounceOwnership()).to.be.revertedWithCustomError(w.arb, "RenounceDisabled");
    await expect(w.arb.connect(w.other).setPool(w.P.p1.address, 0, 0, true)).to.not.be.reverted;
  });

  it("U-18 allowances to Aave and every pool are 0 after success and after revert", async () => {
    const zero = async () => {
      const s = await snapshotState(w);
      for (const [k, v] of Object.entries(s.allow)) expect(v, k).to.equal("0");
    };
    await w.arb.executeArb(await twoHop());
    await zero();
    await w.P.p2.c.setRate(2n, 1n);
    await expect(w.arb.executeArb(await twoHop())).to.be.reverted;
    await zero();
    await w.P.p2.c.setMode(2);
    await expect(w.arb.executeArb(await twoHop())).to.be.reverted;
    await zero();
  });

  it("U-19 deadline", async () => {
    const now = BigInt((await ethers.provider.getBlock("latest")).timestamp);
    const hops = [w.hop(w.P.p1, "A", "B"), w.hop(w.P.p2, "B", "A")];
    await expect(w.arb.executeArb(await w.route("A", L, hops, 0n, now))).to.be.revertedWithCustomError(w.arb, "DeadlineExpired");
    // deadline == block.timestamp is still valid: next block timestamp is now+1
    await ethers.provider.send("evm_setNextBlockTimestamp", [Number(now) + 100]);
    await w.arb.executeArb(await w.route("A", L, hops, 0n, now + 100n));
  });

  it("U-20 paused / caps", async () => {
    await expect(w.arb.pause()).to.emit(w.arb, "Paused");
    await expect(w.arb.executeArb(await twoHop())).to.be.revertedWithCustomError(w.arb, "EnforcedPause");
    await w.tokens.A.mint(w.arbAddr, 7n);
    await expect(w.arb.sweep(w.addr.A, ethers.MaxUint256)).to.emit(w.arb, "Swept").withArgs(w.addr.A, w.owner.address, 7n);
    await w.arb.setToken(w.addr.C, true, 5n); // config works while paused
    await w.arb.unpause();
    await w.arb.executeArb(await twoHop());
    // cap 0 -> cannot be borrowed
    await w.arb.setToken(w.addr.A, true, 0n);
    await expect(w.arb.executeArb(await twoHop())).to.be.revertedWithCustomError(w.arb, "LoanTooLarge").withArgs(L, 0n);
    // exactly at the cap works
    await w.arb.setToken(w.addr.A, true, L);
    await w.arb.executeArb(await twoHop());
    await w.arb.setToken(w.addr.A, true, L - 1n);
    await expect(w.arb.executeArb(await twoHop())).to.be.revertedWithCustomError(w.arb, "LoanTooLarge");
    // AmountTooLarge
    await w.arb.setToken(w.addr.A, true, ethers.MaxUint256);
    await expect(w.arb.executeArb(await twoHop(2n ** 255n))).to.be.revertedWithCustomError(w.arb, "AmountTooLarge");
  });

  it("U-22 no ETH in, ever", async () => {
    await expect(w.owner.sendTransaction({ to: w.arbAddr, value: 1n })).to.be.reverted;
    await expect(w.owner.sendTransaction({ to: w.arbAddr, value: 1n, data: "0x1234" })).to.be.reverted;
    await expect(w.owner.sendTransaction({ to: w.arbAddr, value: 0n, data: "0x" })).to.be.reverted;
    const data = w.arb.interface.encodeFunctionData("pause");
    await expect(w.owner.sendTransaction({ to: w.arbAddr, value: 1n, data })).to.be.reverted;
    expect(await ethers.provider.getBalance(w.arbAddr)).to.equal(0n);
  });

  describe("U-23 simulation harness (eth_call)", () => {
    const decode = (raw) => ethers.AbiCoder.defaultAbiCoder().decode(["bool", "uint256", "uint256", "bytes"], raw);
    async function runHarness(route, { tokens, pools } = {}) {
      const F = await ethers.getContractFactory("FlashArbSimHarness");
      tokens ??= ["A", "B"].map((t) => [w.addr[t], w.cap]);
      pools ??= [w.P.p1, w.P.p2].map((p) => [p.address, p.venue, p.feeBps]);
      const calldata = w.arb.interface.encodeFunctionData("executeArb", [route]);
      const tx = await F.getDeployTransaction(w.aaveAddr, w.addr.A, tokens, pools, calldata);
      const raw = await ethers.provider.call({ from: w.attacker.address, data: tx.data });
      return decode(raw);
    }
    it("profitable route returns (true, profit, gas>0, 0x) and matches the real call", async () => {
      const route = await twoHop();
      const [ok, profit, gas, rd] = await runHarness(route);
      expect(ok).to.equal(true);
      expect(gas).to.be.gt(0n);
      expect(rd).to.equal("0x");
      const real = await w.arb.executeArb.staticCall(route);
      expect(profit).to.equal(real);
      // eth_call changes nothing on chain
      expect(await w.tokens.A.balanceOf(w.aaveAddr)).to.equal(w.BIG);
    });
    it("failing route returns (false, 0, gas>0, InsufficientProfit)", async () => {
      await w.P.p2.c.setRate(2n, 1n);
      const [ok, profit, gas, rd] = await runHarness(await twoHop());
      expect(ok).to.equal(false);
      expect(profit).to.equal(0n);
      expect(gas).to.be.gt(0n);
      expect(rd.slice(0, 10)).to.equal(w.arb.interface.getError("InsufficientProfit").selector);
    });
    it("config failures are reported, not bubbled", async () => {
      const [ok, profit, gas, rd] = await runHarness(await twoHop(), { tokens: [[w.addr.A, w.cap]] }); // B missing
      expect(ok).to.equal(false);
      expect(profit).to.equal(0n);
      expect(gas).to.equal(0n);
      expect(decodeErr(w.arb, rd)).to.equal("TokenNotAllowed");
      const [ok2, , , rd2] = await runHarness(await twoHop(), { pools: [[w.P.p1.address, 9, 0]] });
      expect(ok2).to.equal(false);
      expect(rd2).to.equal("0x"); // enum out of range: empty revert from the ABI decoder (see U-05)
    });
  });

  it("swapOnce is owner-only and validates the hop", async () => {
    await w.tokens.A.mint(w.arbAddr, 10n);
    await expect(w.arb.connect(w.attacker).swapOnce(w.hop(w.P.p1, "A", "B"), 10n)).to.be.revertedWithCustomError(w.arb, "OwnableUnauthorizedAccount");
    await expect(w.arb.swapOnce(w.hop(w.Q.q1, "A", "B"), 10n)).to.be.revertedWithCustomError(w.arb, "PoolNotWhitelisted");
    await w.arb.swapOnce(w.hop(w.P.p1, "A", "B"), 10n);
    expect(await w.tokens.B.balanceOf(w.arbAddr)).to.equal(20n);
  });
});
