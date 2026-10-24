const { expect } = require("chai");
const { ethers } = require("hardhat");
const { deployWorld, snapshotState, E18 } = require("./helpers/world");

// Extra adversarial scenarios on top of the U-xx cases in FlashArb.unit.test.js (hook-token, callback spoofing,
// re-entry, fee-on-transfer and rebasing attacks live there).
describe("FlashArb adversarial", function () {
  let w;
  const L = 100n * E18;
  beforeEach(async () => {
    w = await deployWorld();
  });
  const two = async () => w.route("A", L, [w.hop(w.P.p1, "A", "B"), w.hop(w.P.p2, "B", "A")]);

  it("every pool misbehaviour mode leaves balances, allowances and in-flight storage untouched", async () => {
    for (let mode = 1; mode <= 6; mode++) {
      const before = await snapshotState(w);
      await w.P.p1.c.setMode(mode);
      await expect(w.arb.executeArb(await two())).to.be.reverted;
      await w.P.p1.c.setMode(0);
      expect(await snapshotState(w), `mode ${mode}`).to.deep.equal(before);
    }
  });

  it("a malicious whitelisted pool cannot make FlashArb pay more than the hop amount", async () => {
    await w.P.p2.c.setMode(2); // OVERASK on the second hop
    const before = await snapshotState(w);
    await expect(w.arb.executeArb(await two())).to.be.revertedWithCustomError(w.arb, "BadCallbackAmount");
    expect(await snapshotState(w)).to.deep.equal(before);
  });

  it("dust donations to FlashArb and to a v2 pair do not break the exact delta checks", async () => {
    await w.tokens.B.mint(w.arbAddr, 12345n);
    await w.tokens.A.mint(w.arbAddr, 1n);
    await w.tokens.A.mint(w.P.v2ab.address, 777n); // donation, not synced
    const hops = [w.hop(w.P.v2ab, "A", "B"), w.hop(w.P.p2, "B", "A")];
    await w.arb.executeArb(await w.route("A", 10n * E18, hops));
    expect(await w.tokens.B.balanceOf(w.arbAddr)).to.equal(12345n);
    expect(await w.tokens.A.balanceOf(w.arbAddr)).to.equal(1n);
  });

  it("a stranger cannot change configuration, pause, sweep or take ownership", async () => {
    const a = w.arb.connect(w.attacker);
    await expect(a.setPool(w.Q.q1.address, 0, 0, true)).to.be.revertedWithCustomError(w.arb, "OwnableUnauthorizedAccount");
    await expect(a.setToken(w.addr.A, true, ethers.MaxUint256)).to.be.revertedWithCustomError(w.arb, "OwnableUnauthorizedAccount");
    await expect(a.acceptOwnership()).to.be.revertedWithCustomError(w.arb, "OwnableUnauthorizedAccount");
    expect(await w.arb.owner()).to.equal(w.owner.address);
    expect(await w.arb.paused()).to.equal(false);
  });

  it("a pool disabled and re-pointed to other tokens cannot be used with the old token pair", async () => {
    await w.arb.setPool(w.P.p1.address, 0, 0, false);
    await expect(w.arb.executeArb(await two())).to.be.revertedWithCustomError(w.arb, "PoolNotWhitelisted");
    await w.arb.setToken(w.addr.A, false, 0n);
    await w.arb.setPool(w.P.p1.address, 0, 0, false);
    await expect(w.arb.setPool(w.P.p1.address, 0, 0, true)).to.be.revertedWithCustomError(w.arb, "TokenNotAllowed");
  });
});
