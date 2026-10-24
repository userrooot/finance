require("@nomicfoundation/hardhat-toolbox");

// Fork tests read RPC URLs from the environment (never commit keys). Public RPCs work for recent blocks.
const ARBITRUM_RPC = process.env.ARBITRUM_RPC || "https://arb1.arbitrum.io/rpc";
const BASE_RPC = process.env.BASE_RPC || "https://mainnet.base.org";

/** @type import('hardhat/config').HardhatUserConfig */
module.exports = {
  solidity: {
    version: "0.8.24",
    settings: { optimizer: { enabled: true, runs: 200 }, evmVersion: "paris" },
  },
  networks: {
    hardhat: {
      // Default: local chain. Fork tests opt in with FORK=arbitrum|base (see test/helpers/fork.js).
      forking: process.env.FORK
        ? { url: process.env.FORK === "base" ? BASE_RPC : ARBITRUM_RPC, blockNumber: process.env.FORK_BLOCK ? Number(process.env.FORK_BLOCK) : undefined }
        : undefined,
    },
  },
  paths: { sources: "./src", tests: "./test", cache: "./cache", artifacts: "./artifacts" },
};
