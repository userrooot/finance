const { expect } = require("chai");
const { ethers, artifacts } = require("hardhat");
const { deployWorld, snapshotState, decodeErr, E18, VENUE } = require("./helpers/world");

// Regression and coverage tests added by the T3/T6 security review (docs/FLASH_ARB_SECURITY.md).
// SEC-n tests fail on the pre-review code and pass on the fix; COV-n tests close threat-table rows that had no test.
describe("FlashArb security review", function () {
  const L = 100n * E18;
  let w;
  beforeEach(async () => {
    w = await deployWorld();
  });
  const twoHop = async (x = w) => x.route("A", L, [x.hop(x.P.p1, "A", "B"), x.hop(x.P.p2, "B", "A")]);

  describe("SEC-1 the Aave callback must run exactly once per executeArb", () => {
    it("a flash-loan source that never calls executeOperation makes executeArb revert (was: success, profit 0)", async () => {
      await w.aave.setSkipCallback(true);
      const before = await snapshotState(w);
      await expect(w.arb.executeArb(await twoHop())).to.be.revertedWithCustomError(w.arb, "LoanMismatch");
      expect(await snapshotState(w)).to.deep.equal(before);
    });
    it("a second executeOperation for the same loan is rejected with NoActiveRoute (route hash is consumed)", async () => {
      await w.aave.setDoubleCallback(true);
      await expect(w.arb.executeArb(await twoHop())).to.be.revertedWithCustomError(w.arb, "NoActiveRoute");
      expect(await w.arb.routeHash()).to.equal(ethers.ZeroHash);
    });
  });

  describe("SEC-2 configuration cannot change in the middle of a route", () => {
    // A contract owner (multisig, wrapper) that forwards calls is reachable from token hooks mid-route.
    for (const skip of [0, 1, 2]) {
      it(`setPool / setToken re-entered through the owner at A-transfer #${skip}: ReentrancyGuardReentrantCall`, async () => {
        const h = await deployWorld({ kinds: { A: "hook" }, ownerIsWrapper: true });
        const route = await twoHop(h);
        for (const data of [
          h.wrapper.interface.encodeFunctionData("setPool", [h.P.p2.address, VENUE.PANCAKE_V3, 0, false]),
          h.wrapper.interface.encodeFunctionData("setToken", [h.addr.B, false, 0n]),
        ]) {
          await h.tokens.A.arm(await h.wrapper.getAddress(), data, skip);
          await h.wrapper.executeArb(route);
          expect(await h.tokens.A.fired()).to.equal(1n);
          expect(await h.tokens.A.lastOk(), "mid-route config change must fail").to.equal(false);
          expect(decodeErr(h.arb, await h.tokens.A.lastRet())).to.equal("ReentrancyGuardReentrantCall");
        }
        // config unchanged
        expect((await h.arb.pools(h.P.p2.address)).enabled).to.equal(true);
        expect(await h.arb.allowedToken(h.addr.B)).to.equal(true);
      });
    }
    it("config still works outside a route and while paused", async () => {
      await w.arb.pause();
      await w.arb.setPool(w.P.p2.address, VENUE.PANCAKE_V3, 0, false);
      await w.arb.setToken(w.addr.C, true, 1n);
      await w.arb.setPool(w.P.p2.address, VENUE.PANCAKE_V3, 0, true);
    });
  });

  describe("SEC-3 simulation harness reports bad input as failure, never as success", () => {
    const decode = (raw) => ethers.AbiCoder.defaultAbiCoder().decode(["bool", "uint256", "uint256", "bytes"], raw);
    const badSel = ethers.id("HarnessBadCalldata()").slice(0, 10);
    async function run(calldata, loanToken = w.addr.A) {
      const F = await ethers.getContractFactory("FlashArbSimHarness");
      const tokens = ["A", "B"].map((t) => [w.addr[t], w.cap]);
      const pools = [w.P.p1, w.P.p2].map((p) => [p.address, p.venue, p.feeBps]);
      const tx = await F.getDeployTransaction(w.aaveAddr, loanToken, tokens, pools, calldata);
      return decode(await ethers.provider.call({ from: w.attacker.address, data: tx.data }));
    }
    it("calldata for another FlashArb function (maxLoan of an uncapped token) is rejected (was: success, profit 0)", async () => {
      const data = w.arb.interface.encodeFunctionData("maxLoan", [w.addr.C]);
      const [ok, profit, , rd] = await run(data);
      expect(ok).to.equal(false);
      expect(profit).to.equal(0n);
      expect(rd).to.equal(badSel);
    });
    it("empty / short calldata is rejected", async () => {
      for (const data of ["0x", "0xe77fcf52"]) {
        const [ok, , , rd] = await run(data);
        expect(ok).to.equal(false);
        expect(rd).to.equal(badSel);
      }
    });
    it("loanToken argument that differs from the route's loanToken is rejected", async () => {
      const data = w.arb.interface.encodeFunctionData("executeArb", [await twoHop()]);
      const [ok, , , rd] = await run(data, w.addr.B);
      expect(ok).to.equal(false);
      expect(rd).to.equal(badSel);
    });
    it("a wrong Aave address that accepts the call without lending is reported as failure (LoanMismatch)", async () => {
      await w.aave.setSkipCallback(true);
      const data = w.arb.interface.encodeFunctionData("executeArb", [await twoHop()]);
      const [ok, profit, , rd] = await run(data);
      expect(ok).to.equal(false);
      expect(profit).to.equal(0n);
      expect(decodeErr(w.arb, rd)).to.equal("LoanMismatch");
    });
    it("the correct calldata still succeeds", async () => {
      const route = await twoHop();
      const [ok, profit, , rd] = await run(w.arb.interface.encodeFunctionData("executeArb", [route]));
      expect(ok).to.equal(true);
      expect(rd).to.equal("0x");
      expect(profit).to.equal(await w.arb.executeArb.staticCall(route));
    });
  });

  describe("coverage for threat rows that had no direct test", () => {
    it("COV-T12 Aave leaves part of the allowance unused: executeArb zeroes it, extra stays with the owner as profit", async () => {
      const normal = await w.arb.executeArb.staticCall(await twoHop());
      await w.aave.setPullShortBy(1n);
      const arbBefore = await w.tokens.A.balanceOf(w.arbAddr);
      const ownerBefore = await w.tokens.A.balanceOf(w.owner.address);
      await w.arb.executeArb(await twoHop());
      expect(await w.tokens.A.allowance(w.arbAddr, w.aaveAddr)).to.equal(0n);
      expect(await w.tokens.A.balanceOf(w.arbAddr)).to.equal(arbBefore);
      expect((await w.tokens.A.balanceOf(w.owner.address)) - ownerBefore).to.equal(normal + 1n);
    });

    it("COV-T6/T14 pool asks for less than amountIn in the callback but reports amountIn: BalanceDeltaMismatch on the input", async () => {
      await w.P.p1.c.setMode(7); // UNDERASK
      const before = await snapshotState(w);
      await expect(w.arb.executeArb(await twoHop()))
        .to.be.revertedWithCustomError(w.arb, "BalanceDeltaMismatch")
        .withArgs(0, w.addr.A, L, L - 1n);
      expect(await snapshotState(w)).to.deep.equal(before);
    });

    describe("COV-T4 blacklistable token", () => {
      let b;
      beforeEach(async () => {
        b = await deployWorld({ kinds: { A: "blacklist" } });
      });
      for (const who of ["owner", "arb", "pool"]) {
        it(`${who} blacklisted: the whole arb reverts and nothing is left in FlashArb`, async () => {
          const addr = { owner: b.owner.address, arb: b.arbAddr, pool: b.P.p2.address }[who];
          const before = await snapshotState(b);
          await b.tokens.A.setBlacklisted(addr, true);
          await expect(b.arb.executeArb(await twoHop(b))).to.be.revertedWithCustomError(b.tokens.A, "Blacklisted");
          await b.tokens.A.setBlacklisted(addr, false);
          expect(await snapshotState(b)).to.deep.equal(before);
          expect(await b.arb.routeHash()).to.equal(ethers.ZeroHash);
          await b.arb.executeArb(await twoHop(b)); // works again once lifted
        });
      }
    });

    it("COV-T8 loan token donated in the middle of a route (hop that does not touch it) goes to the owner, never stays", async () => {
      const h = await deployWorld({ kinds: { C: "hook" } });
      const route = await h.route("A", L, [h.hop(h.P.p1, "A", "B"), h.hop(h.P.p3, "B", "C"), h.hop(h.P.p4, "C", "A")]);
      const normal = await h.arb.executeArb.staticCall(route);
      const don = 3n * E18;
      // first C transfer after arming = p3 paying C to FlashArb inside hop 1 (B->C); A is not checked in that hop
      await h.tokens.C.arm(h.addr.A, h.tokens.A.interface.encodeFunctionData("mint", [h.arbAddr, don]), 0);
      const arbBefore = await h.tokens.A.balanceOf(h.arbAddr);
      const ownerBefore = await h.tokens.A.balanceOf(h.owner.address);
      await h.arb.executeArb(route);
      expect(await h.tokens.C.lastOk()).to.equal(true);
      expect(await h.tokens.A.balanceOf(h.arbAddr)).to.equal(arbBefore);
      expect((await h.tokens.A.balanceOf(h.owner.address)) - ownerBefore).to.equal(normal + don);
    });

    it("COV-T16 ETH forced into FlashArb (selfdestruct / coinbase) is inert and does not affect execution", async () => {
      await ethers.provider.send("hardhat_setBalance", [w.arbAddr, "0xde0b6b3a7640000"]);
      const profit = await w.arb.executeArb.staticCall(await twoHop());
      await w.arb.executeArb(await twoHop());
      expect(profit).to.be.gt(0n);
      expect(await ethers.provider.getBalance(w.arbAddr)).to.equal(E18);
    });

    it("COV-T15/T16 runtime bytecode has no DELEGATECALL, CALLCODE, SELFDESTRUCT, CREATE*, ORIGIN, NUMBER, PUSH0, TLOAD/TSTORE; ABI has nothing payable", async () => {
      const art = await artifacts.readArtifact("FlashArb");
      let code = art.deployedBytecode.slice(2);
      const metaLen = parseInt(code.slice(-4), 16); // CBOR metadata length (last 2 bytes)
      code = code.slice(0, code.length - (metaLen + 2) * 2);
      const banned = { 0xf4: "DELEGATECALL", 0xf2: "CALLCODE", 0xff: "SELFDESTRUCT", 0xf0: "CREATE", 0xf5: "CREATE2", 0x32: "ORIGIN", 0x43: "NUMBER", 0x5f: "PUSH0", 0x5c: "TLOAD", 0x5d: "TSTORE" };
      // Legacy codegen appends a data segment after the final INVALID (0xfe): here the 32-byte OZ ReentrancyGuard
      // ERC-7201 slot constant 0x9b77..5f00. Decode linearly, then ignore opcodes after the last INVALID.
      const ops = [];
      for (let i = 0; i < code.length / 2; i++) {
        const op = parseInt(code.substr(i * 2, 2), 16);
        ops.push([i, op]);
        if (op >= 0x60 && op <= 0x7f) i += op - 0x5f; // skip PUSH data
      }
      const lastInvalid = ops.map(([, o]) => o).lastIndexOf(0xfe);
      expect(code.length / 2 - ops[lastInvalid][0], "data tail after the last INVALID").to.be.lte(65);
      const found = [];
      let calls = 0;
      for (const [i, op] of ops.slice(0, lastInvalid)) {
        if (banned[op]) found.push(`${banned[op]}@${i}`);
        if (op === 0xf1) calls++;
      }
      expect(found).to.deep.equal([]);
      expect(calls).to.be.gt(0); // sanity: the scan does see opcodes
      for (const f of art.abi) {
        expect(f.type).to.not.be.oneOf(["receive", "fallback"]);
        if (f.type === "function") expect(f.stateMutability, f.name).to.not.equal("payable");
      }
    });
  });
});
