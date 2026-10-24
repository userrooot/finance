const { expect } = require("chai");
const { ethers } = require("hardhat");
const { takeSnapshot } = require("@nomicfoundation/hardhat-network-helpers");
const { deployWorld, E18 } = require("./helpers/world");

// Seeded property test (section 4.4). FUZZ_RUNS = number of sequences (default 300), 10 calls each.
const RUNS = Number(process.env.FUZZ_RUNS || 300);
const CALLS = 10;

function mulberry32(a) {
  return function () {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

describe("FlashArb invariants (seeded fuzz)", function () {
  this.timeout(0);

  it(`I1-I6 hold over ${RUNS} sequences x ${CALLS} calls`, async () => {
    const w = await deployWorld();
    const attackerC = await (await ethers.getContractFactory("CallbackAttacker")).deploy();
    const tokNames = ["A", "B", "C"];
    const poolList = Object.entries(w.allPools); // whitelisted p1..sol and non-whitelisted q1,q2
    const spenders = [w.aaveAddr, ...poolList.map(([, p]) => p.address)];
    const snap = await takeSnapshot();
    const log = [];
    let succ = 0;
    let fail = 0;

    const bals = async (who) => {
      const o = {};
      for (const t of tokNames) o[t] = await w.tokens[t].balanceOf(who);
      return o;
    };

    async function checkInvariants(ctx) {
      for (const t of tokNames) {
        for (const s of spenders) expect(await w.tokens[t].allowance(w.arbAddr, s), `I2 ${t}`).to.equal(0n);
      }
      expect(await w.arb.routeHash(), "I3 routeHash").to.equal(ethers.ZeroHash);
      expect(await w.arb.pendingPool(), "I3 pending").to.equal(ethers.ZeroAddress);
      expect(await w.arb.pendingMaxPay(), "I3 maxPay").to.equal(0n);
      expect(await w.arb.lastPremium(), "I3 premium").to.equal(0n);
      expect(await ethers.provider.getBalance(w.arbAddr), "I6").to.equal(0n);
      expect(await w.arb.owner(), "I5 owner").to.equal(w.owner.address);
    }

    for (let seed = 1; seed <= RUNS; seed++) {
      const rnd = mulberry32(seed * 7919);
      const pick = (arr) => arr[Math.floor(rnd() * arr.length)];
      const ri = (n) => Math.floor(rnd() * n);
      log.length = 0;
      let paused = false;
      const ownerWl = new Map(Object.entries(w.P).map(([n]) => [n, true]));
      try {
        for (let c = 0; c < CALLS; c++) {
          const before = await bals(w.arbAddr);
          const ownerBefore = await bals(w.owner.address);
          const act = ri(12);
          let desc;
          let expectSweep = null;
          let execInfo = null;
          let sendP;
          if (act <= 4) {
            // executeArb by owner (valid and invalid routes)
            const loan = pick(tokNames);
            let n = rnd() < 0.12 ? 1 + ri(5) : 2 + ri(3);
            const hops = [];
            let cur = loan;
            for (let i = 0; i < n; i++) {
              let cands = poolList.filter(([, p]) => p.t0 === w.addr[cur] || p.t1 === w.addr[cur]);
              if (i === n - 1 && rnd() < 0.85) {
                const closing = cands.filter(([, p]) => p.t0 === w.addr[loan] || p.t1 === w.addr[loan]);
                if (closing.length) cands = closing;
              }
              if (rnd() < 0.9) {
                const fresh = cands.filter(([, p]) => !hops.some((h) => h.pool === p.address));
                if (fresh.length) cands = fresh;
              }
              const nonQ = cands.filter(([k]) => !k.startsWith("q"));
              const [, p] = pick(rnd() < 0.9 && nonQ.length ? nonQ : cands);
              const other = p.t0 === w.addr[cur] ? tokNames.find((t) => w.addr[t] === p.t1) : tokNames.find((t) => w.addr[t] === p.t0);
              const h = w.hop(p, cur, other);
              if (rnd() < 0.05) h.zeroForOne = !h.zeroForOne;
              if (rnd() < 0.03) h.venue = (h.venue + 1) % 4;
              hops.push(h);
              cur = other;
            }
            const caps = [0n, 1n, 10n ** 6n, 5n * E18, 5n * E18, 100n * E18, 100n * E18, 100n * E18, 1000n * E18, 1000n * E18 + 1n];
            const amount = pick(caps);
            const minProfit = pick([0n, 0n, 0n, 0n, 1n, 10n ** 15n, 10n ** 30n]);
            const ts = BigInt((await ethers.provider.getBlock("latest")).timestamp);
            const deadline = rnd() < 0.1 ? ts - 5n : ts + 1000n;
            const route = { loanToken: w.addr[loan], loanAmount: amount, minProfit, deadline, hops };
            const actor = rnd() < 0.15 ? w.attacker : w.owner;
            desc = `executeArb(${actor === w.owner ? "owner" : "attacker"}) loan=${loan} amt=${amount} min=${minProfit} hops=${hops.map((h) => Object.keys(w.allPools).find((k) => w.allPools[k].address === h.pool)).join(",")}`;
            log.push(desc);
            let profitStatic = null;
            if (actor === w.owner) {
              try {
                profitStatic = await w.arb.connect(actor).executeArb.staticCall(route);
              } catch (e) {
                profitStatic = null;
              }
            }
            try {
              const tx = await w.arb.connect(actor).executeArb(route);
              const rc = await tx.wait();
              succ++;
              const ev = rc.logs.map((l) => { try { return w.arb.interface.parseLog(l); } catch { return null; } }).find((e) => e && e.name === "ArbExecuted");
              expect(ev, "event").to.not.equal(undefined);
              execInfo = { loan, profit: ev.args.profit, premium: ev.args.premium, amount, minProfit };
              expect(actor === w.owner, "only owner can execute").to.equal(true);
              expect(profitStatic).to.equal(ev.args.profit);
              expect(ev.args.profit >= minProfit, "profit >= minProfit").to.equal(true);
            } catch (e) {
              if (e.message && e.message.includes("AssertionError")) throw e;
              if (e.name === "AssertionError") throw e;
              fail++;
              if (actor === w.owner && profitStatic !== null) throw new Error("static call succeeded but tx failed: " + e.message);
            }
          } else if (act === 5) {
            const fn = pick(["callUni", "callPancake"]);
            desc = `${fn}(random)`;
            log.push(desc);
            await expect(attackerC[fn](w.arbAddr, BigInt(ri(1000)) - 500n, BigInt(ri(1000)) - 500n)).to.be.reverted;
          } else if (act === 6) {
            desc = "executeOperation direct";
            log.push(desc);
            const caller = rnd() < 0.5 ? w.attacker : w.owner;
            await expect(w.arb.connect(caller).executeOperation(w.addr[pick(tokNames)], BigInt(ri(100)), 0n, pick([w.arbAddr, caller.address]), "0x")).to.be.reverted;
            await expect(attackerC.startLoan(w.aaveAddr, w.arbAddr, w.addr[pick(tokNames)], BigInt(1 + ri(100)), "0x")).to.be.reverted;
          } else if (act === 7) {
            const t = pick(tokNames);
            const amt = BigInt(ri(10000));
            desc = `donate ${amt} ${t}`;
            log.push(desc);
            await w.tokens[t].mint(w.arbAddr, amt);
            const b2 = await bals(w.arbAddr);
            expect(b2[t]).to.equal(before[t] + amt);
            continue; // balance legitimately increased
          } else if (act === 8) {
            const t = pick(tokNames);
            const byOwner = rnd() < 0.6;
            const bal = before[t];
            const amt = rnd() < 0.5 ? ethers.MaxUint256 : BigInt(ri(10000));
            desc = `sweep(${byOwner ? "owner" : "attacker"}) ${t} ${amt}`;
            log.push(desc);
            if (byOwner) {
              const want = amt === ethers.MaxUint256 ? bal : amt;
              if (want > bal) {
                await expect(w.arb.sweep(w.addr[t], amt)).to.be.reverted;
              } else {
                await w.arb.sweep(w.addr[t], amt);
                expectSweep = { t, want };
              }
            } else {
              await expect(w.arb.connect(w.attacker).sweep(w.addr[t], amt)).to.be.reverted;
            }
          } else if (act === 9) {
            const byOwner = rnd() < 0.5;
            desc = `${paused ? "unpause" : "pause"}(${byOwner ? "owner" : "attacker"})`;
            log.push(desc);
            if (byOwner) {
              await (paused ? w.arb.unpause() : w.arb.pause());
              paused = !paused;
            } else {
              await expect(w.arb.connect(w.attacker)[paused ? "unpause" : "pause"]()).to.be.reverted;
            }
          } else if (act === 10) {
            const [name, p] = pick(poolList);
            const byOwner = rnd() < 0.7;
            const en = rnd() < 0.5;
            desc = `setPool(${byOwner ? "owner" : "attacker"}) ${name} ${en}`;
            log.push(desc);
            if (byOwner) await w.arb.setPool(p.address, p.venue, p.feeBps, en);
            else await expect(w.arb.connect(w.attacker).setPool(p.address, p.venue, p.feeBps, en)).to.be.reverted;
          } else {
            // market move: change the rate of a mock v3 pool
            const [name, p] = pick(poolList.filter(([, q]) => q.kind === "v3"));
            const num = BigInt(1 + ri(40));
            const den = BigInt(1 + ri(20));
            desc = `setRate ${name} ${num}/${den}`;
            log.push(desc);
            await p.c.setRate(num, den);
            p.num = num;
            p.den = den;
          }

          // ----- invariants after every call
          await checkInvariants();
          const after = await bals(w.arbAddr);
          const ownerAfter = await bals(w.owner.address);
          for (const t of tokNames) {
            if (expectSweep && expectSweep.t === t) {
              expect(after[t], "I4 sweep lowers by exactly amount").to.equal(before[t] - expectSweep.want);
              expect(ownerAfter[t] - ownerBefore[t], "I4 owner gains swept").to.equal(expectSweep.want);
            } else {
              expect(after[t] >= before[t], `I4 ${t} never decreases`).to.equal(true);
              if (execInfo) expect(after[t], `I1 ${t} unchanged by arb`).to.equal(before[t]);
            }
          }
          if (execInfo) {
            const t = execInfo.loan;
            expect(ownerAfter[t] - ownerBefore[t], "I1 owner gain == profit").to.equal(execInfo.profit);
            for (const o of tokNames) if (o !== t) expect(ownerAfter[o]).to.equal(ownerBefore[o]);
          }
        }
      } catch (e) {
        console.log(`\nFUZZ FAILURE seed=${seed * 7919} (sequence ${seed}); calls:\n  ${log.join("\n  ")}`);
        throw e;
      }
      await snap.restore();
      for (const [n, p] of poolList) {
        if (p.kind === "v3") {
          const def = { p1: [2n, 1n], p2: [18n, 10n], p3: [1n, 1n], p4: [18n, 10n], q1: [2n, 1n], q2: [18n, 10n] }[n];
          p.num = def[0];
          p.den = def[1];
        }
      }
    }
    console.log(`      fuzz: ${RUNS} sequences, executeArb successes=${succ}, reverts=${fail}`);
    expect(succ).to.be.gt(0);
  });
});
